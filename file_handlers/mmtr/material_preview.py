"""See docs/internals/mesh-preview.md, Material bindings."""
from __future__ import annotations

from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import lru_cache
import math
import re
import struct
from typing import NamedTuple

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

lazy from file_handlers.mdf.material_layout import MaterialDescriptorLayout
lazy from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.tex.qt_image_utils import parse_tex_bytes
lazy from file_handlers.tex.texture_quality import mip_for_largest_side
from utils.resource_file_utils import ResourceLocation, resource_version_from_path

lazy from .profiles.re_engine_bindless_material import HeapIndices
lazy from .profiles.re_engine_dxil_recovery import recover_re_engine_dxil_ir

translate = QCoreApplication.translate

# D3D12 numbering.
RANGE_SRV, RANGE_UAV, RANGE_CBV, RANGE_SAMPLER = range(4)
SHADER_VISIBILITY = {"vertex": 1, "hull": 2, "domain": 3, "pixel": 5, "compute": 0}
# D3D12 values; the rasterizer is solid fill, no culling, depth clip.
_TRIANGLE_LIST = 4
_RASTERIZER = (3, 1, 0, 0, 0.0, 0.0, 1)
_NO_DEPTH_STENCIL = (0, 0, 8, 0, 0xFF, 0xFF, (1, 1, 1, 8), (1, 1, 1, 8), 0)
_UNBLENDED = (0, 2, 1, 1, 2, 1, 1, 0xF)
_LINEAR_WRAP = (0x15, 1, 1, 1, 0.0, 1, 1, (0.0, 0.0, 0.0, 0.0), 0.0, 3.402823466e38)
VIEW_DIMENSIONS = {"buffer": 1, "buffer-extended": 1, "texture1d": 2, "texture1d-array": 3, "texture2d": 4,
                    "texture2d-array": 5, "texture3d": 8, "texture-cube": 9, "texture-cube-array": 10}
_UAV_DIMENSIONS = {"buffer": 1, "texture1d": 2, "texture1d-array": 3, "texture2d": 4, "texture2d-array": 5,
                   "texture3d": 8}
# Bytes before the buffer in a GPU buffer file, by ``.gpbf`` version.
_GPU_BUFFER_HEADERS = {1: 0, 2: 4, 3: 5}
# Each field's index names (dimension, texture), or None: all bits set.
_ENGINE_HEAP_FIELDS = {"dynamicTextureTableHandles": None,
                       "dynamicTextureTableNullBlackHandle": ("texture2d", (28, 1, 1, 1, 1, bytes(4))),
                       "DL_TextureBindlessIndex": None}
# By heap index, from 1, the same for every material.
_ENGINE_TEXTURES = dict(enumerate((name for name, held in _ENGINE_HEAP_FIELDS.items() if held is not None), 1))
_MATERIAL_HEAP_START = 1 + len(_ENGINE_TEXTURES)


class TranslatedError(ValueError):
    """Kept untranslated so another process can translate it."""
    context = ""

    def __init__(self, text: str, values: Mapping[str, object] | None = None):
        super().__init__(text, dict(values or {}))
        self.text, self.values = text, dict(values or {})

    def __str__(self) -> str:
        return translate(self.context, self.text).format(**self.values)


class MaterialPreviewError(TranslatedError):
    context = "MaterialPreview"


class Following(NamedTuple):
    """Equal ``key``s share one buffer."""
    key: Hashable
    make: Callable[[], bytes]


@dataclass(frozen=True, slots=True)
class PreviewMaterial:
    layout: MaterialDescriptorLayout
    values: tuple[tuple[int, tuple[float, ...]], ...]
    textures: Mapping[str, str]
    material_flags: int
    shader_lod: int | None = None
    buffers: Mapping[str, str] = field(default_factory=dict)
    animated: Callable[[], tuple[tuple[int, tuple[float, ...]], ...]] | None = field(default=None, compare=False)

    @classmethod
    def from_mdf(cls, material) -> "PreviewMaterial":
        values = tuple((int(field.param_rel_offset), tuple(float(item) for item in field.parameter[:max(
            1, int(field.component_count))])) for field in material.parameters)
        return cls(MaterialDescriptorLayout.from_material(material), values,
                   {str(texture.tex_type): str(texture.tex_path) for texture in material.textures},
                   int(material.header.material_flags),
                   buffers={str(name.name): str(data.name) for name, data in material.gpu_buffers})

    def record(self) -> bytearray:
        result = bytearray(self.layout.allocation_size(self.shader_lod))
        for offset, values in self.values:
            struct.pack_into(f"<{len(values)}f", result, offset, *values)
        return result


