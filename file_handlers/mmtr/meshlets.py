"""See docs/internals/meshlets.md."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_file_name, catalog_reader
lazy from file_handlers.mesh.lod_settings import THRESHOLD_FAMILIES, THRESHOLD_GATES, half_bits
lazy from utils.resource_file_utils import resource_path_with_version

lazy from .material_preview import MaterialPreviewError
from .mesh_draw import SubmeshRange
from .meshlet_deferred import DeferredCatalogError, DeferredTexturing, deferred_from_dict
lazy from .pass_choice import gbuffer_program_name
lazy from .pretransform import SystemProgram

CATALOG_FORMAT = 3
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
# The visibility buffer holds a CulledMeshlet index times 128 plus the triangle's in 32 bits; a CulledMeshlet word
# the instance in its low 24.
_CULLED_ROWS = 1 << 25
_INSTANCE_ROWS = 1 << 24
# Instance culling's screen metric: this over (the box's larger NDC extent times the screen size, plus 1).
_SCREEN_METRIC = 1020.0
_FRACTION_STEPS = 31


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
class MeshletLodRule:
    """How a build picks each instance's meshlet LOD (docs/internals/meshlets.md, LODs)."""
    # The plain meshes' threshold writer (``lod_settings.meshlet_thresholds``) and the LodSettings file it reads.
    thresholds: str
    gate: str | None
    lod_settings: str
    lod_settings_version: int
    # MeshletCullingSettings' CullingLODBias.y and the SpeedTree bias.
    bias: float
    speedtree_bias: float
    # A box corner of clip w under 1 gives the screen metric 0.
    near: bool
    # The CulledMeshlet word packs (lod << 5 | fraction * 31), else lod.
    fraction: bool
    # The distance metric is scaled by k^2, k the base FOV's half-float x scale over the view's.
    base_fov: bool

    def file(self) -> str:
        return resource_path_with_version(self.lod_settings, "lod", self.lod_settings_version)


def _lod_rule(data: Mapping) -> MeshletLodRule:
    gate = data["gate"]
    if data["thresholds"] not in THRESHOLD_FAMILIES or gate is not None and gate not in THRESHOLD_GATES:
        raise MeshletCatalogError(f"a meshlet catalog's LOD thresholds {data['thresholds']!r} gated by {gate!r} are "
                                  f"no build's")
    flags = [data[name] for name in ("near", "fraction", "base_fov")]
    if not all(isinstance(flag, bool) for flag in flags) or not data["lod_settings"]["path"]:
        raise MeshletCatalogError("a meshlet catalog's LOD rule is malformed")
    return MeshletLodRule(str(data["thresholds"]), None if gate is None else str(gate),
                          str(data["lod_settings"]["path"]), int(data["lod_settings"]["version"]),
                          float(data["bias"]), float(data["speedtree_bias"]), *flags)


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
    lods: MeshletLodRule
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
        int(data["table_tag"]), frozendict(instance), _lod_rule(data["lods"]), dict(data.get("source", {})),
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


def meshlet_runs(submeshes: Sequence) -> list[int]:
    """The lengths of the runs of a mesh group's submeshes, in order, that a scene draws as one part: consecutive ones
    of one buffer, material, alpha test and transparency. An MPLY mesh holds a submesh per meshlet chunk; an empty one
    is a run alone (no batch draws it)."""
    runs, last = [], None
    for submesh in submeshes:
        key = None if not int(submesh.indices_count) else (
            int(submesh.buffer_index), int(submesh.material_index), bool(getattr(submesh, "alpha_test", False)),
            bool(getattr(submesh, "transparent", False)))
        if runs and key is not None and key == last:
            runs[-1] += 1
        else:
            runs.append(1)
        last = key
    return runs


def run_range(ranges: Sequence[SubmeshRange]) -> SubmeshRange:
    """The key of a meshlet run's clusters in ``MeshletBuffer.clusters``."""
    first = ranges[0]
    return first if len(ranges) == 1 else SubmeshRange(first.start, sum(item.count for item in ranges),
                                                        first.base_vertex)


