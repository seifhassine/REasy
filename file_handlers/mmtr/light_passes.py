"""See docs/internals/light-passes.md."""
from __future__ import annotations

import base64
import math
import struct
import zlib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial

lazy import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.ocioc import OCIOC_MAGIC, OciocTexture, parse_ocioc
from file_handlers.sdf.contracts import shader_contract_for_program
lazy from file_handlers.sdf.models import program_names
from file_handlers.sdf.sdf_file import parse_sdf_to_draw
from file_handlers.build_catalog import catalog_file_name, catalog_reader
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R32_UINT

from .engine_buffers import FRAME_BUFFERS, Camera, _laid_out, camera_key, frame_values, scene_values, with_words
lazy from .material_preview import (
    RANGE_CBV, RANGE_SAMPLER, RANGE_SRV, RANGE_UAV, _UAV_DIMENSIONS, VIEW_DIMENSIONS, SHADER_VISIBILITY,
    MaterialPreviewError, _texture_entry,
)
from .mesh_draw import program_states
from .mesh_render import DEPTH_VIEWS, PHASES, MeshScene
lazy from .pass_choice import MESH_PROGRAMS
lazy from .profiles.base import mmtr_profile_for_game
lazy from .resident import DEPTH_STENCIL, RENDER_TARGET, TEXTURE_2D, UNORDERED_ACCESS
from .sampler_states import sampler_catalog_for
from .shown_image import shown_step

CATALOG_FORMAT = 6
CATALOG_DIRECTORY = "resources/data/light_passes"
ROLES = ("emissive_decode", "subsurface_param", "deferred_light", "subsurface_blur", "diffuse_integrate", "solid_blur",
         "local_exposure", "tonemap", "copy", "display")
SOURCES = ("gbuffer", "depth", "output", "made", "history", "file", "ocio", "buffer", "derived", "copy", "phase",
           "none")
DERIVED_READ = QT_TRANSLATE_NOOP("MaterialPreview", "The pass reads {name}, which the renderer makes from the lit "
                                                      "image by passes REasy does not run.")


class LightPassCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Source:
    """``value`` is, by ``kind``, a target number, ``PASS.KEY``, ``PHASE.KEY``, a path, a lookup name or a SHA-256.
    Kinds are listed in docs/internals/light-passes.md."""
    kind: str
    value: str = ""


@dataclass(frozen=True, slots=True)
class PassRead:
    """``view_format`` is 0 for buffers; a multi-mip view reads ``source`` then ``levels``."""
    stage: str
    name: str
    source: Source
    view_format: int
    levels: tuple[Source, ...] = ()


@dataclass(frozen=True, slots=True)
class BufferShape:
    """``size`` is in bytes, per dispatched group when ``per_group``."""
    size: int
    per_group: bool


@dataclass(frozen=True, slots=True)
class PassOutput:
    """``key`` is ``target N`` or the declared name. A texture is the screen size halved ``level`` times, or ``size``.
    """
    key: str
    format: int
    texel_bytes: int
    initial: Source
    buffer: BufferShape | None
    size: tuple[int, int, int] | None
    level: int = 0


@dataclass(frozen=True, slots=True)
class Indirect:
    """The group counts are three 32-bit words at byte ``offset`` in ``source``."""
    source: Source
    offset: int


@dataclass(frozen=True, slots=True)
class LightPass:
    """``count`` is (vertices, instances); a dispatch runs one group per ``tile`` texels unless ``indirect``.
    ``constants`` holds the captured per-view fields as 32-bit words."""
    role: str
    name: str
    file: str
    target: int
    kind: str
    count: tuple[int, int]
    tile: tuple[int, int, int]
    stencil_reference: int
    depth: Source | None
    outputs: tuple[PassOutput, ...]
    reads: tuple[PassRead, ...]
    constants: Mapping[str, Mapping[str, tuple[int, ...]]]
    indirect: Indirect | None


@dataclass(frozen=True, slots=True)
class MeshPhase:
    """``purpose`` is one of ``mesh_render.PHASES`` or a master's own pass; only parts whose masters match ``masters``
    draw it."""
    purpose: str
    after: int
    targets: tuple[PassOutput, ...]
    reads: tuple[PassRead, ...]
    constants: Mapping[str, Mapping[str, tuple[int, ...]]]
    masters: tuple[tuple[str, ...], ...] = ()
    depth: bool = True

    @property
    def key(self) -> tuple[str, tuple[tuple[str, ...], ...]]:
        return self.purpose, self.masters


def phase_label(purpose: str, masters: tuple[tuple[str, ...], ...]) -> str:
    return purpose + "".join(f" [{' '.join(names)}]" for names in masters)


@dataclass(frozen=True, slots=True)
class LightPassCatalog:
    game: str
    resource_version: int
    # The master target the captured frames drew the G-buffer with.
    master_target: int
    passes: tuple[LightPass, ...]
    # Keyed by SHA-256.
    buffers: Mapping[str, bytes]
    source: Mapping[str, object]
    # In frame order.
    phases: tuple[MeshPhase, ...] = ()


def _source_to_dict(source: Source) -> dict:
    return {"kind": source.kind, "value": source.value}


def _source_from_dict(data: Mapping) -> Source:
    kind = str(data["kind"])
    if kind not in SOURCES:
        raise LightPassCatalogError(f"source kind {kind!r} is none of {', '.join(SOURCES)}")
    return Source(kind, str(data["value"]))


