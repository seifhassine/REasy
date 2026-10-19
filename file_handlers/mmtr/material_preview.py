"""See docs/internals/mesh-preview.md, Material bindings."""
from __future__ import annotations

from collections.abc import Callable, Collection, Hashable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
import re
import struct
from types import MappingProxyType
from typing import NamedTuple

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

from file_handlers.mdf.material_layout import MaterialDescriptorLayout
from file_handlers.sdf.shader_graph import ShaderGraphError
from file_handlers.tex.qt_image_utils import parse_tex_bytes
from file_handlers.tex.texture_quality import mip_for_largest_side
from utils.resource_file_utils import resource_version_from_path

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


def _holder_bytes(record: bytes, shared: bytes | None, stride: int) -> bytes:
    if shared is not None:
        if any(shared[:len(record)]):
            raise ValueError("the material record overlaps the engine's bytes of the buffer holding it")
        record = record + shared[len(record):]
    return record.ljust(-(-len(record) // stride) * stride, b"\0")


def gbuffer_route(skinned: bool) -> str:
    """The renderer may pick ``PreTransform`` instead."""
    return "DeferredSkinning" if skinned else "DeferredStatic"


# Smallest mip side of a streamed texture; pixel-sampled textures are streamed, others stay resident.
STREAMED_SMALLEST = 32


def streamed_key(path: str) -> str:
    return f"streamed:{path}"


def resident_key(path: str) -> str:
    return f"resident:{path}"


def _texture_entry(index: int, data: bytes, dimension: str, read: dict | None = None,
                   streamed: bool = False) -> tuple | None:
    smallest = STREAMED_SMALLEST if streamed else None
    if read is None:
        found = _texture_texels(data, dimension, None, smallest)
    else:
        key = (data, dimension, smallest)
        if key not in read:
            read[key] = _texture_texels(data, dimension, None, smallest)
        found = read[key]
    return None if found is None else (index, *found)


def _texture_texels(data: bytes | None, dimension: str, largest_mip: int | None,
                    smallest: int | None = None) -> tuple | None:
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
    first = 0 if largest_mip is None else mip_for_largest_side(header, largest_mip)
    last = int(header.mip_count) if smallest is None else max(first + 1, sum(
        1 for level in range(int(header.mip_count)) if min(header.width >> level, header.height >> level) >= smallest))
    levels = range(first, last)
    if dimension == "texture3d" and first:
        return None  # A volume's slices shrink with its mips.
    try:
        payload = b"".join(tex.read_mip_level(level, image) for image in range(images) for level in levels)
    except ValueError:
        return None
    return (int(header.format), max(1, header.width >> first), max(1, header.height >> first), layers,
            len(levels), payload)


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
    from .profiles.re_engine_dxil_recovery import recover_re_engine_dxil_ir
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
    return _MaterialReads(frozenset(holders), MappingProxyType(tables),
                          MappingProxyType(_record_tags(lines, accesses, prefix)), texture_reads,
                          material_buffer_reads(lines, accesses), engine_heap_handles(lines, contract, accesses))


@lru_cache(maxsize=16)
def _heap_indices(contract, shader: bytes):
    from .profiles.re_engine_bindless_material import HeapIndices
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


def gpu_buffer_contents(path: str, data: bytes) -> bytes:
    header = _GPU_BUFFER_HEADERS.get(resource_version_from_path(path, "gpbf"))
    if header is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The GPU buffer file {path} is of a version the preview does not read."), {"path": path})
    return bytes(data[header:])


def _gpu_buffer(name: str, path: str, load_file) -> bytes:
    data = load_file(path) if path else None
    if data is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The material's GPU buffer {name} was not found."),
                                   {"name": name})
    return bytes(data)


def shader_bindings(document, shaders: Mapping[str, tuple], material: PreviewMaterial, load_file,
                    engine_buffer: Callable | None = None, sampler: Callable[[str], tuple] | None = None, *,
                    engine_texture: Callable | None = None, read_textures: dict | None = None,
                    heap_start: int = _MATERIAL_HEAP_START, engine_heap: Callable | None = None,
                    shared_holders: Mapping[str, bytes] | None = None,
                    streamed: Callable[[str], bool] | None = None, resident_file: Callable | None = None,
                    pixel_sampled: Collection[str] = (), made_texture: Callable[[str], tuple] | None = None):
    """Layout rules: docs/internals/mesh-preview.md, Material bindings."""
    if heap_start < _MATERIAL_HEAP_START:
        raise ValueError(f"the material's heap indices start at {heap_start}, before {_MATERIAL_HEAP_START}")
    profile = document.profile.shader_graph_profile
    record, prefix = material.record(), int(profile.material_record_prefix_size)
    reads: dict[str, _MaterialReads] = {}
    tags: dict[int, int] = {}
    for stage, (contract, shader) in shaders.items():
        if int(contract.model_major) < 6:
            continue
        reads[stage] = _material_reads(contract, shader, prefix)
        for offset, value in reads[stage].tags.items():
            if tags.setdefault(offset, value) != value:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The shaders accept different material record kinds."))
    words = {(kind, offset + prefix): name for (kind, name), offset in
             material.layout.word_offsets(material.shader_lod).items()}
    slots = {offset: name for (kind, offset), name in words.items() if kind == "texture"}
    missing, loaded = [], {}
    pixel_read = {slots.get(read.byte_offset) for read in (reads["pixel"].textures if "pixel" in reads else ())}
    pixel_read |= set(pixel_sampled)

    def texture(index: int, name: str, dimension: str) -> tuple | None:
        if (index, name, dimension) not in loaded and made_texture is not None:
            path = material.textures.get(name, "")
            made = made_texture(path) if path else None
            if made is not None:
                loaded[index, name, dimension] = (index, *made[1:]) if made[0] == dimension else None
        if (index, name, dimension) not in loaded:
            path = material.textures.get(name, "")
            pixel = name in pixel_read or resident_file is None
            data = (load_file if pixel else resident_file)(path) if path else None
            loaded[index, name, dimension] = (_texture_entry(index, data, dimension, read_textures,
                                                             pixel and streamed is not None and streamed(path))
                                              if data else None)
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
    for stage, read in ((stage, read) for stage, found in reads.items() for read in found.textures):
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
    for offset in sorted({offset for found in reads.values() for offset in found.buffers}):
        name = words.get(("gpu_buffer", offset))
        if name is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The pass reads the descriptor heap through a record word that names no GPU buffer."))
        if name not in buffers:
            index = buffers[name] = base + len(indices) + len(buffers) + 1
            heap[index] = (RANGE_SRV, VIEW_DIMENSIONS["buffer"], (index, 0, 0, _gpu_buffer(
                name, material.buffers.get(name, ""), load_file)))
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
            return shape(bytes(header) + bytes(now))
        return Following((material.animated, place, held), make)
    sizes = [size for found in reads.values() for size in found.tables.values()]
    if sizes:
        buffer_index, largest = base + len(indices) + len(buffers) + 1, max(sizes)
        heap[buffer_index] = (RANGE_CBV, 0, (buffer_index, given(lambda data: data.ljust(largest, b"\0"),
                                                            ("table", largest))))
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
                    # The record's first ``prefix`` bytes are heap index fields, not material values.
                    constants[(stage, resource.name)] = bytes(record[:size]).ljust(size, b"\0")
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, given(
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
            entries = list(arrays.get((stage, resource.name), {}).values())
            if found is not None and resource.name in found.holders:
                stride = contract.structured_buffer(resource.name).byte_size
                shared = None if shared_holders is None else shared_holders.get(resource.name)
                entries = [(0, 0, stride, given(lambda data, stride=stride, shared=shared: _holder_bytes(
                    data, shared, stride), ("holder", stride, shared)))]
            elif found is not None and resource.name in found.tables:
                entries = [(0, 0, 4, struct.pack("<I", buffer_index))]
            elif resource.bind_count == 1 and resource.name in material.textures and resource.input_type == "texture":
                entries = [entry] if (entry := texture(0, resource.name, dimension)) else []
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
                for kind, dimension, entry in given:
                    if heap.setdefault(entry[0], (kind, dimension, entry)) != (kind, dimension, entry):
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


def pixel_sampled_textures(document, material: PreviewMaterial, pixel_shaders: Iterable[tuple]) -> frozenset[str]:
    """The renderer streams these for every program of the material."""
    prefix = int(document.profile.shader_graph_profile.material_record_prefix_size)
    slots = {offset + prefix: name for (kind, name), offset in material.layout.word_offsets(material.shader_lod).items()
             if kind == "texture"}
    found = {slots.get(read.byte_offset) for contract, shader in pixel_shaders if int(contract.model_major) >= 6
             for read in _material_reads(contract, shader, prefix).textures}
    found.discard(None)
    return frozenset(found)


__all__ = ["Following", "MaterialPreviewError", "PreviewMaterial", "bound_record_size", "gbuffer_route",
           "gpu_buffer_contents", "pixel_sampled_textures", "shader_bindings"]