@dataclass(frozen=True, slots=True, eq=False)
class MeshletBuffer:
    """Clusters by submesh, and by meshlet run (``meshlet_runs``)."""
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
                    held = [(SubmeshRange.from_submesh(submesh), Cluster(
                        (int(submesh.file_offset) - int(layout.gpu_meshlet_offset)) // 4,
                        int(submesh.indices_count) // 3, int(submesh.material_index))) for submesh in group.submeshes]
                    clusters.update((key, (cluster,)) for key, cluster in held)
                    at = 0
                    for count in meshlet_runs(group.submeshes):
                        if count > 1:
                            run = held[at:at + count]
                            found = tuple(cluster for _key, cluster in run)
                            if clusters.setdefault(run_range([key for key, _cluster in run]), found) != found:
                                raise ValueError("a meshlet run's range is another submesh's")
                        at += count
        return cls(layout.gpu_data, frozendict(clusters))


def meshlet_programs(document, target: int, material_flags: int, catalog: MeshletCatalog,
                     skinned: bool = False) -> tuple:
    programs = document.sdf.targets[target].programs
    names = document.sdf.targets[target].program_indices

    def one(name: str):
        named = names.get(name, ())
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
    found = document.sdf.targets[target].program_indices

    def one(name: str):
        named = found.get(name, ())
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
    # Each LOD's clusters in turn, LODs ascending; ``lods`` their (LOD, cluster count).
    clusters: tuple[Cluster, ...]
    lods: tuple[tuple[int, int], ...]
    # The material table's shader ID, ``ShaderReplaceTable``'s visibility ID for it (0: none) and the scene's
    # bindless material record.
    shader: int
    visibility: int
    record: int
    transform: object | None = None

    @property
    def part(self):
        return self.parts[0]


def check_clusters(catalog: MeshletCatalog, clusters: Sequence[Cluster]) -> None:
    if any(cluster.triangles * 3 > catalog.vertices for cluster in clusters):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "A cluster of the meshlet mesh has more triangles than the renderer draws of one."))


def check_shader_ids(count: int) -> None:
    """``count`` shader IDs are taken, from 1."""
    if count + 1 >= _IDS:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The scene draws more materials of meshlet meshes than the renderer tells apart."))


def check_transform_ids(count: int, catalog: MeshletCatalog) -> None:
    """``count`` transform IDs are taken."""
    if count >= (0 if catalog.deferred is None else catalog.deferred.transform_ids):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The scene draws more meshlet materials transforming their triangles than the "
                               "renderer dispatches transforms for."))


def check_culled(rows: int, instances: int) -> None:
    """``rows`` ``CulledMeshlet`` rows are held, of ``instances`` instance records."""
    if rows > _CULLED_ROWS or instances > _INSTANCE_ROWS:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The scene holds more meshlet clusters or instances than the renderer's visibility "
                               "buffer tells apart."))


@dataclass(frozen=True, slots=True, eq=False)
class MeshletSpans:
    """``CulledMeshlet``'s runs, each a group's clusters of one LOD: every group's LOD 0 in group order, then LOD 1's,
    and so on, so LOD 0's rows are a scene of LOD 0 only's. int32 by span."""
    group: np.ndarray
    level: np.ndarray
    first: np.ndarray
    count: np.ndarray


def culled_meshlets(groups: Sequence[MeshletGroup], instances: Mapping[int, int]) -> tuple[bytes, MeshletSpans]:
    """``CulledMeshlet`` (each row the instance's record and the cluster's offset) and its spans."""
    held = []
    for number, group in enumerate(groups):
        levels = [level for level, _count in group.lods]
        if sum(count for _level, count in group.lods) != len(group.clusters) or levels != sorted(set(levels)):
            raise ValueError("a meshlet group's LODs do not list its clusters")
        at = 0
        for level, count in group.lods:
            held.append((level, number, at, count))
            at += count
    level, group, at, count = np.array(sorted(held), np.int64).reshape(-1, 4).T.astype(np.int32)
    rows = np.zeros((int(count.sum()), 2), "<u4")
    if len(held):
        offsets = [np.fromiter((cluster.offset for cluster in item.clusters), np.uint32, len(item.clusters))
                   for item in groups]
        rows[:, 0] = np.repeat([instances[groups[number].owner] for number in group.tolist()], count)
        rows[:, 1] = np.concatenate([offsets[number][start:start + size] for number, start, size in
                                     zip(group.tolist(), at.tolist(), count.tolist())])
    return rows.tobytes(), MeshletSpans(group, level, (np.cumsum(count) - count).astype(np.int32), count)