def _output_to_dict(output: PassOutput) -> dict:
    return {"key": output.key, "format": output.format, "texel_bytes": output.texel_bytes,
            "initial": _source_to_dict(output.initial),
            "buffer": None if output.buffer is None else {"size": output.buffer.size,
                                                          "per_group": output.buffer.per_group},
            "size": None if output.size is None else list(output.size), "level": output.level}


def _read_to_dict(read: PassRead) -> dict:
    return {"stage": read.stage, "name": read.name, "source": _source_to_dict(read.source),
            "view_format": read.view_format, "levels": [_source_to_dict(level) for level in read.levels]}


def _constants_to_dict(constants: Mapping[str, Mapping[str, tuple[int, ...]]]) -> dict:
    return {name: {field: list(words) for field, words in sorted(fields.items())}
            for name, fields in sorted(constants.items())}


def catalog_to_dict(catalog: LightPassCatalog) -> dict:
    return {
        "format": CATALOG_FORMAT, "game": catalog.game, "resource_version": catalog.resource_version,
        "master_target": catalog.master_target, "source": dict(catalog.source),
        "passes": [{
            "role": item.role, "name": item.name, "file": item.file, "target": item.target, "kind": item.kind,
            "count": list(item.count), "tile": list(item.tile),
            "stencil_reference": item.stencil_reference,
            "depth": None if item.depth is None else _source_to_dict(item.depth),
            "outputs": [_output_to_dict(output) for output in item.outputs],
            "reads": [_read_to_dict(read) for read in item.reads],
            "constants": _constants_to_dict(item.constants),
            "indirect": None if item.indirect is None else {"source": _source_to_dict(item.indirect.source),
                                                            "offset": item.indirect.offset},
        } for item in catalog.passes],
        "phases": [{"purpose": phase.purpose, "masters": [list(names) for names in phase.masters],
                    "after": phase.after, "depth": phase.depth,
                    "targets": [_output_to_dict(output) for output in phase.targets],
                    "reads": [_read_to_dict(read) for read in phase.reads],
                    "constants": _constants_to_dict(phase.constants)} for phase in catalog.phases],
        "buffers": {digest: base64.b64encode(zlib.compress(data, 9)).decode("ascii")
                    for digest, data in sorted(catalog.buffers.items())},
    }


def _size(value, name: str) -> tuple[int, int, int]:
    size = tuple(int(item) for item in value)
    if len(size) != 3 or min(size) < 1:
        raise LightPassCatalogError(f"pass {name!r} writes a texture of size {size}")
    return size


def _output_from_dict(output: Mapping, name: str) -> PassOutput:
    return PassOutput(str(output["key"]), int(output["format"]), int(output["texel_bytes"]),
                      _source_from_dict(output["initial"]),
                      None if output["buffer"] is None else BufferShape(int(output["buffer"]["size"]),
                                                                        bool(output["buffer"]["per_group"])),
                      None if output["size"] is None else _size(output["size"], name), int(output["level"]))


def _read_from_dict(read: Mapping) -> PassRead:
    return PassRead(str(read["stage"]), str(read["name"]), _source_from_dict(read["source"]), int(read["view_format"]),
                    tuple(_source_from_dict(level) for level in read["levels"]))


def _constants_from_dict(constants: Mapping) -> dict[str, dict[str, tuple[int, ...]]]:
    return {str(name): {str(field): tuple(int(word) for word in words) for field, words in fields.items()}
            for name, fields in constants.items()}


def _phase_from_dict(item: Mapping, passes: list[LightPass]) -> MeshPhase:
    purpose, after = str(item["purpose"]), int(item["after"])
    masters = tuple(tuple(str(name) for name in names) for names in item["masters"])
    if purpose in PHASES:
        wrong = bool(masters) or not item["depth"]
    else:
        wrong = purpose not in MESH_PROGRAMS or not masters or len(set(masters)) != len(masters) or any(
            purpose not in names or list(names) != sorted(set(names) & set(MESH_PROGRAMS)) for names in masters)
    if wrong:
        raise LightPassCatalogError(f"a {purpose!r} phase is drawn by the masters {masters}")
    if not 0 <= after < len(passes) - 1:
        raise LightPassCatalogError(f"a {purpose!r} phase draws after pass {after} of {len(passes)}")
    phase = MeshPhase(purpose, after, tuple(_output_from_dict(output, purpose) for output in item["targets"]),
                      tuple(_read_from_dict(read) for read in item["reads"]), _constants_from_dict(item["constants"]),
                      masters, bool(item["depth"]))
    if [output.key for output in phase.targets] != [f"target {slot}" for slot in range(len(phase.targets))]:
        raise LightPassCatalogError(f"the {purpose} phase binds render targets out of order: "
                                    f"{[output.key for output in phase.targets]}")
    forms = [output.format for output in phase.targets]
    if not forms or not forms[0] or 0 in forms and any(forms[forms.index(0):]):
        raise LightPassCatalogError(f"the {purpose} phase leaves render targets unbound before others: {forms}")
    if any(output.buffer is not None or output.size is not None or output.level
           or output.initial.kind not in ("output", "copy", "phase", "none")
           or not output.format and output.initial.kind != "none"
           for output in phase.targets) or any(read.source.kind not in (
               "gbuffer", "depth", "output", "history", "file", "derived", "phase", "none") for read in phase.reads):
        raise LightPassCatalogError(f"the {purpose} phase draws onto or reads what no phase can")
    return phase


