from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path

lazy import numpy as np

lazy from file_handlers.engine_math import (
    chain_rotation, chain_world, engine_scale, joint_descriptor, make_trs_matrix, mathex_rotation, quaternion_product,
    transform_product,
)
lazy from file_handlers.fol.fol_file import FolFile, FolTransform
lazy from file_handlers.jcns import JcnsFormatError, read_jcns
lazy from file_handlers.mesh.mesh_file import MeshFile
lazy from file_handlers.mmtr.draw_rules import MESH_COMPONENT, MESH_DECAL_RECEIVE, draw_rules_for
lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.mmtr.user_render_targets import user_render_target_catalog_for
lazy from file_handlers.mmtr.primitives import primitive_catalog_for
lazy from file_handlers.mmtr.shallow_water import shallow_water_catalog_for
lazy from file_handlers.mmtr.volume_decals import volume_decal_catalog_for
lazy from file_handlers.mmtr.water_meshes import water_mesh_catalog_for
lazy from file_handlers.rsz.plain_values import plain_fields, plain_value, root_fields
lazy from file_handlers.rsz.rsz_data_types import (
    ArrayData,
    BoolData,
    Float4Data,
    OBBData,
    ObjectData,
    PositionData,
    RangeIData,
    ResourceData,
    StringData,
    StructData,
)
lazy from file_handlers.rsz.rsz_file import RszFile
lazy from file_handlers.rsz.utils.rsz_instance_defaults import type_table
lazy from file_handlers.sdf.profiles.enum_tables import decal_receive_modes
lazy from utils.enum_manager import EnumDefinitionError, game_enum_definitions
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from file_handlers.stmesh.stmesh_file import COMPONENT, FOLIAGE
lazy from utils.resource_file_utils import normalize_resource_path, streaming_companion_path
lazy from .scn_scene_adapters import TransformAdapter, FolderLinkAdapter, CompositeMeshAdapter, MeshAdapter
lazy from .scene_joints import JointSet, skeleton_joint_descs
lazy from .scene_startup import StartupDraw, apply_startup_transforms, simple_body, start_bones, startup_draw_requests
lazy from .spawn_rules import SpawnChoice, spawners
lazy from .spawned_parts import NOT_SPAWNED, MaterialVariation, SpawnedMesh, spawn_parts
from .scene_assembly import ScnSceneAssembly
from .part_swapper import ScnPartSwapper
from .scn_scene_folders import ScnSceneFolders
from .scn_scene_references import ScnSceneReferences
from .scn_scene_timelines import ScnSceneTimelines
lazy from .scene_time import (
    CurveAnimation, LightBlink, LightRise, RandomRule, SimpleAnimation, TimelineAnimation, TimelineKeys,
)
lazy from .campaign_state import (
    CampaignCatalog, CampaignChoice, CampaignState, ScenarioFlags, campaign_marker, campaign_state, read_catalog,
    read_scenario_flags,
)


VIA_FOLDER = "via.Folder"
VIA_TRANSFORM = "via.Transform"
VIA_RENDER_MESH = MESH_COMPONENT
VIA_RENDER_BLEND_SHAPE_MESH_REBINDER = "via.render.BlendShapeMeshRebinder"
VIA_RENDER_COMPOSITE_MESH = "via.render.CompositeMesh"
VIA_RENDER_COMPOSITE_MESH_INSTANCE_GROUP = "via.render.CompositeMeshInstanceGroup"
VIA_RENDER_COMPOSITE_MESH_TRANSFORM_CONTROLLER = "via.render.CompositeMeshTransformController"
VIA_RENDER_LIGHT_PROBES = "via.render.LightProbes"
VIA_LANDSCAPE_FOLIAGE = "via.landscape.Foliage"
VIA_SPEEDTREE_V8 = "via.speedtree.SpeedTreeV8"
VIA_MOTION_SKELETONS = frozenset({"via.motion.DummySkeleton", "via.motion.CustomSkeleton"})
FOLDER_OPTIONS_RESOURCE = "resources/data/scene_folder_options.json"
SCENE_FIELDS_RESOURCE = "resources/data/scene_fields.json"
JOINT_CONSTRAINTS_RESOURCE = "resources/data/joint_constraints.json"
LIGHT_PROBE_PREVIEW_GAMES = frozenset({
    "DD2",
    "DMC5",
    "KunitsuGami",
    "MHRise",
    "MHST3",
    "MHWilds",
    "O2",
    "OnimushaWOTS",
    "Pragmata",
    "RE2",
    "RE2RT",
    "RE3",
    "RE3RT",
    "RE4",
    "RE7RT",
    "RE8",
    "RE9",
    "SF6",
})


@dataclass(frozen=True, slots=True)
class ScnObjectId:
    document_id: str
    local_object_id: int


@dataclass(frozen=True, slots=True)
class ScnComponentId:
    document_id: str
    instance_id: int


@dataclass(slots=True)
class ScnTransform:
    position: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    scale: tuple[float, float, float]
    local_matrix: np.ndarray
    parent_joint: str = ""
    same_joints_constraint: bool = False
    absolute_scaling: bool = False
    joint_segment_scale: bool = False


@dataclass(frozen=True, slots=True)
class ScnJointConstraints:
    path: str
    model: object | None
    blend_rate: float = 1.0
    paused: bool = False
    reason: str = ""
    timing: int | str = 1


@dataclass(slots=True)
class ScnComponent:
    id: ScnComponentId
    owner: ScnObjectId | None
    object_table_id: int | None
    type_id: int
    type_name: str
    fields: Mapping[str, object]


@dataclass(slots=True)
class ScnSceneObject:
    id: ScnObjectId
    kind: str
    instance_id: int
    parent_id: int
    name: str
    type_id: int
    type_name: str
    fields: Mapping[str, object]
    components: list[ScnComponentId] = field(default_factory=list)
    transform_component: ScnComponentId | None = None
    mesh_component: ScnComponentId | None = None
    transform: ScnTransform | None = None
    document_world_matrix: np.ndarray = field(default_factory=lambda: np.identity(4, dtype=np.float32))
    world_matrix: np.ndarray = field(default_factory=lambda: np.identity(4, dtype=np.float32))
    draw_self: bool = True
    document_world_rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)


@dataclass(slots=True)
class ScnFolderReference:
    source_folder_id: ScnObjectId
    linked_path: str
    folder_offset: tuple[float, float, float] | None
    child_draw: tuple[tuple[str, bool], ...] = ()
    spawner: ScnComponentId | None = None
    parts_off: tuple[int, ...] = ()
    materials: tuple[tuple, ...] = ()
    name: str = ""


@dataclass(frozen=True, slots=True)
class ScnForeignRequest:
    guid: bytes
    layer: int
    draw: bool
    phase: str
    descendants: bool = False
    unless_name: str | None = None
    curves: bool = False


@dataclass(frozen=True, slots=True)
class ScnTimelineNode:
    guid: bytes
    name: str
    tracks: tuple[tuple[str, str, tuple[TimelineKeys | None, ...]], ...]


@dataclass(frozen=True, slots=True)
class ScnTimeline:
    owner: ScnObjectId
    clip: str
    start: float
    end: float
    speed: float
    event: bool
    fps: float
    scale: float
    nodes: tuple[ScnTimelineNode, ...]
    first_back: float
    zero_back: float


@dataclass(frozen=True, slots=True)
class ScnCameraSwitch:
    center: tuple[float, float, float]
    show: float
    no_show: float
    frustum: bool
    frustum_center: tuple[float, float, float]
    axis_z: tuple[float, float, float]
    inside: frozenset[str]
    far: frozenset[str]
    inside_folder: tuple[str, ScnObjectId] | None = None
    far_folder: tuple[str, ScnObjectId] | None = None
    inside_objects: frozenset[ScnObjectId] = frozenset()
    far_objects: frozenset[ScnObjectId] = frozenset()


@dataclass(frozen=True, slots=True)
class ScnCameraArea:
    """Boxes are (axes as scaled rows, centre, half extents)."""
    boxes: tuple[tuple[tuple[tuple[float, float, float], ...], tuple[float, float, float],
                       tuple[float, float, float]], ...]
    radius: float
    others: bool
    inside: frozenset[str]
    inside_folder: tuple[str, ScnObjectId] | None = None
    far: frozenset[str] = frozenset()
    far_folder: tuple[str, ScnObjectId] | None = None
    inside_objects: frozenset[ScnObjectId] = frozenset()
    far_objects: frozenset[ScnObjectId] = frozenset()


@dataclass(frozen=True, slots=True, eq=False)
class ScnCameraRanges:
    positions: np.ndarray
    initially: np.ndarray
    keys: tuple[frozenset[str], ...]
    far: float
    near: float
    cone: float
    turn: float


def camera_ranges_shown(ranges: ScnCameraRanges, view) -> np.ndarray:
    """In float32; ``view``: world to view 3 x 4, looking down -z."""
    view = np.asarray(view, np.float32).reshape(3, 4)
    local = ranges.positions @ view[:, :3].T + view[:, 3]
    turn = np.float32(ranges.turn)
    cos, sin = np.cos(turn), np.sin(turn)
    x = cos * local[:, 0] - sin * local[:, 2]
    y = local[:, 1]
    z = sin * local[:, 0] + cos * local[:, 2]
    squared = y * y + x * x + z * z
    within = ((np.abs(np.arctan2(x, z)).astype(np.float64) < ranges.cone)
              & (np.abs(np.arctan2(y, z)).astype(np.float64) < ranges.cone))
    return (squared < np.float32(ranges.far)) & ((squared <= np.float32(ranges.near)) | within)


def camera_shows(switch: ScnCameraSwitch | ScnCameraArea, view, scale, orthographic: bool = False) -> bool | None:
    """Whether a camera switch or area draws its inside folder and hides its far one; ``view``: world to view 3 x 4,
    looking down -z, ``scale``: the projection's x and y scales; None: no change."""
    view = np.asarray(view, np.float64).reshape(3, 4)
    rotation, translation = view[:, :3], view[:, 3]
    if isinstance(switch, ScnCameraArea):
        eye = -rotation.T @ translation
        for axes, centre, half in switch.boxes:
            axes = np.asarray(axes, np.float64)
            local = np.linalg.solve(axes.T, eye - np.asarray(centre, np.float64))
            closest = np.asarray(centre, np.float64) + axes.T @ np.clip(local, -np.asarray(half), np.asarray(half))
            if float(np.linalg.norm(eye - closest)) <= switch.radius:
                return True
        return None if switch.others else False
    offset = -rotation.T @ translation - np.asarray(switch.center, np.float64)
    distance = float(np.float32(offset @ offset))
    if distance >= switch.no_show:
        return False
    if distance < switch.show:
        return True
    if not switch.frustum or orthographic:
        return None
    if float(-rotation[2] @ np.asarray(switch.axis_z, np.float64)) > 0.0:
        return False
    point = rotation @ np.asarray(switch.frustum_center, np.float64) + translation
    depth = float(-point[2])
    if not depth:
        return False
    x, y = float(scale[0] * point[0] / depth), float(scale[1] * point[1] / depth)
    return -1.0 <= x <= 1.0 and -1.0 <= y <= 1.0


@dataclass(slots=True)
class ScnSceneDocument:
    document_id: str
    source_path: str
    rsz_file: object
    objects: OrderedDict[ScnObjectId, ScnSceneObject] = field(default_factory=OrderedDict)
    components: OrderedDict[ScnComponentId, ScnComponent] = field(default_factory=OrderedDict)
    object_by_local_id: dict[int, ScnObjectId] = field(default_factory=dict)
    folder_references: list[ScnFolderReference] = field(default_factory=list)
    startup_materials: dict[ScnObjectId, tuple[dict[int, Mapping], tuple[Mapping, ...]]] = field(default_factory=dict)
    startup_materials_refused: int = 0
    startup_materials_base: dict[ScnObjectId, tuple[dict[int, Mapping], tuple[Mapping, ...]]] = field(
        default_factory=dict)
    startup_curve_writers: dict[ScnObjectId, frozenset[ScnObjectId]] = field(default_factory=dict)
    startup_time_animations: tuple[CurveAnimation | LightRise | LightBlink | SimpleAnimation, ...] = ()
    startup_curve_animations_refused: int = 0
    startup_simple_refused: tuple[tuple[ScnObjectId, str], ...] = ()
    startup_timelines: tuple[ScnTimeline, ...] = ()
    startup_timelines_refused: tuple[tuple[ScnObjectId, str], ...] = ()
    startup_layers: dict[ScnObjectId, dict[int, tuple[bool, str]]] = field(default_factory=dict)
    startup_foreign: tuple[ScnForeignRequest, ...] = ()
    startup_transforms: tuple[tuple[ScnObjectId, Mapping], ...] = ()
    startup_parents: dict[ScnObjectId, ScnObjectId | None] = field(default_factory=dict)
    startup_rooted: set[ScnObjectId] = field(default_factory=set)
    startup_posed: set[ScnObjectId] = field(default_factory=set)
    startup_meshes: dict[ScnObjectId, dict[int, bool]] = field(default_factory=dict)
    startup_parts: dict[ScnObjectId, dict[int, bool]] = field(default_factory=dict)
    slotted: dict[ScnComponentId, SpawnedMesh] = field(default_factory=dict)
    joints: dict[ScnObjectId, ScnJoints | None] = field(default_factory=dict)
    constraints: dict[ScnObjectId, tuple[ScnJointConstraints, ...]] = field(default_factory=dict)
    copies: dict[ScnObjectId, ScnObjectId] = field(default_factory=dict)
    resources: dict[ScnComponentId, dict[str, str | tuple[str, ...]]] = field(default_factory=dict)
    created: dict[ScnObjectId, dict[str, dict[str, str]]] = field(default_factory=dict)


