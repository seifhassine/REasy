"""See docs/internals/wrinkle.md."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_reader

from .material_preview import MaterialPreviewError
from .pretransform import (
    THREADS as _SKINNING_THREADS, _POSITION as _POSITION_STREAM, _UV0, _UV1, _UV2, ChainLevel, Destination,
    SystemProgram, _program_from_dict, _program_to_dict, _skinning, _words, system_variant,
)

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/wrinkle"
SETUP_ROLES = ("reform", "fill", "number", "sort", "rank", "duplicates", "rest")
FRAME_ROLES = ("area", "clear", "gather", "spread", "draw")
VERTICES, INDICES, REFORMED, SORTED, ORDER, KEYS, RANKS, DUPLICATES, REST = (
    "vertices", "indices", "reformed", "sorted", "order", "keys", "ranks", "duplicates", "rest")
SKINNED, AREAS, STRETCHED, SQUEEZED = "skinned", "areas", "stretched", "squeezed"
# Bytes per position, and per word.
_POSITION, _WORD = 12, 4
# Thread group size, and indices per thread when widening.
THREADS, _CORNERS, _REFORMED = 128, 3, 6
# The executable's placeholder for unread fields, and the weights it tells the area pass.
_UNUSED, _WEIGHTS_TOLD = 0x12345678, 8
# The only averaging mode a capture shows (``CalcMode``).
AVERAGE = 1


class WrinkleCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WrinkleComponent:
    """Each field holds the component's serialized field name."""
    type: str
    render_target: str
    calc_mode: str
    uv_gap: str
    spread: str
    proxy: str
    mesh: str


@dataclass(frozen=True, slots=True)
class WrinkleCatalog:
    game: str
    resource_version: int
    target: int
    # Keyed by ``SETUP_ROLES`` and ``FRAME_ROLES``.
    programs: Mapping[str, SystemProgram]
    # Keyed by ``system_variant``.
    skinning: Mapping[str, SystemProgram]
    # Duplicates per vertex the executable allows.
    duplicate_slots: int
    component: WrinkleComponent
    source: Mapping = field(default_factory=dict)

    def files(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(program.file for program in (*self.programs.values(), *self.skinning.values())))


_COMPONENT_FIELDS = ("type", "render_target", "calc_mode", "uv_gap", "spread", "proxy", "mesh")


def catalog_to_dict(catalog: WrinkleCatalog) -> dict:
    return {"format": CATALOG_FORMAT, "game": catalog.game, "resource_version": catalog.resource_version,
            "target": catalog.target,
            "programs": {role: _program_to_dict(catalog.programs[role]) for role in (*SETUP_ROLES, *FRAME_ROLES)},
            "skinning": {key: _program_to_dict(program) for key, program in sorted(catalog.skinning.items())},
            "duplicate_slots": catalog.duplicate_slots,
            "component": {name: getattr(catalog.component, name) for name in _COMPONENT_FIELDS},
            "source": dict(catalog.source)}


def catalog_from_dict(data: Mapping) -> WrinkleCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise WrinkleCatalogError(f"wrinkle catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    programs = {str(role): _program_from_dict(program) for role, program in data["programs"].items()}
    if set(programs) != {*SETUP_ROLES, *FRAME_ROLES}:
        raise WrinkleCatalogError(f"the wrinkle catalog names the programs {sorted(programs)}")
    component = data["component"]
    return WrinkleCatalog(
        str(data["game"]), int(data["resource_version"]), int(data["target"]), programs,
        {str(key): _program_from_dict(program) for key, program in data["skinning"].items()},
        int(data["duplicate_slots"]), WrinkleComponent(*(str(component[name]) for name in _COMPONENT_FIELDS)),
        dict(data.get("source", {})))


wrinkle_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, WrinkleCatalogError, "wrinkle")