class RasterBatches:
    """The visibility raster draws, one per batch of groups (docs/internals/meshlets.md, Raster batches). ``SortData``
    lists every ``CulledMeshlet`` row in turn, then holds each batch's room, its clusters shown first. A group's
    object shows one LOD, so a room holds each group's largest span."""

    def __init__(self, groups: Sequence[MeshletGroup], spans: MeshletSpans, batches: Sequence[Sequence[int]],
                 vertices: int):
        members = np.array([number for batch in batches for number in batch], np.int64)
        order = np.full(len(groups), -1, np.int64)
        order[members] = np.arange(len(members))
        batched = np.flatnonzero(order[spans.group] >= 0)
        # Batched spans by batch, then group in batch order, then LOD.
        batched = batched[np.lexsort((spans.level[batched], order[spans.group[batched]]))]
        # Each batched span's group, LOD, first ``CulledMeshlet`` row, cluster count and object.
        self.group, self.level = spans.group[batched], spans.level[batched]
        self.first, self.count = spans.first[batched], spans.count[batched]
        self.owner = np.array([group.owner for group in groups], np.int32).reshape(-1)[self.group]
        largest = np.zeros(len(groups), np.int64)
        np.maximum.at(largest, spans.group, spans.count)
        rooms = np.array([largest[list(batch)].sum() for batch in batches], np.int64)
        batch_of = np.repeat(np.arange(len(batches), dtype=np.int32), [len(batch) for batch in batches])
        self.batch = batch_of[order[self.group]]
        self.base = int(spans.count.sum())
        self.room = int(rooms.sum())
        self.starts = self.base + np.cumsum(rooms) - rooms
        self.vertices = vertices

    def laid(self, shown: np.ndarray) -> tuple[bytes, bytes]:
        """``SortData`` and each batch draw's arguments (``DrawInstanced``'s four words) for the batched spans
        ``shown`` (one truth each)."""
        kept = np.flatnonzero(shown)
        sizes = self.count[kept].astype(np.int64)
        total = int(sizes.sum())
        clusters = np.repeat(self.first[kept] - (np.cumsum(sizes) - sizes), sizes) + np.arange(total)
        batches = np.repeat(self.batch[kept], sizes)
        counts = np.bincount(batches, minlength=len(self.starts))
        data = np.zeros(self.base + self.room, "<u4")
        data[:self.base] = np.arange(self.base)
        data[self.starts[batches] + np.arange(total) - np.repeat(np.cumsum(counts) - counts, counts)] = clusters
        arguments = np.zeros((len(self.starts), 4), "<u4")
        arguments[:, 0], arguments[:, 1] = self.vertices, counts
        return data.tobytes(), arguments.tobytes()


def special_offsets(spans: MeshletSpans, groups: int, batches: RasterBatches) -> tuple[bytes, dict]:
    """``SpecialShaderOffset``: each group's LOD 0 start (0 for a group of none), each batch's room, then each coarser
    span's start; and the ``RootConstant`` of each span, by (group, LOD)."""
    starts = np.zeros(groups, np.int64)
    coarse = np.flatnonzero(spans.level != 0)
    finest = np.flatnonzero(spans.level == 0)
    starts[spans.group[finest]] = spans.first[finest]
    roots = {(int(group), 0): int(group) for group in spans.group[finest]}
    past = groups + len(batches.starts)
    roots.update(((int(spans.group[span]), int(spans.level[span])), past + number)
                 for number, span in enumerate(coarse))
    return np.concatenate([starts, batches.starts, spans.first[coarse]]).astype("<u4").tobytes(), roots


# A blob's header: box min, LOD count and floor (bytes 12, 13), box max, then the thresholds' 8 halves at 64.
_BLOB_HEAD = 80
_THRESHOLDS_AT = 64
_LODS = 8
# Box corner c takes axis j's max where bit j of c is set.
_CORNER_PICKS = tuple(tuple(corner >> axis & 1 for axis in range(3)) for corner in range(8))


@dataclass(frozen=True, slots=True, eq=False)
class MeshletLods:
    """A meshlet mesh's LOD inputs as instance culling reads them from its blob; its instances share them."""
    # 2 x 3 float32, blob bytes 0-11 and 16-27.
    box: np.ndarray
    # 8 float32: a SpeedTree mesh's distances, a plain one's screen metrics.
    thresholds: np.ndarray
    count: int
    floor: int
    # SpeedTree: the squared distance to the world box's centre, not the screen metric.
    distance: bool