def _pass_output(source: Source, passes: list[LightPass]) -> PassOutput | None:
    index, _dot, key = source.value.partition(".")
    if not index.isdigit() or int(index) >= len(passes):
        return None
    return next((item for item in passes[int(index)].outputs if item.key == key), None)


def _check_own_textures(phases: tuple[MeshPhase, ...], passes: list[LightPass]) -> None:
    own: dict[str, PassOutput] = {}
    for number, phase in enumerate(phases):
        for source in [read.source for read in phase.reads] + [output.initial for output in phase.targets]:
            if source.kind == "phase" and source.value not in own:
                raise LightPassCatalogError(f"the {phase.purpose} phase draws onto or reads {source.value}, which no "
                                            f"phase before it starts")
        for output in phase.targets:
            if output.initial.kind == "phase" and (output.format, output.texel_bytes) != (
                    own[output.initial.value].format, own[output.initial.value].texel_bytes):
                raise LightPassCatalogError(f"the {phase.purpose} phase draws onto {output.initial.value} in another "
                                            f"format than it starts in")
        for output in phase.targets:
            if output.initial.kind == "copy":
                copied = _pass_output(output.initial, passes)
                if copied is None or copied.buffer is not None or copied.size is not None or copied.level \
                        or (copied.format, copied.texel_bytes) != (output.format, output.texel_bytes):
                    raise LightPassCatalogError(f"the {phase.purpose} phase copies {output.initial.value}, not an "
                                                f"output of the screen's size in its target's format")
                own[f"{number}.{output.key}"] = output


def _check_levels(read: PassRead, passes: list[LightPass], name: str) -> None:
    if not read.levels:
        return
    found = [_pass_output(source, passes) if source.kind in ("output", "history") else None
             for source in (read.source, *read.levels)]
    if None in found or any(output.buffer is not None or output.size is not None for output in found) \
            or [output.level for output in found] != list(range(found[0].level, found[0].level + len(found))) \
            or len({output.texel_bytes for output in found}) != 1:
        raise LightPassCatalogError(f"{name} reads {read.name} from {[read.source, *read.levels]}, not the outputs of "
                                    f"consecutive levels of one texel size")


def catalog_from_dict(data: Mapping) -> LightPassCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise LightPassCatalogError(f"light pass catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    passes = []
    for item in data["passes"]:
        if item["role"] not in ROLES or item["kind"] not in ("draw", "dispatch"):
            raise LightPassCatalogError(f"pass {item['name']!r} is a {item['kind']} of role {item['role']!r}")
        count, tile = tuple(int(value) for value in item["count"]), tuple(int(value) for value in item["tile"])
        if len(count) != 2 or len(tile) != 3:
            raise LightPassCatalogError(f"pass {item['name']!r} has counts {count} and tile {tile}")
        passes.append(LightPass(
            str(item["role"]), str(item["name"]), str(item["file"]), int(item["target"]), str(item["kind"]), count, tile,
            int(item["stencil_reference"]),
            None if item["depth"] is None else _source_from_dict(item["depth"]),
            tuple(_output_from_dict(output, item["name"]) for output in item["outputs"]),
            tuple(_read_from_dict(read) for read in item["reads"]), _constants_from_dict(item["constants"]),
            None if item["indirect"] is None else Indirect(_source_from_dict(item["indirect"]["source"]),
                                                           int(item["indirect"]["offset"]))))
        if len({output.size for output in passes[-1].outputs if output.buffer is None}) > 1:
            raise LightPassCatalogError(f"pass {item['name']!r} writes textures of several sizes")
        if any(read.source.kind == "derived" for read in passes[-1].reads):
            raise LightPassCatalogError(f"pass {item['name']!r} reads a derived texture")
        for read in passes[-1].reads:
            _check_levels(read, passes, f"pass {item['name']!r}")
    if not passes or passes[-1].role != "display":
        raise LightPassCatalogError("the light pass catalog does not end with the display pass")
    phases = tuple(_phase_from_dict(item, passes) for item in data["phases"])
    for phase in phases:
        for read in phase.reads:
            _check_levels(read, passes, f"the {phase.purpose} phase")
    drawing = [(phase.purpose, names) for phase in phases for names in phase.masters or ((),)]
    if len(set(drawing)) != len(drawing) or [phase.after for phase in phases] != sorted(
            phase.after for phase in phases):
        raise LightPassCatalogError("the light pass catalog's phases repeat a purpose or are out of order")
    _check_own_textures(phases, passes)
    return LightPassCatalog(str(data["game"]), int(data["resource_version"]), int(data["master_target"]),
                            tuple(passes), {str(digest): zlib.decompress(base64.b64decode(blob))
                                            for digest, blob in data["buffers"].items()},
                            dict(data["source"]), phases)


light_pass_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, LightPassCatalogError,
                                        "light pass", (zlib.error,))


_DIMENSIONS = {False: ("texture2d", "texture2d-array"), True: ("texture3d",)}
_ALL_BITS = 0xFFFFFFFF
_STRUCTURED_BUFFERS = ("structured-buffer", "rw-structured-buffer")
_BUFFERS = (*_STRUCTURED_BUFFERS, "byte-address-buffer", "rw-byte-address-buffer")
_LIGHT_BUFFERS = ("LightInfo", "DirectionalLightParameter")
_ENGINE_BUFFERS = ("SceneInfo", *_LIGHT_BUFFERS, *FRAME_BUFFERS, "ImageSize")
# The lit-image blurs clamp to these, so they hold size - 1.
IMAGE_SIZE_FIELDS = ("ImageWidth", "ImageHeight")


