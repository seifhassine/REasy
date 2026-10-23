"""See docs/internals/mesh-preview.md."""
from __future__ import annotations

import copy
import struct
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from functools import partial

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP, QObject, Qt, Signal

from file_handlers.mmtr.engine_buffers import Camera, Placement, scene_values
from file_handlers.mmtr.light_passes import LightPasses, captured_constants, light_pass_catalog_for, parsed_pass_file
from file_handlers.mmtr.material_preview import MaterialPreviewError, gbuffer_route
from file_handlers.mmtr.mesh_draw import MeshBuffer, SubmeshRange
from file_handlers.mmtr.mesh_render import (
    DEPTH_VIEWS, MeshBounds, MeshObject, MeshPart, MeshScene, ObjectBox, PartError, boxes_seen,
    mesh_bounds, object_box, uv_density, world_boxes,
)
from file_handlers.mmtr.meshlets import (
    ROUTE as MESHLET_ROUTE, LodTable, MeshletBuffer, MeshletLodRule, MeshletLods, blob_halves, blob_lods,
    meshlet_catalog_for, meshlet_lods, run_range,
)
from file_handlers.mmtr.overlay import Overlay, view_projection
from file_handlers.sdf.sdf_file import CULL_NONE
from file_handlers.mmtr.pass_choice import PassChoiceError
from file_handlers.mmtr.parsed_files import Lease
from file_handlers.mmtr.pretransform import (
    ROUTE, ChainLevel, Deformation, blend_weights, chain_level, deformed_route, masters_skinning_program,
    pretransform_catalog_for,
)
from file_handlers.mmtr.pretransform_passes import SystemPrograms
from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.mmtr.speedtree_catalog import speedtree_catalog_for
from file_handlers.mmtr.temporal import FRAMES
from file_handlers.mmtr.wrinkle import WrinkleObject, mesh_joints, wrinkle_catalog_for, wrinkle_object, wrinkle_proxy
lazy from file_handlers.stmesh.lit import drawn_chunks, drawn_cluster, engine_buffers as stmesh_engine_buffers
lazy from file_handlers.stmesh.stmesh_file import FLAG_BILLBOARD_LOD
lazy from file_handlers.stmesh.stmesh_handler import StmeshHandler
from file_handlers.strands.chain_frame import Stepped
from file_handlers.strands.chain_scene import ChainScene
from file_handlers.strands.chain_view import StrandView
from file_handlers.tex.texture_quality import texture_quality_profile
lazy from ui.scene.mesh_scene import mesh_lod_parts, meshlet_part_runs
from ui.scene.scene_view import SceneGeometry, ScenePlacement, ScenePose, SceneView, moved_placements
from utils.native_build import ensure_fastmesh
from utils.resource_file_utils import PrefetchedFiles, held_folder_lookups, resource_version_from_path

lazy from .lod_settings import LodSettings, LodSettingsError, meshlet_thresholds, read_lod_settings
from .lit_preview import (
    DEFAULT_INTENSITY, NO_LIGHT_PASSES, UNKNOWN_SKINNING, GameLighting, LitDrawer, LitOverlays, LitPart, bind_part,
    camera_light, deformed, level_bounds, pass_files, scene_file, stencil_reference, submesh_groups,
)
from .material_resolver import MeshMaterialResolver
from .mesh_file import MeshFile
lazy from .scene_overlay import SceneOverlay
lazy from .scene_pick import ScenePickable
from .material_shader_preview import preview_material
from .scene_textures import SceneTexturesPreview

_UNMORPHED = QT_TRANSLATE_NOOP(
    "MaterialPreview", "The mesh is morphed, and REasy has no catalog of this build's mesh deformation passes.")
# Camera wind: m/s of wind per m/s of camera movement, and the share kept per ``_WIND_TIME``th of a second.
_WIND_PER_SPEED = 6.0
_WIND_KEPT, _WIND_TIME = 0.9, 60
_GRAVITY_GAIN = 2.0
# How much wider than the view objects are added in, so a turning camera finds them made.
_AHEAD = 1.25
# Seconds a still frame spends adding objects not added yet.
_FILL = 0.016

_fastmesh = ensure_fastmesh()


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitMesh:
    """``groups`` maps each submesh to its mesh group."""
    skinned: bool
    bones: int
    stencil_reference: int
    bounds: MeshBounds | None
    level: ChainLevel | None
    groups: Mapping[int, int]
    channels: Mapping[str, tuple[int, ...]]
    meshlets: bool = False
    engine: Mapping[str, bytes] = frozendict()
    box: ObjectBox | None = None

    @property
    def route(self) -> str:
        if self.meshlets:
            return MESHLET_ROUTE
        return gbuffer_route(self.skinned) if self.level is None else ROUTE


@dataclass(frozen=True, slots=True)
class SceneLitPart:
    """``piece`` is ``SceneDrawBatch.part_index``."""
    number: int
    piece: int | None
    part: LitPart


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitLevels:
    """A SpeedTree mesh's LODs after its first, drawn by the camera's distance. A vertex-stream mesh's from its file's
    ``LodProfile``: each 3D LOD over an even share of ``start`` to ``end`` and the last 3D LOD from there, the
    billboard LOD (the last, if the file has one) from ``billboard`` (inf without), and none past ``cull``. A meshlet
    mesh's (``meshlet``, its blob's) the one instance culling picks by ``rule`` (docs/internals/meshlets.md, LODs).
    Instances share them."""
    lods: tuple[tuple[SceneLitMesh, tuple[LitPart, ...]], ...]
    start: float
    end: float
    billboard: float
    cull: float
    rule: MeshletLodRule | None = None
    meshlet: MeshletLods | None = None


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitLods:
    """A meshlet ``.mesh``'s LODs after its first, which one owner holds with it; frames draw the LOD instance culling
    picks from ``lods`` (docs/internals/meshlets.md, LODs). ``parts`` are (LOD, piece, part). Instances share them."""
    parts: tuple[tuple[int, int | None, LitPart], ...]
    lods: MeshletLods


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitObject:
    """``edge_excluded``, ``decal_receive_mode`` and ``transparent_bias`` are the component's ``NREdgeExcluded``,
    ``DecalReciveMode`` and ``TransparentBias``; ``levels`` its SpeedTree mesh's other LODs, drawn by distance, and
    ``lods`` its meshlet ``.mesh``'s."""
    mesh: int
    of: SceneLitMesh
    parts: tuple[SceneLitPart, ...]
    batches: tuple[tuple[int | None, int], ...]
    edge_excluded: bool = False
    decal_receive_mode: int = 0
    transparent_bias: float = 0.0
    wrinkle: WrinkleObject | None = None
    levels: SceneLitLevels | None = None
    lods: SceneLitLods | None = None



def _program(item: SceneLitObject) -> str | None:
    """None when the renderer skins as it draws."""
    return None if item.of.level is None else masters_skinning_program(item.of.level, item.edge_excluded)


def _piece(batch) -> tuple[int, int, int]:
    """A gathered part's key: the batch's LOD, first submesh and submeshes (a meshlet run holds more than one)."""
    return (*batch.submesh, batch.submeshes)


def _run(held: list[tuple[SubmeshRange, object]]) -> tuple[SubmeshRange, object]:
    """The range and buffer of a run of read submeshes; a run's submeshes share their buffer (``meshlet_runs``)."""
    return run_range([submesh for submesh, _buffer in held]), held[0][1]


class MaterialValues:
    def __init__(self) -> None:
        self.now: Mapping[str, Mapping[str, float]] = {}

    def of(self, key: str) -> Mapping[str, float]:
        return self.now.get(key, {})


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitContent:
    """The objects' parts are numbered by their batches, over ``meshes``."""
    geometry: int
    game: str
    meshes: tuple[SceneGeometry, ...]
    objects: tuple[SceneLitObject, ...]
    masters: Mapping[str, tuple[str, bytes]]
    files: Mapping[str, bytes]
    strands: ChainScene | None = None
    strands_object: int | None = None
    strands_refused: str = ""
    values: MaterialValues = field(default_factory=MaterialValues)
    wrinkles_refused: str = ""
    lods_refused: str = ""


@dataclass(frozen=True, slots=True)
class SceneLitFrame:
    view: SceneView
    drawn: bool
    failures: Mapping[int, Exception]
    missing_textures: tuple[str, ...]
    pickable: ScenePickable | None = field(default=None, kw_only=True)
    settling: bool = field(default=False, kw_only=True)
    stepped: Stepped | None = field(default=None, kw_only=True)
    # Why objects of meshlet LODs draw their first at every distance.
    lods_refused: str = field(default="", kw_only=True)


def _placement(of: SceneLitMesh, placement: ScenePlacement) -> Placement:
    world = placement.world
    if not of.skinned:
        return Placement(world)
    bones = np.tile(world, (of.bones, 1, 1))
    pose = placement.pose
    if pose is not None and pose.bones is not None:
        posed = np.asarray(pose.bones, np.float64)
        bones[:len(posed), :, :3] = np.matmul(world[:, :3], posed[:, :, :3])
        bones[:len(posed), :, 3] = posed[:, :, 3] @ world[:, :3].T + world[:, 3]
    return Placement(world, bones)


def _weights(item: SceneLitObject, pose: ScenePose | None) -> np.ndarray | None:
    of = item.of
    blend = None if of.level is None else of.level.blend
    morphs = () if pose is None else pose.morphs
    if blend is None:
        if any(weight and name in of.channels for name, weight in morphs):
            raise MaterialPreviewError(_UNMORPHED)
        return None
    weights = np.zeros(blend.channel_count, np.float32)
    for name, weight in morphs:
        weights[[index for index in of.channels.get(name, ()) if index < len(weights)]] = weight
    return blend_weights(blend, weights)


@dataclass(slots=True)
class _Entry:
    number: int
    placement: Placement
    deformation: Deformation | None
    skins: list[MeshPart]
    parts: list[tuple[int, MeshPart]]
    of: SceneLitMesh | None = None
    level: int = 0
    # The bound parts of the object's coarser meshlet LODs, with their ``SceneLitLods.lods``; why they are dropped.
    coarser: list[MeshPart] = field(default_factory=list)
    lods: MeshletLods | None = None
    lods_refused: str = ""


@dataclass(slots=True)
class _Held:
    owners: list[int]
    placements: tuple[ScenePlacement, ...]
    worlds: np.ndarray
    poses: list[ScenePose | None]
    weights: list[np.ndarray | None]
    # Each owner's LOD of its object, and the entry it was added by.
    levels: list[int]
    entries: list[_Entry]