def bound_record_size(document, material: PreviewMaterial) -> int:
    return (int(document.profile.shader_graph_profile.material_record_prefix_size)
            + material.layout.allocation_size(material.shader_lod))


def _holder_bytes(record: bytes, stride: int) -> bytes:
    return record.ljust(-(-len(record) // stride) * stride, b"\0")


def gbuffer_route(skinned: bool) -> str:
    """The renderer may pick ``PreTransform`` instead."""
    return "DeferredSkinning" if skinned else "DeferredStatic"


# Smallest mip side of a streamed texture; pixel-sampled textures are streamed, others stay resident.
STREAMED_SMALLEST = 32
# Reading and decoding release the GIL, so frames go on meanwhile.
_STREAMS = ThreadPoolExecutor(max_workers=2, thread_name_prefix="Texture streaming")


def streamed_key(path: str) -> str:
    return f"streamed:{path}"


def resident_key(path: str) -> str:
    return f"resident:{path}"


# Bytes of a file its key's hash takes.
_CONTENT_SAMPLES = 512


class _FileContent:
    """A file's bytes as a key: hashed by a bounded sample of them, equal by all of them. Once released it holds
    them no more and equals only itself."""
    __slots__ = ("data", "_hash")

    def __init__(self, data: bytes):
        self.data = data
        self._hash = hash((len(data), data[::max(1, len(data) // _CONTENT_SAMPLES)]))

    def __hash__(self) -> int:
        return self._hash

    def __eq__(self, other) -> bool:
        if not isinstance(other, _FileContent):
            return NotImplemented
        return self is other or self.data is not None and (self.data is other.data or self.data == other.data)

    def release(self) -> None:
        self.data = None

    def __repr__(self) -> str:
        return f"_FileContent({'released' if self.data is None else f'{len(self.data)} bytes'})"


# Bytes of a texture's texels its key's hash takes.
_TEXEL_SAMPLES = 64


class TexelRows:
    """A texture's subresources in Direct3D's order, each as ``TexFile.stored_mip_rows`` gives it, in place in its
    file: its rows of texels, or a GDeflate stream the device inflates. Texels of streams are compared by the
    streams' bytes."""
    __slots__ = ("rows", "sample", "packed")

    def __init__(self, rows: tuple[tuple, ...]):
        self.rows = rows
        self.packed = any(len(row) == 7 for row in rows)
        sample = _sample([row[5] if len(row) == 7 else row[3] * row[4] for row in rows], self._byte)
        self.sample = (*sample, "streams") if self.packed else sample

    def _byte(self, subresource: int, at: int) -> int:
        data, offset, pitch, size, _count, *stream = self.rows[subresource]
        if stream:
            return data[offset + at]
        row, column = divmod(at, size)
        return data[offset + row * pitch + column]

    def release(self) -> None:
        """Lets go of the texels once the device holds them; ``same_texels`` then finds them like no others."""
        self.rows = ()

    def pieces(self) -> Iterator[tuple[bytes, int, int]]:
        """The texels packed tightly, or the streams, piece by piece: (bytes, offset, length)."""
        for data, offset, pitch, size, count, *stream in self.rows:
            if stream:
                yield data, offset, stream[0]
                continue
            if pitch == size:
                yield data, offset, size * count
                continue
            for row in range(count):
                yield data, offset + row * pitch, size

    def __repr__(self) -> str:
        return f"TexelRows({len(self.rows)} subresources, {self.sample[0]} bytes)"


def _sample(sizes: list[int], byte: Callable[[int, int], int]) -> tuple[int, bytes]:
    """The length of texels packed tightly, and their bytes at evenly spaced places; ``byte(part, at)`` is the byte at
    ``at`` of the part ``sizes`` gives the length of."""
    total = sum(sizes)
    count = min(_TEXEL_SAMPLES, total)
    picked, part, start = bytearray(count), 0, 0
    for number in range(count):
        place = number * total // count
        while place >= start + sizes[part]:
            start += sizes[part]
            part += 1
        picked[number] = byte(part, place - start)
    return total, bytes(picked)


def texel_sample(texels: bytes | TexelRows) -> tuple[int, bytes]:
    """What equal texels share, packed bytes or rows alike."""
    if isinstance(texels, TexelRows):
        return texels.sample
    return _sample([len(texels)], lambda _part, at: texels[at])


def same_texels(one: bytes | TexelRows, other: bytes | TexelRows) -> bool:
    """Whether the texels packed tightly are the same bytes; their lengths must be equal."""
    if any(isinstance(texels, TexelRows) and not texels.rows for texels in (one, other)):
        return one is other
    if any(isinstance(texels, TexelRows) and texels.packed for texels in (one, other)) and not (
            isinstance(one, TexelRows) and isinstance(other, TexelRows) and one.packed and other.packed
            and [row[2:] for row in one.rows] == [row[2:] for row in other.rows]):
        return False
    left, right = (texels.pieces() if isinstance(texels, TexelRows) else iter(((texels, 0, len(texels)),))
                   for texels in (one, other))
    held = given = (b"", 0, 0)
    while True:
        held = held if held[2] else next(left, None)
        given = given if given[2] else next(right, None)
        if held is None or given is None:
            return held is given
        (data, start, size), (other_data, other_start, other_size) = held, given
        length = min(size, other_size)
        # A memcmp in place: comparing memoryviews goes byte by byte.
        if not data.startswith(memoryview(other_data)[other_start:other_start + length], start):
            return False
        held, given = (data, start + length, size - length), (other_data, other_start + length, other_size - length)


class TextureReads:
    """A scene's decoded textures, by the file a loader gives for a path and by the file's bytes, so files of the same
    bytes are one texture. Once their bytes are released, a file asked for again is read no more: it is the texture
    the device holds. Given ``upload`` (``Resident.read``), each goes to the device as it is first read, and its
    texels and file are let go of then, the loader's too by ``forget(load, path)``. A streamed texture is read from
    the last mip whose smaller side spans the texels ``side(path)`` gives (None: from the first); ``refine`` reads
    it again from finer mips once ``side`` gives more, on streaming threads, ``refill(texture, dimension, texels)``
    making its device texture anew."""
    __slots__ = ("files", "contents", "upload", "forget", "side", "refill", "streamed", "refining")

    def __init__(self, upload: Callable[[int, tuple], tuple] | None = None,
                 forget: Callable[[Callable, str], None] | None = None,
                 side: Callable[[str], int | None] | None = None,
                 refill: Callable[[int, int, tuple], None] | None = None):
        self.files: dict[tuple, tuple | None] = {}
        self.contents: dict[tuple, tuple | None] = {}
        self.upload, self.forget, self.side, self.refill = upload, forget, side, refill
        # By ``files`` key, of each texture read from a later mip: the side it was read for, its device texture and
        # its mips.
        self.streamed: dict[tuple, tuple[float, int, int]] = {}
        # By ``streamed`` key: the side being read for, and the read.
        self.refining: dict[tuple, tuple[float, Future]] = {}

    def texels(self, load: Callable[[str], bytes | None], path: str, dimension: str,
               smallest: int | None, whole: bool = False) -> tuple | None:
        key = (load, path, dimension, smallest, whole)
        if key not in self.files:
            data = load(path)
            side = None if smallest is None or whole or self.side is None else self.side(path)
            content, self.files[key] = self._decoded(data, dimension, smallest, side) if data else (None, None)
            found = self.files[key]
            texture = None if content is None or found is None or self.upload is None else self._uploaded(
                found, dimension)
            if texture is not None:
                content.release()
                if self.forget is not None:
                    self.forget(load, path)
                if side is not None and self.refill is not None:
                    self.streamed[key] = (side, texture, found[4])
        return self.files[key]

    def refine(self, reads: int) -> bool:
        """Makes anew the streamed textures read again, and reads again from finer mips, ``reads`` at a time, those
        ``side`` now gives more texels than they were read for; whether some are being read."""
        for key, (wanted, read) in list(self.refining.items()):
            if not read.done():
                continue
            del self.refining[key]
            _held, texture, mips = self.streamed[key]
            found = read.result()
            if found is not None and found[4] > mips:
                self.refill(texture, VIEW_DIMENSIONS[key[2]], found)
                found[-1].release()
                mips = found[4]
            self.streamed[key] = (wanted, texture, mips)
        for key, (held, _texture, _mips) in self.streamed.items():
            if len(self.refining) >= reads:
                break
            load, path, dimension, smallest, _whole = key
            side = self.side(path)
            wanted = math.inf if side is None else side
            if wanted > held and key not in self.refining:
                self.refining[key] = wanted, _STREAMS.submit(self._refined, load, path, dimension, smallest, side)
        return bool(self.refining)

    def _refined(self, load: Callable[[str], bytes | None], path: str, dimension: str, smallest: int | None,
                 side: int | None) -> tuple | None:
        data = load(path)
        if data and self.forget is not None:
            self.forget(load, path)
        return _texture_texels(data, dimension, None, smallest, side) if data else None

    def stop(self) -> None:
        """Lets go of the reads under way."""
        for _wanted, read in self.refining.values():
            read.cancel()
        self.refining.clear()

    def _decoded(self, data: bytes, dimension: str, smallest: int | None,
                 side: int | None) -> tuple[_FileContent | None, tuple | None]:
        """The texels, and the key made of the file's bytes when no other file of them was read."""
        content = _FileContent(data)
        key = (content, dimension, smallest, side)
        if key in self.contents:
            return None, self.contents[key]
        self.contents[key] = _texture_texels(data, dimension, None, smallest, side)
        return content, self.contents[key]

    def _uploaded(self, found: tuple, dimension: str) -> int | None:
        """The device texture."""
        try:
            texture = self.upload(VIEW_DIMENSIONS[dimension], (0, *found))[2]
        except (RuntimeError, ValueError):
            # Met again when a step binds the texture, as without the upload here.
            return None
        found[-1].release()
        return texture


class BufferReads:
    """A scene's GPU buffer files, by the file a loader gives for a path: each goes to the device (``upload``, its
    buffer id kept) as it is first read, and the loader lets go of the file (``forget(load, path)``)."""
    __slots__ = ("files", "upload", "forget")

    def __init__(self, upload: Callable[[bytes], int], forget: Callable[[Callable, str], None] | None = None):
        self.files: dict[tuple, int | None] = {}
        self.upload, self.forget = upload, forget

    def buffer(self, load: Callable[[str], bytes | None], path: str) -> int | None:
        key = (load, path)
        if key not in self.files:
            data = load(path)
            self.files[key] = None if data is None else self.upload(data)
            if data is not None and self.forget is not None:
                self.forget(load, path)
        return self.files[key]


def _texture_entry(index: int, path: str, load: Callable[[str], bytes | None], dimension: str,
                   read: TextureReads | None = None, streamed: bool = False, whole: bool = False) -> tuple | None:
    """``whole``: read from the first mip, as a read naming a mip or texel outright needs."""
    smallest = STREAMED_SMALLEST if streamed else None
    if read is None:
        data = load(path)
        found = _texture_texels(data, dimension, None, smallest) if data else None
    else:
        found = read.texels(load, path, dimension, smallest, whole)
    return None if found is None else (index, *found)


def _texture_texels(data: bytes | None, dimension: str, largest_mip: int | None,
                    smallest: int | None = None, side: int | None = None) -> tuple | None:
    """From the mip whose largest side ``largest_mip`` bounds or, given ``side``, the last whose smaller side spans
    it, but none past the last mip of a smaller side ``smallest`` or more."""
    tex = parse_tex_bytes(data) if data else None
    if tex is None:
        return None
    header = tex.header
    if dimension == "texture3d":
        if not header.is_volume:
            return None
        images, layers = 1, int(header.depth)
    elif dimension in {"texture-cube", "texture-cube-array"}:
        if not header.is_cubemap or header.image_count % 6:
            return None
        images = layers = int(header.image_count) if dimension == "texture-cube-array" else 6
    elif header.is_volume or header.is_cubemap or dimension.startswith("texture1d") and header.height != 1:
        return None
    else:
        images = layers = int(header.image_count) if dimension.endswith("-array") else 1
    sides = [min(header.width >> level, header.height >> level) for level in range(int(header.mip_count))]
    kept = len(sides) if smallest is None else sum(1 for value in sides if value >= smallest)
    if side is not None:
        first = min(max(kept, 1) - 1, sum(1 for value in sides[1:] if value >= side))
    else:
        first = 0 if largest_mip is None else mip_for_largest_side(header, largest_mip)
    levels = range(first, max(first + 1, kept))
    if dimension == "texture3d" and first:
        return None  # A volume's slices shrink with its mips.
    try:
        rows = tuple(tex.stored_mip_rows(level, image) for image in range(images) for level in levels)
    except ValueError:
        return None
    return (int(header.format), max(1, header.width >> first), max(1, header.height >> first), layers,
            len(levels), TexelRows(rows))


def _record_tags(lines, accesses, prefix: int) -> dict[int, int]:
    tags = {}
    for access in accesses:
        for word in access.words:
            if word.byte_offset >= prefix:
                continue
            pattern = re.compile(rf"= icmp eq i32 (?:{re.escape(word.value)}, (-?\d+)|(-?\d+), {re.escape(word.value)})$")
            found = {int(match[1] or match[2]) for line in lines if (match := pattern.search(line.strip()))}
            if len(found) > 1:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The pixel shader accepts several material record kinds."))
            if found:
                tags[word.byte_offset] = found.pop() & 0xFFFFFFFF
    return tags


@dataclass(frozen=True)
class _MaterialReads:
    holders: frozenset[str]
    tables: Mapping[str, int]
    tags: Mapping[int, int]
    textures: tuple
    buffers: tuple[int, ...]
    engine: tuple


@lru_cache(maxsize=16)
def _dxil_lines(shader: bytes) -> tuple[str, ...]:
    return tuple(item.text for item in recover_re_engine_dxil_ir(shader).records)


@lru_cache(maxsize=128)
def _material_reads(contract, shader: bytes, prefix: int) -> _MaterialReads:
    from .profiles.re_engine_bindless_material import (engine_heap_handles, material_buffer_reads,
                                                       material_input_accesses)
    lines = list(_dxil_lines(shader))
    try:
        accesses, texture_reads = material_input_accesses(lines, contract)
    except ShaderGraphError as exc:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The pass's material reads are not understood: {reason}"), {"reason": str(exc)}) from exc
    holders, tables = set(), {}
    for access in accesses:
        if access.storage != "descriptor-heap":
            holders.add(access.resource_name)
            continue
        table = contract.resource(access.resource_name)
        if (table is None or table.input_type != "structured-buffer" or table.bind_count != 1
                or contract.structured_buffer(table.name).byte_size != 4):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The pass names its material constant buffer through a table the preview does not "
                "lay out."))
        tables[table.name] = max(tables.get(table.name, 0), access.constant_buffer_size)
    return _MaterialReads(frozenset(holders), frozendict(tables),
                          frozendict(_record_tags(lines, accesses, prefix)), texture_reads,
                          material_buffer_reads(lines, accesses), engine_heap_handles(lines, contract, accesses))


@lru_cache(maxsize=16)
def _heap_indices(contract, shader: bytes):
    return HeapIndices(list(_dxil_lines(shader)), contract)


def _engine_heap_fields(layout, data: bytes) -> bytes:
    indices = {name: index for index, name in _ENGINE_TEXTURES.items()}
    data = bytearray(data)
    for variable in layout.variables:
        if variable.name not in _ENGINE_HEAP_FIELDS:
            continue
        if variable.name in indices and variable.byte_size != 4:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The engine field {name} is declared {declaration}, which the preview does not lay "
                "out."), {"name": variable.name, "declaration": variable.hlsl_declaration or ""})
        end = variable.byte_offset + variable.byte_size
        data[variable.byte_offset:end] = (struct.pack("<I", indices[variable.name]) if variable.name in indices
                                          else b"\xff" * variable.byte_size)
    return bytes(data)


def _heap_fields_kept(layout, data: bytes, bound: bytes) -> bytes:
    data = bytearray(data)
    for variable in layout.variables:
        if variable.name in _ENGINE_HEAP_FIELDS:
            end = variable.byte_offset + variable.byte_size
            data[variable.byte_offset:end] = bound[variable.byte_offset:end]
    return bytes(data)


def _gpu_buffer_header(path: str) -> int:
    header = _GPU_BUFFER_HEADERS.get(resource_version_from_path(path, "gpbf"))
    if header is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The GPU buffer file {path} is of a version the preview does not read."), {"path": path})
    return header


def gpu_buffer_contents(path: str, data: bytes) -> bytes:
    return bytes(data[_gpu_buffer_header(path):])


@dataclass(frozen=True, slots=True)
class GpuBufferLocation(ResourceLocation):
    """A ``.gpbf`` found, of a version the preview reads, read as the buffer it holds."""

    @classmethod
    def of(cls, found: ResourceLocation) -> GpuBufferLocation:
        _gpu_buffer_header(found.path)
        return cls(found.path, found.pak)

    def read(self) -> tuple[str, bytes] | None:
        found = ResourceLocation.read(self)
        return None if found is None else (found[0], gpu_buffer_contents(*found))


def _gpu_buffer(name: str, path: str, load_file, read: BufferReads | None) -> bytes | int:
    data = None if not path else load_file(path) if read is None else read.buffer(load_file, path)
    if data is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The material's GPU buffer {name} was not found."),
                                   {"name": name})
    return data if read is not None else bytes(data)


@dataclass(frozen=True, slots=True, eq=False)
class MaterialRecord:
    """``held`` (the prefix, then ``record``) is what shaders read at the material's handle."""
    reads: tuple[tuple[str, _MaterialReads], ...]
    record: bytearray
    held: bytes
    given: Callable[[Callable[[bytes], bytes], Hashable], bytes | Following]
    heap: dict[int, tuple]
    arrays: dict[tuple[str, str], dict[int, tuple]]
    # Heap index of the constant buffer view of ``held``.
    view: int | None
    texture: Callable[[int, str, str], tuple | None]
    missing: list[str]


@dataclass(frozen=True, slots=True, eq=False)
class SharedMaterials:
    """Materials a draw's lanes find at their own handles: ``holder`` by element size, ``table`` the view table."""
    holder: Callable[[int], bytes | Following]
    table: bytes
    heap: Mapping[int, tuple]


def material_record(document, stages: Sequence[tuple[str, object, bytes]], material: PreviewMaterial, load_file, *,
                    read_textures: TextureReads | None = None, read_buffers: BufferReads | None = None,
                    heap_start: int = _MATERIAL_HEAP_START, streamed: Callable[[str], bool] | None = None,
                    resident_file: Callable | None = None,
                    made_texture: Callable[[str], tuple] | None = None) -> MaterialRecord:
    """``stages`` are (stage, contract, shader) of every program reading the record."""
    if heap_start < _MATERIAL_HEAP_START:
        raise ValueError(f"the material's heap indices start at {heap_start}, before {_MATERIAL_HEAP_START}")
    profile = document.profile.shader_graph_profile
    record, prefix = material.record(), int(profile.material_record_prefix_size)
    reads: list[tuple[str, _MaterialReads]] = []
    tags: dict[int, int] = {}
    for stage, contract, shader in stages:
        if int(contract.model_major) < 6:
            continue
        found = _material_reads(contract, shader, prefix)
        reads.append((stage, found))
        for offset, value in found.tags.items():
            if tags.setdefault(offset, value) != value:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The shaders accept different material record kinds."))
    words = {(kind, offset + prefix): name for (kind, name), offset in
             material.layout.word_offsets(material.shader_lod).items()}
    slots = {offset: name for (kind, offset), name in words.items() if kind == "texture"}
    missing, loaded = [], {}
    pixel_read = {slots.get(read.byte_offset) for stage, found in reads if stage == "pixel" for read in found.textures}
    addressed = {slots.get(read.byte_offset) for _stage, found in reads for read in found.textures if read.addressed}

    def texture(index: int, name: str, dimension: str) -> tuple | None:
        if (index, name, dimension) not in loaded and made_texture is not None:
            path = material.textures.get(name, "")
            made = made_texture(path) if path else None
            if made is not None:
                loaded[index, name, dimension] = (index, *made[1:]) if made[0] == dimension else None
        if (index, name, dimension) not in loaded:
            path = material.textures.get(name, "")
            pixel = name in pixel_read or resident_file is None
            loaded[index, name, dimension] = (_texture_entry(index, path, load_file if pixel else resident_file,
                                                             dimension, read_textures,
                                                             pixel and streamed is not None and streamed(path),
                                                             name in addressed)
                                              if path else None)
        entry = loaded[index, name, dimension]
        if entry is None:
            missing.append(name)
        return entry

    # Index 0 stays empty so unset record words read nothing.
    base = heap_start - 1
    indices: dict[str, int] = {}
    arrays: dict[tuple[str, str], dict[int, tuple]] = {}
    heap: dict[int, tuple] = {}
    words_read: tuple[set[int], set[int]] = (set(), set())
    for stage, read in ((stage, read) for stage, found in reads for read in found.textures):
        name = slots.get(read.byte_offset)
        if name is None and read.resource is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The pass reads the descriptor heap through a record word that names no texture."))
        if name is None or read.resource is not None and read.resource.bind_count:
            continue
        index = indices.setdefault(name, len(indices) + 1)
        words_read[read.resource is not None].add(read.byte_offset)
        if read.resource is None:
            if read.dimension not in VIEW_DIMENSIONS:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The pass reads a {kind} texture."),
                                           {"kind": read.dimension})
            view, at = (RANGE_SRV, VIEW_DIMENSIONS[read.dimension]), base + index
            if at not in heap:
                entry = texture(at, name, read.dimension)
                heap[at] = (*view, (at,) if entry is None else entry)
                struct.pack_into("<I", record, read.byte_offset - prefix, at)
            elif heap[at][:2] != view:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The pass reads one texture from the descriptor heap as several kinds."))
            continue
        entries = arrays.setdefault((stage, read.resource.name), {})
        if index not in entries and (entry := texture(index, name, read.resource.dimension)) is not None:
            entries[index] = entry
            struct.pack_into("<I", record, read.byte_offset - prefix, index)
    if words_read[0] & words_read[1]:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The pass reads one record word's texture from the descriptor heap and from an array."))
    buffers: dict[str, int] = {}
    for offset in sorted({*found.buffers for _stage, found in reads}):
        name = words.get(("gpu_buffer", offset))
        if name is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The pass reads the descriptor heap through a record word that names no GPU buffer."))
        if name not in buffers:
            index = buffers[name] = base + len(indices) + len(buffers) + 1
            heap[index] = (RANGE_SRV, VIEW_DIMENSIONS["buffer"], (index, 0, 0, _gpu_buffer(
                name, material.buffers.get(name, ""), load_file, read_buffers)))
        struct.pack_into("<I", record, offset - prefix, buffers[name])
    header = bytearray(prefix)
    for offset, value in tags.items():
        struct.pack_into("<I", header, offset, value)
    held = bytes(header) + bytes(record)

    def given(shape: Callable[[bytes], bytes], place: Hashable) -> bytes | Following:
        bound = shape(held)
        if material.animated is None:
            return bound

        def make() -> bytes:
            animated = material.animated()
            if not animated:
                return bound
            now = bytearray(record)
            for offset, values in animated:
                struct.pack_into(f"<{len(values)}f", now, offset, *values)
            return shape(bytes(header) + now.take_bytes())
        return Following((material.animated, place, held), make)
    sizes = [*found.tables.values() for _stage, found in reads]
    view = None
    if sizes:
        view, largest = base + len(indices) + len(buffers) + 1, max(sizes)
        heap[view] = (RANGE_CBV, 0, (view, given(lambda data: data.ljust(largest, b"\0"), ("table", largest))))
    return MaterialRecord(tuple(reads), record, held, given, heap, arrays, view, texture, missing)