def image_size(size: tuple[int, int]) -> tuple[int, int]:
    return size[0] - 1, size[1] - 1


@dataclass(frozen=True, slots=True)
class Light:
    """``direction`` points towards the light (as ``DL_Direction``); ``color`` is linear, times intensity."""
    direction: tuple[float, float, float]
    color: tuple[float, float, float]


def derived_read(phase: MeshPhase, names: Iterable[str]) -> str | None:
    derived = {read.name for read in phase.reads if read.source.kind == "derived"}
    return next((name for name in names if name in derived), None)


def _same_depth(resource, size: tuple[int, int, int]) -> None:
    if resource.dimension not in _DIMENSIONS[size[2] > 1]:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The light pass reads a texture {depth} texels deep as a {kind}."),
            {"depth": size[2], "kind": resource.dimension})


def light_pass_files(catalog: LightPassCatalog) -> tuple[str, ...]:
    return tuple(dict.fromkeys([item.file for item in catalog.passes] + [
        read.source.value for item in (*catalog.passes, *catalog.phases) for read in item.reads
        if read.source.kind == "file"]))


def _engine_values(name: str, camera: Camera, light: Light, size: tuple[int, int]) -> dict[str, np.ndarray]:
    if name == "SceneInfo":
        return scene_values(camera)
    if name in _LIGHT_BUFFERS:
        return {"DL_Direction": np.asarray(light.direction, np.float64),
                "DL_Color": np.asarray(light.color, np.float64)}
    if name in FRAME_BUFFERS:
        return frame_values(name)
    if name == "ImageSize":
        return dict(zip(IMAGE_SIZE_FIELDS, map(np.array, image_size(size))))
    return {}


def _follows(name: str, camera: Camera, light: Light, size: tuple[int, int]) -> tuple:
    if name == "SceneInfo":
        return camera_key(camera)
    if name in _LIGHT_BUFFERS:
        return (np.asarray((*light.direction, *light.color), np.float64).tobytes(),)
    if name == "ImageSize":
        return image_size(size)
    return ()


def _constant_buffer(name: str, layout, camera: Camera, light: Light,
                     captured: Mapping[str, tuple[int, ...]], size: tuple[int, int]) -> bytes:
    data = _laid_out(layout, _engine_values(name, camera, light, size), True)
    words_of = dict(captured)
    if name in _LIGHT_BUFFERS:
        words_of.update(DL_Enable=(_ALL_BITS,), DL_TextureBindlessIndex=(_ALL_BITS,))
    return with_words(data, layout, words_of).take_bytes()


def captured_constants(catalog: LightPassCatalog) -> dict[str, dict[str, tuple[int, ...]]]:
    found: dict[str, set] = {}
    for item in (*catalog.passes, *catalog.phases):
        for name, fields in item.constants.items():
            if fields:
                found.setdefault(name, set()).add(frozendict(fields))
    if any(len(kinds) > 1 for kinds in found.values()):
        raise ValueError(f"the catalog's passes capture {sorted(n for n, k in found.items() if len(k) > 1)} differently")
    return {name: dict(*kinds) for name, kinds in found.items()}