@dataclass(slots=True)
class ScnDocumentInstance:
    instance_id: str
    document_id: str
    parent_instance_id: str | None
    source_link_index: int | None
    include_chain: tuple[str, ...]
    base_world_matrix: np.ndarray
    drawn: bool = True


@dataclass(slots=True)
class ScnSceneLink:
    source_document_instance_id: str
    source_folder_id: ScnObjectId
    linked_path: str
    resolved_document_id: str
    resolved_path: str
    include_chain: tuple[str, ...]
    folder_offset: tuple[float, float, float] | None = None
    parts_off: tuple[int, ...] = ()
    spawner: ScnComponentId | None = None
    name: str = ""


@dataclass(slots=True)
class ScnRenderableMesh:
    document_instance_id: str
    source_object_id: ScnObjectId
    source_component_id: ScnComponentId
    mesh_path: str
    mdf_path: str
    document_world_matrix: np.ndarray
    world_matrix: np.ndarray
    source_kind: str = "mesh"
    source_group_instance_id: int | None = None
    source_transform_instance_id: int | None = None
    visible_by_default: bool = True
    enabled_parts: tuple[bool, ...] | None = None
    normal_recalculation_enabled: bool = True
    normal_recalc_edge_exclusion: bool = False
    skin_weight_source: str = "primary"
    decal_receive: tuple[str, str | None] = MESH_DECAL_RECEIVE
    # ``via.render.Mesh``'s ``TransparentBias``; 0 is its default.
    transparent_bias: float = 0.0
    # One of ``stmesh_file.PLACEMENTS``.
    speedtree_placement: str | None = None
    blend_shape_normal: bool = True
    blend_shape_real_time_lod: bool = False
    draw_state: Mapping[str, object] | None = None
    lod_parent_mesh: bool = False
    start_pose: np.ndarray | None = None
    mdf_material: int | None = None
    drawn_group: int | None = None

    @property
    def key(self) -> str:
        return (
            f"{self.document_instance_id}:"
            f"{self.source_object_id.document_id}:"
            f"{self.source_object_id.local_object_id}:"
            f"{self.source_component_id.instance_id}:"
            f"{self.source_kind}:"
            f"{self.source_group_instance_id or 0}:"
            f"{self.source_transform_instance_id or 0}"
        )


@dataclass(eq=False, slots=True)
class ScnOrientedBox:
    field_data: OBBData
    axes: tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
    center: tuple[float, float, float]
    extent: tuple[float, float, float]

    def corners(self) -> np.ndarray:
        center = np.asarray(self.center, dtype=np.float32)
        axes = np.asarray(self.axes, dtype=np.float32).reshape(3, 3)
        extent = np.asarray(self.extent, dtype=np.float32)
        x, y, z = axes * extent[:, np.newaxis]
        return np.asarray(
            (
                center + x + y + z,
                center + x + y - z,
                center + x - y - z,
                center + x - y + z,
                center - x + y + z,
                center - x + y - z,
                center - x - y - z,
                center - x - y + z,
            ),
            dtype=np.float32,
        )

    def is_default_unit_box(self) -> bool:
        return (
            np.allclose(np.asarray(self.axes, dtype=np.float32), np.identity(3, dtype=np.float32))
            and np.allclose(np.asarray(self.center, dtype=np.float32), np.zeros(3, dtype=np.float32))
            and np.allclose(np.asarray(self.extent, dtype=np.float32), np.ones(3, dtype=np.float32))
        )

    def matrix(self) -> np.ndarray:
        matrix = np.identity(4, dtype=np.float32)
        axes = np.asarray(self.axes, dtype=np.float32).reshape(3, 3)
        extent = np.asarray(self.extent, dtype=np.float32).reshape(3)
        matrix[:3, :3] = (axes * extent[:, np.newaxis]).T
        matrix[:3, 3] = np.asarray(self.center, dtype=np.float32)
        return matrix

    def set_from_matrix(self, matrix: np.ndarray) -> None:
        matrix = np.asarray(matrix, dtype=np.float32).reshape(4, 4)
        columns = matrix[:3, :3].T
        lengths = np.linalg.norm(columns, axis=1)
        axes = np.zeros((3, 3), dtype=np.float32)
        np.divide(columns, lengths[:, np.newaxis], out=axes, where=lengths[:, np.newaxis] > 1e-6)
        fallback_axes = np.asarray(self.axes, dtype=np.float32).reshape(3, 3)
        fallback_extent = np.asarray(self.extent, dtype=np.float32).reshape(3)
        invalid = ~np.isfinite(axes).all(axis=1) | (lengths <= 1e-6)
        if np.any(invalid):
            axes[invalid] = fallback_axes[invalid]
            lengths[invalid] = fallback_extent[invalid]
        self.axes = tuple(tuple(float(component) for component in row) for row in axes)
        self.center = tuple(float(component) for component in matrix[:3, 3])
        self.extent = tuple(float(max(0.0, component)) for component in lengths)

    def write_field(self) -> None:
        raw = np.asarray(self.field_data.values, dtype=np.float32).reshape(5, 4)
        raw[:3, :3] = np.asarray(self.axes, dtype=np.float32).reshape(3, 3)
        raw[3, :3] = np.asarray(self.center, dtype=np.float32).reshape(3)
        raw[4, :3] = np.asarray(self.extent, dtype=np.float32).reshape(3)
        self.field_data.values = [float(value) for value in raw.reshape(-1)]


def _component_key(item, kind: str) -> str:
    return (f"{item.document_instance_id}:{item.source_object_id.document_id}:"
            f"{item.source_object_id.local_object_id}:{item.source_component_id.instance_id}:{kind}")


@dataclass(eq=False, slots=True)
class ScnLightProbeBinding:
    document_instance_id: str
    source_object_id: ScnObjectId
    source_component_id: ScnComponentId
    lprb_path: str
    prb_path: str
    obbs: list[ScnOrientedBox] = field(default_factory=list)
    priority: int = 0
    intensity: float = 1.0

    @property
    def key(self) -> str:
        return _component_key(self, "light_probes")


@dataclass(eq=False, slots=True)
class ScnVolumeDecal:
    document_instance_id: str
    source_object_id: ScnObjectId
    source_component_id: ScnComponentId
    world_matrix: np.ndarray
    fields: Mapping[str, object]
    visible_by_default: bool = True

    @property
    def key(self) -> str:
        return _component_key(self, "volume_decal")


@dataclass(eq=False, slots=True)
class ScnPrimitive:
    document_instance_id: str
    source_object_id: ScnObjectId
    source_component_id: ScnComponentId
    world_matrix: np.ndarray
    fields: Mapping[str, object]
    visible_by_default: bool = True

    @property
    def key(self) -> str:
        return _component_key(self, "primitive")


@dataclass(eq=False, slots=True)
class ScnRenderTargetModifier:
    document_instance_id: str
    source_object_id: ScnObjectId
    source_component_id: ScnComponentId
    fields: Mapping[str, object]
    visible_by_default: bool = True

    @property
    def key(self) -> str:
        return _component_key(self, "render_target")


@dataclass(eq=False, slots=True)
class ScnShallowWaterFluid:
    """``rotation`` (x, y, z, w) is None where a placement turns it, which REasy does not chain; ``scale`` as
    Transform::getScale gives it."""
    world_matrix: np.ndarray
    rotation: tuple[float, float, float, float] | None
    scale: tuple[float, float, float]
    resolution: tuple[int, int]
    drawn: bool


@dataclass(eq=False, slots=True)
class ScnShallowWater:
    document_instance_id: str
    source_object_id: ScnObjectId
    source_component_id: ScnComponentId
    component: str
    world_matrix: np.ndarray
    scale: tuple[float, float, float]
    point_world: np.ndarray | None
    point_scale: tuple[float, float, float] | None
    point_missing: bool
    fields: Mapping[str, object]
    visible_by_default: bool = True
    controller: str | None = None
    fluid: ScnShallowWaterFluid | None = None
    fluid_children: int = 0

    @property
    def key(self) -> str:
        return _component_key(self, "shallow_water")


@dataclass(slots=True)
class ScnSceneDiagnostic:
    severity: str
    code: str
    message: str
    document_id: str = ""
    document_instance_id: str = ""
    object_id: ScnObjectId | None = None
    component_id: ScnComponentId | None = None
    path: str = ""


@dataclass(slots=True)
class ScnResolvedResource:
    path: str
    data: bytes | None = None
    rsz_file: object | None = None


@dataclass(slots=True)
class ScnSceneGraph:
    root_document_id: str
    root_instance_id: str
    documents: OrderedDict[str, ScnSceneDocument] = field(default_factory=OrderedDict)
    document_instances: OrderedDict[str, ScnDocumentInstance] = field(default_factory=OrderedDict)
    links: list[ScnSceneLink] = field(default_factory=list)
    renderables: list[ScnRenderableMesh] = field(default_factory=list)
    light_probes: list[ScnLightProbeBinding] = field(default_factory=list)
    volume_decals: list[ScnVolumeDecal] = field(default_factory=list)
    primitives: list[ScnPrimitive] = field(default_factory=list)
    render_targets: list[ScnRenderTargetModifier] = field(default_factory=list)
    shallow_waters: list[ScnShallowWater] = field(default_factory=list)
    camera_switches: list[ScnCameraSwitch | ScnCameraArea] = field(default_factory=list)
    camera_ranges: ScnCameraRanges | None = None
    timelines: list[TimelineAnimation] = field(default_factory=list)
    random: RandomRule | None = None
    field_names: Mapping[str, str | None] = field(default_factory=dict)
    startup_drawn: dict[str, dict[ScnObjectId, bool]] = field(default_factory=dict)
    startup_unwritten: set[tuple[str, ScnObjectId]] = field(default_factory=set)
    spawned_meshes: dict[tuple[str, ScnComponentId], SpawnedMesh] = field(default_factory=dict)
    variations: list[MaterialVariation] = field(default_factory=list)
    spawn_choices: list[SpawnChoice] = field(default_factory=list)
    spawned_owners: dict[str, ScnObjectId] = field(default_factory=dict)
    hung_spawns: dict[str, tuple[str, ScnObjectId, ScnObjectId]] = field(default_factory=dict)
    followed_joints: dict[str, tuple[str, ...]] = field(default_factory=dict)
    same_joints: dict[str, frozenset[ScnObjectId]] = field(default_factory=dict)
    reparented: dict[str, frozenset[ScnObjectId]] = field(default_factory=dict)
    shells: dict[ScnObjectId, tuple[str, tuple, tuple]] = field(default_factory=dict)
    lent: set[tuple[ScnComponentId, str]] = field(default_factory=set)
    diagnostics: list[ScnSceneDiagnostic] = field(default_factory=list)

    @property
    def drawn_lists(self) -> tuple[list, ...]:
        return self.renderables, self.volume_decals, self.primitives, self.render_targets, self.shallow_waters

    @property
    def objects(self) -> OrderedDict[ScnObjectId, ScnSceneObject]:
        return OrderedDict((key, value) for document in self.documents.values() for key, value in document.objects.items())

    @property
    def components(self) -> OrderedDict[ScnComponentId, ScnComponent]:
        return OrderedDict((key, value) for document in self.documents.values() for key, value in document.components.items())


ResourceResolver = Callable[[str, str | None], ScnResolvedResource | None]


def normalize_scene_path(path: str) -> str:
    return normalize_resource_path(path)


def normalize_document_id(path: str) -> str:
    value = normalize_scene_path(path)
    if not value:
        return "<memory>"
    try:
        p = Path(value)
        if p.exists():
            value = str(p.resolve())
    except Exception:
        pass
    return value.replace("\\", "/").casefold()


def _identity() -> np.ndarray:
    return np.identity(4, dtype=np.float32)


_IDENTITY = _identity()