class _Streams:
    """Which objects of a content a scene holds, at which LOD. One drawn whatever the view (deformed, skinned,
    wrinkled, unbounded, or of a scene with strands) is added when the scene is made; another the first time it is
    near the view, at the LOD its distance from the eye calls for (``SceneLitLevels``). Frames hide the LODs not
    called for."""

    def __init__(self, content: SceneLitContent, placements: tuple[ScenePlacement, ...],
                 held: _Streams | None = None):
        objects = content.objects
        lazy = [] if content.strands is not None else [
            number for number, item in enumerate(objects)
            if item.of.bounds is not None and item.of.level is None and not item.of.skinned and item.wrinkle is None]
        self.always = sorted(set(range(len(objects))) - set(lazy))
        self.lazy = np.array(lazy, np.int64)
        self._meshes = np.array([objects[number].mesh for number in lazy], np.int64)
        self.index = np.full(len(objects), -1, np.int64)
        self.index[self.lazy] = np.arange(len(lazy))
        levels = [objects[number].levels for number in lazy]
        self.count = np.array([1 if item is None else 1 + len(item.lods) for item in levels], np.int64)
        self.start = np.array([np.inf if item is None else item.start for item in levels], np.float64)
        self.end = np.array([np.inf if item is None else item.end for item in levels], np.float64)
        self.billboard = np.array([np.inf if item is None else item.billboard for item in levels], np.float64)
        self.cull = np.array([np.inf if item is None else item.cull for item in levels], np.float64)
        # 3D LODs.
        self.solid = (self.count - np.isfinite(self.billboard)).astype(np.int64)
        # Those whose LOD instance culling picks, all by one build's rule.
        self.picked = np.array([row for row, item in enumerate(levels)
                                if item is not None and item.meshlet is not None], np.int64)
        self._meshlets = [levels[row].meshlet for row in self.picked.tolist()]
        self._rule = levels[self.picked[0]].rule if len(self.picked) else None
        self._table: LodTable | None = None
        # By object and LOD: added, or refused.
        self.tried = np.zeros((len(lazy), int(self.count.max(initial=1))), bool)
        if held is not None and len(held.lazy):
            self.tried[self.index[held.lazy], :held.tried.shape[1]] = held.tried
        # Objects drawn whatever the view not added yet: those of meshes hidden wait to be shown.
        self.pending = set(self.always) - (set() if held is None else set(held.always) - held.pending)
        self._placed: tuple | None = None
        self._looked: tuple | None = None
        self._hid: tuple | None = None
        self._owned: tuple | None = None
        # The look, marks and meshes hidden ``wanted`` answered last, and its answer.
        self._wanted: tuple | None = None
        self._marks = 0
        self.place(content, placements)

    def place(self, content: SceneLitContent, placements: tuple[ScenePlacement, ...]) -> None:
        held = self._placed
        if held is not None and held[0] is placements:
            return
        objects = content.objects
        moved = moved_placements(placements, None if held is None else held[0])[self._meshes]
        rows = np.flatnonzero(moved)
        lazy = self.lazy[rows].tolist()
        worlds = np.array([placements[objects[number].mesh].world for number in lazy], np.float64).reshape(-1, 3, 4)
        origins, boxes = (np.zeros((len(moved), 3)), np.zeros((len(moved), 2, 3))) if held is None else held[1:]
        if lazy:
            origins[rows] = worlds[:, :, 3]
            boxes[rows] = world_boxes(worlds[:, None], [objects[number].of.bounds for number in lazy])
        self._placed = placements, origins, boxes
        if self._meshlets:
            picked = np.flatnonzero(moved[self.picked])
            placed = worlds[np.searchsorted(rows, self.picked[picked])]
            if self._table is None:
                self._table = LodTable(self._meshlets, placed)
            elif len(picked):
                self._table.place(picked, placed)
        if lazy:
            self._looked = None

    def _look(self, camera: Camera) -> tuple:
        """Of the objects added near the view: whether each is near it, its LOD (-1: none) and its distance."""
        key = np.asarray(camera.view, np.float64).tobytes(), camera.scale, camera.size, camera.orthographic
        if self._looked is None or self._looked[0] != key:
            _placements, origins, boxes = self._placed
            view = np.asarray(camera.view, np.float64).reshape(3, 4)
            distance, level = _fastmesh.stream_levels(origins, tuple(view[:, :3].T @ view[:, 3]), self.start, self.end,
                                                      self.billboard, self.cull, self.solid, self.count)
            distance, level = np.frombuffer(distance, np.float64), np.frombuffer(level, np.int64)
            if self._table is not None:
                # As ``MeshScene._lods``: the view without its jitter.
                matrix = scene_values(replace(camera, jitter=(0.0, 0.0), previous=None))["viewProjMat"]
                level[self.picked] = meshlet_lods(self._rule, self._table, matrix, -view[:, :3].T @ view[:, 3],
                                                  camera.scale[0], camera.size)[0]
            ahead = replace(camera, scale=(camera.scale[0] / _AHEAD, camera.scale[1] / _AHEAD))
            seen = boxes_seen(boxes, view_projection(ahead))
            self._looked = key, seen, level, distance
        return self._looked

    def wanted(self, camera: Camera, hidden: np.ndarray | None) -> list[tuple[int, int]]:
        """(object, LOD) of the objects drawn whatever the view not added yet, then of those near the view not added
        yet at the LOD they are drawn at; none of meshes ``hidden`` (by object, None: none). Not to be changed."""
        looked = self._look(camera)
        held = self._wanted
        if held is not None and held[0] is looked and held[1] == self._marks and held[2] is hidden:
            return held[3]
        _key, seen, level, _distance = looked
        rows = np.flatnonzero(seen & (level >= 0))
        rows = rows[~self.tried[rows, level[rows]]]
        if hidden is not None:
            rows = rows[~hidden[self.lazy[rows]]]
        found = [(number, 0) for number in sorted(self.pending) if hidden is None or not hidden[number]] + list(
            zip(self.lazy[rows].tolist(), level[rows].tolist()))
        self._wanted = looked, self._marks, hidden, found
        return found

    def nearest(self, camera: Camera, hidden: np.ndarray | None):
        """(object, LOD) of the objects not added yet, nearest the eye first."""
        _key, _seen, level, distance = self._look(camera)
        lod = np.maximum(level, 0)
        rows = np.flatnonzero(~self.tried[np.arange(len(lod)), lod])
        if hidden is not None:
            rows = rows[~hidden[self.lazy[rows]]]
        rows = rows[np.argsort(distance[rows], kind="stable")]
        return zip(self.lazy[rows].tolist(), lod[rows].tolist())

    def rest(self, camera: Camera, hidden: np.ndarray | None) -> list[tuple[int, int]]:
        """(object, LOD) of every LOD not added yet, nearest the eye first, the LOD drawn there before the others."""
        _key, _seen, level, distance = self._look(camera)
        rows, lods = np.nonzero(~self.tried & (np.arange(self.tried.shape[1]) < self.count[:, None]))
        if hidden is not None:
            kept = ~hidden[self.lazy[rows]]
            rows, lods = rows[kept], lods[kept]
        order = np.lexsort((np.abs(lods - np.maximum(level[rows], 0)), distance[rows]))
        return list(zip(self.lazy[rows[order]].tolist(), lods[order].tolist()))

    def mark(self, units: Sequence[tuple[int, int]]) -> None:
        self.pending.difference_update(number for number, _level in units)
        self._marks += 1
        if units:
            numbers, levels = np.array(units, np.int64).T
            rows = self.index[numbers]
            self.tried[rows[rows >= 0], levels[rows >= 0]] = True

    def owned(self, held: _Held) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """The owners' objects, rows of those added near the view (-1: others) and LODs."""
        if self._owned is None or self._owned[0] is not held or self._owned[1] != len(held.owners):
            owners = np.array(held.owners, np.int64)
            self._owned = held, len(owners), owners, self.index[owners], np.array(held.levels, np.int64)
        return self._owned[2:]

    def hidden(self, camera: Camera, held: _Held) -> np.ndarray:
        """Of the owners: those of LODs their object is not drawn at."""
        looked = self._look(camera)
        if self._hid is None or self._hid[0] is not looked or self._hid[1] != len(held.owners):
            _owners, rows, levels = self.owned(held)
            lazy = rows >= 0
            hidden = np.zeros(len(rows), bool)
            hidden[lazy] = looked[2][rows[lazy]] != levels[lazy]
            self._hid = looked, len(held.owners), hidden
        return self._hid[2]


def _strand_image(strands: StrandView, mesh: MeshScene, content: SceneLitContent,
                  placements: tuple[ScenePlacement, ...], rate: float, wind: np.ndarray | None,
                  turn: np.ndarray | None) -> None:
    placement = placements[content.objects[content.strands_object].mesh]
    world = np.eye(4)
    world[:3] = placement.world
    strands.image(mesh, rate, world.T, None if placement.pose is None else placement.pose.joints, wind, turn)


def _same_look(one: tuple | None, other: tuple | None) -> bool:
    """Of ``SceneLitDrawer._looking``: the same camera, placements, streams and meshes hidden."""
    return one is not None and other is not None and one[0] == other[0] and one[1] is other[1] \
        and one[2] is other[2] and one[3] == other[3]


def _grows(drawn: tuple, key: tuple, content: SceneLitContent, start: int) -> bool:
    """Whether the scene drawn for the key ``drawn`` is the one for ``key`` once the objects from ``start`` are added:
    the objects drawn are shown as they were, and refused but for added ones."""
    names = {content.meshes[item.mesh].key for item in content.objects[:start]}

    def held(parts: frozenset) -> frozenset:
        return frozenset(item for item in parts if item[0] in names)
    return held(key[0]) == held(drawn[0]) and key[2] == drawn[2] and drawn[1] <= key[1] \
        and all(number >= start for number in key[1] - drawn[1])


def _retired(mesh: MeshScene, entries: list[_Entry], error: PartError, failures: dict[int, Exception]) -> bool:
    """Retires the parts ``error`` refuses when all are of ``entries``, the objects just added, telling their failures;
    False when it refuses others."""
    try:
        _without(entries, error, failures)
    except PartError:
        return False
    mesh.retire()
    return True


def _same(item: SceneLitObject, old: SceneLitObject) -> SceneLitObject:
    """``old`` when ``item`` draws as it does, so the drawer keeps what it made of it."""
    if (item.mesh, item.batches, item.edge_excluded, item.decal_receive_mode, item.transparent_bias) != (
            old.mesh, old.batches, old.edge_excluded, old.decal_receive_mode, old.transparent_bias) \
            or item.of is not old.of or item.wrinkle is not old.wrinkle or item.levels is not old.levels \
            or item.lods is not old.lods or len(item.parts) != len(old.parts):
        return item
    if all(new.number == was.number and new.piece == was.piece and new.part is was.part
           for new, was in zip(item.parts, old.parts)):
        return old
    return item