class LightPasses:
    """Positions are (N, -1) for pass N and (N, M) for phase M."""

    def __init__(self, scene: MeshScene, *, pass_file: Callable[[str, bytes], _PassFile]):
        profile, load_file = scene.profile, scene.load_file
        catalog, samplers = light_pass_catalog_for(profile), sampler_catalog_for(profile)
        if catalog is None or samplers is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy has no catalog of the renderer's light passes for this game build."))
        if scene.phases != {phase.key for phase in catalog.phases}:
            raise ValueError(f"the scene leaves out the phases {sorted(scene.phases)}, not the catalog's")
        self.scene, self.device, self.catalog, self.samplers = scene, scene.device, catalog, samplers
        self.light: Light | None = None
        self.programs = []
        documents = {}
        for item in catalog.passes:
            if item.file not in documents:
                data = load_file(item.file)
                if data is None:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The game's shader file {path} was not found."), {"path": item.file})
                documents[item.file] = pass_file(item.file, data)
            target = documents[item.file].document.targets[item.target]
            found = [index for index, name in enumerate(program_names(target)) if name == item.name]
            if len(found) != 1:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The game's shader file {path} has {count} programs named {name}."),
                    {"path": item.file, "count": len(found), "name": item.name})
            program = target.programs[found[0]]
            self.programs.append((program, {
                shader.role: shader_contract_for_program(profile.sdf_profile, program, shader.role)
                for shader in program.shaders if shader.present}))
        self.lookups = {item.file: documents[item.file].lookups for item in catalog.passes}
        sources = [source for item in (*catalog.passes, *catalog.phases) for read in item.reads
                   for source in (read.source, *read.levels)]
        sources += [output.initial for item in catalog.passes for output in item.outputs]
        sources += [item.indirect.source for item in catalog.passes if item.indirect is not None]
        self._drawn_onto = {output.initial.value for phase in catalog.phases for output in phase.targets
                            if output.initial.kind == "output"}
        self._made_apart = {source.value for source in sources if source.kind == "made"} & self._drawn_onto
        self._carried = sorted({source.value for source in sources if source.kind == "history"})
        self._starting: list[Callable[[], list[int]]] = []
        self._made_constants: dict[tuple, tuple[tuple, bytes]] = {}
        self._added: dict[tuple[int, int, bool], list[Callable[[], list[int]]]] = {}
        self.end = (len(catalog.passes), -1)
        self._screen()

    def _screen(self) -> None:
        self.outputs: dict[str, int] = {}
        self._made: dict[str, int] = {}
        self._history: dict[str, int] = {}
        self._own: dict[str, int] = {}
        self._pass_steps: dict[int, tuple[list[int], int | dict, list[int]]] = {}
        self._dispatches: dict[tuple[int, tuple[int, int, int]], int] = {}
        self._phase_steps: dict[int, tuple[list[int], list[int]]] = {}
        self._tested: int | None = None
        self._mips: dict[tuple, int] = {}
        self._before: dict[tuple[int, int], int] = {}
        # By phase: what its parts read through and the copies those reads need (``_reads``).
        self._phase_reads: dict[int, tuple] = {}
        self._shown: dict[tuple, tuple[int, int]] = {}
        self._zeros: dict[str, int] = {}

    def resize(self) -> None:
        """Call after ``MeshScene.resize``."""
        old = [*self.outputs.values(), *self._made.values(), *self._history.values(), *self._own.values(),
               *self._mips.values(), *self._before.values(), self._tested,
               *(texture for _step, texture in self._shown.values()), *self._zeros.values()]
        self._screen()
        self.device.release(old)

    def added(self) -> None:
        """Call after ``MeshScene.add``: the phases draw the objects added too, onto the textures they had."""
        self._phase_steps = {}

    def texture_size(self, output: PassOutput) -> tuple[int, int, int]:
        if output.size is not None:
            return output.size
        width, height = self.scene.size
        return max(1, width >> output.level), max(1, height >> output.level), 1

    def extent(self, item: LightPass) -> tuple[int, int, int]:
        textures = [output for output in item.outputs if output.buffer is None]
        return self.texture_size(min(textures, key=lambda output: output.level)) if textures else (*self.scene.size, 1)

    def output(self, value: str) -> PassOutput:
        index, key = value.split(".", 1)
        return next(output for output in self.catalog.passes[int(index)].outputs if output.key == key)

    def groups(self, index: int, item: LightPass) -> tuple[int, int, int]:
        if item.indirect is None:
            return tuple(-(-size // texels) for size, texels in zip(self.extent(item), item.tile))
        try:
            data = self.device.flush([self.contents(item.indirect.source, (index, -1))])[0]
        except RuntimeError as exc:
            raise _failed(item, exc) from exc
        if len(data) < item.indirect.offset + 12:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The light pass {name} dispatches the groups past the end of a buffer."),
                {"name": item.name})
        return struct.unpack_from("<3I", data, item.indirect.offset)

    def size(self, value: str) -> int:
        output = self.output(value)
        if output.buffer is None:
            return math.prod(self.texture_size(output)) * output.texel_bytes
        if not output.buffer.per_group:
            return output.buffer.size
        index = int(value.split(".", 1)[0])
        item = self.catalog.passes[index]
        if item.indirect is not None:
            # Untested: no catalog has such a buffer.
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The light pass {name} sizes a buffer by the groups it dispatches indirectly, "
                                   "which the preview does not follow."), {"name": item.name})
        return output.buffer.size * math.prod(self.groups(index, item))

    def _flags(self, value: str) -> int:
        dispatched = self.catalog.passes[int(value.split(".", 1)[0])].kind == "dispatch"
        return (UNORDERED_ACCESS if dispatched else RENDER_TARGET) | (RENDER_TARGET if value in self._drawn_onto else 0)

    def _held(self, held: dict[str, int], value: str) -> int:
        if value not in held:
            output, zeros = self.output(value), bytes(self.size(value))
            if output.buffer is None:
                held[value] = self.device.texture(output.format, self.texture_size(output), self._flags(value), zeros)
            else:
                held[value] = self.device.scene.buffer(zeros, self._flags(value) & UNORDERED_ACCESS)
        return held[value]

    def _zero(self, resource: int, value: str) -> int:
        if value not in self._zeros:
            output, zeros = self.output(value), bytes(self.size(value))
            self._zeros[value] = (
                self.device.scene.buffer(zeros, 0) if output.buffer is not None
                else self.device.texture(output.format, self.texture_size(output), self._flags(value), zeros))
        return self.device.copy(resource, self._zeros[value])

    def contents(self, source: Source, at: tuple[int, int]) -> int | None:
        kind, value = source.kind, source.value
        if kind == "none":
            return None
        if kind in ("output", "copy"):
            return self._held(self.outputs, value)
        if kind == "made":
            return self._made[value] if value in self._made_apart else self.outputs[value]
        if kind == "phase":
            number = int(value.split(".", 1)[0])
            if (self.catalog.phases[number].after, number) < at:
                return self._own[value]
        if kind == "history":
            return self._held(self._history, value)
        if kind == "gbuffer":
            found = self.scene.targets.get(int(value))
            if found is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The light pass reads G-buffer target {target}, which no pass drew."),
                    {"target": value})
            return found
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "REasy's light pass catalog gives a screen texture from a {kind}."), {"kind": kind})

    def shape(self, source: Source) -> tuple[int, tuple[int, int, int]]:
        screen = (*self.scene.size, 1)
        if source.kind == "gbuffer":
            return self.scene.target_formats[int(source.value)], screen
        if source.kind == "phase":
            number, key = source.value.split(".", 1)
            return next(output.format for output in self.catalog.phases[int(number)].targets
                        if output.key == key), screen
        output = self.output(source.value)
        return output.format, self.texture_size(output)

    @staticmethod
    def stride(resource, contract) -> int:
        if resource.input_type in _STRUCTURED_BUFFERS:
            return contract.structured_buffer(resource.name).byte_size
        return 0

    def entry(self, read: PassRead, resource, contract, at: tuple[int, int], depth: int, before: list[int],
              file: str = "") -> tuple | None:
        source = read.source
        if source.kind == "none":
            return None
        if resource.input_type in _BUFFERS:
            held = self.device.buffer(self.catalog.buffers[source.value]) if source.kind == "buffer" \
                else self.contents(source, at)
            return 0, 0, self.stride(resource, contract), held
        if source.kind == "buffer":
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The light pass reads {name} as a {kind}."),
                {"name": resource.name, "kind": resource.input_type})
        if source.kind == "file":
            entry = _texture_entry(0, source.value, self.scene.load_file, resource.dimension, self.scene.read_textures)
            if entry is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The game's texture {path} was not found, or is not the shape the light pass "
                                       "reads."), {"path": source.value})
            return self.device.read(VIEW_DIMENSIONS[resource.dimension], entry)
        if source.kind == "ocio":
            texture = self.lookups[file].get(source.value)
            if texture is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The OpenColorIO configuration {path} has no lookup texture {name}."),
                    {"path": file, "name": source.value})
            size = (texture.width, texture.height, texture.depth)
            _same_depth(resource, size)
            return self.device.read(VIEW_DIMENSIONS[resource.dimension],
                                    (0, read.view_format, *size, 1, texture.texels))
        if source.kind == "depth":
            _resource, as_float, as_uint = DEPTH_VIEWS[self.scene.depth_format]
            form = {"float": as_float, "uint": as_uint}.get(resource.return_type)
            if form is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The light pass reads the depth as {kind}."), {"kind": resource.return_type})
            size, entry = (*self.scene.size, 1), (0, form, depth)
        else:
            sources = (source, *read.levels)
            found = [self.contents(item, at) for item in sources]
            form, size = self.shape(source)
            if any(self.shape(item)[0] != read.view_format for item in sources):
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The light pass reads {name} in another format than it is written in."),
                    {"name": resource.name})
            held = found[0]
            if read.levels:
                key = (at, read.stage, read.name)
                if key not in self._mips:
                    self._mips[key] = self.device.scene.texture(form, *size, len(sources), TEXTURE_2D, 0, None)
                held = self._mips[key]
                before += [self.device.copy(held, level, mip, 0) for mip, level in enumerate(found)]
            entry = (0, form, held)
        _same_depth(resource, size)
        return entry

    def tables(self, index: int, item: LightPass, contracts: Mapping, before: list[int]) -> list:
        """Appends to ``before`` the steps that must run first."""
        at = (index, -1)
        reads = {(read.stage, read.name): read for read in item.reads}
        outputs = {output.key: output for output in item.outputs}
        tables = []
        for stage, contract in contracts.items():
            visibility = SHADER_VISIBILITY[stage]
            for resource in contract.resources:
                kind, place = resource.register_class, (resource.bind_point, resource.register_space,
                                                        resource.bind_count)
                if kind == "s":
                    state = self.samplers.states[resource.name].descriptor()
                    tables.append((RANGE_SAMPLER, *place, 0, visibility,
                                   tuple((slot, *state) for slot in range(max(1, resource.bind_count)))))
                elif kind == "b":
                    layout = contract.buffer(resource.name)
                    # A pass's constant buffers of one name and layout are shared by its stages.
                    key = (index, resource.name, layout.byte_size, layout.variables)
                    held = self.device.following(key, partial(self._constants, key, item, resource.name, layout))
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, held),)))
                elif kind == "t":
                    dimension = resource.dimension if resource.input_type == "texture" else "buffer"
                    if dimension not in VIEW_DIMENSIONS:
                        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The light pass reads a {kind} texture."), {"kind": dimension})
                    read = reads.get((stage, resource.name))
                    entry = None if read is None else self.entry(read, resource, contract, at, self.scene.depth,
                                                                 before, item.file)
                    tables.append((RANGE_SRV, *place, VIEW_DIMENSIONS[dimension], visibility,
                                   () if entry is None else (entry,)))
                elif kind == "u":
                    output = outputs.get(resource.name)
                    dimension = resource.dimension if resource.input_type.startswith("rw-typed") else "buffer"
                    if output is None:
                        tables.append((RANGE_UAV, *place, _UAV_DIMENSIONS.get(dimension, 1), visibility, ()))
                        continue
                    value = f"{index}.{output.key}"
                    held = self._held(self.outputs, value)
                    initial = self.contents(output.initial, at)
                    before.append(self._zero(held, value) if initial is None else self.device.copy(held, initial))
                    if output.buffer is not None and resource.input_type in _BUFFERS:
                        entry = (0, 0, self.stride(resource, contract), held)
                    elif output.buffer is None and dimension in _DIMENSIONS[self.texture_size(output)[2] > 1]:
                        entry = (0, output.format, held)
                    else:
                        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The light pass writes {name} as a {kind}."),
                            {"name": resource.name, "kind": resource.input_type})
                    tables.append((RANGE_UAV, *place, _UAV_DIMENSIONS[dimension], visibility, (entry,)))
                else:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The light pass declares a resource of register class {kind}."),
                        {"kind": kind})
        return tables

    def pass_steps(self, index: int) -> list[int]:
        item, (program, contracts) = self.catalog.passes[index], self.programs[index]
        groups = self.groups(index, item) if item.kind == "dispatch" else None
        try:
            if index not in self._pass_steps:
                before: list[int] = []
                run = self.draw(index, item, program, contracts, before) if groups is None else {
                    "compute_shader": bytes(program.shader("compute").payload),
                    "tables": self.tables(index, item, contracts, before), "heap": []}
                after = [self.device.copy(self._held(self._made, value), self.outputs[value])
                         for value in (f"{index}.{output.key}" for output in item.outputs)
                         if value in self._made_apart]
                self._pass_steps[index] = (before, run, after)
            before, run, after = self._pass_steps[index]
            if groups is None:
                return [*before, run, *after]
            if not all(groups):
                return [*before, *after]
            if (index, groups) not in self._dispatches:
                self._dispatches[index, groups] = self.device.scene.dispatch(run | {"groups": groups})
            return [*before, self._dispatches[index, groups], *after]
        except RuntimeError as exc:
            raise _failed(item, exc) from exc

    def draw(self, index: int, item: LightPass, program, contracts: Mapping, before: list[int]) -> int:
        scene = self.device.scene
        width, height = self.scene.size
        targets = {int(output.key.removeprefix("target ")): output for output in item.outputs}
        if sorted(targets) != list(range(len(targets))):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The light pass {name} leaves a render target between others unbound."),
                {"name": item.name})
        bound = []
        for _slot, output in sorted(targets.items()):
            held = self._held(self.outputs, f"{index}.{output.key}")
            initial = self.contents(output.initial, (index, -1))
            before.append(scene.clear(output.format, held) if initial is None else self.device.copy(held, initial))
            bound.append((output.format, held))
        tables = self.tables(index, item, contracts, before)
        if item.depth is not None:
            if self._tested is None:
                self._tested = self.device.texture(self.scene.depth_resource_format, self.scene.size, DEPTH_STENCIL)
            before.append(self.device.copy(self._tested, self.scene.depth))
        vertices, instances = item.count
        return scene.draw({
            "vertex_shader": bytes(program.shader("vertex").payload),
            "pixel_shader": bytes(program.shader("pixel").payload), "vertex_buffers": [], "elements": [],
            "indices": self.device.buffer(np.arange(vertices, dtype=np.uint32).tobytes()),
            "index_format": DXGI_FORMAT_R32_UINT,
            "draw": (vertices, instances, 0, 0, 0), "size": (width, height),
            "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0), "scissor": (0, 0, width, height),
            "targets": bound, "depth": None if item.depth is None else (self.scene.depth_format, self._tested),
            "tables": tables, "heap": [], **program_states(program, len(bound), item.stencil_reference)})

    def phase_steps(self, number: int) -> tuple[list[int], list[int]]:
        if number in self._phase_steps:
            return self._phase_steps[number]
        phase = self.catalog.phases[number]
        at = (phase.after, number)
        before: list[int] = []
        targets = []
        for output in phase.targets:
            if output.initial.kind == "copy":
                key = f"{number}.{output.key}"
                if key not in self._own:
                    self._own[key] = self.device.texture(output.format, self.scene.size, RENDER_TARGET)
                own = self._own[key]
                before.append(self.device.copy(own, self.contents(output.initial, at)))
                targets.append((output.format, own))
            else:
                targets.append((output.format, self.contents(output.initial, at)))
        if number not in self._phase_reads:
            self._phase_reads[number] = self._reads(number, phase, at, targets)
        texture, constant_buffer, copies = self._phase_reads[number]
        unblended, blended = self.scene.draw_phase(phase.key, targets, phase.depth, texture, constant_buffer,
                                                   (*at, True) in self._added)
        self._phase_steps[number] = before + copies + unblended, blended
        return self._phase_steps[number]

    def _reads(self, number: int, phase: MeshPhase, at: tuple[int, int], targets: list[tuple]) -> tuple:
        """What phase ``number``'s parts read through, made once with its textures, so the jobs made of them are kept;
        and the copies those reads need run first, gathered as the reads are taken and run by every making of the
        phase's steps."""
        reads = {read.name: read for read in phase.reads}
        onto = {texture for _form, texture in targets if texture is not None}
        copies: list[int] = []

        def texture(name: str, resource, depth: int) -> tuple | None:
            read = reads.get(name)
            if read is not None and read.source.kind == "derived":
                raise MaterialPreviewError(DERIVED_READ, {"name": name})
            needed: list[int] = []
            entry = None if read is None else self.entry(read, resource, None, at, depth, needed)
            if entry is not None and len(entry) == 3 and entry[2] in onto:
                # Parts read a texture they draw onto as the phase found it.
                if (number, entry[2]) not in self._before:
                    self._before[number, entry[2]] = self.device.texture(*self.shape(read.source), 0)
                needed.append(self.device.copy(self._before[number, entry[2]], entry[2]))
                entry = (*entry[:2], self._before[number, entry[2]])
            copies.extend(step for step in dict.fromkeys(needed) if step not in copies)
            return entry

        def constant_buffer(name: str, layout) -> bytes | None:
            if name not in _ENGINE_BUFFERS and name not in phase.constants:
                return None
            return _constant_buffer(name, layout, self.scene.camera, self.light, phase.constants.get(name, {}),
                                    self.scene.size)
        return texture, constant_buffer, copies

    def add_start(self, steps: Callable[[], list[int]]) -> None:
        self._starting.append(steps)

    def add(self, at: tuple[int, int], steps: Callable[[], list[int]], inside: bool = False) -> None:
        """With ``inside``, ``steps()`` runs within phase ``at`` after its non-blending parts."""
        held = {(index, -1) for index in range(len(self.catalog.passes))} | {
            (phase.after, number) for number, phase in enumerate(self.catalog.phases)}
        if tuple(at) not in held or inside and at[1] < 0:
            raise ValueError(f"the light passes' frame holds no position {tuple(at)}{' inside a phase' if inside else ''}")
        self._added.setdefault((*at, inside), []).append(steps)
        if inside:
            self._phase_steps.pop(at[1], None)

    def _captured(self, name: str) -> dict:
        return captured_constants(self.catalog).get(name, {})

    def gives(self, name: str) -> bool:
        return name in _ENGINE_BUFFERS or bool(self._captured(name))

    def engine_block(self, name: str, layout) -> bytes:
        captured = self._captured(name)
        return _constant_buffer(name, layout, self.scene.camera, self.light, captured, self.scene.size)

    def _constants(self, key: tuple, item: LightPass, name: str, layout) -> bytes:
        size = self.extent(item)[:2]
        follows = _follows(name, self.scene.camera, self.light, size)
        made = self._made_constants.get(key)
        if made is None or made[0] != follows:
            made = self._made_constants[key] = follows, _constant_buffer(
                name, layout, self.scene.camera, self.light, item.constants.get(name, {}), size)
        return made[1]

    def _added_steps(self, key: tuple[int, int, bool]) -> list[int]:
        return [*steps() for steps in self._added.get(key, ())]

    def _frame(self) -> None:
        for index in range(len(self.catalog.passes)):
            self.device.pending += self.pass_steps(index) + self._added_steps((index, -1, False))
            for number, phase in enumerate(self.catalog.phases):
                if phase.after == index:
                    unblended, blended = self.phase_steps(number)
                    self.device.pending += [*unblended, *self._added_steps((index, number, True)), *blended,
                                            *self._added_steps((index, number, False))]

    def _image(self) -> int:
        return self.outputs[f"{len(self.catalog.passes) - 1}.{self.catalog.passes[-1].outputs[0].key}"]

    def _shown_image(self) -> tuple[int, int]:
        key = tuple(self.scene.coverage)
        if key not in self._shown:
            self._shown[key] = shown_step(self.scene, self._image(), self.catalog.passes[-1].outputs[0].format)
        return self._shown[key]

    @property
    def shown(self) -> int:
        """The texture frames show, as the last frame drew it."""
        return self._shown_image()[1]

    @property
    def carries(self) -> bool:
        """Whether frames carry passes' outputs to the next: frames of the same inputs may differ."""
        return bool(self._carried)

    def drawn(self, light: Light, after: Callable[[int], list[int]]) -> None:
        self.light = light
        scene, pending = self.scene, self.device.pending
        scene.begin()
        pending += scene.gbuffer()
        pending += [*steps() for steps in self._starting]
        self._frame()
        step, texture = self._shown_image()
        pending += [step, *after(texture)]
        pending += [self.device.copy(self.contents(Source("history", value), self.end), self.outputs[value])
                    for value in self._carried]
        scene.finish()