def _vector3(value, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    try:
        return (float(value.x), float(value.y), float(value.z))
    except (AttributeError, TypeError, ValueError):
        return default


def _translation_matrix(position: tuple[float, float, float]) -> np.ndarray:
    matrix = _identity()
    matrix[:3, 3] = np.asarray(position, dtype=np.float32)
    return matrix


@dataclass(frozen=True, slots=True, eq=False)
class ScnJoints:
    names: tuple[str, ...]
    parents: tuple[int, ...]
    rotations: tuple[tuple[float, float, float, float], ...]
    matrices: tuple[np.ndarray, ...]
    hashes: tuple[int, ...]
    positions: tuple[tuple[float, float, float], ...] = ()
    scales: tuple[tuple[float, float, float], ...] = ()
    # Inverse bind matrices (row vectors) and the joint of each bone remap entry.
    inverse_binds: np.ndarray | None = None
    remap: tuple[int, ...] = ()
    symmetry: tuple[int, ...] = ()

    @classmethod
    def of_descs(cls, names, parents, positions, rotations, scales, inverse_binds=None, remap=(),
                 symmetry=()) -> ScnJoints:
        rotations = tuple(tuple(float(np.float32(value)) for value in rotation) for rotation in rotations)
        positions, scales = tuple(map(tuple, positions)), tuple(map(tuple, scales))
        return cls(tuple(names), tuple(int(parent) for parent in parents), rotations,
                   tuple(make_trs_matrix(position, rotation, scale)
                         for position, rotation, scale in zip(positions, rotations, scales)),
                   tuple(murmur3_hash_utf16le(name) for name in names), positions, scales, inverse_binds,
                   tuple(remap), tuple(int(other) for other in symmetry))

    @classmethod
    def of_mesh(cls, joints: JointSet, inverse_binds=None, remap=()) -> ScnJoints:
        descs = [joint_descriptor(matrix) for matrix in joints.locals]
        return cls.of_descs(joints.names, joints.parents, *(zip(*descs) if descs else ((), (), ())), inverse_binds,
                            remap, joints.symmetry)

    @classmethod
    def joined(cls, parts) -> ScnJoints:
        rows: dict[int, int] = {}
        picked = []
        for joints in parts:
            for index, value in enumerate(joints.hashes):
                if value not in rows:
                    rows[value] = len(picked)
                    picked.append((joints, index))

        def parent(joints, index, links):
            above = links[index] if index < len(links) else -1
            return rows[joints.hashes[above]] if 0 <= above < len(joints.hashes) else -1
        return cls(tuple(joints.names[index] for joints, index in picked),
                   tuple(parent(joints, index, joints.parents) for joints, index in picked),
                   tuple(joints.rotations[index] for joints, index in picked),
                   tuple(joints.matrices[index] for joints, index in picked), tuple(rows),
                   tuple(joints.positions[index] for joints, index in picked),
                   tuple(joints.scales[index] for joints, index in picked),
                   symmetry=tuple(parent(joints, index, joints.symmetry) for joints, index in picked))

    def posed(self, index: int, position=None, rotation=None) -> ScnJoints:
        positions, rotations, matrices = list(self.positions), list(self.rotations), list(self.matrices)
        if position is not None:
            positions[index] = tuple(float(np.float32(value)) for value in position)
        if rotation is not None:
            rotations[index] = tuple(float(np.float32(value)) for value in rotation)
        matrices[index] = make_trs_matrix(positions[index], rotations[index], self.scales[index])
        return replace(self, positions=tuple(positions), rotations=tuple(rotations), matrices=tuple(matrices))

    def find(self, key: int) -> int | None:
        return next((index for index, value in enumerate(self.hashes) if value == key), None)

    def index(self, name: str) -> int | None:
        return self.find(murmur3_hash_utf16le(name)) if name else None


def mesh_joints(mesh) -> ScnJoints | None:
    joints = JointSet.of_mesh(mesh) if mesh.header.decoded_content_flags.has_joint else None
    binds = np.asarray(mesh.inverse_bind_matrices, np.float32).reshape(-1, 4, 4) \
        if len(mesh.inverse_bind_matrices) == mesh.joint_count else None
    return None if joints is None else ScnJoints.of_mesh(joints, binds, mesh.bone_remap_indices)


def shared_joint(document, item, index: int):
    if item.transform is None or not item.transform.same_joints_constraint:
        return None
    parent = document.objects.get(document.object_by_local_id.get(item.parent_id))
    joints = document.joints.get(parent.id) if parent is not None and parent.kind != "folder" else None
    found = None if joints is None else joints.find(document.joints[item.id].hashes[index])
    return None if found is None else (parent, found)


def joint_world(document, item, index: int, world_of: Callable) -> np.ndarray:
    if (other := shared_joint(document, item, index)) is not None:
        return joint_world(document, *other, world_of)
    joints, chain = document.joints[item.id], [index]
    while 0 <= (parent := joints.parents[chain[-1]]) < len(joints.names) and parent not in chain:
        if (other := shared_joint(document, item, parent)) is not None:
            return chain_world([joints.matrices[joint] for joint in chain], joint_world(document, *other, world_of))
        chain.append(parent)
    return chain_world([joints.matrices[joint] for joint in chain], world_of(item))


def joint_rotation(document, item, index: int, rotation_of: Callable) -> tuple[float, float, float, float]:
    joints, chain = document.joints[item.id], []
    while 0 <= index < len(joints.names) and index not in chain:
        if (other := shared_joint(document, item, index)) is not None:
            rotation = joint_rotation(document, *other, rotation_of)
            break
        chain.append(index)
        index = joints.parents[index]
    else:
        rotation = rotation_of(item)
    return chain_rotation([joints.rotations[joint] for joint in chain], rotation)


def placed_matrix(base: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    if np.array_equal(base, _IDENTITY):
        return np.array(matrix, dtype=np.float32)
    return transform_product(base, matrix)


def instance_world(instance, item) -> np.ndarray:
    return item.document_world_matrix.copy() if instance.parent_instance_id is None else placed_matrix(
        instance.base_world_matrix, item.document_world_matrix)


def linked_base(graph, instance_id: str, folder_world: np.ndarray, offset: np.ndarray) -> np.ndarray:
    if (hung := graph.hung_spawns.get(instance_id)) is None:
        return placed_matrix(folder_world, offset)
    host_instance_id, host_id, root_id = hung
    document, instance = graph.documents[host_id.document_id], graph.document_instances[host_instance_id]
    host, root = document.objects[host_id], graph.documents[root_id.document_id].objects[root_id]
    index = document.joints[host_id].index(root.transform.parent_joint)
    world = joint_world(document, host, index, lambda owner: instance_world(instance, owner))
    if not root.transform.absolute_scaling:
        return world
    turn = mathex_rotation(instance.base_world_matrix.T)
    rotation = joint_rotation(document, host, index,
                              lambda owner: quaternion_product(owner.document_world_rotation, turn))
    return transform_product(compose_parented_transform(world, root.transform, rotation),
                             np.linalg.inv(root.document_world_matrix))


def folder_universal_offset(fields: Mapping[str, object]) -> tuple[float, float, float] | None:
    return next((_vector3(value) for value in fields.values() if isinstance(value, PositionData)), None)


def folder_offsets(document, local_id: int) -> list[tuple[float, float, float]]:
    offsets, seen = [], set()
    item = document.objects.get(document.object_by_local_id.get(local_id))
    while item is not None and item.kind == "folder" and item.id not in seen:
        seen.add(item.id)
        if (offset := folder_universal_offset(item.fields)) is not None:
            offsets.append(offset)
        item = document.objects.get(document.object_by_local_id.get(item.parent_id))
    return offsets


def root_position(position, offsets) -> tuple[float, float, float]:
    """A root game object's position: in double precision its own plus each folder's UniversalOffset, innermost
    first, rounded to single precision."""
    x, y, z = (float(np.float32(value)) for value in position)
    for ox, oy, oz in offsets:
        x, y, z = x + ox, y + oy, z + oz
    return float(np.float32(x + 0.0)), float(np.float32(y + 0.0)), float(np.float32(z + 0.0))


def _object_scale(item, world: np.ndarray) -> tuple[float, float, float]:
    """``Transform::getScale`` for a world matrix (column vectors)."""
    transform = item.transform
    local = (1.0, 1.0, 1.0) if transform is None else transform.scale
    absolute = transform is not None and transform.absolute_scaling
    return tuple(float(value) for value in engine_scale(np.asarray(world, np.float32).T, local, absolute))


def parent_of(document, item):
    if item.id in document.startup_parents:
        moved = document.startup_parents[item.id]
        return None if moved is None else document.objects.get(moved)
    return document.objects.get(document.object_by_local_id.get(item.parent_id))


def root_location(document, item) -> tuple[float, float, float]:
    transform = item.transform
    return transform.position if item.id in document.startup_rooted else root_position(
        transform.position, folder_offsets(document, item.parent_id))


def root_world(document, item, transform=None) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    transform = item.transform if transform is None else transform
    if transform is None:
        return _identity(), (0.0, 0.0, 0.0, 1.0)
    return make_trs_matrix(root_location(document, item), transform.rotation, transform.scale), transform.rotation


def parent_frame(document, item, world_of: Callable) -> tuple[np.ndarray, tuple[float, float, float, float]] | None:
    parent = parent_of(document, item)
    if parent is None or parent.kind == "folder":
        return None
    parent_world, parent_rotation = world_of(parent)
    transform = item.transform
    joints = document.joints.get(parent.id) if transform is not None and transform.parent_joint else None
    index = None if joints is None else joints.index(transform.parent_joint)
    if index is not None:
        parent_world = joint_world(document, parent, index, lambda owner: world_of(owner)[0])
        parent_rotation = joint_rotation(document, parent, index, lambda owner: world_of(owner)[1])
    return parent_world, parent_rotation


def object_world(document, item, world_of: Callable,
                 rotation: tuple[float, float, float, float] | None = None
                 ) -> tuple[np.ndarray, tuple[float, float, float, float]]:
    frame = parent_frame(document, item, world_of)
    transform = item.transform
    if rotation is not None:
        transform = replace(transform, rotation=rotation, local_matrix=None if frame is None or
                            transform.absolute_scaling else make_trs_matrix(transform.position, rotation,
                                                                            transform.scale))
    if frame is None:
        return root_world(document, item, transform)
    parent_world, parent_rotation = frame
    if transform is None:
        return parent_world.copy(), parent_rotation
    return (compose_parented_transform(parent_world, transform, parent_rotation=parent_rotation),
            quaternion_product(transform.rotation, parent_rotation))


def compose_document_worlds(document, on_cycle: Callable | None = None) -> None:
    visiting, visited = set(), set()

    def world_of(item) -> tuple[np.ndarray, tuple[float, float, float, float]]:
        if item.id not in visited:
            if item.id in visiting:
                if on_cycle is not None:
                    on_cycle(item)
                item.document_world_matrix, item.document_world_rotation = root_world(document, item)
            else:
                visiting.add(item.id)
                item.document_world_matrix, item.document_world_rotation = object_world(document, item, world_of)
                visiting.discard(item.id)
            visited.add(item.id)
        return item.document_world_matrix, item.document_world_rotation

    for item in document.objects.values():
        world_of(item)
    # The closure holds itself: let go of it.
    world_of = None


def turned_worlds(document, rotations: Mapping[ScnObjectId, tuple[float, float, float, float]],
                  objects: Iterable) -> dict[ScnObjectId, tuple[np.ndarray, tuple[float, float, float, float]]]:
    wanted = {item.id for item in objects}
    worlds: dict[ScnObjectId, tuple[np.ndarray, tuple[float, float, float, float]]] = {}

    def world_of(item) -> tuple[np.ndarray, tuple[float, float, float, float]]:
        if item.id not in wanted:
            return item.document_world_matrix, item.document_world_rotation
        if (found := worlds.get(item.id)) is None:
            found = worlds[item.id] = object_world(document, item, world_of, rotations.get(item.id))
        return found

    for item in objects:
        world_of(item)
    # The closure holds itself: let go of it, so its worlds go without the cyclic collector.
    world_of = None
    return worlds


def compose_parented_transform(
    parent_matrix: np.ndarray,
    transform: ScnTransform,
    parent_rotation: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0),
) -> np.ndarray:
    parent = np.asarray(parent_matrix, dtype=np.float32).reshape(4, 4)
    if not transform.absolute_scaling:
        return transform_product(parent, transform.local_matrix)
    x, y, z = (np.float32(value) for value in transform.position)
    position = ((parent[:3, 1] * y + parent[:3, 0] * x) + parent[:3, 2] * z) + parent[:3, 3]
    return make_trs_matrix(tuple(position), quaternion_product(transform.rotation, parent_rotation), transform.scale)


def _fol_transform(transform: FolTransform) -> ScnTransform:
    position = tuple(float(v) for v in transform.position)
    rotation = tuple(float(v) for v in transform.rotation)
    scale = tuple(float(v) for v in transform.scale)
    return ScnTransform(position, rotation, scale, make_trs_matrix(position, rotation, scale))


def _strip_value(value) -> str:
    return str(value or "").strip().rstrip("\x00").strip()


def _resource_string_fields(fields: Mapping[str, object]) -> list[tuple[str, str]]:
    return [
        (name, _strip_value(getattr(value, "value", "")))
        for name, value in fields.items()
        if isinstance(value, (ResourceData, StringData))
    ]


def _numeric_field(fields: Mapping[str, object], name: str | None, default: float, raw: str | None = None) -> float:
    if name is None:
        return float(default)
    target = name.casefold()
    field = next((value for key, value in fields.items() if key.casefold() == target), None)
    data = getattr(field, "raw_bytes", None)
    try:
        value = float(np.frombuffer(data, raw)[0] if raw and data and len(data) == np.dtype(raw).itemsize
                      else getattr(field, "value", field))
    except (TypeError, ValueError):
        return float(default)
    return value if np.isfinite(value) else float(default)


def _bool_field(fields: Mapping[str, object], name: str | None, default: bool) -> bool:
    if name is None:
        return default
    target = name.casefold()
    field = next(
        (value for key, value in fields.items() if key.casefold() == target),
        None,
    )
    value = getattr(field, "value", field)
    return value if type(value) is bool else default


def _object_runs(document: ScnSceneDocument, object_id: ScnObjectId, when: str | None, spec: Mapping) -> bool:
    if when is None:
        return True
    if when != spec["flag"]:
        return _bool_field(document.objects[object_id].fields, when, True)
    node: ScnObjectId | None = object_id
    while node is not None:
        item = document.objects[node]
        if item.kind != "folder" and not _bool_field(item.fields, spec["own"], True):
            return False
        parent = document.object_by_local_id.get(item.parent_id)
        node = None if parent == node else parent
    return True


@dataclass(frozen=True, slots=True)
class ScnRangeI:
    s: int
    r: int


def _plain_fields(fields: Mapping[str, object]) -> Mapping[str, object]:
    plain = {}
    for key, value in fields.items():
        if isinstance(value, Float4Data):
            plain[key] = (float(value.x), float(value.y), float(value.z), float(value.w))
        elif isinstance(value, RangeIData):
            plain[key] = ScnRangeI(int(value.min), int(value.max))
        elif isinstance(value, (ResourceData, StringData)):
            plain[key] = _strip_value(value.value)
        elif type(inner := getattr(value, "value", value)) in (bool, int, float) and np.isfinite(inner):
            plain[key] = inner
    return frozendict(plain)


def _flag_fields(fields: Mapping[str, object]) -> Mapping[str, object]:
    return frozendict({key: value for key, value in _plain_fields(fields).items()
                       if not isinstance(value, (str, tuple))})


def _mesh_fields(fields: Mapping[str, object], resolve: Callable[[int], Mapping[str, object]]) -> Mapping[str, object]:
    plain = dict(_flag_fields(fields))
    for key, value in fields.items():
        entries = getattr(value, "values", None) if isinstance(value, ArrayData) else None
        if entries and all(isinstance(entry, ObjectData) for entry in entries):
            plain[key] = tuple(_plain_fields(resolve(_object_ref_id(entry))) for entry in entries)
    return frozendict(plain)


def _obb_fields(fields: Mapping[str, object]) -> list[ScnOrientedBox]:
    boxes: list[ScnOrientedBox] = []
    for value in fields.values():
        if not isinstance(value, OBBData):
            continue
        raw = np.asarray(value.values, dtype=np.float32).reshape(5, 4)
        axes = raw[:3, :3]
        center = raw[3, :3]
        extent = raw[4, :3]
        if not np.isfinite(raw).all():
            continue
        boxes.append(
            ScnOrientedBox(
                field_data=value,
                axes=tuple(tuple(float(component) for component in row) for row in axes),
                center=tuple(float(component) for component in center),
                extent=tuple(float(max(0.0, component)) for component in extent),
            )
        )
    return boxes


def _object_ref_id(value) -> int:
    if isinstance(value, ObjectData):
        return int(value.value or 0)
    return int(value) if isinstance(value, int) else 0


def unpicked_folder_prefixes(game: str) -> tuple[str, ...]:
    scripts = type_table(FOLDER_OPTIONS_RESOURCE).get(str(game).upper(), {})
    return tuple(prefix for script in scripts.values() for prefix, value in script["prefixes"].items()
                 if value != script["highest"])


def scene_fields(game: str) -> dict:
    return type_table(SCENE_FIELDS_RESOURCE).get(str(game).upper(), {})


def renderable_visible(
    fields: Mapping[str, str | None],
    document: ScnSceneDocument,
    source_object: ScnSceneObject,
    component: ScnComponent,
    drawn: Mapping[ScnObjectId, bool] | None = None,
) -> bool:
    # ``DrawDefault`` means drawn in the main view; RE9 strand prefabs clear it on hair cards. Start-up enable
    # requests on the Mesh replace its Enabled: on while no layer's last request turns it off.
    layers = document.startup_meshes.get(source_object.id) if component.id == source_object.mesh_component else None
    enabled = all(layers.values()) if layers else _bool_field(component.fields, fields.get("enabled"), True)
    if not (enabled and _numeric_field(component.fields, fields.get("draw default"), True)):
        return False
    return ScnSceneGraphBuilder._chain_drawn(document, source_object, drawn)


def joint_constraint_holders(game: str) -> dict[str, dict]:
    entry = type_table(JOINT_CONSTRAINTS_RESOURCE)["games"].get(str(game).upper(), {})
    return {holder["type"]: holder for holder in entry.get("holders", ())}


def joint_constraint_copiers(game: str) -> dict[str, dict]:
    entry = type_table(JOINT_CONSTRAINTS_RESOURCE)["games"].get(str(game).upper(), {})
    return {copier["component"]: copier for copier in entry.get("copies", ())}


def joint_constraint_phases() -> tuple[int | str, ...]:
    return tuple(type_table(JOINT_CONSTRAINTS_RESOURCE)["phases"])


def _first_name(fields: Mapping[str, object], fallback: str) -> str:
    return next((text for _name, text in _resource_string_fields(fields) if text), fallback)


class ScnSceneGraphBuilder(ScnSceneAssembly, ScnPartSwapper, ScnSceneFolders, ScnSceneReferences, ScnSceneTimelines):
    def __init__(
        self,
        type_registry,
        *,
        resource_resolver: ResourceResolver | None = None,
        game_version: str = "",
        max_depth: int = 8,
        campaign: CampaignChoice | None = None,
        variations: Mapping[str, int] | None = None,
        user_files: Mapping[str, RszFile] | None = None,
        user_reads: dict | None = None,
        choices: Mapping[str, int] | None = None,
    ):
        self.type_registry = type_registry
        self.resource_resolver = resource_resolver
        self.game_version = game_version
        self.fields = scene_fields(game_version)
        self.max_depth = max(0, int(max_depth))
        self.campaign = campaign
        self.variations = variations or {}
        self.choices = choices or {}
        self._campaign_states: dict[str, CampaignState | None] = {}
        self._scenario_flags: ScenarioFlags | bool | None = None
        self._document_source_cache: dict[str, ScnSceneDocument] = {}
        # By file id, holding the file: a freed file's id could otherwise come back on another file.
        self._type_name_cache: dict[int, tuple[object, dict[int, str]]] = {}
        self._type_parent_cache: dict[str, frozenset[str]] = {}
        self._joints_cache: dict[str, ScnJoints | bool | None] = {}
        self._jcns_cache: dict[str, tuple[object | None, str]] = {}
        self._entries_cache: dict[tuple, dict] = {}
        self.user_files, self.user_reads = user_files or {}, {} if user_reads is None else user_reads
        self._user_cache: dict[str, RszFile | None] = dict(self.user_files)
        self._user_paths: dict[int, tuple[object, dict[int, str]]] = {}
        self._clip_cache: dict[str, object | None] = {}
        self._unread_resources: dict[str, tuple[str, str]] = {}
        profile = mmtr_profile_for_game(game_version) if game_version else None
        self._draw_rules = None if profile is None else draw_rules_for(profile)
        self._decal_catalog = None if profile is None else volume_decal_catalog_for(profile)
        self._render_target_catalog = None if profile is None else user_render_target_catalog_for(profile)
        self._water_catalog = None if profile is None else water_mesh_catalog_for(profile)
        self._primitive_catalog = None if profile is None else primitive_catalog_for(profile)
        self._shallow_water_catalog = None if profile is None else shallow_water_catalog_for(profile)

    def build(self, root_scn, *, root_path: str | None = None) -> ScnSceneGraph:
        source_path = root_path or getattr(root_scn, "filepath", "") or "<memory>"
        root_document_id = normalize_document_id(source_path)
        root_instance_id = self._document_instance_id("root", root_document_id, None)
        graph = ScnSceneGraph(root_document_id, root_instance_id, field_names=self.fields)
        self._spawn_leaders = {}
        self._parse_source_document(root_scn, source_path, root_document_id, graph)
        self._instantiate_document(graph, root_document_id, root_instance_id, None, None, _identity(), (root_document_id,), 0)
        self._join_part_skeletons(graph)
        self._apply_foreign_requests(graph)
        self._hide_ancestor_copies(graph)
        self._collect_camera_switches(graph)
        self._collect_camera_ranges(graph)
        self._bind_timelines(graph)
        if (random := startup_draw_requests(self.game_version).get("random")) is not None:
            graph.random = RandomRule.of(random)
        for instance in graph.document_instances.values():
            for object_id, reason in graph.documents[instance.document_id].startup_simple_refused:
                self._warn(graph, "simple_animation_not_played",
                           f"REasy does not turn this object over time: {reason}.", instance.document_id,
                           instance.instance_id, object_id)
        for path, (document_id, reason) in self._unread_resources.items():
            self._warn(graph, "startup_resource_unread",
                       f"Unable to read {path} ({reason}): the start-up rules reading it keep the files' states.",
                       document_id, path=path)
        self._unread_resources.clear()
        self._entries_cache.clear()
        return graph

    def _hide_ancestor_copies(self, graph: ScnSceneGraph) -> None:
        """RE9's character prefabs repeat a mesh on a shadow-only child. RE4's breakable props keep a disabled whole
        mesh on their root and draw it from the ``Before`` child, so only an enabled ancestor hides a copy."""
        held: dict[tuple[str, ScnObjectId], list[ScnRenderableMesh]] = {}
        enabled: dict[tuple[str, ScnObjectId], list[ScnRenderableMesh]] = {}
        components = graph.components
        for renderable in graph.renderables:
            if renderable.source_kind == "mesh":
                key = (renderable.document_instance_id, renderable.source_object_id)
                held.setdefault(key, []).append(renderable)
                component = components.get(renderable.source_component_id)
                if component is None or _bool_field(component.fields, self.fields.get("enabled"), True):
                    enabled.setdefault(key, []).append(renderable)
        for (instance, object_id), renderables in held.items():
            document = graph.documents.get(object_id.document_id)
            current = None if document is None else document.objects.get(object_id)
            seen = {object_id}
            while current is not None:
                parent = document.object_by_local_id.get(current.parent_id)
                if parent is None or parent in seen:
                    break
                seen.add(parent)
                above = enabled.get((instance, parent), ())
                for renderable in renderables:
                    if any(other.mesh_path == renderable.mesh_path
                           and np.allclose(other.world_matrix, renderable.world_matrix, rtol=0.0, atol=1e-6)
                           for other in above):
                        renderable.visible_by_default = False
                current = document.objects.get(parent)

    def _parse_source_document(self, scn, source_path: str, document_id: str, graph: ScnSceneGraph) -> ScnSceneDocument:
        existing = self._document_source_cache.get(document_id)
        if existing is not None:
            graph.documents.setdefault(document_id, existing)
            return existing

        document = ScnSceneDocument(document_id, source_path, scn)
        self._document_source_cache[document_id] = document
        graph.documents[document_id] = document

        self._collect_gameobjects(document, graph)
        self._collect_spawner_host(document)
        self._swap_parts(document, graph)
        self._collect_folders(document, graph)
        startup = self._apply_startup_draw_requests(document)
        if document_id == graph.root_document_id:
            self._show_spawn_template(document)
        self._collect_joints(document, graph)
        self._collect_constraints(document)
        for object_id in document.constraints:
            if document.objects[object_id].mesh_component is None:
                # A holder without a mesh (MH Wilds' NPC cores) poses its skeleton component's joints.
                self._collect_object_joints(document, document.objects[object_id], graph)
        apply_startup_transforms(document, lambda item: self._collect_parent_joints(document, item, graph),
                                 lambda item: self._collect_object_joints(document, item, graph))
        simple = []
        if startup is not None:
            # Curve animators' processes start on their first update, from the transforms the start-up writes left.
            animations, document.startup_curve_animations_refused = startup.curve_animations(
                self._material_param_names())
            document.startup_time_animations = (*animations, *startup.rises, *startup.blinks)
            timelines, refused = startup.timelines()
            document.startup_timelines, document.startup_timelines_refused = tuple(timelines), tuple(refused)
            simple = startup.simple_animations()
        self._compute_document_world_matrices(document, graph)
        if simple:
            built, document.startup_simple_refused = self._simple_animations(
                document, graph, simple, startup.rules["simple animations"])
            document.startup_time_animations = (*document.startup_time_animations, *built)
        self._collect_folder_references(document, graph)
        return document

    def _simple_animations(self, document: ScnSceneDocument, graph: ScnSceneGraph, found: list,
                           spec: Mapping) -> tuple[list[SimpleAnimation], tuple[tuple[ScnObjectId, str], ...]]:
        turned = {process.target for animation in document.startup_time_animations
                  for process in animation.processes if process.axis is not None}
        posed: dict[ScnObjectId, set[int]] = {}
        resolved, refused = [], []
        for object_id, component, distance, params in found:
            item = document.objects[object_id]
            if not distance > 0:
                # Its update times nothing (SimpleAnimation.update): it never moves.
                continue
            reason = "a param is not read" if params is None else "its transform scales absolutely" \
                if item.transform is None or item.transform.absolute_scaling else None
            joints = None
            if reason is None and any(name for name, _param in params):
                self._collect_object_joints(document, item, graph)
                joints = document.joints.get(item.id)
            placed = []
            for name, param in params if reason is None else ():
                index = None if joints is None else joints.index(name)
                if index is not None and shared_joint(document, item, index) is not None:
                    reason = f"its joint '{name}' follows another object's"
                elif index is not None and (joints.inverse_binds is None or not joints.remap):
                    reason = f"its mesh has no skin for its joint '{name}' to move"
                placed.append(replace(param, joint=index))
            if reason is not None:
                refused.append((object_id, f"{component.type_name}: {reason}"))
                continue
            for param in placed:
                if param.joint is None:
                    turned.add(object_id)
                else:
                    posed.setdefault(object_id, set()).add(param.joint)
            resolved.append((item, component, distance, tuple(placed), joints))
        built = []
        for item, component, distance, params, joints in resolved:
            if (mover := self._turned_above(document, item, turned, posed)) is not None:
                refused.append((item.id, f"{component.type_name}: it hangs under '{mover.name}', which time turns too"))
                continue
            built.append(SimpleAnimation(item.id, *simple_body(document, item, joints), params, distance,
                                         tuple(spec["powers"]), spec["ease"], spec["least"],
                                         *(float(spec[name]) for name in ("start power", "near power", "far power",
                                                                          "full turn")),
                                         tuple(spec["radians"])))
        return built, tuple(refused)

    @staticmethod
    def _turned_above(document: ScnSceneDocument, item: ScnSceneObject, turned: set[ScnObjectId],
                      posed: Mapping[ScnObjectId, set[int]]) -> ScnSceneObject | None:
        below, seen = item, {item.id}
        while (above := parent_of(document, below)) is not None and above.kind != "folder" and above.id not in seen:
            seen.add(above.id)
            if above.id in turned:
                return above
            name = below.transform.parent_joint if below.transform is not None else ""
            joints = document.joints.get(above.id) if name and above.id in posed else None
            index, chain = None if joints is None else joints.index(name), set()
            while index is not None and 0 <= index < len(joints.parents) and index not in chain:
                if index in posed[above.id]:
                    return above
                chain.add(index)
                index = joints.parents[index]
            below = above
        return None

    def _type_info(self, type_id: int) -> Mapping[str, object]:
        if not type_id or self.type_registry is None:
            return {}
        try:
            return self.type_registry.get_type_info(type_id) or {}
        except Exception:
            return {}

    def _type_name(self, scn, instance_id: int) -> str:
        if instance_id <= 0 or instance_id >= len(getattr(scn, "instance_infos", [])):
            return ""
        names = self._type_name_cache.get(id(scn))
        if names is None or names[0] is not scn:
            names = self._type_name_cache[id(scn)] = (scn, {})
        cached = names[1].get(int(instance_id))
        if cached is not None:
            return cached
        type_id = getattr(scn.instance_infos[instance_id], "type_id", 0)
        type_name = str(self._type_info(type_id).get("name", "") or "")
        names[1][int(instance_id)] = type_name
        return type_name

    def _instance_type_id(self, scn, instance_id: int) -> int:
        if instance_id <= 0 or instance_id >= len(getattr(scn, "instance_infos", [])):
            return 0
        return int(getattr(scn.instance_infos[instance_id], "type_id", 0) or 0)

    def _type_is_a(self, type_name: str, base_type_name: str) -> bool:
        if type_name == base_type_name:
            return True
        parents = self._type_parent_cache.get(type_name)
        if parents is None:
            try:
                get_parents = getattr(self.type_registry, "getTypeParents")
                parents = frozenset(get_parents(type_name) or ())
            except (AttributeError, TypeError, ValueError):
                parents = frozenset()
            self._type_parent_cache[type_name] = parents
        return base_type_name in parents

    def _object_instance_id(self, scn, local_object_id: int) -> int:
        table = getattr(scn, "object_table", [])
        if 0 <= int(local_object_id) < len(table):
            return int(table[int(local_object_id)])
        return 0

    def _fields(self, scn, instance_id: int) -> Mapping[str, object]:
        fields = getattr(scn, "parsed_elements", {}).get(instance_id, {})
        return fields if isinstance(fields, Mapping) else {}

    def _add_object(self, document, record, kind: str, graph) -> ScnSceneObject | None:
        scn = document.rsz_file
        local_id = int(getattr(record, "id", -1))
        instance_id = self._object_instance_id(scn, local_id)
        label = "GameObject" if kind == "gameobject" else "Folder"
        if instance_id <= 0:
            self._warn(
                graph, f"missing_{'object' if kind == 'gameobject' else kind}_instance",
                f"{label} object-table id {local_id} has no valid instance.",
                document.document_id,
            )
            return None

        fields = self._fields(scn, instance_id)
        object_id = ScnObjectId(document.document_id, local_id)
        scene_object = ScnSceneObject(
            object_id,
            kind,
            instance_id,
            int(getattr(record, "parent_id", -1)),
            _first_name(fields, f"{label}_{local_id}"),
            self._instance_type_id(scn, instance_id),
            self._type_name(scn, instance_id),
            fields,
            draw_self=kind == "folder" or _bool_field(fields, self.fields.get("draw self"), True),
        )
        document.objects[object_id] = scene_object
        document.object_by_local_id[local_id] = object_id
        return scene_object

    def _collect_gameobjects(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        scn = document.rsz_file
        for go in getattr(scn, "gameobjects", []) or []:
            scene_object = self._add_object(document, go, "gameobject", graph)
            if scene_object is not None:
                self._collect_components(document, scene_object, go, graph)

    def _collect_components(self, document: ScnSceneDocument, scene_object: ScnSceneObject, go, graph: ScnSceneGraph) -> None:
        scn = document.rsz_file
        for offset in range(1, int(getattr(go, "component_count", 0) or 0) + 1):
            object_table_id = int(getattr(go, "id", -1)) + offset
            component_instance_id = self._object_instance_id(scn, object_table_id)
            if component_instance_id <= 0:
                self._warn(
                    graph, "missing_component_instance",
                    f"Component object-table id {object_table_id} has no valid instance.",
                    document.document_id, object_id=scene_object.id,
                )
                continue
            type_id = self._instance_type_id(scn, component_instance_id)
            type_name = self._type_name(scn, component_instance_id)
            component_id = ScnComponentId(document.document_id, component_instance_id)
            component = ScnComponent(
                component_id,
                scene_object.id,
                object_table_id,
                type_id,
                type_name,
                self._fields(scn, component_instance_id),
            )
            document.components[component_id] = component
            scene_object.components.append(component_id)

            if type_name == VIA_TRANSFORM and scene_object.transform_component is None:
                scene_object.transform_component = component_id
                scene_object.transform = self._extract_transform(component, graph, scene_object.id)
            elif type_name == VIA_RENDER_MESH and scene_object.mesh_component is None:
                scene_object.mesh_component = component_id

    def _collect_folders(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        scn = document.rsz_file
        for folder in getattr(scn, "folder_infos", []) or []:
            self._add_object(document, folder, "folder", graph)

    def _apply_startup_draw_requests(self, document: ScnSceneDocument) -> StartupDraw | None:
        rules = startup_draw_requests(self.game_version)
        if not rules:
            return None
        startup = StartupDraw(self, document, rules)
        for object_id in startup.lit:
            document.objects[object_id].draw_self = True
        for object_id, layers in startup.run().items():
            document.objects[object_id].draw_self = all(layers.values())
        document.startup_transforms = tuple(startup.transforms)
        document.startup_meshes, document.startup_parts = startup.meshes, startup.parts
        for object_id, drawn in startup.folders().items():
            document.objects[object_id].draw_self = drawn
        document.startup_materials, document.startup_materials_refused = startup.materials(
            self._material_param_names())
        document.startup_materials_base = startup.base
        document.startup_curve_writers = {key: frozenset(value) for key, value in startup.curve_writers.items()}
        document.startup_layers = {object_id: {layer: (draw, startup.phases[object_id, layer])
                                               for layer, draw in layers.items()}
                                   for object_id, layers in startup.requests.items()}
        document.startup_foreign = tuple(startup.foreign)
        return startup

    def _material_param_names(self):
        rules = self._draw_rules
        return None if rules is None else rules.material_params.get(VIA_RENDER_MESH)

    def _mesh_state(self, document: ScnSceneDocument, object_id: ScnObjectId, component,
                    writes: Mapping | None = None, spawned: tuple[Mapping, ...] = ()) -> Mapping[str, object]:
        state = _mesh_fields(component.fields, partial(self._fields, document.rsz_file))
        written = (document.startup_materials if writes is None else writes).get(object_id)
        names = self._material_param_names()
        if names is not None and not names.own:
            state = frozendict({key: value for key, value in state.items() if key != names.list})
        if names is None or written is None and not spawned:
            return state
        replaced, appended = written or ({}, ())
        entries = list(state.get(names.list, ()))
        for index, values in replaced.items():
            if index < len(entries):
                entries[index] = frozendict({**entries[index], **values})
        return frozendict({**state, names.list: tuple(entries) + appended + spawned})

    def _extract_transform(self, component, graph, object_id) -> ScnTransform | None:
        try:
            return TransformAdapter(component.fields, self.fields).read()
        except ValueError:
            self._warn(
                graph, "invalid_transform",
                "via.Transform does not have usable TRS fields.",
                object_id.document_id, object_id=object_id, component_id=component.id,
            )
            return None

    def _collect_joints(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        for item in document.objects.values():
            if item.transform is not None and (item.transform.parent_joint or item.transform.same_joints_constraint):
                self._collect_parent_joints(document, item, graph)

    def _collect_constraints(self, document: ScnSceneDocument) -> None:
        if holders := joint_constraint_holders(self.game_version):
            for item in document.objects.values():
                self._collect_object_constraints(document, item, holders)
        copiers = joint_constraint_copiers(self.game_version)
        for item in document.objects.values() if copiers else ():
            for component in (document.components.get(c) for c in item.components):
                copier = None if component is None else copiers.get(component.type_name)
                if copier is not None and _bool_field(component.fields, copier["enabled"], True) and (
                        target := self._referenced(document, item, copier)) is not None:
                    document.copies[item.id] = target.id

    def _collect_object_constraints(self, document: ScnSceneDocument, item: ScnSceneObject, holders: dict) -> None:
        layers = []
        held = [(component.fields, holders.get(component.type_name), document.resources.get(component.id, {}))
                for component in (document.components.get(c) for c in item.components) if component is not None]
        for given, holder, lent in held + [({}, holders.get(kind), lent)
                                           for kind, lent in document.created.get(item.id, {}).items()]:
            if holder is None or not _bool_field(given, holder["enabled"], True):
                continue
            timing = int(_numeric_field(given, holder["timing"], 1))
            scn, listed, early = document.rsz_file, given.get(holder["layer"]), ()
            if "data" in lent:
                rsz = self._user_file(lent["data"], document.document_id) if lent["data"] else None
                root = self._fields(rsz, rsz.object_table[0]) if rsz is not None and rsz.object_table else {}
                if not _bool_field(root, holder["data"]["enable"], False):
                    continue
                main = self._fields(rsz, _object_ref_id(root.get(holder["data"]["main"])))
                scn, listed = rsz, main.get(holder["data"]["layers"])
                timing = int(_numeric_field(main, holder["data"]["timing"], timing))
                updater = holder["data"]["updater"]
                early = self._fields(rsz, _object_ref_id(root.get(updater["holder"]))).get(updater["flags"])
                early = plain_value(early) if lent.get("updater") and early is not None else ()
            stages = self._behavior_stages(document, item, holder.get("behavior"), timing)
            for index, (_instance, fields) in enumerate(self._nested_entries(scn, listed, "")):
                path = normalize_scene_path(not index and "data" not in lent and lent.get(holder["layer"])
                                            or plain_value(fields.get(holder["asset"], "")))
                if not path or not _bool_field(fields, holder["enable"], True):
                    continue
                if path not in self._jcns_cache:
                    model, reason = None, ""
                    if (data := self._resource_bytes(path, document.document_id)) is None:
                        reason = "not found"
                    else:
                        try:
                            model = read_jcns(data)
                        except JcnsFormatError as exc:
                            reason = str(exc)
                    self._jcns_cache[path] = model, reason
                model, reason = self._jcns_cache[path]
                rate = _numeric_field(fields, holder["blend_rate"], 1.0)
                own = int(_numeric_field(fields, holder["override timing"], timing)) \
                    if "data" not in lent and _bool_field(fields, holder["override"], False) else timing
                layers.append(ScnJointConstraints(
                    path, model, min(max(rate, 0.0), 1.0) if "data" in lent else rate,
                    _bool_field(fields, holder["pause"], False), reason, updater["stage"] if index < len(early)
                    and early[index] else stages[index] if own == timing and index < len(stages) else own))
        if layers:
            document.constraints[item.id] = tuple(layers)

    def _behavior_stages(self, document: ScnSceneDocument, item: ScnSceneObject, behavior, timing: int) -> tuple:
        if behavior is None or timing != {entry["name"]: entry["value"] for entry in game_enum_definitions(
                self.game_version)[behavior["timing"][0]]}[behavior["timing"][1]]:
            return ()
        component = next((document.components[key] for key in item.components if key in document.components
                          and document.components[key].type_name == behavior["type"]), None)
        if component is None:
            return ()
        path = document.resources.get(component.id, {}).get(behavior["asset"]) or plain_value(
            component.fields.get(behavior["asset"], ""))
        rsz = self._user_file(path, document.document_id) if path else None
        flags = (root_fields(rsz) or {}).get(behavior["flags"]) or () if rsz is not None else ()
        return tuple(behavior["stages"][bool(flag)] for flag in (*flags, *(False,) * behavior["padding"]))

    def _collect_parent_joints(self, document: ScnSceneDocument, item: ScnSceneObject, graph: ScnSceneGraph) -> None:
        holder, seen = parent_of(document, item), set()
        while holder is not None and holder.kind != "folder" and holder.id not in seen:
            seen.add(holder.id)
            self._collect_object_joints(document, holder, graph)
            if holder.transform is None or not holder.transform.same_joints_constraint:
                break
            holder = document.objects.get(document.object_by_local_id.get(holder.parent_id))

    def _collect_object_joints(self, document: ScnSceneDocument, item: ScnSceneObject, graph: ScnSceneGraph) -> None:
        if item.id in document.joints:
            return
        document.joints[item.id], reason = self._object_joints(document, item)
        if reason is not None:
            self._warn(graph, "joint_parent_unread",
                       f"The joints of '{item.name}' are not built ({reason}): what hangs on them or follows them is "
                       "placed without them.", document.document_id, object_id=item.id)

    def _object_joints(self, document: ScnSceneDocument, item: ScnSceneObject) -> tuple[ScnJoints | None, str | None]:
        components = [document.components[c] for c in item.components if c in document.components]
        mesh = next((c for c in components if c.type_name == VIA_RENDER_MESH), None)
        mesh_path = self._mesh_from_fields(mesh.fields)[0] if mesh is not None else ""
        skeleton = next((c for c in components if c.type_name in VIA_MOTION_SKELETONS
                         and _bool_field(c.fields, self.fields.get("skeleton auto apply"), True)), None)
        name = self.fields.get("skeleton resource")
        skeleton_path = "" if skeleton is None else normalize_scene_path(
            document.resources.get(skeleton.id, {}).get(name) or plain_value(skeleton.fields.get(name, "")))
        if mesh_path and skeleton_path:
            return None, (f"its Mesh and its {skeleton.type_name} both set them, and which one sets them last is not "
                          "known")
        if mesh_path and plain_value(mesh.fields.get(self.fields.get("shared skeleton"), False)):
            return None, "its Mesh shares another's skeleton (SharedSkeleton)"
        if item.transform is not None and item.transform.joint_segment_scale and (mesh_path or skeleton_path):
            return None, "they scale by segment (via::Joint::getPartSegmentScaleMatrix, unread)"
        if mesh_path or skeleton_path:
            if (joints := self._resource_joints(mesh_path or skeleton_path, document.document_id)) is False:
                return None, f"REasy cannot read {mesh_path or skeleton_path}"
            return joints, None
        return None, None

    def campaign_catalog(self, path: str, document_id: str = "") -> CampaignCatalog | None:
        spec = startup_draw_requests(self.game_version).get("campaign")
        marker = campaign_marker(path, spec) if spec else None
        return None if marker is None else read_catalog(spec, marker, partial(self._resource_rsz,
                                                                               document_id=document_id))

    def _campaign_state(self, document: ScnSceneDocument, spec: Mapping | None) -> CampaignState | None:
        marker = campaign_marker(str(document.source_path), spec) if self.campaign is not None and spec else None
        if marker is None:
            return None
        if marker not in self._campaign_states:
            read_rsz = partial(self._resource_rsz, document_id=document.document_id)
            catalog = read_catalog(spec, marker, read_rsz)
            if self._scenario_flags is None:
                self._scenario_flags = read_scenario_flags(spec["flags"], read_rsz, partial(
                    self._resource_bytes, document_id=document.document_id)) or False
            self._campaign_states[marker] = None if catalog is None or not self._scenario_flags else \
                campaign_state(catalog, self._scenario_flags, self.campaign, spec)
        return self._campaign_states[marker]

    def _unread(self, path: str, document_id: str, exc: Exception) -> None:
        self._unread_resources[path] = document_id, f"{type(exc).__name__}: {exc}"

    def _resolved(self, path: str, document_id: str):
        try:
            return self.resource_resolver(path, document_id) if self.resource_resolver else None
        except Exception as exc:
            self._unread(path, document_id, exc)
            return None

    def _resource_bytes(self, path: str, document_id: str) -> bytes | None:
        resolved = self._resolved(path, document_id)
        return None if resolved is None or resolved.data is None else bytes(resolved.data)

    def _resource_rsz(self, path: str, document_id: str) -> RszFile | None:
        resolved = self._resolved(path, document_id)
        if resolved is None or resolved.rsz_file is not None or resolved.data is None:
            return None if resolved is None else resolved.rsz_file
        try:
            found = RszFile()
            # The resolved name's version suffix picks the format (pfb.16, scn.18, scn.19).
            found.filepath, found.type_registry, found.game_version = resolved.path or path, self.type_registry, \
                self.game_version
            found.read(resolved.data)
        except Exception as exc:
            self._unread(path, document_id, exc)
            return None
        return found

    def _user_fields(self, path: str, document_id: str) -> Mapping[str, object] | None:
        user = self._user_file(path, document_id)
        return user.parsed_elements.get(user.object_table[0]) if user is not None and user.object_table else None

    def _user_file(self, path: str, document_id: str) -> RszFile | None:
        key = normalize_scene_path(path)
        if key not in self._user_cache:
            self._user_cache[key] = self._resource_rsz(path, document_id) if key else None
        return self._user_cache[key]

    def _resource_joints(self, path: str, document_id: str) -> ScnJoints | bool | None:
        key = normalize_scene_path(path)
        if key not in self._joints_cache:
            found = False
            try:
                resolved = self.resource_resolver(path, document_id) if self.resource_resolver else None
                suffix = str(getattr(resolved, "path", "") or "").rsplit(".", 1)[-1]
                if resolved is not None and resolved.data is not None and suffix.isdigit():
                    if key.endswith(".mesh"):
                        mesh, companion = MeshFile(), streaming_companion_path(resolved.path)
                        mesh.read(resolved.data, file_version=int(suffix), load_mode="light",
                                  streaming_data_loader=lambda: companion and getattr(
                                      self.resource_resolver(companion, document_id), "data", None))
                        found = mesh_joints(mesh)
                    else:
                        *descs, symmetry = skeleton_joint_descs(bytes(resolved.data))
                        found = ScnJoints.of_descs(*descs, symmetry=symmetry)
            except Exception:
                found = False
            self._joints_cache[key] = found
        return self._joints_cache[key]

    def _compute_document_world_matrices(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        compose_document_worlds(document, lambda item: self._warn(
            graph, "transform_cycle", "Cycle detected while composing SCN object transforms.",
            document.document_id, object_id=item.id))
        for scene_object in document.objects.values():
            scene_object.world_matrix = scene_object.document_world_matrix.copy()

    def _collect_folder_references(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        found = spawners(self.game_version)
        unpicked = unpicked_folder_prefixes(self.game_version)
        for scene_object in document.objects.values():
            if scene_object.kind != "folder":
                self._collect_spawned_prefabs(document, scene_object, found, graph)
                continue
            if scene_object.type_name != VIA_FOLDER:
                self._warn(
                    graph, "unsupported_folder_type",
                    f"Folder object uses unsupported type '{scene_object.type_name}'.",
                    document.document_id, object_id=scene_object.id,
                )
                continue
            if not _bool_field(scene_object.fields, self.fields.get("folder startup"), True) \
                    and scene_object.name.startswith(unpicked):
                continue
            linked_path = self._folder_link_path(scene_object.fields)
            if not linked_path:
                continue
            # The linked scene's roots take the offsets of this folder and those holding it (``root_position``).
            offsets = folder_offsets(document, scene_object.id.local_object_id)
            document.folder_references.append(ScnFolderReference(
                scene_object.id, linked_path, tuple(map(sum, zip(*offsets))) if offsets else None))

    def _field_at(self, document: ScnSceneDocument, fields: Mapping[str, object], steps) -> object:
        for step in steps[:-1]:
            fields = self._fields(document.rsz_file, _object_ref_id(fields.get(step)))
        return fields.get(steps[-1])

    def _folder_link_path(self, fields: Mapping[str, object]) -> str:
        return FolderLinkAdapter(fields).scene_path()

    def _instantiate_document(
        self, graph, document_id, document_instance_id, parent_instance_id, source_link_index, base_world_matrix,
        include_chain, depth, drawn: bool = True,
    ) -> None:
        if document_instance_id in graph.document_instances:
            return
        document = graph.documents.get(document_id)
        if document is None:
            return
        graph.document_instances[document_instance_id] = ScnDocumentInstance(
            document_instance_id,
            document_id,
            parent_instance_id,
            source_link_index,
            include_chain,
            base_world_matrix.astype(np.float32),
            drawn,
        )

        instance = graph.document_instances[document_instance_id]
        first = [len(items) for items in graph.drawn_lists]
        for source_object in document.objects.values():
            source_object.world_matrix = instance_world(instance, source_object)
            self._collect_renderables_for_object(
                graph,
                document,
                source_object,
                document_instance_id,
                base_world_matrix,
            )
        if not drawn:
            for items, start in zip(graph.drawn_lists, first):
                for renderable in items[start:]:
                    renderable.visible_by_default = False

        if depth >= self.max_depth:
            if document.folder_references:
                self._warn(
                    graph, "max_depth",
                    f"Maximum linked SCN depth {self.max_depth} reached.",
                    document_id, document_instance_id,
                )
            return

        for folder_reference in document.folder_references:
            self._instantiate_linked_document(
                graph,
                document,
                document_instance_id,
                folder_reference,
                include_chain,
                depth,
            )

    def _collect_renderables_for_object(self, graph, document, source_object, document_instance_id, base_world_matrix) -> None:
        for component_id in source_object.components:
            component = document.components.get(component_id)
            if component is None:
                continue
            if component.type_name == VIA_RENDER_MESH:
                self._append_mesh_renderable(graph, document, source_object, component, document_instance_id)
            elif component.type_name == VIA_RENDER_COMPOSITE_MESH:
                self._append_composite_mesh_renderables(
                    graph,
                    document,
                    source_object,
                    component,
                    document_instance_id,
                    base_world_matrix,
                )
            elif component.type_name == VIA_SPEEDTREE_V8:
                self._append_speedtree_renderable(graph, document, source_object, component, document_instance_id)
            elif component.type_name == VIA_LANDSCAPE_FOLIAGE:
                self._append_foliage_renderables(
                    graph,
                    document,
                    source_object,
                    component,
                    document_instance_id,
                    base_world_matrix,
                )
            elif self._type_is_a(component.type_name, VIA_RENDER_LIGHT_PROBES):
                self._append_light_probe_binding(graph, document, source_object, component, document_instance_id)
            elif self._water_catalog is not None and component.type_name == self._water_catalog.component:
                self._append_water_renderable(graph, document, source_object, component, document_instance_id)
            elif self._decal_catalog is not None and component.type_name == self._decal_catalog.component:
                catalog = self._decal_catalog
                names = {*catalog.fields.values(), *catalog.map_bits, *catalog.bindings.values()}
                graph.volume_decals.append(ScnVolumeDecal(
                    document_instance_id, source_object.id, component.id, source_object.world_matrix.copy(),
                    frozendict(plain_fields(document, component, names)),
                    renderable_visible(self.fields, document, source_object, component)))
            elif self._primitive_catalog is not None and component.type_name == self._primitive_catalog.component:
                graph.primitives.append(ScnPrimitive(
                    document_instance_id, source_object.id, component.id, source_object.world_matrix.copy(),
                    frozendict(plain_fields(document, component, self._primitive_catalog.read)),
                    renderable_visible(self.fields, document, source_object, component)))
            elif self._render_target_catalog is not None \
                    and component.type_name == self._render_target_catalog.component.type:
                names = self._render_target_catalog.component
                graph.render_targets.append(ScnRenderTargetModifier(
                    document_instance_id, source_object.id, component.id,
                    frozendict(plain_fields(document, component, {names.target, names.material, names.parameters})),
                    renderable_visible(self.fields, document, source_object, component)))
            elif self._shallow_water_catalog is not None \
                    and component.type_name in self._shallow_water_catalog.kinds:
                self._append_shallow_water(graph, document, source_object, component, document_instance_id,
                                           base_world_matrix)

    def _append_shallow_water(self, graph, document, source_object, component, document_instance_id,
                              base_world_matrix) -> None:
        kind = self._shallow_water_catalog.kinds[component.type_name]
        fields = plain_fields(document, component, kind.read)
        fields |= {name: value for name, value in _mesh_fields(
            component.fields, partial(self._fields, document.rsz_file)).items() if isinstance(value, tuple)}
        point = kind.fields.get("point")
        raw = None if point is None else getattr(component.fields.get(point), "raw_bytes", None)
        point_world = point_scale = target = None
        missing = False
        if raw and any(raw):
            target = document.objects.get(self._guid_objects(document).get(bytes(raw)))
            if target is None:
                missing = True
            else:
                point_world = placed_matrix(base_world_matrix, target.document_world_matrix)
                point_scale = _object_scale(target, point_world)
        controller = self._shallow_water_controller(document, source_object, component)
        holders = [] if controller is None or target is None else [
            item for item in document.objects.values() if item.kind != "folder"
            and item.parent_id == target.id.local_object_id and item.id != target.id
            and any(document.components[held].type_name == controller.fluid for held in item.components)]
        fluid = None
        if len(holders) == 1:
            holder, = holders
            held = next(document.components[held] for held in holder.components
                        if document.components[held].type_name == controller.fluid)
            world = placed_matrix(base_world_matrix, holder.document_world_matrix)
            turned = not np.array_equal(np.asarray(base_world_matrix, np.float32)[:3, :3], _IDENTITY[:3, :3])
            resolution = plain_value(held.fields.get(controller.resolution))
            fluid = ScnShallowWaterFluid(world, None if turned else holder.document_world_rotation,
                                         _object_scale(holder, world), (int(resolution[0]), int(resolution[1])),
                                         renderable_visible(self.fields, document, holder, held))

        graph.shallow_waters.append(ScnShallowWater(
            document_instance_id, source_object.id, component.id, component.type_name,
            source_object.world_matrix.copy(), _object_scale(source_object, source_object.world_matrix), point_world,
            point_scale, missing,
            frozendict(fields), renderable_visible(self.fields, document, source_object, component),
            None if controller is None else controller.component, fluid, len(holders)))

    def _shallow_water_controller(self, document, source_object, component):
        update = (startup_draw_requests(self.game_version).get("options") or {}).get("update") or {"flag": None}
        for held in source_object.components:
            found = self._shallow_water_catalog.controllers.get(document.components[held].type_name)
            if found is not None and found.water == component.type_name \
                    and _bool_field(document.components[held].fields, self.fields.get("enabled"), True) \
                    and _object_runs(document, source_object.id, found.when, update):
                return found
        return None

    @staticmethod
    def _guid_objects(document) -> dict[bytes, ScnObjectId]:
        scn = document.rsz_file
        return {} if scn.is_pfb else {bytes(go.guid): document.object_by_local_id[go.id] for go in scn.gameobjects
                                      if any(go.guid) and go.id in document.object_by_local_id}

    @staticmethod
    def _guid_holders(graph) -> dict[bytes, list[tuple[str, ScnObjectId]]]:
        holders: dict[bytes, list[tuple[str, ScnObjectId]]] = {}
        for instance in graph.document_instances.values():
            document = graph.documents[instance.document_id]
            for go in () if document.rsz_file.is_pfb else document.rsz_file.gameobjects:
                object_id = document.object_by_local_id.get(go.id)
                if object_id is not None and any(go.guid):
                    holders.setdefault(bytes(go.guid), []).append((instance.instance_id, object_id))
        return holders

    def _append_light_probe_binding(self, graph, document, source_object, component, document_instance_id) -> None:
        if self.game_version not in LIGHT_PROBE_PREVIEW_GAMES:
            return
        values = [value for _name, value in _resource_string_fields(component.fields) if value]
        if len(values) < 2:
            self._warn_component(
                graph,
                "missing_light_probe_paths",
                f"Light probe component '{component.type_name}' ({component.id.instance_id}) has fewer than two resource/string paths.",
                document,
                document_instance_id,
                source_object,
                component,
            )
            return
        graph.light_probes.append(
            ScnLightProbeBinding(
                document_instance_id=document_instance_id,
                source_object_id=source_object.id,
                source_component_id=component.id,
                lprb_path=normalize_scene_path(values[0]),
                prb_path=normalize_scene_path(values[1]),
                obbs=_obb_fields(component.fields),
                priority=int(_numeric_field(component.fields, self.fields.get("probe priority"), 0.0)),
                intensity=max(0.0, _numeric_field(component.fields, self.fields.get("probe intensity"), 1.0)),
            )
        )

    def _append_mesh_renderable(self, graph, document, source_object, component, document_instance_id) -> None:
        mesh_path, mdf_path, enabled_parts = self._mesh_from_fields(
            component.fields
        )
        spawned = graph.spawned_meshes.get((document_instance_id, component.id)) or document.slotted.get(
            component.id, NOT_SPAWNED)
        mesh_path, mdf_path = spawned.mesh or mesh_path, spawned.material or mdf_path
        enabled_parts = spawned.enabled if spawned.enabled is not None else enabled_parts
        written = document.startup_parts.get(source_object.id) if component.id == source_object.mesh_component else None
        if written and enabled_parts is not None:
            enabled_parts = tuple(written.get(index, enabled) for index, enabled in enumerate(enabled_parts))
        link = graph.document_instances[document_instance_id].source_link_index
        off = () if link is None or source_object.parent_id in document.object_by_local_id \
            or component.id != source_object.mesh_component else graph.links[link].parts_off
        if off:
            enabled_parts = tuple(index not in off and enabled for index, enabled in enumerate(
                (*(enabled_parts or ()), *(True,) * (max(off) + 1 - len(enabled_parts or ())))))
        if not mesh_path:
            self._warn_component(
                graph, "missing_mesh_path",
                f"via.render.Mesh component {component.id.instance_id} has no usable mesh path.",
                document, document_instance_id, source_object, component,
            )
            return
        self._append_renderable(
            graph,
            source_object,
            component,
            document_instance_id,
            mesh_path,
            mdf_path,
            source_object.document_world_matrix,
            source_object.world_matrix,
            visible_by_default=renderable_visible(
                self.fields,
                document,
                source_object,
                component,
            ),
            enabled_parts=enabled_parts,
            # Read as numbers: RE8's dump types these flags as one-byte data.
            normal_recalculation_enabled=bool(_numeric_field(
                component.fields,
                self.fields.get("normal recalculation"),
                True,
            )) if spawned.normal_recalculation is None else spawned.normal_recalculation,
            normal_recalc_edge_exclusion=bool(_numeric_field(
                component.fields,
                self.fields.get("normal edge exclusion"),
                False,
            )),
            skin_weight_source=self._skin_weight_source(
                document,
                source_object,
            ),
            decal_receive=self._decal_mode(VIA_RENDER_MESH, component.fields),
            transparent_bias=_numeric_field(component.fields, self.fields.get("transparent bias"), 0.0, "<f4"),
            blend_shape_normal=bool(_numeric_field(component.fields, self.fields.get("blend shape normal"), True)),
            blend_shape_real_time_lod=bool(_numeric_field(
                component.fields, self.fields.get("blend shape real time lod"), False)),
            draw_state=self._mesh_state(document, source_object.id, component, spawned=spawned.writes),
            lod_parent_mesh=self._lod_parent_mesh(document, source_object, component),
            start_pose=start_bones(document, source_object) if component.id == source_object.mesh_component else None,
        )

    def _lod_parent_mesh(self, document, source_object, component) -> bool:
        rules = self._draw_rules
        rule = None if rules is None else rules.lods.get(VIA_RENDER_MESH)
        if rule is None or "follow_parent" not in rule.modes or int(_numeric_field(
                component.fields, rule.mode, rules.component[VIA_RENDER_MESH][rule.mode])) \
                != rule.modes["follow_parent"]:
            return False
        seen, current = {source_object.id}, source_object
        while (parent := document.object_by_local_id.get(current.parent_id)) is not None and parent not in seen:
            seen.add(parent)
            current = document.objects[parent]
            if any(self._type_is_a(document.components[item].type_name, VIA_RENDER_MESH)
                   for item in current.components if item in document.components):
                return True
        return False

    def _append_speedtree_renderable(self, graph, document, source_object, component, document_instance_id) -> None:
        values = [value for _name, value in _resource_string_fields(component.fields) if value]
        mesh_path = next((value for value in values if ".stmesh" in value.lower()), "")
        mdf_path = next((value for value in values if ".mdf2" in value.lower()), "")
        if not mesh_path:
            self._warn_component(
                graph, "missing_mesh_path",
                f"via.speedtree.SpeedTreeV8 component {component.id.instance_id} has no usable mesh path.",
                document, document_instance_id, source_object, component,
            )
            return
        self._append_renderable(
            graph,
            source_object,
            component,
            document_instance_id,
            normalize_scene_path(mesh_path),
            normalize_scene_path(mdf_path) if mdf_path else "",
            source_object.document_world_matrix,
            source_object.world_matrix,
            visible_by_default=renderable_visible(self.fields, document, source_object, component),
            decal_receive=self._decal_mode(VIA_SPEEDTREE_V8, component.fields),
            speedtree_placement=COMPONENT,
            draw_state=_mesh_fields(component.fields, partial(self._fields, document.rsz_file)),
        )

    def _append_water_renderable(self, graph, document, source_object, component, document_instance_id) -> None:
        catalog = self._water_catalog
        state = plain_fields(document, component, catalog.read)
        controller = next((document.components[key] for key in source_object.components
                           if document.components[key].type_name == catalog.controller), None)
        if controller is not None:
            state |= {name: value for name, value in _mesh_fields(
                controller.fields, partial(self._fields, document.rsz_file)).items() if isinstance(value, tuple)}
        paths = dict(_resource_string_fields(component.fields))
        mesh_path, mdf_path = paths.get(catalog.fields["mesh"], ""), paths.get(catalog.fields["material"], "")
        if not mesh_path:
            self._warn_component(
                graph, "missing_mesh_path",
                f"{component.type_name} component {component.id.instance_id} has no usable mesh path.",
                document, document_instance_id, source_object, component,
            )
            return
        self._append_renderable(
            graph,
            source_object,
            component,
            document_instance_id,
            normalize_scene_path(mesh_path),
            normalize_scene_path(mdf_path) if mdf_path else "",
            source_object.document_world_matrix,
            source_object.world_matrix,
            source_kind="water",
            visible_by_default=renderable_visible(self.fields, document, source_object, component),
            decal_receive=(component.type_name, None),
            draw_state=frozendict(state),
            mdf_material=catalog.material,
            drawn_group=catalog.group,
        )

    def _decal_mode(self, component_type: str, fields) -> tuple[str, str | None]:
        rule = None if self._draw_rules is None else self._draw_rules.decal_receive.get(component_type)
        if rule is None or rule.field is None:
            return component_type, None
        return component_type, self._enum_member(rule.enum, _numeric_field(fields, rule.field, 0))

    def _enum_member(self, enum_name: str, value: float) -> str | None:
        try:
            return decal_receive_modes(self.game_version, enum_name).get(int(value))
        except EnumDefinitionError:
            return None

    @staticmethod
    def _skin_weight_source(document, source_object) -> str:
        current = source_object
        while current is not None:
            for component_id in current.components:
                candidate = document.components.get(component_id)
                if (
                    candidate is not None
                    and candidate.type_name == VIA_RENDER_BLEND_SHAPE_MESH_REBINDER
                ):
                    return "additional"
            parent_id = document.object_by_local_id.get(current.parent_id)
            current = (
                document.objects.get(parent_id)
                if parent_id is not None
                else None
            )
        return "primary"

    def _append_composite_mesh_renderables(self, graph, document, source_object, component, document_instance_id, base_world_matrix) -> None:
        groups = self._nested_entries_from_fields(
            document.rsz_file,
            component.fields,
            VIA_RENDER_COMPOSITE_MESH_INSTANCE_GROUP,
        )
        for group_instance_id, group_fields in groups:
            mesh_path, mdf_path, enabled_parts = self._mesh_from_fields(
                group_fields
            )
            if not mesh_path:
                self._warn_component(
                    graph, "missing_mesh_path",
                    (
                        f"via.render.CompositeMesh component {component.id.instance_id}"
                        f" group {group_instance_id or 0} has no usable mesh path."
                    ),
                    document, document_instance_id, source_object, component,
                )
                continue
            for transform_instance_id, transform_fields in self._nested_entries_from_fields(
                document.rsz_file,
                group_fields,
                VIA_RENDER_COMPOSITE_MESH_TRANSFORM_CONTROLLER,
                last=True,
            ):
                if not self._composite_transform_enabled(transform_fields):
                    continue
                transform = self._extract_composite_transform(transform_fields)
                if transform is None:
                    self._warn_component(
                        graph, "invalid_composite_transform",
                        (
                            f"via.render.CompositeMesh component {component.id.instance_id}"
                            f" transform {transform_instance_id or 0} has no usable TRS."
                        ),
                        document, document_instance_id, source_object, component,
                    )
                    continue
                document_world = transform_product(source_object.document_world_matrix, transform.local_matrix)
                self._append_renderable(
                    graph,
                    source_object,
                    component,
                    document_instance_id,
                    mesh_path,
                    mdf_path,
                    document_world,
                    placed_matrix(base_world_matrix, document_world),
                    source_kind="composite_mesh",
                    source_group_instance_id=group_instance_id,
                    source_transform_instance_id=transform_instance_id,
                    decal_receive=self._decal_mode(VIA_RENDER_COMPOSITE_MESH, component.fields),
                    draw_state=_flag_fields(component.fields),
                    visible_by_default=renderable_visible(
                        self.fields,
                        document,
                        source_object,
                        component,
                    ),
                    enabled_parts=enabled_parts,
                    normal_recalculation_enabled=_bool_field(
                        group_fields,
                        self.fields.get("normal recalculation"),
                        _bool_field(
                            component.fields,
                            self.fields.get("normal recalculation"),
                            True,
                        ),
                    ),
                    normal_recalc_edge_exclusion=_bool_field(
                        group_fields,
                        self.fields.get("normal edge exclusion"),
                        _bool_field(
                            component.fields,
                            self.fields.get("normal edge exclusion"),
                            False,
                        ),
                    ),
                )

    def _append_foliage_renderables(self, graph, document, source_object, component, document_instance_id, base_world_matrix) -> None:
        fol_path = self._foliage_path(component.fields)
        if not fol_path:
            self._warn_component(
                graph, "missing_foliage_path",
                f"via.landscape.Foliage component {component.id.instance_id} has no usable FOL path.",
                document, document_instance_id, source_object, component,
            )
            return
        resolved = self.resource_resolver(fol_path, document.document_id) if self.resource_resolver else None
        if resolved is None or resolved.data is None:
            self._warn_component(
                graph, "missing_foliage_resource",
                f"Unable to resolve foliage resource: {fol_path}",
                document, document_instance_id, source_object, component, fol_path,
            )
            return
        try:
            fol = FolFile()
            if not fol.read(resolved.data):
                raise ValueError("invalid FOL header")
        except Exception as exc:
            self._warn_component(
                graph, "invalid_foliage_resource",
                f"Unable to parse foliage resource {fol_path}: {exc}",
                document, document_instance_id, source_object, component, fol_path,
            )
            return
        decal_receive = self._decal_mode(VIA_LANDSCAPE_FOLIAGE, component.fields)
        for group in fol.groups:
            mesh_path = normalize_scene_path(group.mesh_path)
            if not mesh_path:
                self._warn_component(
                    graph, "missing_mesh_path",
                    f"via.landscape.Foliage component {component.id.instance_id} group {group.index} has no usable mesh path.",
                    document, document_instance_id, source_object, component, fol_path,
                )
                continue
            for transform_index, fol_transform in enumerate(group.transforms, 1):
                transform = _fol_transform(fol_transform)
                document_world = transform_product(source_object.document_world_matrix, transform.local_matrix)
                self._append_renderable(
                    graph,
                    source_object,
                    component,
                    document_instance_id,
                    mesh_path,
                    normalize_scene_path(group.material_path),
                    document_world,
                    placed_matrix(base_world_matrix, document_world),
                    source_kind="foliage",
                    source_group_instance_id=group.index,
                    source_transform_instance_id=transform_index,
                    decal_receive=decal_receive,
                    speedtree_placement=FOLIAGE,
                    visible_by_default=renderable_visible(
                        self.fields,
                        document,
                        source_object,
                        component,
                    ),
                )

    @staticmethod
    def _append_renderable(
        graph,
        source_object,
        component,
        document_instance_id,
        mesh_path,
        mdf_path,
        document_world_matrix,
        world_matrix,
        *,
        source_kind="mesh",
        source_group_instance_id=None,
        source_transform_instance_id=None,
        visible_by_default=True,
        enabled_parts=None,
        normal_recalculation_enabled=True,
        normal_recalc_edge_exclusion=False,
        skin_weight_source="primary",
        decal_receive=MESH_DECAL_RECEIVE,
        transparent_bias=0.0,
        speedtree_placement=None,
        blend_shape_normal=True,
        blend_shape_real_time_lod=False,
        draw_state=None,
        lod_parent_mesh=False,
        start_pose=None,
        mdf_material=None,
        drawn_group=None,
    ) -> None:
        graph.renderables.append(
            ScnRenderableMesh(
                document_instance_id=document_instance_id,
                source_object_id=source_object.id,
                source_component_id=component.id,
                mesh_path=mesh_path,
                mdf_path=mdf_path,
                document_world_matrix=document_world_matrix.copy(),
                world_matrix=world_matrix.copy(),
                source_kind=source_kind,
                source_group_instance_id=source_group_instance_id,
                source_transform_instance_id=source_transform_instance_id,
                visible_by_default=visible_by_default,
                enabled_parts=enabled_parts,
                normal_recalculation_enabled=normal_recalculation_enabled,
                normal_recalc_edge_exclusion=normal_recalc_edge_exclusion,
                skin_weight_source=skin_weight_source,
                decal_receive=decal_receive,
                transparent_bias=transparent_bias,
                speedtree_placement=speedtree_placement,
                blend_shape_normal=blend_shape_normal,
                blend_shape_real_time_lod=blend_shape_real_time_lod,
                draw_state=draw_state,
                lod_parent_mesh=lod_parent_mesh,
                start_pose=start_pose,
                mdf_material=mdf_material,
                drawn_group=drawn_group,
            )
        )

    def _nested_entries_from_fields(self, scn, fields, type_name: str, *, last: bool = False) -> list[tuple[int | None, Mapping[str, object]]]:
        fallback: list[tuple[int | None, Mapping[str, object]]] = []
        selected: list[tuple[int | None, Mapping[str, object]]] = []
        for field_name, value in fields.items():
            typed = self._nested_entries(scn, value, type_name)
            any_entries = typed or self._nested_entries(scn, value, "")
            if typed or (last and field_name == self.fields.get("composite transforms") and any_entries):
                selected = any_entries
                if not last:
                    return selected
            elif any_entries:
                fallback = any_entries if last or not fallback else fallback
        return selected or fallback

    def _nested_entries(self, scn, value, type_name: str) -> list[tuple[int | None, Mapping[str, object]]]:
        if isinstance(value, StructData):
            if type_name and value.orig_type != type_name:
                return []
            return [(None, item) for item in value.values if isinstance(item, Mapping)]

        if not isinstance(value, ArrayData):
            return []

        entries: list[tuple[int | None, Mapping[str, object]]] = []
        for item in value.values:
            instance_id = _object_ref_id(item)
            if instance_id > 0:
                if type_name and self._type_name(scn, instance_id) != type_name:
                    continue
                item_fields = self._fields(scn, instance_id)
                if item_fields:
                    entries.append((instance_id, item_fields))
            elif isinstance(item, Mapping) and (not type_name or value.orig_type == type_name):
                entries.append((None, item))
        return entries

    def _composite_transform_enabled(self, fields: Mapping[str, object]) -> bool:
        first = next(iter(fields.values()), None)
        if isinstance(first, BoolData):
            return bool(first.value)
        return True

    def _extract_composite_transform(self, fields: Mapping[str, object]) -> ScnTransform | None:
        try:
            return CompositeMeshAdapter(fields).read_transform()
        except ValueError:
            return None

    def _mesh_from_fields(
        self,
        fields: Mapping[str, object],
    ) -> tuple[str, str, tuple[bool, ...] | None]:
        adapter = MeshAdapter(fields, self.fields)
        return (*adapter.paths(), adapter.enabled_parts())

    def _foliage_path(self, fields: Mapping[str, object]) -> str:
        values = [value for _name, value in _resource_string_fields(fields) if value]
        hit = next((value for value in values if ".fol" in value.lower()), values[0] if values else "")
        return normalize_scene_path(hit)

    def _instantiate_linked_document(self, graph, document, document_instance_id, folder_reference, include_chain, depth) -> None:
        if self.resource_resolver is None:
            self._warn_link(
                graph, "missing_resolver",
                f"No resolver is available for linked SCN path: {folder_reference.linked_path}",
                document, document_instance_id, folder_reference, folder_reference.linked_path,
            )
            return

        resolved = None
        try:
            resolved = self.resource_resolver(folder_reference.linked_path, document.document_id)
        except Exception as exc:
            self._warn_link(
                graph, "link_resolve_error",
                f"Failed to resolve linked SCN path: {exc}",
                document, document_instance_id, folder_reference, folder_reference.linked_path,
            )
            return

        if resolved is None:
            self._warn_link(
                graph, "missing_link",
                f"Linked SCN could not be resolved: {folder_reference.linked_path}",
                document, document_instance_id, folder_reference, folder_reference.linked_path,
            )
            return

        child_path = resolved.path or folder_reference.linked_path
        child_document_id = normalize_document_id(child_path)
        if child_document_id in include_chain:
            self._append_link(graph, document_instance_id, folder_reference, child_document_id, child_path, include_chain)
            self._warn_link(
                graph, "link_cycle",
                f"Skipping cyclic linked SCN: {child_path}",
                document, document_instance_id, folder_reference, child_path,
            )
            return

        child_scn = resolved.rsz_file
        if child_scn is None:
            if resolved.data is None:
                self._warn_link(
                    graph, "missing_link_data",
                    f"Linked SCN resolved without data: {child_path}",
                    document, document_instance_id, folder_reference, child_path,
                )
                return
            try:
                child_scn = RszFile()
                child_scn.filepath = child_path
                child_scn.type_registry = self.type_registry
                child_scn.game_version = self.game_version or getattr(document.rsz_file, "game_version", "")
                child_scn.read(resolved.data)
            except Exception as exc:
                self._warn_link(
                    graph, "link_parse_error",
                    f"Failed to parse linked SCN '{child_path}': {exc}",
                    document, document_instance_id, folder_reference, child_path,
                )
                return

        self._parse_source_document(child_scn, child_path, child_document_id, graph)
        child_chain = include_chain + (child_document_id,)
        link_index = self._append_link(graph, document_instance_id, folder_reference, child_document_id, child_path, child_chain)

        source_folder = document.objects.get(folder_reference.source_folder_id)
        folder_world = source_folder.world_matrix if source_folder is not None else _identity()
        folder_offset_matrix = (
            _translation_matrix(folder_reference.folder_offset)
            if folder_reference.folder_offset is not None
            else _identity()
        )
        child_instance_id = self._document_instance_id(document_instance_id, child_document_id, folder_reference.source_folder_id)
        if folder_reference.spawner is not None:
            spawn_parts(self, graph, document, folder_reference.spawner, graph.documents[child_document_id],
                        child_instance_id)
            self._spawn_materials(graph, folder_reference.materials, graph.documents[child_document_id],
                                  child_instance_id)
            self._hang_spawned(graph, document, document_instance_id, folder_reference,
                               graph.documents[child_document_id], child_instance_id)
        child_base_world = linked_base(graph, child_instance_id, folder_world, folder_offset_matrix)
        self._instantiate_document(
            graph,
            child_document_id,
            child_instance_id,
            document_instance_id,
            link_index,
            child_base_world,
            child_chain,
            depth + 1,
            graph.document_instances[document_instance_id].drawn
            and (source_folder is None or self._chain_drawn(document, source_folder)),
        )
        if folder_reference.child_draw:
            self._apply_child_draw(graph, graph.documents[child_document_id], child_instance_id,
                                   dict(folder_reference.child_draw))

    def _apply_child_draw(self, graph, document, document_instance_id, child_draw: Mapping[str, bool]) -> None:
        roots = {item.local_object_id for item in document.objects if document.objects[item].parent_id not in document.object_by_local_id}
        drawn: dict[ScnObjectId, bool] = {}
        named: set[str] = set()
        for object_id, item in document.objects.items():
            if item.parent_id in roots and item.name in child_draw and item.name not in named:
                named.add(item.name)
                drawn[object_id] = child_draw[item.name]
        if not drawn:
            return
        for renderable in [item for items in graph.drawn_lists for item in items]:
            if renderable.document_instance_id != document_instance_id:
                continue
            source_object = document.objects.get(renderable.source_object_id)
            component = document.components.get(renderable.source_component_id)
            if source_object is not None and component is not None:
                renderable.visible_by_default = graph.document_instances[document_instance_id].drawn and \
                    renderable_visible(self.fields, document, source_object, component, drawn)

    @staticmethod
    def _append_link(graph, document_instance_id, folder_reference, child_document_id, child_path, include_chain) -> int:
        graph.links.append(
            ScnSceneLink(
                document_instance_id,
                folder_reference.source_folder_id,
                folder_reference.linked_path,
                child_document_id,
                child_path,
                include_chain,
                folder_reference.folder_offset,
                folder_reference.parts_off,
                folder_reference.spawner,
                folder_reference.name,
            )
        )
        return len(graph.links) - 1

    def _document_instance_id(self, parent_instance_id: str, document_id: str, source_folder_id: ScnObjectId | None) -> str:
        if source_folder_id is None:
            return f"{parent_instance_id}:{document_id}"
        return (
            f"{parent_instance_id}/"
            f"{source_folder_id.document_id}:{source_folder_id.local_object_id}->"
            f"{document_id}"
        )

    def _warn_component(self, graph, code, message, document, document_instance_id, source_object, component, path="") -> None:
        self._warn(graph, code, message, document.document_id, document_instance_id, source_object.id, component.id, path)

    def _warn_link(self, graph, code, message, document, document_instance_id, folder_reference, path) -> None:
        self._warn(graph, code, message, document.document_id, document_instance_id, folder_reference.source_folder_id, path=path)

    def _warn(self, graph, code, message, document_id="", document_instance_id="", object_id=None, component_id=None, path="") -> None:
        graph.diagnostics.append(
            ScnSceneDiagnostic(
                "info" if code == "missing_mesh_path" else "warning",
                code,
                message,
                document_id,
                document_instance_id,
                object_id,
                component_id,
                path,
            )
        )