def blob_halves(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, "<u2", _LODS, _THRESHOLDS_AT).copy()


def blob_lods(blob: bytes, distance: bool, halves: np.ndarray | None = None) -> MeshletLods:
    """``halves`` replace the blob's thresholds, as the build's writer sets them on load (``lod_settings``)."""
    if len(blob) < _BLOB_HEAD or not 1 <= blob[12] <= _LODS:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The meshlet mesh's header gives no LOD count the renderer picks LODs of."))
    halves = blob_halves(blob) if halves is None else np.asarray(halves, "<u2")
    box = np.stack([np.frombuffer(blob, "<f4", 3, 0), np.frombuffer(blob, "<f4", 3, 16)])
    return MeshletLods(box, halves.view("<f2").astype(np.float32), int(blob[12]), int(blob[13]), bool(distance))


class LodTable:
    """Objects' ``MeshletLods`` stacked, with their world boxes' corners, for ``meshlet_lods``."""

    def __init__(self, lods: Sequence[MeshletLods], worlds: np.ndarray):
        self.boxes = np.array([item.box for item in lods], np.float32).reshape(-1, 2, 3)
        self.thresholds = np.array([item.thresholds for item in lods], np.float32).reshape(-1, _LODS)
        self.counts = np.array([item.count for item in lods], np.int64)
        self.floors = np.array([item.floor for item in lods], np.int64)
        self.distance = np.array([item.distance for item in lods], bool)
        self.corners = np.zeros((len(lods), 8, 3), np.float32)
        self.centres = np.zeros((len(lods), 3), np.float32)
        self.place(np.arange(len(lods)), worlds)

    def place(self, rows, worlds: np.ndarray, boxes: np.ndarray | None = None) -> None:
        """Objects ``rows`` are at ``worlds`` (3 x 4 each); ``boxes`` replace their boxes (a skinned one's posed world
        box, at identity rows)."""
        rows = np.asarray(rows, np.intp)
        if boxes is not None:
            self.boxes[rows] = boxes
        worlds = np.asarray(worlds, np.float32).reshape(-1, 3, 4)
        picks = self.boxes[rows][:, np.array(_CORNER_PICKS), np.arange(3)]
        # The shader's order: the translation, then each axis' term.
        corners = np.repeat(worlds[:, None, :, 3], 8, axis=1)
        for axis in range(3):
            corners = corners + worlds[:, None, :, axis] * picks[:, :, axis:axis + 1]
        self.corners[rows] = corners
        # The world box's centre, the SpeedTree metric's.
        self.centres[rows] = (corners.max(1) + corners.min(1)) * np.float32(0.5)


def meshlet_lods(rule: MeshletLodRule, table: LodTable, view_projection: np.ndarray, eye: np.ndarray, scale: float,
                 size: tuple[int, int]) -> tuple[np.ndarray, np.ndarray]:
    """Each object's LOD (uint8) and its ``CulledMeshlet`` word's high byte (uint32), as instance culling picks them:
    ``view_projection`` for row vectors, ``eye`` the camera's position, ``scale`` its projection's x scale and ``size``
    MeshletLODScreenSize, the render size."""
    f32 = np.float32
    rows = np.arange(len(table.counts))
    metric = np.zeros(len(rows), f32)
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        screen = np.flatnonzero(~table.distance)
        if len(screen):
            matrix = np.asarray(view_projection, f32)
            clip = table.corners[screen] @ matrix[:3] + matrix[3]
            x, y = clip[..., 0] / clip[..., 3], clip[..., 1] / clip[..., 3]
            extent = np.maximum(f32(size[0]) * (x.max(1) - x.min(1)), f32(size[1]) * (y.max(1) - y.min(1)))
            values = f32(_SCREEN_METRIC) / (extent + f32(1))
            metric[screen] = np.where(clip[..., 3].min(1) < 1, f32(0), values) if rule.near else values
        far = np.flatnonzero(table.distance)
        if len(far):
            offset = np.asarray(eye, f32) - table.centres[far]
            distances = (offset * offset).sum(1, dtype=f32)
            if rule.base_fov:
                k = f32(1) / f32(scale) * np.array(half_bits(scale), "<u2").view("<f2").astype(f32)
                distances = k * k * distances
            metric[far] = distances
        thresholds = np.where(table.distance[:, None], table.thresholds * table.thresholds, table.thresholds)
        # The shader's binary search; LOD 0 is never tested.
        low, high = np.zeros(len(rows), np.int64), table.counts - 1
        while (active := low != high).any():
            middle = high - (high - low) // 2
            below = metric < thresholds[rows, middle]
            high = np.where(active & below, middle - 1, high)
            low = np.where(active & ~below, middle, low)
        level = low.astype(f32)
        if rule.fraction:
            start, end = thresholds[rows, low], thresholds[rows, np.minimum(low + 1, _LODS - 1)]
            fraction = np.clip(f32(1) / np.maximum(end - start, f32(2 ** -23)) * (metric - start), 0, 1)
            level = level + np.where((low < table.counts - 1) & ~np.isnan(fraction), fraction, f32(0))
        level = level + np.where(table.distance, f32(rule.speedtree_bias), f32(rule.bias))
        level = np.minimum(np.maximum(level, table.floors.astype(f32)), (table.counts - 1).astype(f32))
    lods = level.astype(np.uint8)
    if not rule.fraction:
        return lods, lods.astype(np.uint32)
    steps = ((level - np.floor(level)) * f32(_FRACTION_STEPS)).astype(np.uint32)
    return lods, lods.astype(np.uint32) << 5 | steps