def _without(entries: list[_Entry], error: PartError, failures: dict[int, Exception]) -> list[_Entry]:
    """Takes the refusals in order: each leaves out its part, and every part of the entries only skinning it. A part of
    an entry's coarser LODs leaves them all out, telling no failure, and so does one of its first LOD, which fails,
    as its piece's coarser parts would draw it from afar: the object draws its first LOD at every distance."""
    given = {id(part) for entry in entries
             for part in (*(part for _number, part in entry.parts), *entry.skins, *entry.coarser)}
    if any(id(refused) not in given for refused, _reason in error.refusals):
        raise error
    # The entries drawing each part as one of their coarser LODs', by id.
    coarse: dict[int, list[int]] = {}
    for index, entry in enumerate(entries):
        for part in entry.coarser:
            coarse.setdefault(id(part), []).append(index)
    # Per entry: the numbers of each part it still draws and the parts it skins, by id.
    drawn: list[dict[int, list[int]]] = [{} for _entry in entries]
    skinned = [{id(part) for part in entry.skins} for entry in entries]
    # The entries drawing or skinning each part, by id, in order.
    holding: dict[int, list[int]] = {}
    for index, entry in enumerate(entries):
        for number, part in entry.parts:
            drawn[index].setdefault(id(part), []).append(number)
        for key in drawn[index].keys() | skinned[index]:
            holding.setdefault(key, []).append(index)
    left = [len(entry.parts) for entry in entries]
    gone = [False] * len(entries)
    changed = set()
    for refused, reason in error.refusals:
        key = id(refused)
        for index in coarse.pop(key, ()):
            entry = entries[index]
            if entry.lods is not None:
                entry.coarser, entry.lods, entry.lods_refused = [], None, str(reason)
        if key not in holding:
            continue
        for index in holding[key]:
            if gone[index]:
                continue
            numbers = drawn[index].pop(key, None)
            if numbers is not None:
                failures.update((number, reason) for number in numbers)
                left[index] -= len(numbers)
                gone[index] = not left[index]
                changed.add(index)
                entry = entries[index]
                if entry.lods is not None:
                    entry.coarser, entry.lods, entry.lods_refused = [], None, str(reason)
            elif key in skinned[index]:
                # An object that only skins the refused part can't be deformed, so all its parts fail.
                failures.update((number, reason) for number, part in entries[index].parts
                                if id(part) in drawn[index])
                gone[index] = True
                changed.add(index)
        # Only the entries skinning the part still hold it.
        holding[key] = [index for index in holding[key] if not gone[index] and key in skinned[index]]
    for index in changed:
        entry = entries[index]
        entry.parts = [] if gone[index] else [(number, part) for number, part in entry.parts
                                              if id(part) in drawn[index]]
    return [entry for index, entry in enumerate(entries) if not gone[index]]