@dataclass(frozen=True, slots=True)
class _PassFile:
    document: object
    lookups: Mapping[str, OciocTexture]


def parsed_pass_file(data: bytes, game: str, label: str) -> _PassFile:
    profile = mmtr_profile_for_game(game)
    lookups = {}
    if data[:len(OCIOC_MAGIC)] == OCIOC_MAGIC:
        configuration = parse_ocioc(data)
        data, lookups = configuration.sdf, {texture.name: texture for texture in configuration.textures}
    return _PassFile(parse_sdf_to_draw(data, profile.sdf_profile, label), lookups)


def _failed(item: LightPass, exc: RuntimeError) -> MaterialPreviewError:
    return MaterialPreviewError(QT_TRANSLATE_NOOP(
        "MaterialPreview", "Direct3D could not run the light pass {name}: {reason}"),
        {"name": item.name, "reason": str(exc)})


__all__ = [
    "BufferShape", "CATALOG_DIRECTORY", "DERIVED_READ", "IMAGE_SIZE_FIELDS", "Indirect", "Light", "LightPass",
    "LightPassCatalog", "LightPassCatalogError", "LightPasses", "MeshPhase", "PassOutput", "PassRead",
    "ROLES", "SOURCES", "Source", "captured_constants", "catalog_file_name", "catalog_from_dict", "catalog_to_dict",
    "derived_read",
    "image_size", "light_pass_catalog_for", "light_pass_files", "phase_label",
]