def record_handles(sizes: Sequence[int]) -> tuple[list[int], int]:
    """Each record's handle, in whole steps from 0, and the bytes they take."""
    handles, at = [], 0
    for size in sizes:
        handles.append(at)
        at += -(-size // HANDLE_STEP) * HANDLE_STEP
    return handles, at


def material_views(handles: Sequence[int], views: Sequence[int], room: int) -> bytes:
    """``BindlessConstantBufferViewTbl``: by handle step, the heap index of the view of the record there."""
    table = np.zeros(max(1, room // HANDLE_STEP), "<u4")
    table[np.asarray(handles, np.int64) // HANDLE_STEP] = views
    return table.tobytes()


def material_tables(catalog: MeshletCatalog, groups: Sequence[MeshletGroup], handles: Sequence[int],
                    start: int = 0) -> tuple[bytes, dict[int, int]]:
    """``handles`` by group record; the tables start at ``start``."""
    ids: dict[int, dict[int, tuple[int, int]]] = {}
    for group in groups:
        ids.setdefault(group.owner, {})[group.material] = (handles[group.record], group.shader)
    data, offsets = bytearray(start), {}
    for owner, materials in ids.items():
        count = max(materials) + 1
        words = [count, catalog.table_tag]
        for material in range(count):
            words += materials.get(material, (0, 0))
        offsets[owner] = len(data)
        table = np.array(words, "<u4").tobytes()
        data += table + bytes(-len(table) % 16)
    return data.take_bytes(), offsets


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
    return data.take_bytes(), offsets


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
    return records.take_bytes()


def replace_table(layout, ids: int, visibility: Mapping[int, int],
                  transforms: Mapping[int, int] | None = None) -> bytes:
    """``ids`` shader IDs are taken (with 0); ``visibility`` and ``transforms`` by shader ID."""
    data = bytearray(layout.byte_size)
    named = np.zeros(_IDS, "<u4")
    for shader, shown in visibility.items():
        named[shader] = shown
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
    return data.take_bytes()


def root_constant(layout, value: int) -> bytes:
    return np.array([value], "<u4").tobytes().ljust(layout.byte_size, b"\0")


def material_culling_size() -> int:
    return _ID_SCALE * 4


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "Cluster", "DECAL_RECEIVE_BIT", "EXTENDED", "HANDLE_STEP",
           "INSTANCE_FIELDS", "LodTable", "MeshletBuffer", "MeshletCatalog", "MeshletCatalogError", "MeshletGroup",
           "MeshletLodRule", "MeshletLods", "MeshletPrograms", "MeshletSkinning", "MeshletSpans", "MeshletTransform",
           "NO_TRANSFORM", "ROUTE", "RasterBatches", "blob_halves", "blob_lods", "catalog_file_name",
           "catalog_from_dict", "check_clusters", "check_culled", "check_shader_ids", "check_transform_ids",
           "culled_meshlets", "extended_blocks", "instance_records", "material_culling_size", "material_tables",
           "material_views", "meshlet_catalog_for", "meshlet_lods", "meshlet_programs", "meshlet_runs",
           "named_meshlet_programs", "receives_decals", "record_handles", "replace_table", "root_constant", "run_range",
           "skinning_records", "special_offsets"]