class SceneLitDrawer(LitDrawer):
    """Camera wind and gravity are REasy's stand-ins (docs/internals/mesh-preview.md)."""

    def __init__(self):
        super().__init__()
        # Keyed by (id of the ``LitPart``, skinning program).
        self._bound: dict[int, tuple[MeshPart | None, Exception | None]] | None = None
        # ``bind_part`` by what it reads.
        self._materials: dict[tuple, tuple] = {}
        # Each material context's number by value, and by object: equal contexts of other MDF files are other objects.
        self._contexts: dict[object, int] = {}
        self._context_numbers: dict[int, tuple[object, int]] = {}
        self._morphs: list = []
        self._weighed: tuple[ScenePlacement, ...] | None = None
        # The objects whose weights the placements weighed refuse.
        self._unweighed: dict[int, Exception] = {}
        self._of_meshes: tuple[SceneLitContent, np.ndarray] | None = None
        # The content and owners of the frame's meshes, which ``_frame`` names by owner.
        self._owner_meshes: tuple[object, int, list[int], list[int]] | None = None
        self._held: _Held | None = None
        # The content, key and error of the last scene made that raised: frames of them raise it again, unmade.
        self._failed: tuple[SceneLitContent, tuple, Exception] | None = None
        self._scenes = SceneOverlay(self._scene_cull)
        self._faces: dict[int, tuple[int, bool] | None] = {}
        # The content, meshes hidden and whether each of its objects is of one of them.
        self._hiding: tuple | None = None
        # The view and placements drawn last, the last all objects were added for, and how many a still frame adds.
        self._looked: tuple | None = None
        self._filled: tuple | None = None
        self._fills = 32
        # What the scene's image the last frame drew is of: view, placements and objects; frames running drawn so.
        self._shown_for: tuple | None = None
        self._shown_times = 0
        # World space.
        self._wind = np.zeros(3)
        self._eye: np.ndarray | None = None
        self._rate: float | None = None
        self._streams: _Streams | None = None
        # Why objects of the scene drawn draw their first meshlet LOD at every distance (``_Entry.lods_refused``).
        self._lods_refused: dict[str, None] = {}
        # The content and the keys of its meshlet meshes; those of meshes whose deformation was refused.
        self._meshlet_keys: tuple[SceneLitContent, set[str]] | None = None
        self._undeformed: set[str] = set()
        # The parts hidden, held objects and owners the scene's parts were last left out for, and (owner, part) of
        # those left out.
        self._parts_for: tuple | None = None
        self._parts_hidden: list[tuple[int, MeshPart]] = []
        # The content and each mesh's first part number; the failures, parts hidden and failures shown last.
        self._firsts: tuple[SceneLitContent, np.ndarray] | None = None
        self._failures_shown: tuple | None = None

    def _blow(self, camera: Camera, rate: float) -> np.ndarray:
        view = np.asarray(camera.view, np.float64).reshape(3, 4)
        eye = -view[:, :3].T @ view[:, 3]
        moved = np.zeros(3) if self._eye is None else (eye - self._eye) * rate
        self._eye = eye
        kept = _WIND_KEPT ** (_WIND_TIME / rate)
        self._wind = self._wind * kept + moved * (_WIND_PER_SPEED * (1 - kept))
        return self._wind

    @staticmethod
    def _fall(camera: Camera) -> np.ndarray:
        turn = np.asarray(camera.view, np.float64).reshape(3, 4)[:, :3].T
        return turn * (1 + _GRAVITY_GAIN * (1 - turn[1, 1]))

    def _leave(self) -> None:
        super()._leave()
        self._scenes.forget()
        self._eye = None
        self._wind = np.zeros(3)

    def _hidden_objects(self, content: SceneLitContent, view: SceneView) -> np.ndarray | None:
        """Whether each object is of a mesh hidden; None: none is."""
        if not view.hidden:
            return None
        if self._hiding is None or self._hiding[0] is not content or self._hiding[1] != view.hidden:
            self._hiding = content, view.hidden, np.array(
                [content.meshes[item.mesh].key in view.hidden for item in content.objects], bool)
        return self._hiding[2]

    def _hidden(self, content: SceneLitContent, view: SceneView, camera: Camera) -> np.ndarray:
        """Of the owners: those of LODs their object is not drawn at, and those of meshes hidden."""
        hidden, objects = self._streams.hidden(camera, self._held), self._hidden_objects(content, view)
        return hidden if objects is None else hidden | objects[self._streams.owned(self._held)[0]]

    def _scene_cull(self, number: int) -> tuple[int, bool] | None:
        """None while no LOD 0 of the batch's object is added."""
        if number not in self._faces:
            return None
        return self._faces[number] or (CULL_NONE, True)

    def _drawn_meshes(self, content: SceneLitContent) -> list[int]:
        """The overlay's meshes: an object's LODs are one."""
        owners = self._held.owners
        made = self._owner_meshes
        if made is None or made[0] is not content or made[1] != len(owners) or made[2] is not owners:
            made = self._owner_meshes = content, len(owners), owners, [content.objects[number].mesh
                                                                        for number in dict.fromkeys(owners)]
        return made[3]

    def _rebuilt(self, content: SceneLitContent, view: SceneView) -> frozenset:
        """The parts hidden the scene is made without: those of meshlet meshes, whose clusters of a material draw
        together, of meshes whose deformation was refused, and of any mesh in a scene with strands."""
        if not view.hidden_parts or content.strands is not None:
            return view.hidden_parts
        held = self._meshlet_keys
        if held is None or held[0] is not content:
            held = self._meshlet_keys = content, {
                content.meshes[item.mesh].key for item in content.objects
                if item.lods is not None or isinstance(item.parts[0].part.buffer, MeshletBuffer)}
        keys = held[1] | self._undeformed
        return frozenset(item for item in view.hidden_parts if item[0] in keys)

    def _show_parts(self, mesh: MeshScene, content: SceneLitContent, view: SceneView) -> None:
        """Leaves the other parts hidden out of frames, an object's coarser LOD once all its pieces are, and deforms
        the objects for the mesh groups of their pieces shown."""
        held = self._held
        hidden = view.hidden_parts - self._rebuilt(content, view)
        done, count = self._parts_for, len(held.owners)
        # Owners added since were added for the parts hidden: their deformations are of the pieces shown.
        start = done[2] if done is not None and done[0] == hidden and done[1] is held else 0
        if start == count:
            return
        self._parts_for = hidden, held, count
        keys, hiding = {key for key, _piece in hidden}, self._parts_hidden if start else []
        for owner in range(start, count):
            entry = held.entries[owner]
            item = content.objects[entry.number]
            name = content.meshes[item.mesh].key
            if not start and mesh.objects[owner].deformation is not None:
                self._deform(mesh, owner, content, item, name, view)
            if name not in keys:
                continue
            if entry.level:
                if all((name, spot.piece) in hidden for spot in item.parts):
                    hiding += [(owner, part) for _number, part in entry.parts]
                continue
            pieces = {spot.number: spot.piece for spot in item.parts}
            hiding += [(owner, part) for number, part in entry.parts if (name, pieces[number]) in hidden]
        self._parts_hidden = hiding
        mesh.hide_parts(hiding)

    def _deform(self, mesh: MeshScene, owner: int, content: SceneLitContent, item: SceneLitObject, name: str,
                view: SceneView) -> None:
        visible = [submesh for piece, submesh in item.batches if (name, piece) not in view.hidden_parts]
        if frozenset(item.of.groups[index] for index in visible) == mesh.objects[owner].deformation.enabled:
            return
        program = _program(item)
        held = {spot.part.index: self._bound[id(spot.part), program][0] for spot in item.parts
                if self._bound[id(spot.part), program][0] is not None}
        try:
            deformation, skins = deformed(content.game, item.of.level, item.of.groups, visible, held,
                                          self._morphs[self._held.entries[owner].number][1],
                                          edge_excluded=item.edge_excluded)
        except MaterialPreviewError:
            self._undeformed.add(name)
            raise
        drawn = mesh.objects[owner].parts
        mesh.deform(owner, deformation, (part for part in skins if all(part is not other for other in drawn)))

    def _shown_failures(self, content: SceneLitContent, view: SceneView,
                        failures: Mapping[int, Exception]) -> Mapping[int, Exception]:
        """``failures`` but of the parts hidden."""
        if not view.hidden_parts or not failures:
            return failures
        held = self._failures_shown
        if held is not None and held[0] is failures and held[1] == view.hidden_parts:
            return held[2]
        if self._firsts is None or self._firsts[0] is not content:
            self._firsts = content, np.cumsum([0, *(len(geometry.batches) for geometry in content.meshes)])
        firsts, shown = self._firsts[1], {}
        for number, reason in failures.items():
            mesh = int(np.searchsorted(firsts, number, "right")) - 1
            geometry = content.meshes[mesh]
            if (geometry.key, geometry.batches[number - firsts[mesh]].part_index) not in view.hidden_parts:
                shown[number] = reason
        self._failures_shown = failures, view.hidden_parts, shown
        return shown

    def _stands(self, drawn: tuple, content: SceneLitContent, view: SceneView,
                placements: tuple[ScenePlacement, ...]) -> bool:
        """Whether the scene's image the last frame drew is what drawing it again would show: averaged in full, or of
        the same view (material values move it), placements and objects, drawn ``FRAMES`` times running where passes
        carry outputs on."""
        mesh, passes, _overlay, strands = drawn
        if strands is not None or mesh.streaming or mesh.refills != self._refills:
            return False
        if self._still is not None:
            return self._settled
        return self._shown_for == (view, placements, len(self._held.owners)) and (
            not passes.carries or self._shown_times >= FRAMES)

    def _overlaid(self, drawn: tuple, content: SceneLitContent, view: SceneView,
                  placements: tuple[ScenePlacement, ...], overlays: LitOverlays, camera: Camera) -> None:
        """The overlays drawn again over the scene's image the last frame drew."""
        mesh, passes, overlay, _strands = drawn
        shown = passes.shown if self._still is None else self._temporal.image
        mesh.device.pending += self._scenes.steps(overlay, shown, overlays, camera, content.meshes,
                                                  self._drawn_meshes(content), placements, view.hidden_parts,
                                                  view.hidden)
        mesh.device.flush()

    def _frame(self, drawn: tuple, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
               overlays: LitOverlays, camera: Camera, light) -> None:
        mesh, passes, overlay, strands = drawn
        self._show_parts(mesh, content, view)
        meshes = self._drawn_meshes(content)
        shown_for = view, placements, len(self._held.owners)
        self._shown_times = self._shown_times + 1 if shown_for == self._shown_for else 1
        self._shown_for = shown_for

        def after(shown: int | None, averaged: bool = True) -> list[int]:
            self._streamed_in(mesh)
            steps = []
            if averaged and overlays.filled:
                steps, shown = self._average(overlay, shown, overlays.background,
                                             None if strands is None else strands.chain.drawn_opacity)
            return steps + self._scenes.steps(overlay, shown, overlays, camera, content.meshes, meshes, placements,
                                              view.hidden_parts, view.hidden)
        if not overlays.filled:
            mesh.device.pending += after(None, averaged=False)
            mesh.device.flush()
        else:
            if strands is not None:
                _strand_image(strands, mesh, content, placements, self._rate, self._blow(camera, self._rate),
                              self._fall(camera))
            passes.drawn(light, after)

    def draw(self, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
             overlays: LitOverlays, rate: float, materials: Mapping[str, Mapping[str, float]] = frozendict()
             ) -> SceneLitFrame:
        content.values.now = materials
        self._rate = rate
        kept = None
        if content is not self._scene:
            kept = self._kept(content)
            if kept is None:
                self._leave()
                self._lease.close()
                self._scene, self._bound, self._lease = content, None, Lease()
                self._morphs, self._weighed, self._unweighed = [None] * len(content.objects), None, {}
                self._undeformed = set()
            else:
                self._scene = content
                self._morphs += [None] * (len(content.objects) - kept)
                self._weighed = None
        if not content.objects:
            return SceneLitFrame(view, False, {}, ())
        profile = mmtr_profile_for_game(content.game)
        catalog = light_pass_catalog_for(profile)
        if self._bound is None:
            self._materials, self._bound, self._contexts, self._context_numbers = {}, {}, {}, {}
            self._bind(content, profile, catalog, 0)
        elif kept is not None:
            self._bind(content, profile, catalog, kept)
        refused = self._weigh(content, placements)
        # Meshes hidden are drawn hidden (``_hidden``) and most parts hidden left out of frames (``_show_parts``),
        # not out of the scene.
        key = self._rebuilt(content, view), frozenset(refused), view.strands_only
        made = self._drawn is None or self._drawn[0] != key or kept is not None
        grows = kept is not None and self._drawn is not None and self._drawn[1] is not None \
            and _grows(self._drawn[0], key, content, kept)
        if made and not grows:
            self._leave()
        camera = view.camera()
        # The view without what it draws: an average begun again when only that changed keeps the texels it left.
        framed = replace(view, content=0, motion=0, hidden=frozenset(), hidden_parts=frozenset())
        drawn_by = self._antialias(camera, (view, placements), view.antialiased, framed)
        nearer = not made and self._drawn[1] is not None and self._nearer(content, view, placements, camera)
        filled = None if made or grows or nearer or self._drawn[1] is None else self._fill(content, view, placements,
                                                                                           camera)
        if grows or nearer or filled:
            started = time.perf_counter()
            failures = self._grow(self._drawn[1], content, view, placements, overlays, refused,
                                  kept if grows else None, dict(self._drawn[2]), camera, drawn_by, filled)
            if filled:
                took = time.perf_counter() - started
                self._fills = self._fills * 2 if took < _FILL else max(self._fills // 2, 1) if took > 2 * _FILL \
                    else self._fills
            if failures is not None:
                drawn, made = self._drawn[1], False
                self._drawn = key, drawn, failures
                drawn[0].release_texels()
                content.files.release()
            else:
                self._leave()
                made = True
                drawn_by = self._antialias(camera, (view, placements), view.antialiased, framed)
        if made:
            failed = self._failed
            if failed is not None and failed[0] is content and failed[1] == key and kept is None:
                raise failed[2].with_traceback(None)
            try:
                drawn, failures = self._build(content, view, placements, overlays, profile, catalog, refused,
                                              drawn_by)
            except Exception as exc:
                # A copy: no traceback holding the frames of the scene being made.
                self._failed = content, key, copy.copy(exc)
                raise
            self._failed = None
            self._drawn = key, drawn, failures
            if drawn is not None:
                drawn[0].release_texels()
                content.files.release()
        elif not (grows or nearer or filled):
            _key, drawn, failures = self._drawn
            if drawn is not None and overlays.filled and self._stands(drawn, content, view, placements):
                self._overlaid(drawn, content, view, placements, overlays, camera)
            elif drawn is not None:
                mesh = drawn[0]
                if mesh.size != tuple(view.size):
                    self._resize(drawn, view.size)
                mesh.camera = drawn_by
                self._place(mesh, content, placements)
                mesh.hide(self._hidden(content, view, camera))
                try:
                    self._frame(drawn, content, view, placements, overlays, camera,
                                camera_light(camera, DEFAULT_INTENSITY))
                except (PartError, MaterialPreviewError):
                    # Refused once deformed for other mesh groups: made again, as a scene of these shown is.
                    self._leave()
                    return self.draw(content, view, placements, overlays, rate, materials)
        failures = self._shown_failures(content, view, failures)
        if drawn is None:
            return SceneLitFrame(view, False, failures, ())
        self._looked = self._looking(view, placements, camera)
        return SceneLitFrame(view, True, failures, drawn[0].missing_textures, pickable=self._scenes.pickable(),
                             settling=self._settling or drawn[0].streaming
                             or not _same_look(self._filled, self._looked),
                             stepped=None if drawn[3] is None else drawn[3].chain.stepped,
                             lods_refused="; ".join(self._lods_refused))

    def _bind(self, content: SceneLitContent, profile, catalog, start: int) -> None:
        """Binds the parts of the objects from ``start`` not bound yet."""
        materials, bound = self._materials, self._bound
        for item in content.objects[start:]:
            route, program = item.of.route, _program(item)
            coarser = () if item.levels is None else (part for _of, parts in item.levels.lods for part in parts)
            lods = () if item.lods is None else (part for _lod, _piece, part in item.lods.parts)
            for part in (*(spot.part for spot in item.parts), *coarser, *lods):
                if (id(part), program) in bound:
                    continue
                # What bind_part reads.
                drawn_as = (part.mmtr_path, self._context_number(part.context), part.material.material_flags,
                            part.route or route, part.meshlet_programs, program, item.of.skinned)
                found = materials.get(drawn_as)
                if found is None:
                    found = materials[drawn_as] = bind_part(content.game, content.masters, route, part, profile,
                                                            catalog, self._lease, skinned=item.of.skinned,
                                                            program=program)
                document, refusal = found
                skin = None
                if document is not None:
                    own = deformed_route(document, catalog.master_target, part.route or route, program,
                                         item.of.skinned)
                    skin = MeshPart(document, part.material, part.buffer, part.submesh,
                                    own if own != route else part.route, part.meshlet_programs, part.texel_density,
                                    part.level)
                bound[id(part), program] = skin, refusal

    def _context_number(self, context) -> int:
        held = self._context_numbers.get(id(context))
        if held is None:
            # The context is held with its number, so its ``id`` stays its own.
            held = self._context_numbers[id(context)] = context, self._contexts.setdefault(context, len(self._contexts))
        return held[1]

    def _object_meshes(self, content: SceneLitContent) -> np.ndarray:
        """Each object's mesh."""
        if self._of_meshes is None or self._of_meshes[0] is not content:
            self._of_meshes = content, np.array([item.mesh for item in content.objects], np.int64)
        return self._of_meshes[1]

    def _weigh(self, content: SceneLitContent, placements: tuple[ScenePlacement, ...]) -> dict[int, Exception]:
        if placements is not self._weighed:
            moved = moved_placements(placements, self._weighed)[self._object_meshes(content)]
            for number in np.flatnonzero(moved).tolist():
                item = content.objects[number]
                pose, held = placements[item.mesh].pose, self._morphs[number]
                if held is not None and held[0] is pose:
                    continue
                try:
                    self._morphs[number] = pose, _weights(item, pose)
                    self._unweighed.pop(number, None)
                except MaterialPreviewError as exc:
                    self._morphs[number] = pose, exc
                    self._unweighed[number] = exc
            self._weighed = placements
        return self._unweighed

    def _kept(self, content: SceneLitContent) -> int | None:
        """How many objects of the scene ``content`` holds first, as they were, when it only adds others to them;
        None when it is another scene."""
        held = self._scene
        if held is None or self._bound is None or content.game != held.game or content.files is not held.files \
                or content.strands is not None or held.strands is not None \
                or len(content.objects) <= len(held.objects) \
                or any(new is not old for new, old in zip(content.objects, held.objects)):
            return None
        return len(held.objects)

    def _grow(self, drawn: tuple, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
              overlays: LitOverlays, refused: Mapping[int, Exception], start: int, failures: dict[int, Exception],
              camera: Camera, drawn_by: Camera, units: list[tuple[int, int]] | None = None
              ) -> dict[int, Exception] | None:
        """Adds the objects from ``start`` (None: none) and those come near the view (or ``units``) to the scene drawn,
        and draws its frame. Their parts refused once they are added are retired; None when a part drawn before is
        refused: the scene is made again."""
        mesh, passes, _overlay, _strands = drawn
        streams = self._streams
        if start is not None:
            streams = self._streams = _Streams(content, placements, streams)
        if units is None:
            units = streams.wanted(camera, self._hidden_objects(content, view))
        if start is not None:
            units = sorted(units)
        streams.mark(units)
        entries = self._entries(content, view, placements, refused, failures, units)
        owners = len(mesh.objects)
        while entries:
            try:
                mesh.add(self._mesh_objects(content, entries, False))
            except PartError as exc:
                if len(mesh.objects) == owners:
                    entries = _without(entries, exc, failures)
                    continue
                if not _retired(mesh, entries, exc, failures):
                    return None
            break
        given = [placements[content.objects[entry.number].mesh] for entry in entries]
        held = self._held
        held.owners += [entry.number for entry in entries]
        held.worlds = np.concatenate([held.worlds, np.array([placement.world for placement in given],
                                                            np.float64).reshape(-1, 3, 4)])
        held.poses += [placement.pose for placement in given]
        held.weights += [None if entry.deformation is None else entry.deformation.weights for entry in entries]
        held.levels += [entry.level for entry in entries]
        held.entries += entries
        self._faces.update((number, mesh.faces(owner, part)) for owner, entry in enumerate(entries, owners)
                           if not entry.level for number, part in entry.parts)
        passes.added()
        mesh.hide(self._hidden(content, view, camera))
        if mesh.size != tuple(view.size):
            self._resize(drawn, view.size)
        mesh.camera = drawn_by
        self._place(mesh, content, placements)
        while True:
            try:
                self._frame(drawn, content, view, placements, overlays, camera,
                            camera_light(camera, DEFAULT_INTENSITY))
            except PartError as exc:
                if not _retired(mesh, entries, exc, failures):
                    return None
                passes.added()
                continue
            except MaterialPreviewError:
                # The scene made again meets the error as a scene made with the objects does.
                return None
            self._told_lods(entries)
            return failures

    def _fill(self, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
              camera: Camera) -> list[tuple[int, int]] | None:
        """While the view stays, the LODs of objects not added yet, nearest first: once all are, moving adds none, as
        with a scene made of every object."""
        looked = self._looking(view, placements, camera)
        if not _same_look(looked, self._looked) or _same_look(looked, self._filled):
            return None
        units = self._streams.rest(camera, self._hidden_objects(content, view))[:self._fills]
        if not units:
            self._filled = looked
        return units or None

    def _looking(self, view: SceneView, placements: tuple[ScenePlacement, ...], camera: Camera) -> tuple:
        return self._streams._look(camera)[0], placements, self._streams, view.hidden

    def _nearer(self, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
                camera: Camera) -> bool:
        """Whether objects came near the view, or to a LOD not added yet."""
        self._streams.place(content, placements)
        return bool(self._streams.wanted(camera, self._hidden_objects(content, view)))

    def _entries(self, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
                 refused: Mapping[int, Exception], failures: dict[int, Exception],
                 units: Sequence[tuple[int, int]]) -> list[_Entry]:
        """An entry per (object, LOD) of ``units`` that draws a part. A coarser LOD draws its parts for the object's
        first part shown, and tells their failures on it. An object's coarser meshlet LODs draw the parts of the pieces
        shown, and none when one of them is refused."""
        entries, hidden = [], self._rebuilt(content, view)
        for number, level in units:
            item = content.objects[number]
            name = content.meshes[item.mesh].key
            shown = [spot for spot in item.parts if (name, spot.piece) not in hidden]
            if number in refused:
                failures.update((spot.number, refused[number]) for spot in shown)
                continue
            parts, of = [], item.of
            if level:
                of, coarser = item.levels.lods[level - 1]
                spots = [(shown[0].number, part) for part in coarser] if shown else []
            else:
                spots = [(spot.number, spot.part) for spot in shown]
            for label, part in spots:
                skin, refusal = self._bound[id(part), _program(item)]
                if refusal is None:
                    parts.append((label, skin))
                else:
                    failures[label] = refusal
            if not parts:
                continue
            deformation, skins = None, []
            if item.of.level is not None:
                visible = [submesh for piece, submesh in item.batches if (name, piece) not in view.hidden_parts]
                held = {spot.part.index: self._bound[id(spot.part), _program(item)][0] for spot in item.parts
                        if self._bound[id(spot.part), _program(item)][0] is not None}
                try:
                    deformation, skins = deformed(content.game, item.of.level, item.of.groups, visible, held,
                                                  self._morphs[number][1], edge_excluded=item.edge_excluded)
                except MaterialPreviewError as exc:
                    failures.update((part_number, exc) for part_number, _part in parts)
                    self._undeformed.add(name)
                    continue
            coarser, lods, lods_refused = [], None if level else item.lods, ""
            for _lod, piece, part in () if lods is None else lods.parts:
                if (name, piece) in hidden:
                    continue
                skin, refusal = self._bound[id(part), _program(item)]
                if refusal is not None:
                    coarser, lods, lods_refused = [], None, str(refusal)
                    break
                coarser.append(skin)
            entries.append(_Entry(number, _placement(item.of, placements[item.mesh]), deformation, skins, parts, of,
                                  level, coarser, None if lods is None else lods.lods, lods_refused))
        return entries

    def _told_lods(self, entries: list[_Entry]) -> None:
        self._lods_refused.update(dict.fromkeys(entry.lods_refused for entry in entries if entry.lods_refused))

    @staticmethod
    def _mesh_objects(content: SceneLitContent, entries: list[_Entry], alone: bool) -> list[MeshObject]:
        objects = []
        for entry in entries:
            shown, of = content.objects[entry.number], entry.of
            # The first LOD's parts first: an object's meshlet groups are numbered by their first part.
            drawn = [] if alone else [*(part for _number, part in entry.parts), *entry.coarser]
            skins = tuple(part for part in entry.skins if all(part is not item for item in drawn))
            objects.append(MeshObject(tuple(drawn), entry.placement, of.skinned, of.stencil_reference, of.bounds,
                                      entry.deformation, skins, of.engine, shown.decal_receive_mode, of.box,
                                      shown.transparent_bias, shown.wrinkle, None if alone else entry.lods))
        return objects

    def _build(self, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
               overlays: LitOverlays, profile, catalog, refused: Mapping[int, Exception], drawn_by: Camera) -> tuple:
        failures: dict[int, Exception] = {}
        self._lods_refused = {}
        camera = view.camera()
        streams = self._streams = _Streams(content, placements)
        hidden = self._hidden_objects(content, view)
        units = sorted(streams.wanted(camera, hidden))
        streams.mark(units)
        entries = self._entries(content, view, placements, refused, failures, units)
        if not entries:
            # A scene draws something, to add to as objects come near the view: the nearest the eye that draws.
            for unit in streams.nearest(camera, hidden):
                streams.mark([unit])
                entries = self._entries(content, view, placements, refused, failures, [unit])
                if entries:
                    break
        alone = view.strands_only and content.strands is not None
        if alone:
            entries = [entry for entry in entries
                       if entry.number == content.strands_object and entry.deformation is not None]
        light = camera_light(camera, DEFAULT_INTENSITY)

        def pass_file(path: str, data: bytes):
            return self._lease.parsed(path, data, parsed_pass_file, profile.game, path)
        while entries:
            mesh = None
            objects = self._mesh_objects(content, entries, alone)
            try:
                mesh = MeshScene(objects, content.files.get, drawn_by, shader_target=catalog.master_target,
                                 hardware=True, phases={phase.key for phase in catalog.phases},
                                 pass_file=lambda path, data: pass_file(path, data).document,
                                 captured=captured_constants(catalog), forget_file=content.files.forget)
                passes = LightPasses(mesh, pass_file=pass_file)
                owner = next((owner for owner, entry in enumerate(entries)
                              if entry.number == content.strands_object), None)
                strands = None if content.strands is None or owner is None else StrandView(
                    content.strands, passes, SystemPrograms(profile, partial(scene_file, content)),
                    catalog.master_target, owner)
                overlay = Overlay(mesh.device, view.size, (DEPTH_VIEWS[mesh.depth_format][1], mesh.depth))
                given = [placements[content.objects[entry.number].mesh] for entry in entries]
                self._held = _Held(
                    [entry.number for entry in entries], placements,
                    np.array([placement.world for placement in given], np.float64).reshape(-1, 3, 4),
                    [placement.pose for placement in given],
                    [None if entry.deformation is None else entry.deformation.weights for entry in entries],
                    [entry.level for entry in entries], list(entries))
                self._faces = {number: mesh.faces(owner, part) for owner, entry in enumerate(entries)
                               if not entry.level for number, part in entry.parts}
                mesh.hide(self._hidden(content, view, camera))
                drawn = mesh, passes, overlay, strands
                while True:
                    try:
                        self._frame(drawn, content, view, placements, overlays, camera, light)
                        break
                    except PartError as exc:
                        # The parts a frame refuses are left out of the frames after, as a scene made without them
                        # leaves them out; a scene with strands is made again.
                        if strands is not None or not _retired(mesh, entries, exc, failures):
                            raise
                        passes.added()
            except PartError as exc:
                if mesh is not None:
                    self._leave()
                    mesh.close()
                entries = _without(entries, exc, failures)
                continue
            self._told_lods(entries)
            return drawn, failures
        return None, failures

    def _place(self, mesh: MeshScene, content: SceneLitContent, placements: tuple[ScenePlacement, ...]) -> None:
        held = self._held
        if placements is held.placements:
            return
        owners = self._streams.owned(held)[0]
        moved = moved_placements(placements, held.placements)[self._object_meshes(content)[owners]]
        for owner in np.flatnonzero(moved).tolist():
            number = held.owners[owner]
            item = content.objects[number]
            placement = placements[item.mesh]
            world = np.asarray(placement.world, np.float64).reshape(3, 4)
            if (world != held.worlds[owner]).any() or item.of.skinned and placement.pose is not held.poses[owner]:
                mesh.place(owner, _placement(item.of, placement))
                held.poses[owner] = placement.pose
            held.worlds[owner] = world
            weights = self._morphs[number][1]
            if weights is not None and not np.array_equal(weights, held.weights[owner]):
                mesh.morph(owner, weights)
                held.weights[owner] = weights
        held.placements = placements


@dataclass(frozen=True, slots=True, eq=False)
class _Lod:
    submeshes: tuple[tuple[SubmeshRange, MeshBuffer | MeshletBuffer], ...]
    level: ChainLevel | None
    groups: Mapping[int, int]
    channels: Mapping[str, tuple[int, ...]]
    bounds: MeshBounds | None


@dataclass(slots=True)
class _MeshRead:
    mesh: object
    bones: int
    buffers: dict[int, MeshBuffer | MeshletBuffer] = field(default_factory=dict)
    lods: dict[int, _Lod | str] = field(default_factory=dict)


def _read_stmesh_lod(handler, lod: int) -> _Lod | str:
    parts = mesh_lod_parts(handler.mesh, lod)
    try:
        if handler.stmesh.meshlets is not None:
            drawn = [handler.meshlet_draw(submesh)[::-1] for _group, submesh in parts]
        else:
            drawn = [(draw.submesh, draw.buffer) for draw in (handler.cluster_draw(lod, submesh)
                                                              for _group, submesh in parts)]
    except MaterialPreviewError as exc:
        return str(exc)
    payloads = handler.mesh.mesh_buffer.buffer_payloads
    bounds = mesh_bounds((payloads[index] for index in sorted({submesh.buffer_index for _group, submesh in parts})),
                         None)
    return _Lod(tuple(drawn), None, {}, {}, bounds)


def _read_lod(mesh, lod: int, chains, skinned: bool, read: _MeshRead) -> _Lod:
    parts = mesh_lod_parts(mesh, lod)
    payloads = mesh.mesh_buffer.buffer_payloads
    for _group, submesh in parts:
        if submesh.buffer_index not in read.buffers:
            read.buffers[submesh.buffer_index] = (MeshletBuffer.from_mesh(mesh)
                                                  or MeshBuffer.from_payload(payloads[submesh.buffer_index]))
    # The game deforms LODs with blend shapes or normal-recalc data before drawing them.
    level = chain_level(mesh, lod, chains) if chains is not None and parts else None
    groups = {} if level is None else dict(enumerate(submesh_groups(level, (submesh for _group, submesh in parts))))
    body = None if mesh.blend_shape_data is None else next(
        (item for item in mesh.blend_shape_data.bodies if item.lod_index == lod), None)
    channels: dict[str, list[int]] = {}
    for channel in () if body is None else body.channels:
        if channel.name:
            channels.setdefault(channel.name, []).append(int(channel.index))
    bounds = level_bounds(
        level, (payloads[index] for index in sorted({submesh.buffer_index for _group, submesh in parts})),
        read.bones if skinned else None)
    return _Lod(tuple((SubmeshRange.from_submesh(submesh), read.buffers[submesh.buffer_index])
                      for _group, submesh in parts),
                level, groups, {name: tuple(numbers) for name, numbers in channels.items()}, bounds)


@dataclass(slots=True)
class _Gathered:
    game: str
    reason: str
    names: dict[str, str]
    placement: str | None = None
    resolve: Callable[[str], tuple[str, bytes] | None] | None = None
    cache: dict = field(default_factory=dict)
    meshes: dict[int, SceneLitMesh | str] = field(default_factory=dict)
    lods: dict[int, _Lod] = field(default_factory=dict)
    parts: dict[tuple[int, int], LitPart] = field(default_factory=dict)
    unusable: dict[str, str] = field(default_factory=dict)
    # By material: the masters and files its draws read.
    drawn: dict[str, tuple[dict, dict]] = field(default_factory=dict)
    # A SpeedTree mesh's other LODs: what they are, read once, and the levels made of their parts once all are drawn
    # (``SceneLitPreview._levels``).
    level_pieces: tuple | None = None
    levels: SceneLitLevels | None = None
    levels_made: bool = False
    # A meshlet mesh's, the same way (``SceneLitPreview._meshlet_lods``), and why the last gather can't draw them.
    meshlet_pieces: tuple | str | None = None
    meshlet_lods: SceneLitLods | None = None
    meshlet_lods_made: bool = False
    lods_refused: str = ""


def _first_densities(group: _Gathered) -> dict[str, float]:
    """The least texel density of each material's first-LOD parts."""
    densities: dict[str, float] = {}
    for (lod, _number, _count), part in group.parts.items():
        if lod == 0 and part.texel_density is not None:
            densities[part.key] = min(densities.get(part.key, np.inf), part.texel_density)
    return densities


def _meshlet_blob(lod: _Lod | None) -> bytes | None:
    """The meshlet blob of a LOD's mesh; None for a mesh of no meshlets."""
    if lod is None or not lod.submeshes or not isinstance(lod.submeshes[0][1], MeshletBuffer):
        return None
    return lod.submeshes[0][1].blob


class SceneLitPreview(GameLighting, SceneTexturesPreview):
    _game = True

    def __init__(self, viewport, materials, parent=None):
        super().__init__(viewport, materials, parent)
        self._read: dict[int, tuple[object, object, _MeshRead]] = {}
        # Kept while active, so a gather after an added SCN draws the meshes gathered before as they were.
        self._kept: dict[tuple, tuple[tuple, _Gathered]] = {}
        self._passes: dict[tuple, dict[str, bytes]] = {}
        self._previous: SceneLitContent | None = None
        self._values = MaterialValues()
        # By (game, resource scope): the LodSettings file it finds, or why it can't; by the bytes found: their reading.
        self._lod_settings: dict[tuple | bytes, LodSettings | str] = {}
        self.set_limit(viewport.fps_limit)
        viewport.fps_limit_changed.connect(self.set_limit)
        viewport.stepped_time = self.stepped_time

    def set_active(self, active: bool) -> None:
        super().set_active(active)
        if not active:
            self._read.clear()
            self._kept.clear()
            self._passes.clear()
            self._lod_settings.clear()
            self._previous = None
            self._viewport.set_strands_stepped(None)

    def _on_finished(self, thread, request: tuple, result) -> None:
        super()._on_finished(thread, request, result)
        if thread is self._thread:
            self._viewport.set_strands_stepped(None if isinstance(result, Exception) else result.stepped)

    def _stepping(self) -> bool:
        return self._active and self._scene is not None and self._scene.strands is not None

    def status(self) -> list[str]:
        refused = "" if self._scene is None else self._scene.strands_refused
        wrinkles = "" if self._scene is None else self._scene.wrinkles_refused
        lods = "; ".join(dict.fromkeys(filter(None, (
            "" if self._scene is None else self._scene.lods_refused,
            "" if self._frame is None else self._frame.lods_refused))))
        return (super().status() + ([self.tr("Strands not drawn: {reason}").format(reason=refused)] if refused else [])
                + ([self.tr("Wrinkles not drawn: {reason}").format(reason=wrinkles)] if wrinkles else [])
                + ([self.tr("LODs not drawn: {reason}").format(reason=lods)] if lods else []))

    def _make_drawer(self) -> SceneLitDrawer:
        return SceneLitDrawer()

    def _request(self) -> tuple:
        return (*super()._request(), self.step_rate, self._viewport.device_materials())

    def _gather(self) -> SceneLitContent:
        with held_folder_lookups():
            return self._lit_content()

    def _lit_content(self) -> SceneLitContent:
        number, meshes = self._told()
        # Their unposed shapes, for the parts' UV densities; the frame asks for the same placements.
        shapes = [placement.shape for placement in self._viewport.device_motion()] if meshes else []
        # By the ids of a shape and a batch's indices, which meshes of the same vertices share.
        self._densities: dict[tuple[int, int], float | None] = {}
        streaming = texture_quality_profile(self._materials.texture_quality).prefer_streaming
        owners = [[self._materials.owner(batch.material_name) if batch.material_name else None
                   for batch in mesh.batches] for mesh in meshes]
        wanted: dict[object, set[int]] = {}
        for mesh, sessions in zip(meshes, owners, strict=True):
            for batch, session in zip(mesh.batches, sessions, strict=True):
                if session is not None and batch.submesh is not None:
                    wanted.setdefault(session, set()).add(batch.submesh[0])
        gathered: dict[object, _Gathered] = {}
        scopes: dict[tuple, dict] = {}
        read: dict[int, tuple[object, object, _MeshRead]] = {}
        masters: dict[str, tuple[str, bytes]] = {}
        files: dict[str, bytes] = {}
        # (group, material) whose masters and files are in these: merging them again changes nothing.
        merged: set[tuple[int, str]] = set()
        # By group: its levels and meshlet LODs, gathered once.
        coarser: dict[int, tuple[SceneLitLevels | None, SceneLitLods | None]] = {}
        values, animated = self._values, self._viewport.device_materials().keys()
        first: _Gathered | None = None
        objects, sources = [], []
        for mesh_number, (mesh, sessions) in enumerate(zip(meshes, owners, strict=True)):
            made: dict[tuple, tuple[list, list]] = {}
            for batch, session in zip(mesh.batches, sessions, strict=True):
                index = len(self._labels)
                name = batch.material_name
                self._objects.append(mesh.key)
                if session is None:
                    self._labels.append(name or mesh.key)
                    self._failures[index] = self.tr("No MDF material.")
                    continue
                grouped = (session, mesh.speedtree_placement)
                if grouped not in gathered:
                    gathered[grouped] = self._group(session, mesh.speedtree_placement, wanted.get(session, set()),
                                                    scopes, read, streaming, animated)
                group = gathered[grouped]
                self._labels.append(group.names.get(name, name))
                if first is None and not group.reason:
                    first = group
                reason = group.reason or ("" if group.game == first.game else self.tr(
                    "Game lighting draws the meshes of one game build at a time."))
                if batch.submesh is None:
                    self._failures[index] = reason or self.tr("Game lighting draws only the meshes' main geometry.")
                    continue
                reason = reason or self._unread(group, batch)
                if not reason:
                    parts, batches = made.setdefault((grouped, batch.submesh[0]), ([], []))
                    batches.append((batch.part_index, batch.submesh[1]))
                    reason = self._gather_lit_part(session, group, shapes, mesh_number, mesh, batch, masters,
                                                   files, merged, streaming, values if name in animated else None)
                if reason:
                    self._failures[index] = reason
                    continue
                parts.append(SceneLitPart(index, batch.part_index, group.parts[_piece(batch)]))
            for (grouped, lod), (parts, batches) in made.items():
                if parts:
                    group = gathered[grouped]
                    levels = lods = None
                    if not lod:
                        if id(group) not in coarser:
                            coarser[id(group)] = (
                                self._levels(grouped[0], group, masters, files, merged, streaming, values, animated),
                                self._meshlet_lods(grouped[0], group, read, masters, files, merged, streaming, values,
                                                   animated))
                        levels, lods = coarser[id(group)]
                    objects.append(SceneLitObject(mesh_number, group.meshes[lod], tuple(parts), tuple(batches),
                                                  mesh.edge_excluded, mesh.decal_receive_mode, mesh.transparent_bias,
                                                  levels=levels, lods=lods))
                    sources.append((grouped, lod))
        self._read = read
        self._kept = {key: kept for key, kept in self._kept.items() if key in gathered}
        if first is None:
            uncatalogued = str(MaterialPreviewError(NO_LIGHT_PASSES))
            if gathered and all(group.reason == uncatalogued for group in gathered.values()):
                self._viewport.refuse_game_lighting(uncatalogued)
            self._previous = None
            return SceneLitContent(number, "", meshes, (), {}, {})
        profile = mmtr_profile_for_game(first.game)
        chains = pretransform_catalog_for(profile)
        wrinkled, wrinkles_refused = self._gather_wrinkles(objects, sources, meshes, gathered, read, profile, files)
        previous = self._previous if self._previous is not None and self._previous.game == first.game else None
        if previous is not None:
            objects = [_same(item, previous.objects[number]) if number < len(previous.objects) else item
                       for number, item in enumerate(objects)]
        passes = (first.game, any(item.of.level is not None for item in objects),
                  any(item.of.meshlets for item in objects), wrinkled)
        if passes not in self._passes:
            self._passes[passes] = pass_files(profile, light_pass_catalog_for(profile),
                                              chains if passes[1] else None, first.resolve,
                                              meshlet_catalog_for(profile) if passes[2] else None,
                                              wrinkle_catalog_for(profile) if wrinkled else None)
        drawn = dict(self._passes[passes])
        drawn.update(files)
        strands, owner, refused, told = None, None, "", self._viewport.device_strands()
        if told is not None:
            key, handler, shown, edited = told
            owner = next((number for number, (item, ((session, _placement), _lod))
                          in enumerate(zip(objects, sources))
                          if meshes[item.mesh].key == key and session.handler is handler), None)
            if owner is None:
                refused = self.tr("The mesh they are over is not drawn.")
            else:
                (session, placement), lod = sources[owner]
                # Strands need the whole mesh file (bone boxes, mesh groups).
                strands, refused = self._gather_strands(handler, read[id(handler)][2].mesh, first.game, profile,
                                                        shown, lod, objects[owner].of.level,
                                                        gathered[session, placement].resolve, masters, drawn,
                                                        edited)
        if previous is not None and previous.files.holds(drawn):
            previous.files.add(drawn)
            prefetched = previous.files
        else:
            prefetched = PrefetchedFiles(drawn)
        lods_refused = "; ".join(dict.fromkeys(group.lods_refused for group in gathered.values() if group.lods_refused))
        self._previous = SceneLitContent(number, first.game, meshes, tuple(objects), masters, prefetched, strands,
                                         owner, refused, values, wrinkles_refused, lods_refused)
        return self._previous

    def _gather_wrinkles(self, objects: list[SceneLitObject], sources: list, meshes, gathered: dict, read: dict,
                         profile, files: dict) -> tuple[bool, str]:
        told = {item.key: item for item in self._viewport.device_wrinkles()}
        catalog = wrinkle_catalog_for(profile) if told else None
        chains = pretransform_catalog_for(profile) if told else None
        refused, proxies = [], {}
        for number, item in enumerate(objects):
            wrinkle = told.get(meshes[item.mesh].key)
            if wrinkle is None:
                continue
            (session, placement), _lod = sources[number]
            resolve = gathered[session, placement].resolve
            try:
                if wrinkle.refused:
                    raise MaterialPreviewError(wrinkle.refused)
                if catalog is None or chains is None:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "REasy has no catalog of the wrinkle passes of this game build."))
                if not item.of.skinned or id(session.handler) not in read:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The wrinkle passes skin their proxy by the bones of a skinned mesh."))
                if wrinkle.proxy not in proxies:
                    proxies[wrinkle.proxy] = self._wrinkle_proxy(resolve, wrinkle.proxy, chains)
                target = resolve(wrinkle.render_target)
                if target is None:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The render texture file {path} was not found."),
                        {"path": wrinkle.render_target})
                files[wrinkle.render_target] = target[1]
                joints, binds = mesh_joints(read[id(session.handler)][2].mesh)
                objects[number] = replace(item, wrinkle=wrinkle_object(
                    proxies[wrinkle.proxy], joints, binds, wrinkle.render_target, wrinkle.spread))
            except MaterialPreviewError as exc:
                refused.append(str(exc))
        return any(item.wrinkle is not None for item in objects), "; ".join(dict.fromkeys(refused))

    @staticmethod
    def _wrinkle_proxy(resolve, path: str, chains):
        found = resolve(path)
        if found is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The wrinkle proxy mesh {path} was not found."), {"path": path})
        mesh = MeshFile()
        try:
            mesh.read(found[1], file_version=resource_version_from_path(found[0], "mesh"))
        except (ValueError, IndexError, struct.error) as exc:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The wrinkle proxy mesh {path} could not be read: {reason}"),
                {"path": path, "reason": str(exc)}) from exc
        return wrinkle_proxy(mesh, chains)

    def _group(self, session, placement: str | None, lods: set[int], scopes: dict[tuple, dict],
               read: dict[int, tuple[object, object, _MeshRead]], streaming: bool, animated) -> _Gathered:
        """The session's meshes and parts as gathered before, while its mesh file and materials, which of them are
        animated and the texture quality are the same: the drawer keeps what it bound of them."""
        state = (session.profiles, session.handler.mesh, streaming, frozenset(animated & session.profiles.keys()))
        kept = self._kept.get((session, placement))
        if kept is not None and kept[0][0] is state[0] and kept[0][1] is state[1] and kept[0][2:] == state[2:] \
                and lods <= kept[1].meshes.keys():
            group = kept[1]
            if id(session.handler) in self._read:
                read[id(session.handler)] = self._read[id(session.handler)]
            return group
        group = self._session_gathered(session, placement, lods, scopes, read)
        self._kept[session, placement] = state, group
        return group

    def _session_gathered(self, session, placement: str | None, lods: set[int], scopes: dict[tuple, dict],
                          read: dict[int, tuple[object, object, _MeshRead]]) -> _Gathered:
        handler = session.handler
        game = MeshMaterialResolver.material_profile(handler).game
        cache = scopes.setdefault(MeshMaterialResolver.resource_scope(handler), {})
        group = _Gathered(game, "", {session.material_key(binding.mesh_material_name): binding.mesh_material_name
                                     for binding in session.bindings}, placement, self._resolver(handler, cache),
                          cache)
        profile = mmtr_profile_for_game(game)
        flags = handler.mesh.header.decoded_content_flags
        if profile is None or light_pass_catalog_for(profile) is None:
            group.reason = str(MaterialPreviewError(NO_LIGHT_PASSES))
        elif isinstance(handler, StmeshHandler):
            engine = frozendict(stmesh_engine_buffers(handler.stmesh, speedtree_catalog_for(profile),
                                                      placement))
            stencil = stencil_reference(session)
            for lod in lods:
                held = _read_stmesh_lod(handler, lod)
                group.meshes[lod] = held if isinstance(held, str) else SceneLitMesh(
                    False, 1, stencil, held.bounds, None, {}, {}, handler.stmesh.meshlets is not None, engine)
                if not isinstance(held, str):
                    group.lods[lod] = held
        elif flags is None:
            group.reason = str(MaterialPreviewError(UNKNOWN_SKINNING))
        else:
            chains, skinned = pretransform_catalog_for(profile), bool(flags.is_skinning)
            whole = self._read_whole(handler, lods, chains, skinned, read)
            if isinstance(whole, str):
                group.reason = whole
                return group
            first = whole.lods.get(0) if 0 in lods else None
            blob = _meshlet_blob(first if isinstance(first, _Lod) else None)
            # A meshlet mesh's object holds every LOD (``_meshlet_lods``); they share the LOD's buffer.
            if blob is not None and len(blob) > 12 and blob[12] > 1:
                self._read_whole(handler, set(range(blob[12])), chains, skinned, read)
            stencil = stencil_reference(session)
            for lod in lods:
                held = whole.lods[lod]
                group.meshes[lod] = held if isinstance(held, str) else SceneLitMesh(
                    bool(flags.is_skinning), whole.bones, stencil, held.bounds, held.level, held.groups,
                    held.channels, any(isinstance(buffer, MeshletBuffer) for _range, buffer in held.submeshes),
                    box=object_box(whole.mesh))
                if not isinstance(held, str):
                    group.lods[lod] = held
        return group

    def _read_whole(self, handler, lods: set[int], chains, skinned: bool,
                    read: dict[int, tuple[object, object, _MeshRead]]) -> _MeshRead | str:
        held = read.get(id(handler)) or self._read.get(id(handler))
        entry = held[2] if held is not None and held[0] is handler and held[1] is handler.mesh else None
        if entry is None:
            # A handler read lighter parses the file again for each.
            try:
                whole = handler.whole_mesh()
            except ValueError as exc:
                return str(exc)
            if whole is None:
                return self.tr("The mesh file {path} could not be read whole.").format(path=handler.filepath)
            entry = _MeshRead(whole, max(int(whole.joint_count), int(whole.bone_remap_count), 1))
        for lod in lods - entry.lods.keys():
            try:
                entry.lods[lod] = _read_lod(entry.mesh, lod, chains, skinned, entry)
            except MaterialPreviewError as exc:
                entry.lods[lod] = str(exc)
        read[id(handler)] = handler, handler.mesh, entry
        return entry

    def _unread(self, group: _Gathered, batch) -> str:
        lod, number = batch.submesh
        if isinstance(group.meshes[lod], str):
            return group.meshes[lod]
        if number + batch.submeshes > len(group.lods[lod].submeshes):
            return self.tr("The mesh file read again is not the one shown.")
        return ""

    def _gather_lit_part(self, session, group: _Gathered, shapes: Sequence, number_shown: int, mesh: SceneGeometry,
                         batch, masters: dict, files: dict, merged: set, streaming: bool,
                         values: MaterialValues | None) -> str:
        made = self._made_part(session, group, batch.submesh, batch.submeshes, batch.material_name, masters, files,
                               merged, streaming, values, lambda: None if mesh.uvs is None else self._density(
                                   shapes[number_shown].positions, (mesh.uvs, *mesh.more_uvs), batch.indices))
        return made if isinstance(made, str) else ""

    def _made_part(self, session, group: _Gathered, submesh: tuple[int, int], count: int, name: str, masters: dict,
                   files: dict, merged: set, streaming: bool, values: MaterialValues | None,
                   density: Callable[[], float | None]) -> LitPart | str:
        """The part of ``count`` submeshes from (LOD, number) the group's mesh draws with the material ``name``, or
        why it can't be drawn."""
        lod, number = submesh
        surface = session.profiles.get(name)
        if surface is None or surface.instance_context is None:
            return self.tr("No MDF material.")
        if name not in group.unusable:
            drawn_masters, drawn_files = group.drawn[name] = {}, {}
            group.unusable[name] = self._gather_drawn(
                session.handler, surface, group.resolve, drawn_masters, drawn_files, streaming, group.cache)
        drawn_masters, drawn_files = group.drawn[name]
        if group.unusable[name]:
            return group.unusable[name]
        if (id(group), name) not in merged:
            reason = self._merged(masters, drawn_masters, files, drawn_files)
            if reason:
                return reason
            merged.add((id(group), name))
        piece = (lod, number, count)
        if piece not in group.parts:
            ranged, buffer = _run(group.lods[lod].submeshes[number:number + count])
            route = programs = None
            handler = session.handler
            if isinstance(handler, StmeshHandler):
                speedtree = speedtree_catalog_for(mmtr_profile_for_game(group.game))
                viewed = mesh_lod_parts(handler.mesh, lod)[number][1]
                try:
                    if handler.stmesh.meshlets is not None:
                        _buffer, _range, programs = drawn_chunks(handler, lod, viewed, int(surface.material_flags),
                                                                 speedtree, group.placement)
                    else:
                        _draw, route = drawn_cluster(handler, lod, viewed, int(surface.material_flags), speedtree,
                                                     group.placement)
                except (MaterialPreviewError, PassChoiceError) as exc:
                    return str(exc)
            # The shown mesh's triangles of the part: the meshes of a group share them.
            group.parts[piece] = LitPart(number, name, surface.mmtr_path, surface.instance_context,
                                         preview_material(surface, None if values is None else
                                                          partial(values.of, name), group.cache),
                                         buffer, ranged, route, programs, density(), lod)
        return group.parts[piece]

    def _coarser_parts(self, session, group: _Gathered, pieces: Sequence[tuple[int, int, int, str]], masters: dict,
                       files: dict, merged: set, streaming: bool, values: MaterialValues | None,
                       animated) -> list[LitPart] | str:
        """The parts of a mesh's LODs after its first, each (LOD, number, count, material), or why one can't be drawn.
        Gathered every time, so a kept group's masters and files are merged again. A coarser LOD's part takes the least
        texel density of its material's first-LOD parts."""
        densities = _first_densities(group)
        parts = []
        for lod, number, count, name in pieces:
            made = self._made_part(session, group, (lod, number), count, name, masters, files, merged, streaming,
                                   values if name in animated else None, partial(densities.get, name))
            if isinstance(made, str):
                return made
            parts.append(made)
        return parts

    def _levels(self, session, group: _Gathered, masters: dict, files: dict, merged: set, streaming: bool,
                values: MaterialValues, animated) -> SceneLitLevels | None:
        """A SpeedTree mesh's LODs after its first, read once a group; None for another mesh, a mesh of one LOD, or,
        telling why in ``group.lods_refused``, one whose other LODs can't be drawn."""
        if not group.levels_made:
            group.levels_made = True
            group.level_pieces = self._level_pieces(session, group)
        held = group.level_pieces
        group.lods_refused = held if isinstance(held, str) else ""
        if held is None or isinstance(held, str):
            return None
        bounds, pieces, distances = held
        parts = self._coarser_parts(session, group, pieces, masters, files, merged, streaming, values, animated)
        if isinstance(parts, str):
            group.lods_refused = parts
            return None
        if group.levels is None:
            lods: dict[int, list[LitPart]] = {lod: [] for lod in bounds}
            for (lod, _number, _count, _name), part in zip(pieces, parts):
                lods[lod].append(part)
            first = group.meshes[0]
            group.levels = SceneLitLevels(tuple((replace(first, bounds=bounds[lod]), tuple(held))
                                                for lod, held in lods.items()), *distances)
        return group.levels

    def _level_pieces(self, session, group: _Gathered) -> tuple[dict, tuple, tuple] | str | None:
        """The bounds of each LOD after the first, its parts as ``_coarser_parts`` takes them, and the distances, or
        why they can't be drawn. A meshlet mesh's LOD is the one instance culling picks from its blob's distances."""
        handler = session.handler
        if not isinstance(handler, StmeshHandler) or handler.stmesh.lod_count < 2 or 0 not in group.lods:
            return None
        if handler.stmesh.meshlets is None:
            if not handler.stmesh.lod_profile.lod_is_present:
                return None
            profile = handler.stmesh.lod_profile
            # Halfway through the billboard's fade in.
            billboard = profile.billboard_start_distance + profile.billboard_range / 2 \
                if handler.stmesh.flags & FLAG_BILLBOARD_LOD else np.inf
            cull = profile.billboard_cull_distance if profile.billboard_cull_distance > 0 else np.inf
            distances = (profile.high_detail_3d_distance, profile.low_detail_3d_distance, float(billboard),
                         float(cull))
        else:
            blob, catalog = _meshlet_blob(group.lods[0]), meshlet_catalog_for(mmtr_profile_for_game(group.game))
            if blob is None or catalog is None:
                return None
            try:
                meshlet = blob_lods(blob, True)
            except MaterialPreviewError as exc:
                return str(exc)
            if meshlet.count != handler.stmesh.lod_count:
                return self.tr("The meshlet mesh's header gives {count} LODs, and its file holds {held}.").format(
                    count=meshlet.count, held=handler.stmesh.lod_count)
            distances = (np.inf, np.inf, np.inf, np.inf, catalog.lods, meshlet)
        names, bounds, pieces = handler.mesh.material_names, {}, []
        for lod in range(1, handler.stmesh.lod_count):
            held = group.lods.get(lod)
            if held is None:
                held = _read_stmesh_lod(handler, lod)
                if isinstance(held, str):
                    return held
                group.lods[lod] = held
            bounds[lod] = held.bounds
            for number, (_index, submesh) in enumerate(mesh_lod_parts(handler.mesh, lod)):
                material = submesh.material_index
                pieces.append((lod, number, 1, session.material_key(names[material] if 0 <= material < len(names)
                                                                    else "")))
        return bounds, tuple(pieces), distances

    def _meshlet_lods(self, session, group: _Gathered, read: dict[int, tuple[object, object, _MeshRead]],
                      masters: dict, files: dict, merged: set, streaming: bool, values: MaterialValues,
                      animated) -> SceneLitLods | None:
        """A meshlet ``.mesh``'s LODs after its first, read once a group; None for another mesh (a meshlet
        ``.stmesh``'s are ``_levels``), a mesh of one LOD, or, telling why in ``group.lods_refused``, one whose other
        LODs can't be drawn."""
        if isinstance(session.handler, StmeshHandler):
            return None
        if not group.meshlet_lods_made:
            group.meshlet_lods_made = True
            blob = _meshlet_blob(group.lods.get(0))
            if not (blob is None or len(blob) > 12 and blob[12] < 2):
                group.meshlet_pieces = self._meshlet_pieces(session, group, blob, read)
        held = group.meshlet_pieces
        group.lods_refused = held if isinstance(held, str) else ""
        if held is None or isinstance(held, str):
            return None
        lods, pieces = held
        parts = self._coarser_parts(session, group, [piece[:4] for piece in pieces], masters, files, merged,
                                    streaming, values, animated)
        if isinstance(parts, str):
            group.lods_refused = parts
            return None
        if group.meshlet_lods is None:
            group.meshlet_lods = SceneLitLods(tuple((lod, piece, part) for (lod, _number, _count, _name, piece), part
                                                    in zip(pieces, parts)), lods)
        return group.meshlet_lods

    def _meshlet_pieces(self, session, group: _Gathered, blob: bytes,
                        read: dict[int, tuple[object, object, _MeshRead]]) -> tuple[MeshletLods, tuple] | str:
        """The mesh's ``MeshletLods`` and the parts of its LODs after the first, each (LOD, number, count, material,
        piece), or why they can't be drawn."""
        try:
            count = blob_lods(blob, False).count
            catalog = meshlet_catalog_for(mmtr_profile_for_game(group.game))
            if catalog is None:
                # No meshlet mesh of the build draws.
                return ""
            rule = catalog.lods
            settings = self._lod_settings_for(session, group, rule)
            if isinstance(settings, str):
                return settings
            # The thresholds are written as the mesh loads.
            whole = read[id(session.handler)][2]
            header = whole.mesh.header
            lods = blob_lods(blob, False, meshlet_thresholds(settings, rule.thresholds, rule.gate,
                                                             int(header.lod_hash), count, header.lod_flags,
                                                             blob_halves(blob)))
        except MaterialPreviewError as exc:
            return str(exc)
        mesh = whole.mesh
        names, pieces = mesh.material_names, []
        # The renderer draws a coarser LOD's part only with its first LOD's parts of its material (``MeshScene``).
        first = {int(submesh.material_index) for _piece, submesh in mesh_lod_parts(mesh, 0)
                 if int(submesh.indices_count)}
        for lod in range(1, lods.count):
            held = whole.lods[lod]
            if isinstance(held, str):
                return held
            group.lods[lod] = held
            parts = mesh_lod_parts(mesh, lod)
            # Its parts are its runs of submeshes (``meshlet_part_runs``).
            at, before = 0, len(pieces)
            for run in meshlet_part_runs(mesh, lod) or []:
                piece, submesh = parts[at]
                if int(submesh.indices_count):
                    material = int(submesh.material_index)
                    if material not in first:
                        return self.tr("A coarser LOD of the meshlet mesh draws a material its first LOD does not, and "
                                       "the preview draws a coarser LOD only with its first LOD's materials.")
                    pieces.append((lod, at, run, session.material_key(
                        names[material] if 0 <= material < len(names) else ""), piece))
                at += run
            # The game would draw nothing of the object at that LOD.
            if len(pieces) == before:
                return self.tr("The meshlet mesh's header gives {count} LODs, and its file holds no triangles of LOD "
                               "{lod}.").format(count=lods.count, lod=lod)
        return lods, tuple(pieces)

    def _lod_settings_for(self, session, group: _Gathered, rule: MeshletLodRule) -> LodSettings | str:
        """The build's LodSettings file as the group's resource scope finds it, or why it can't: looked for once a
        scope, through its first group's resolver, and read once for the bytes found."""
        scope = group.game, MeshMaterialResolver.resource_scope(session.handler)
        if scope not in self._lod_settings:
            path = rule.file()
            found = group.resolve(path)
            if found is None:
                self._lod_settings[scope] = self.tr("The LOD settings file {path} was not found.").format(path=path)
                return self._lod_settings[scope]
            data = bytes(found[1])
            if data not in self._lod_settings:
                try:
                    self._lod_settings[data] = read_lod_settings(data)
                except LodSettingsError as exc:
                    self._lod_settings[data] = self.tr(
                        "The LOD settings file {path} could not be read: {reason}").format(path=path, reason=str(exc))
            self._lod_settings[scope] = self._lod_settings[data]
        return self._lod_settings[scope]

    def _density(self, positions: np.ndarray, uv_sets: Sequence[np.ndarray], indices: np.ndarray) -> float | None:
        key = id(positions), id(indices)
        if key not in self._densities:
            self._densities[key] = uv_density(positions, uv_sets, indices)
        return self._densities[key]

    def _merged(self, masters: dict, more_masters: dict, files: dict, more_files: dict) -> str:
        for held, more in ((masters, more_masters), (files, more_files)):
            clash = next((path for path, value in more.items() if held.get(path, value) != value), None)
            if clash is not None:
                return self.tr("Another mesh of the scene reads another file by the path {path}.").format(path=clash)
        masters.update(more_masters)
        files.update(more_files)
        return ""


