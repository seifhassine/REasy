"""See docs/internals/meshlets.md."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_file_name, catalog_reader
from file_handlers.sdf.models import program_names

from .material_preview import MaterialPreviewError
from .mesh_draw import SubmeshRange
from .meshlet_deferred import DeferredCatalogError, DeferredTexturing, deferred_from_dict
from .pass_choice import gbuffer_program_name
from .pretransform import SystemProgram

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/meshlets"
# REasy's own route name; the game names each meshlet step's programs instead.
ROUTE = "Meshlet"
INSTANCE_FIELDS = ("geometry", "material")
_CATALOG_WORDS = 6
_RECORD = 80
# Handle scale: a material's constant buffer index and its record's byte offset in ``BindlessBuffer``.
HANDLE_STEP = 256
DECAL_RECEIVE_BIT = 1 << 30
# Shader IDs ``ShaderReplaceTable`` maps (1024 rows of four); 0 names none.
_IDS = 4096
# The material-ID depth holds an ID over this.
_ID_SCALE = 65535
NO_TRANSFORM = 65535
EXTENDED = "extendedBindlessIndex"
_EXTENDED_HEAD = 16


class MeshletCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MeshletTransform:
    program: str
    reads: frozenset[str]


@dataclass(frozen=True, slots=True)
class MeshletSkinning:
    raster: str
    shade: str
    transform: str
    word: str
    flag: int


@dataclass(frozen=True, slots=True)
class MeshletCatalog:
    game: str
    resource_version: int
    raster: str
    shade: str
    forward: str | None
    material_ids: SystemProgram
    target: int
    vertices: int
    tile_vertices: int
    visibility_format: int
    material_id_formats: tuple[int, int, int]
    table_tag: int
    instance: Mapping[str, int]
    source: Mapping = field(default_factory=dict)
    deferred: DeferredTexturing | None = None
    transform: MeshletTransform | None = None
    skinning: MeshletSkinning | None = None
    # Whether the game draws posed skinned meshlets in their bind pose.
    bind_pose_skinned: bool = False

    def files(self) -> tuple[str, ...]:
        return (self.material_ids.file,)


def catalog_from_dict(data: Mapping) -> MeshletCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise MeshletCatalogError(f"meshlet catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    instance = {str(name): int(value) for name, value in data["instance"].items()}
    if len(instance) != _CATALOG_WORDS:
        raise MeshletCatalogError(f"the instance words {list(instance)} are not {_CATALOG_WORDS}")
    formats = tuple(int(value) for value in data["material_id_formats"])
    if len(formats) != 3 or int(data["vertices"]) % 3:
        raise MeshletCatalogError("a meshlet catalog's formats or vertex count are malformed")
    programs = data["material_ids"]
    deferred, transform, skinning = data.get("deferred"), data.get("transform"), data.get("skinning")
    if transform is not None and not transform["reads"]:
        raise MeshletCatalogError("a meshlet catalog's transform names no shading read")
    if skinning is not None and (deferred is None or skinning["word"] not in instance or "flag" not in skinning):
        raise MeshletCatalogError("a meshlet catalog's skinning needs its deferred texturing, an instance word and "
                                  "its flag")
    return MeshletCatalog(
        str(data["game"]), int(data["resource_version"]), str(data["raster"]), str(data["shade"]),
        None if data["forward"] is None else str(data["forward"]), SystemProgram(str(programs["file"]), str(programs["program"])), int(programs["target"]),
        int(data["vertices"]), int(data["tile_vertices"]), int(data["visibility_format"]), formats,
        int(data["table_tag"]), MappingProxyType(instance), dict(data.get("source", {})),
        None if deferred is None else deferred_from_dict(deferred),
        None if transform is None else MeshletTransform(str(transform["program"]),
                                                        frozenset(map(str, transform["reads"]))),
        None if skinning is None else MeshletSkinning(str(skinning["raster"]), str(skinning["shade"]),
                                                      str(skinning["transform"]), str(skinning["word"]),
                                                      int(skinning["flag"])),
        bool(data.get("bind_pose_skinned", False)))


meshlet_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, MeshletCatalogError, "meshlet",
                                     (AttributeError, DeferredCatalogError))


@dataclass(frozen=True, slots=True)
class Cluster:
    offset: int
    triangles: int
    material: int


@dataclass(frozen=True, slots=True, eq=False)
class MeshletBuffer:
    blob: bytes
    clusters: Mapping[SubmeshRange, tuple[Cluster, ...]]

    @classmethod
    def from_mesh(cls, mesh) -> MeshletBuffer | None:
        layout = None if mesh.mesh_buffer is None else mesh.mesh_buffer.meshlet_layout
        if layout is None or not layout.gpu_data:
            return None
        clusters = {}
        for data in mesh.meshes:
            for lod in data.lods:
                for group in lod.mesh_groups:
                    for submesh in group.submeshes:
                        offset = int(submesh.file_offset) - int(layout.gpu_meshlet_offset)
                        clusters[SubmeshRange.from_submesh(submesh)] = (Cluster(
                            offset // 4, int(submesh.indices_count) // 3, int(submesh.material_index)),)
        return cls(layout.gpu_data, MappingProxyType(clusters))


def meshlet_programs(document, target: int, material_flags: int, catalog: MeshletCatalog,
                     skinned: bool = False) -> tuple:
    programs = document.sdf.targets[target].programs
    names = program_names(document.sdf.targets[target])

    def one(name: str):
        named = [index for index, item in enumerate(names) if item == name]
        if len(named) != 1:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The master has {count} programs named {name}, which the renderer draws meshlet "
                                   "meshes with."), {"count": len(named), "name": name})
        return programs[named[0]]
    skinning = catalog.skinning if skinned else None
    raster = gbuffer_program_name(material_flags, catalog.raster if skinning is None else skinning.raster)
    if raster in names:
        program = one(raster)
        states = program.blend_state
        blends = states is not None and states.render_targets and states.render_targets[0].blend_enable
        if skinning is not None:
            if blends:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "No capture of this game build shows how the renderer draws a skinned meshlet "
                                       "mesh whose material blends."))
            return "gbuffer", program, one(skinning.shade), one(skinning.transform)
        if blends:
            return "blending", program, None
        shade = one(catalog.shade)
        transform = catalog.transform
        if transform is None or not transform.reads & {item.name for item in shade.shader_resources}:
            return "gbuffer", program, shade
        if catalog.deferred is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "No capture of this game build shows how the renderer runs the geometry transform "
                                   "program whose output the master's shading reads."))
        return "gbuffer", program, shade, one(transform.program)
    if catalog.forward is None or skinning is not None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The master has no program {name} for the material, and no capture of this game build "
                               "shows how the renderer draws such a meshlet mesh."), {"name": raster})
    return "forward", one(gbuffer_program_name(material_flags, catalog.forward)), None


@dataclass(frozen=True, slots=True)
class MeshletPrograms:
    raster: str
    shade: str
    transform: str


def named_meshlet_programs(document, target: int, names: MeshletPrograms) -> tuple[str, object, object, object]:
    programs = document.sdf.targets[target].programs
    found = program_names(document.sdf.targets[target])

    def one(name: str):
        named = [index for index, item in enumerate(found) if item == name]
        if len(named) != 1:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The master has {count} programs named {name}, which the renderer draws meshlet "
                                   "meshes with."), {"count": len(named), "name": name})
        return programs[named[0]]
    return "gbuffer", one(names.raster), one(names.shade), one(names.transform)


@dataclass(frozen=True, slots=True, eq=False)
class MeshletGroup:
    owner: int
    material: int
    parts: tuple
    purpose: str
    raster: object
    shade: object | None
    clusters: tuple[Cluster, ...]
    id: int
    transform: object | None = None

    @property
    def part(self):
        return self.parts[0]


def check_clusters(catalog: MeshletCatalog, clusters: Sequence[Cluster]) -> None:
    if any(cluster.triangles * 3 > catalog.vertices for cluster in clusters):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "A cluster of the meshlet mesh has more triangles than the renderer draws of one."))


def check_group(count: int) -> None:
    if count + 1 >= _IDS:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The scene draws more materials of meshlet meshes than the renderer tells apart."))


def culled_meshlets(groups: Sequence[MeshletGroup], instances: Mapping[int, int]) -> tuple[bytes, bytes, bytes]:
    rows = [(instances[group.owner], cluster.offset) for group in groups for cluster in group.clusters]
    starts = np.cumsum([0, *(len(group.clusters) for group in groups[:-1])])
    return (np.array(rows, "<u4").tobytes(), np.arange(len(rows), dtype="<u4").tobytes(),
            np.asarray(starts, "<u4").tobytes())


def material_tables(catalog: MeshletCatalog, groups: Sequence[MeshletGroup],
                    start: int = 0) -> tuple[bytes, dict[int, int]]:
    ids: dict[int, dict[int, int]] = {}
    for group in groups:
        ids.setdefault(group.owner, {})[group.material] = group.id
    data, offsets = bytearray(start), {}
    for owner, materials in ids.items():
        count = max(materials) + 1
        words = [count, catalog.table_tag]
        for material in range(count):
            words += [0, materials.get(material, 0)]
        offsets[owner] = len(data)
        table = np.array(words, "<u4").tobytes()
        data += table + bytes(-len(table) % 16)
    return bytes(data), offsets


def extended_blocks(start: int, blocks: Sequence[bytes | None]) -> tuple[bytes, list[int]]:
    data, offsets = bytearray(), []
    for block in blocks:
        if block is None:
            offsets.append(-1)
            continue
        head = start + len(data)
        offsets.append(head)
        data += np.array([head + _EXTENDED_HEAD], "<u4").tobytes().ljust(_EXTENDED_HEAD, b"\0")
        data += bytes(block) + bytes(-len(block) % 16)
    return bytes(data), offsets


def receives_decals(mode: int, skinned: bool) -> bool:
    return mode == 1 or mode != 2 and not skinned


def instance_records(catalog: MeshletCatalog, worlds: np.ndarray, geometry: Sequence[int], tables: Sequence[int],
                     decals: Sequence[bool], extended: Sequence[int] | None = None,
                     skinning: Sequence[int] | None = None) -> bytes:
    count = len(worlds)
    records = np.zeros((count, _RECORD // 4), "<u4")
    records[:, :12] = np.asarray(worlds, "<f4").reshape(count, 12).view("<u4")
    records[:, 12], records[:, 13] = geometry, tables
    records[:, 14:] = list(catalog.instance.values())
    if "visibleFlag" in catalog.instance:
        column = 14 + list(catalog.instance).index("visibleFlag")
        records[:, column] = np.where(np.asarray(decals, bool), records[:, column] | DECAL_RECEIVE_BIT,
                                      records[:, column] & ~np.uint32(DECAL_RECEIVE_BIT))
    if extended is not None and any(offset >= 0 for offset in extended):
        if EXTENDED not in catalog.instance:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy's meshlet catalog of this game build names no instance word for the block "
                                   "the mesh's programs read."))
        column = 14 + list(catalog.instance).index(EXTENDED)
        given = np.asarray(extended, np.int64) >= 0
        records[given, column] = np.asarray(extended, np.int64)[given]
    if skinning is not None and any(index >= 0 for index in skinning):
        column = 14 + list(catalog.instance).index(catalog.skinning.word)
        given = np.asarray(skinning, np.int64) >= 0
        records[given, column] = np.asarray(skinning, np.int64)[given] | catalog.skinning.flag
    return records.tobytes()


# An ``InstancesSkinning`` record: ``maxAABB``, ``minAABB``, ``jointOffset``, ``prevJointOffset``.
_SKINNING_RECORD = 32


def skinning_records(boxes: np.ndarray, joints: Sequence[tuple[int, int]]) -> bytes:
    """``boxes`` are in world space; ``joints`` are (current, previous) bone row offsets."""
    records = bytearray(_SKINNING_RECORD * len(joints))
    for number, (box, (joint, previous)) in enumerate(zip(np.asarray(boxes, np.float64), joints)):
        records[_SKINNING_RECORD * number:_SKINNING_RECORD * (number + 1)] = (
            np.asarray([*box[1], *box[0]], "<f4").tobytes() + np.asarray([joint, previous], "<i4").tobytes())
    return bytes(records)


def replace_table(layout, ids: int, transforms: Mapping[int, int] | None = None) -> bytes:
    data = bytearray(layout.byte_size)
    named = np.arange(_IDS, dtype="<u4")
    named[ids:] = 0
    transformed = named
    if transforms is not None:
        transformed = np.full(_IDS, NO_TRANSFORM, "<u4")
        transformed[ids:] = 0
        for shader, transform in transforms.items():
            transformed[shader] = transform
    for variable in layout.variables:
        if variable.name in ("VisibilityShaderShaderID", "GeometryTransformShaderID"):
            if variable.byte_size != _IDS * 4:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The engine field {name} is declared {declaration}, which the preview does not "
                                       "lay out."), {"name": variable.name, "declaration": variable.hlsl_declaration})
            values = named if variable.name == "VisibilityShaderShaderID" else transformed
            data[variable.byte_offset:variable.byte_offset + _IDS * 4] = values.tobytes()
    return bytes(data)


def root_constant(layout, value: int) -> bytes:
    return np.array([value], "<u4").tobytes().ljust(layout.byte_size, b"\0")


def material_culling_size() -> int:
    return _ID_SCALE * 4


__all__ = ["MeshletSkinning", "skinning_records", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "Cluster", "DECAL_RECEIVE_BIT", "EXTENDED", "HANDLE_STEP",
           "MeshletTransform",
           "INSTANCE_FIELDS", "MeshletBuffer", "MeshletCatalog", "MeshletCatalogError", "MeshletGroup",
           "MeshletPrograms", "NO_TRANSFORM", "ROUTE", "catalog_file_name", "catalog_from_dict", "check_clusters",
           "check_group", "culled_meshlets", "extended_blocks", "instance_records", "material_culling_size",
           "material_tables", "meshlet_catalog_for", "meshlet_programs", "named_meshlet_programs",
           "receives_decals", "replace_table", "root_constant"]