@dataclass(frozen=True, slots=True)
class WrinkleProxy:
    level: ChainLevel
    vertex_count: int
    indices: bytes
    wide: bool
    index_count: int
    joints: np.ndarray
    inverse_binds: np.ndarray

    @property
    def triangles(self) -> int:
        return -(-self.index_count // _CORNERS)

    @property
    def position_count(self) -> int:
        """The setup sorts every position in the vertex buffer, not only ``vertex_count``."""
        return len(self.level.source) // self.level.streams[_POSITION_STREAM][1]

    @property
    def padded(self) -> int:
        return 1 << (self.position_count - 1).bit_length()

    @property
    def uv_count(self) -> int:
        return sum(kind in self.level.streams for kind in (_UV0, _UV1, _UV2))

    @property
    def variant(self) -> str:
        return system_variant(self.level.streams, False)

    def uvs(self) -> tuple[bytes, int]:
        offset, stride = self.level.streams[_UV0]
        return self.level.source[offset:], stride


def mesh_joints(mesh) -> tuple[np.ndarray, np.ndarray]:
    remap = np.asarray(mesh.bone_remap_indices, np.int64)
    binds = np.asarray(mesh.inverse_bind_matrices, np.float64).reshape(-1, 4, 4)
    return remap, binds[remap]


def wrinkle_proxy(mesh, chains) -> WrinkleProxy:
    from .pretransform import chain_level

    level = chain_level(mesh, 0, chains, deformed_only=False)
    submeshes = [] if level is None else [item for group in mesh.meshes[0].lods[0].mesh_groups
                                          for item in group.submeshes]
    if len(submeshes) != 1 or int(submeshes[0].faces_index_offset) or int(submeshes[0].verts_index_offset):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture shows the wrinkle passes on a proxy mesh other than one submesh."))
    submesh, = submeshes
    payload = mesh.mesh_buffer.buffer_payloads[submesh.buffer_index]
    joints, binds = mesh_joints(mesh)
    return WrinkleProxy(level, level.submeshes[0].count, bytes(payload.face_bytes), payload.integer_faces is not None,
                        int(submesh.indices_count), joints, binds)


@dataclass(frozen=True, slots=True, eq=False)
class WrinkleObject:
    """``bones`` maps proxy bones to drawn-mesh bones (-1 for none); ``corrections`` turn those into the proxy's."""
    proxy: WrinkleProxy
    render_target: str
    spread: bool
    bones: np.ndarray
    corrections: np.ndarray


def _column_binds(binds: np.ndarray) -> np.ndarray:
    return np.transpose(np.asarray(binds, np.float64), (0, 2, 1))


def wrinkle_object(proxy: WrinkleProxy, joints: np.ndarray, inverse_binds: np.ndarray, render_target: str,
                   spread: bool) -> WrinkleObject:
    """Proxy bones follow joints by number, whatever their names."""
    numbers = {int(joint): number for number, joint in enumerate(joints)}
    bones = np.array([numbers.get(int(joint), -1) for joint in proxy.joints], np.int64)
    held = _column_binds(inverse_binds)
    own = _column_binds(proxy.inverse_binds)
    corrections = np.tile(np.eye(4), (len(bones), 1, 1))
    found = bones >= 0
    corrections[found] = np.linalg.inv(held[bones[found]]) @ own[found]
    return WrinkleObject(proxy, render_target, spread, bones, corrections)


def proxy_bones(wrinkle: WrinkleObject, world: np.ndarray, bones: np.ndarray | None) -> np.ndarray:
    world = np.asarray(world, np.float64).reshape(3, 4)
    held = np.tile(world, (len(wrinkle.bones), 1, 1))
    count = int(wrinkle.bones.max(initial=-1)) + 1
    given = np.tile(world, (count, 1, 1)) if bones is None else np.asarray(bones, np.float64).reshape(-1, 3, 4)
    found = (wrinkle.bones >= 0) & (wrinkle.bones < len(given))
    held[found] = given[wrinkle.bones[found]] @ wrinkle.corrections[found]
    return held


def object_scale(world: np.ndarray) -> np.ndarray:
    axes = np.asarray(world, np.float32).reshape(3, 4)[:, :3]
    squares = axes * axes
    return np.sqrt(squares[0] + squares[1] + squares[2]).astype("<f4")


def component_settings(fields: Mapping[str, object], catalog: WrinkleCatalog) -> tuple[str, bool]:
    names = catalog.component
    if int(fields.get(names.calc_mode, AVERAGE)) != AVERAGE or bool(fields.get(names.uv_gap, False)):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture shows the wrinkle passes of a component taking the maximum or closing UV "
                               "gaps."))
    target = str(fields.get(names.render_target) or "")
    if not target:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The wrinkle component names no render texture."))
    return target, bool(fields.get(names.spread, False))