class SceneDevicePreview(QObject):
    status_changed = Signal()

    def __init__(self, viewport, materials, parent=None):
        from file_handlers.mmtr.resident import warm_up

        super().__init__(parent)
        # Both lighting modes draw on the hardware device.
        warm_up()
        self._viewport = viewport
        self._active = False
        self._textures = SceneTexturesPreview(viewport, materials, self)
        self._lit = SceneLitPreview(viewport, materials, self)
        self._logged: set[tuple[str, str, str]] = set()
        # The previews' failed parts logged, by id (held, so ids stay theirs); the last refused with their keys.
        self._logged_from: dict[int, list] = {}
        self._refused: list[tuple[list, list, set[str]]] = []
        for preview in (self._textures, self._lit):
            preview.status_changed.connect(self._log_failures)
            preview.status_changed.connect(self.status_changed)
        # Handled after the previews get the view: its lighting picks which one draws.
        viewport.device_view_changed.connect(self._pick, Qt.ConnectionType.QueuedConnection)
        viewport.offer_game_lighting()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._pick()

    @property
    def active(self) -> bool:
        return self._active

    def hold(self, held: bool) -> None:
        for preview in (self._textures, self._lit):
            preview.hold(held)

    def status(self) -> list[str]:
        refusal = self._viewport.game_lighting_refusal
        told = [self.tr("{reason} The meshes are drawn with their textures.").format(reason=refusal)] if refusal else []
        return [*told, *self._textures.status(), *self._lit.status()]

    def refused(self) -> set[str]:
        textures, lit = self._textures.failed_parts(), self._lit.failed_parts()
        for held_textures, held_lit, keys in self._refused:
            if held_textures is textures and held_lit is lit:
                return keys
        keys = {key for key, _label, _reason in (*textures, *lit)}
        # Each preview keeps two lists of its failed parts.
        self._refused = [(textures, lit, keys), *self._refused[:3]]
        return keys

    def _log_failures(self) -> None:
        told = [parts for parts in (self._textures.failed_parts(), self._lit.failed_parts())
                if id(parts) not in self._logged_from]
        if len(self._logged_from) > 16:
            self._logged_from.clear()
        self._logged_from.update((id(parts), parts) for parts in told)
        for failed in (failed for parts in told for failed in parts):
            if failed not in self._logged:
                self._logged.add(failed)
                key, label, reason = failed
                print(f"Scene [warning] not_drawn: {label}: {reason} {key}")

    def strands_changed(self) -> None:
        self._lit.scene_changed()

    def _pick(self, *_told) -> None:
        game = self._viewport.lighting_mode == "game"
        shown, other = (self._lit, self._textures) if game else (self._textures, self._lit)
        other.set_active(False)
        shown.set_active(self._active)


__all__ = ["MaterialValues", "SceneDevicePreview", "SceneLitContent", "SceneLitDrawer", "SceneLitFrame", "SceneLitLods",
           "SceneLitMesh", "SceneLitObject", "SceneLitPart", "SceneLitPreview"]