_SHARED_UNBOUND = QT_TRANSLATE_NOOP(
    "MaterialPreview", "The pass binds part of its material to the draw itself, which REasy's meshlet draws, reading "
                       "every material at its own handle, do not lay out.")


def shader_bindings(document, shaders: Mapping[str, tuple], material: PreviewMaterial, load_file,
                    engine_buffer: Callable | None = None, sampler: Callable[[str], tuple] | None = None, *,
                    engine_texture: Callable | None = None, read_textures: TextureReads | None = None,
                    read_buffers: BufferReads | None = None,
                    heap_start: int = _MATERIAL_HEAP_START, engine_heap: Callable | None = None,
                    shared: SharedMaterials | None = None,
                    streamed: Callable[[str], bool] | None = None, resident_file: Callable | None = None,
                    made_texture: Callable[[str], tuple] | None = None):
    """Layout rules: docs/internals/mesh-preview.md, Material bindings; ``shared``: docs/internals/meshlets.md."""
    stages = [(stage, contract, shader) for stage, (contract, shader) in shaders.items()]
    prefix = int(document.profile.shader_graph_profile.material_record_prefix_size)
    if shared is None:
        owned = material_record(document, stages, material, load_file, read_textures=read_textures,
                                read_buffers=read_buffers, heap_start=heap_start, streamed=streamed,
                                resident_file=resident_file, made_texture=made_texture)
        reads, heap, missing = dict(owned.reads), owned.heap, owned.missing
    else:
        owned, heap, missing = None, dict(shared.heap), []
        reads = {stage: _material_reads(contract, shader, prefix) for stage, contract, shader in stages
                 if int(contract.model_major) >= 6}
        if any(read.resource is not None and not read.resource.bind_count
               for found in reads.values() for read in found.textures):
            raise MaterialPreviewError(_SHARED_UNBOUND)
    user_buffers = set(document.profile.user_material_buffer_names)
    tables, unbound, constants = [], set(), {}
    for stage, (contract, _shader) in shaders.items():
        visibility = SHADER_VISIBILITY[stage]
        found = reads.get(stage)
        for resource in contract.resources:
            kind, place = resource.register_class, (resource.bind_point, resource.register_space, resource.bind_count)
            if kind == "s":
                state = _LINEAR_WRAP if sampler is None else sampler(resource.name)
                tables.append((RANGE_SAMPLER, *place, 0, visibility,
                               tuple((index, *state) for index in range(max(1, resource.bind_count)))))
                continue
            if kind == "b":
                size = contract.buffer(resource.name).byte_size
                if resource.name in user_buffers:
                    if owned is None:
                        raise MaterialPreviewError(_SHARED_UNBOUND)
                    # The record's first ``prefix`` bytes are heap index fields, not material values.
                    constants[(stage, resource.name)] = bytes(owned.record[:size]).ljust(size, b"\0")
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, owned.given(
                        lambda data, size=size: data[prefix:prefix + size].ljust(size, b"\0"), ("user", size))),)))
                    continue
                data = bytes(size) if engine_buffer is None else engine_buffer(contract, resource)
                if data is not None:
                    data = _engine_heap_fields(contract.buffer(resource.name), data)
                tables.append((RANGE_CBV, *place, 0, visibility, () if data is None else ((0, data),)))
                constants[(stage, resource.name)] = data
                continue
            dimension = resource.dimension if resource.input_type == "texture" or resource.input_type.startswith(
                "rw-typed") else "buffer"
            if kind == "u":
                tables.append((RANGE_UAV, *place, _UAV_DIMENSIONS.get(dimension, 1), visibility, ()))
                continue
            if dimension not in VIEW_DIMENSIONS:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The pass reads a {kind} texture."),
                                           {"kind": dimension})
            entries = [] if owned is None else list(owned.arrays.get((stage, resource.name), {}).values())
            if found is not None and resource.name in found.holders:
                stride = contract.structured_buffer(resource.name).byte_size
                entries = [(0, 0, stride, shared.holder(stride) if owned is None else owned.given(
                    lambda data, stride=stride: _holder_bytes(data, stride), ("holder", stride)))]
            elif found is not None and resource.name in found.tables:
                entries = [(0, 0, 4, shared.table if owned is None else struct.pack("<I", owned.view))]
            elif resource.bind_count == 1 and resource.name in material.textures and resource.input_type == "texture":
                if owned is None:
                    raise MaterialPreviewError(_SHARED_UNBOUND)
                entries = [entry] if (entry := owned.texture(0, resource.name, dimension)) else []
            elif resource.bind_count == 1 and resource.input_type == "texture" and engine_texture is not None:
                entries = [entry] if (entry := engine_texture(contract, resource)) else []
            elif dimension == "buffer" and engine_buffer is not None:
                data = engine_buffer(contract, resource)
                stride = (contract.structured_buffer(resource.name).byte_size
                          if resource.input_type == "structured-buffer" else 0)
                entries = [] if data is None else [(0, 0, stride, data)]
            if dimension == "buffer" and not entries:
                unbound.add((stage, resource.name))
            tables.append((RANGE_SRV, *place, VIEW_DIMENSIONS[dimension], visibility, tuple(entries)))
    def place(index: int, dimension: str, entry: tuple) -> None:
        view = (RANGE_SRV, VIEW_DIMENSIONS[dimension])
        if heap.setdefault(index, (*view, entry))[:2] != view:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The pass reads one engine heap index as several kinds."))

    for stage, found in reads.items():
        contract, shader = shaders[stage]

        zeroed = {}

        def word(buffer: str, offset: int, stage=stage, contract=contract) -> int | None:
            data = constants.get((stage, buffer))
            if data is None or offset + 4 > len(data):
                return None
            if not any(variable.name in _ENGINE_HEAP_FIELDS and 0 <= offset - variable.byte_offset < variable.byte_size
                       for variable in contract.buffer(buffer).variables):
                zeroed[buffer] = None
                return None
            return struct.unpack_from("<I", data, offset)[0]

        for handle in found.engine:
            given = None if engine_heap is None else engine_heap(contract, handle)
            if given is not None:
                for item in given:
                    if heap.setdefault(item[2][0], item) != item:
                        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The pass reads one engine heap index as several kinds."))
                continue
            # A heap index from a null buffer is 0: a null view of the read kind.
            if (handle.source is not None and (stage, handle.source.name) in unbound
                    and handle.dimension in VIEW_DIMENSIONS):
                place(0, handle.dimension, (0,))
                continue
            zeroed.clear()
            made = _heap_indices(contract, shader).values(handle.handle, word)
            if made is None and zeroed:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The pass reads the engine's resources at descriptor heap indices it takes from "
                    "{buffers}, which the engine fills from the scene and the preview leaves at zero."), {"buffers": ", ".join(zeroed)})
            if made is None or any(source is None or index not in _ENGINE_TEXTURES
                                   or _ENGINE_HEAP_FIELDS[_ENGINE_TEXTURES[index]][0] != handle.dimension
                                   for index, source in made):
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The pass reads the engine's resources straight from the descriptor heap, "
                    "which the preview does not lay out."))
            for index, _source in made:
                place(index, handle.dimension, (index, *_ENGINE_HEAP_FIELDS[_ENGINE_TEXTURES[index]][1]))
    return tables, tuple(heap.values()), tuple(dict.fromkeys(missing))


__all__ = ["BufferReads", "Following", "GpuBufferLocation", "MaterialPreviewError", "MaterialRecord", "PreviewMaterial",
           "SharedMaterials", "TexelRows", "TextureReads", "bound_record_size", "gbuffer_route", "gpu_buffer_contents",
           "material_record", "same_texels", "shader_bindings", "texel_sample"]