_RTEX_MAGIC, _RTEX_VERSION = b"RTEX", 4


def render_target_form(data: bytes) -> tuple[int, tuple[int, int]]:
    """The format is a DXGI_FORMAT."""
    if len(data) < 24 or data[:4] != _RTEX_MAGIC or int.from_bytes(data[4:8], "little") != _RTEX_VERSION:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The render texture file is not one REasy reads."))
    form, width, height = np.frombuffer(data, "<u4", 3, 12).tolist()
    return form, (width, height)


@dataclass(frozen=True, slots=True)
class WrinkleStep:
    """``buffers`` maps resource names to buffer kinds; a ``copy`` (to, from) runs no program."""
    program: SystemProgram | None
    constants: Mapping[str, np.ndarray]
    buffers: Mapping[str, str]
    groups: int = 0
    copy: tuple[str, str] | None = None
    draw: bool = False


def _groups(threads: int) -> int:
    return -(-threads // THREADS)


def sort_passes(count: int) -> list[dict[str, int]]:
    """``cbSortVertex`` fields per bitonic sort pass; ``count`` must be a power of two."""
    passes, block = [], 2
    while block <= count:
        step = block // 2
        while step:
            passes.append({"cSVBlock": block, "cSVStep": step, "cSVMaskLow": step - 1,
                           "cSVMaskHigh": 0xFFFFFFFF ^ (step - 1)})
            step //= 2
        block *= 2
    return passes


def buffer_sizes(proxy: WrinkleProxy, catalog: WrinkleCatalog) -> dict[str, int]:
    count, padded = proxy.position_count, proxy.padded
    indices = len(proxy.indices) // (4 if proxy.wide else 2)
    return {REFORMED: indices * _WORD, SORTED: padded * _POSITION, ORDER: padded * _WORD, KEYS: padded * _WORD,
            RANKS: padded * _WORD, DUPLICATES: count * catalog.duplicate_slots * _WORD, REST: proxy.triangles * _WORD,
            SKINNED: proxy.vertex_count * _POSITION, AREAS: proxy.triangles * _WORD, STRETCHED: count * _WORD,
            SQUEEZED: count * _WORD}


def _area_fields(proxy: WrinkleProxy) -> dict:
    return {"cSrcVBStride": proxy.level.streams[_POSITION_STREAM][1], "cSrcVBPositionOffset": 0,
            "cSrcVBPositionType": _UNUSED, "cPrimitiveIndex": 0, "cVertexCount": proxy.index_count,
            "cUVCount": proxy.uv_count}


def setup_steps(proxy: WrinkleProxy, catalog: WrinkleCatalog) -> list[WrinkleStep]:
    programs, count, padded = catalog.programs, proxy.position_count, proxy.padded
    stride = 4 if proxy.wide else 2
    sorting = [_words(item) for item in sort_passes(padded)]
    return [
        WrinkleStep(programs["reform"],
                    _words({"cSrcBaseAddr": 0, "cDestBaseAddr": 0, "cDestEndAddr": proxy.index_count * _WORD,
                            "cSrcStride": stride}),
                    {"bSrcIB": INDICES, "bDestIB": REFORMED}, _groups(-(-proxy.index_count // _REFORMED))),
        # ``cSrcVBBaseAddr`` stays unwritten, as the executable leaves it.
        WrinkleStep(programs["fill"], _words({"cNumElements": count, "cSrcVBBaseAddr": 0, "cDestPosVBBaseAddr": 0,
                                              "cNumSurplusElements": padded - count}),
                    {"bSrcPosVB": VERTICES, "bDestPosVB": SORTED}, _groups(padded)),
        WrinkleStep(programs["number"], _words({"cRVINumElements": count}),
                    {"bDestSortedIndex": ORDER, "bDestRefSortedIndex": RANKS}, _groups(padded)),
        *(WrinkleStep(programs["sort"], constants, {"bDestSortedPosVB": SORTED, "bDestSortedIndex": ORDER},
                      _groups(padded // 2)) for constants in sorting),
        WrinkleStep(None, {}, {}, copy=(KEYS, ORDER)),
        *(WrinkleStep(programs["rank"], constants, {"bDestSortedIndex": KEYS, "bDestRefSortedIndex": RANKS},
                      _groups(padded // 2)) for constants in sorting),
        WrinkleStep(programs["duplicates"],
                    _words({"cFDVVertexCount": count, "cFDVDupListMaxCount": catalog.duplicate_slots}),
                    {"bSrcPosVB": VERTICES, "bSrcSortedIndex": ORDER, "bSrcRefSortedIndex": RANKS,
                     "bDestDupVertexList": DUPLICATES}, _groups(count)),
        WrinkleStep(programs["rest"], _words(_area_fields(proxy)),
                    {"InputByteBuffer": VERTICES, "bSrcIB": REFORMED, "bDestAreaRest": REST},
                    _groups(proxy.triangles)),
    ]


def frame_steps(proxy: WrinkleProxy, catalog: WrinkleCatalog, scale: Sequence[float],
                spread: bool) -> list[WrinkleStep]:
    """The skinning's engine buffers are the caller's."""
    programs, level, count = catalog.programs, proxy.level, proxy.position_count
    skinning = catalog.skinning.get(proxy.variant)
    if skinning is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture of this game build shows the program skinning a wrinkle proxy whose "
                               "vertices hold {weights}."), {"weights": proxy.variant})
    unused = dict.fromkeys(("cSrcVBWeightOffset", "cSrcVBWeightOffset2", "cSrcVBWeightType", "cSrcVBIndicesOffset",
                            "cSrcVBIndicesOffset2", "cSrcVBIndicesType"), _UNUSED)
    sums = {"bDestVertArea": STRETCHED, "bDestVertAreaNeg": SQUEEZED}
    steps = [
        WrinkleStep(skinning, _words(_skinning(level, Destination(proxy.vertex_count), 0, proxy.vertex_count,
                                               False)),
                    {"InputByteBuffer": VERTICES, "bDestPosVB": SKINNED},
                    -(-proxy.vertex_count // _SKINNING_THREADS)),
        WrinkleStep(programs["area"], {**_words({**_area_fields(proxy), **unused, "cSrcWeightNum": _WEIGHTS_TOLD}),
                                       "cGameObjectScale": np.asarray(scale, "<f4")},
                    {"InputByteBuffer": SKINNED, "bSrcIB": REFORMED, "bSrcAreaRest": REST, "bDestAreaSkin": AREAS},
                    _groups(proxy.triangles)),
        WrinkleStep(programs["clear"], {}, {"bDestVertArea": STRETCHED}, _groups(count)),
        WrinkleStep(programs["clear"], {}, {"bDestVertArea": SQUEEZED}, _groups(count)),
        WrinkleStep(programs["gather"], _words({"cVasPrimStart": 0, "cVasPrimCount": proxy.triangles}),
                    {"bSrcIB": REFORMED, "bSrcAreaSkin": AREAS, **sums}, _groups(proxy.triangles)),
    ]
    if spread:
        steps.append(WrinkleStep(programs["spread"], _words({"cPdvVertexStart": 0, "cPdvVertexCount": count}),
                                 {"bSrcDupVertexList": DUPLICATES, **sums}, _groups(count)))
    # ``cPrimitiveTotalCount`` is the position count, as the executable sets it.
    steps.append(WrinkleStep(programs["draw"], _words({"cPrimitiveStart": 0, "cPrimitiveTotalCount": count}),
                             {"bVertexAreaDiff": STRETCHED, "bVertexAreaDiffNeg": SQUEEZED}, draw=True))
    return steps


__all__ = [
    "AREAS", "AVERAGE", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "DUPLICATES", "FRAME_ROLES", "INDICES", "KEYS", "ORDER",
    "RANKS", "REFORMED", "REST", "SETUP_ROLES", "SKINNED", "SORTED", "SQUEEZED", "STRETCHED", "THREADS", "VERTICES",
    "WrinkleCatalog", "WrinkleCatalogError", "WrinkleComponent", "WrinkleObject", "WrinkleProxy", "WrinkleStep",
    "buffer_sizes", "catalog_from_dict", "catalog_to_dict", "component_settings", "frame_steps", "mesh_joints",
    "object_scale", "proxy_bones", "render_target_form", "setup_steps", "sort_passes", "wrinkle_catalog_for",
    "wrinkle_object", "wrinkle_proxy",
]
