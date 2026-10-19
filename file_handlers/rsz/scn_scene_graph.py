from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import numpy as np

from file_handlers.fol.fol_file import FolFile, FolTransform
from file_handlers.rsz.rsz_data_types import (
    ArrayData,
    BoolData,
    OBBData,
    ObjectData,
    PositionData,
    ResourceData,
    StringData,
    StructData,
)
from file_handlers.rsz.rsz_file import RszFile
from file_handlers.rsz.utils.rsz_instance_defaults import type_table
from file_handlers.stmesh.stmesh_file import COMPONENT, FOLIAGE
from utils.resource_file_utils import normalize_resource_path


VIA_FOLDER = "via.Folder"
VIA_TRANSFORM = "via.Transform"
VIA_RENDER_MESH = "via.render.Mesh"
VIA_RENDER_BLEND_SHAPE_MESH_REBINDER = "via.render.BlendShapeMeshRebinder"
VIA_RENDER_COMPOSITE_MESH = "via.render.CompositeMesh"
VIA_RENDER_COMPOSITE_MESH_INSTANCE_GROUP = "via.render.CompositeMeshInstanceGroup"
VIA_RENDER_COMPOSITE_MESH_TRANSFORM_CONTROLLER = "via.render.CompositeMeshTransformController"
VIA_RENDER_LIGHT_PROBES = "via.render.LightProbes"
VIA_LANDSCAPE_FOLIAGE = "via.landscape.Foliage"
VIA_SPEEDTREE_V8 = "via.speedtree.SpeedTreeV8"
SPAWNED_PREFABS_RESOURCE = "resources/data/spawned_prefabs.json"
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


@dataclass(slots=True)
class ScnFolderReference:
    source_folder_id: ScnObjectId
    linked_path: str
    folder_offset: tuple[float, float, float] | None
    # Per object name, the ``DrawSelf`` a spawning component (``spawned_prefabs``) sets on its spawned prefab's objects.
    child_draw: tuple[tuple[str, bool], ...] = ()


@dataclass(slots=True)
class ScnSceneDocument:
    document_id: str
    source_path: str
    rsz_file: object
    objects: OrderedDict[ScnObjectId, ScnSceneObject] = field(default_factory=OrderedDict)
    components: OrderedDict[ScnComponentId, ScnComponent] = field(default_factory=OrderedDict)
    object_by_local_id: dict[int, ScnObjectId] = field(default_factory=dict)
    folder_references: list[ScnFolderReference] = field(default_factory=list)


@dataclass(slots=True)
class ScnDocumentInstance:
    instance_id: str
    document_id: str
    parent_instance_id: str | None
    source_link_index: int | None
    include_chain: tuple[str, ...]
    base_world_matrix: np.ndarray


@dataclass(slots=True)
class ScnSceneLink:
    source_document_instance_id: str
    source_folder_id: ScnObjectId
    linked_path: str
    resolved_document_id: str
    resolved_path: str
    include_chain: tuple[str, ...]
    folder_offset: tuple[float, float, float] | None = None


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
    # ``via.render.Mesh``'s ``DecalReciveMode``; 0 is its default.
    decal_receive_mode: int = 0
    # ``via.render.Mesh``'s ``TransparentBias``; 0 is its default.
    transparent_bias: float = 0.0
    # One of ``stmesh_file.PLACEMENTS``.
    speedtree_placement: str | None = None

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
        return (
            f"{self.document_instance_id}:"
            f"{self.source_object_id.document_id}:"
            f"{self.source_object_id.local_object_id}:"
            f"{self.source_component_id.instance_id}:light_probes"
        )


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
    diagnostics: list[ScnSceneDiagnostic] = field(default_factory=list)

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


def _vector3(value, default=(0.0, 0.0, 0.0)) -> tuple[float, float, float]:
    try:
        return (float(value.x), float(value.y), float(value.z))
    except (AttributeError, TypeError, ValueError):
        return default


def _translation_matrix(position: tuple[float, float, float]) -> np.ndarray:
    matrix = _identity()
    matrix[:3, 3] = np.asarray(position, dtype=np.float32)
    return matrix


def _quaternion_matrix(rotation: tuple[float, float, float, float]) -> np.ndarray:
    x, y, z, w = rotation
    return np.array(
        [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - w * z), 2.0 * (x * z + w * y), 0.0],
            [2.0 * (x * y + w * z), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - w * x), 0.0],
            [2.0 * (x * z - w * y), 2.0 * (y * z + w * x), 1.0 - 2.0 * (x * x + y * y), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float32,
    )


def make_trs_matrix(
    position: tuple[float, float, float],
    rotation: tuple[float, float, float, float],
    scale: tuple[float, float, float],
) -> np.ndarray:
    matrix = _quaternion_matrix(rotation)
    matrix[:3, :3] *= np.asarray(scale, dtype=np.float32)
    matrix[:3, 3] = np.asarray(position, dtype=np.float32)
    return matrix


def compose_parented_transform(
    parent_matrix: np.ndarray,
    transform: ScnTransform,
    joint_matrix: np.ndarray | None = None,
) -> np.ndarray:
    """Compose a column-vector scene transform, optionally below a parent joint."""
    parent = np.asarray(parent_matrix, dtype=np.float32).reshape(4, 4)
    if joint_matrix is not None:
        joint = np.asarray(joint_matrix, dtype=np.float32).reshape(4, 4)
        if not np.isfinite(joint).all():
            raise ValueError("parent joint matrix is non-finite")
        parent = parent @ joint
    world = (parent @ transform.local_matrix).astype(np.float32)
    if not transform.absolute_scaling:
        return world

    basis = world[:3, :3].astype(np.float64)
    if not np.isfinite(basis).all():
        raise ValueError("parented transform contains a non-finite basis")
    left, _singular, right = np.linalg.svd(basis)
    rotation = left @ right
    if np.linalg.det(rotation) < 0.0:
        left[:, -1] *= -1.0
        rotation = left @ right
    world[:3, :3] = (
        rotation * np.asarray(transform.scale, dtype=np.float64)
    ).astype(np.float32)
    return world


def _normalize_quaternion_values(values) -> tuple[float, float, float, float]:
    quat = tuple(float(v) for v in values)
    length = float(np.linalg.norm(np.asarray(quat, dtype=np.float32)))
    return tuple(float(v / length) for v in quat) if length > 1e-8 else (0.0, 0.0, 0.0, 1.0)


def _fol_transform(transform: FolTransform) -> ScnTransform:
    position = tuple(float(v) for v in transform.position)
    rotation = _normalize_quaternion_values(transform.rotation)
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


def _numeric_field(fields: Mapping[str, object], name: str, default: float) -> float:
    target = name.casefold()
    field = next((value for key, value in fields.items() if key.casefold() == target), None)
    try:
        value = float(getattr(field, "value", field))
    except (TypeError, ValueError):
        return float(default)
    return value if np.isfinite(value) else float(default)


def _bool_field(fields: Mapping[str, object], name: str, default: bool) -> bool:
    target = name.casefold()
    field = next(
        (value for key, value in fields.items() if key.casefold() == target),
        None,
    )
    value = getattr(field, "value", field)
    return value if type(value) is bool else default


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


def spawned_prefabs() -> dict:
    return type_table(SPAWNED_PREFABS_RESOURCE)


def _first_name(fields: Mapping[str, object], fallback: str) -> str:
    return next((text for _name, text in _resource_string_fields(fields) if text), fallback)


class ScnSceneGraphBuilder:
    def __init__(
        self,
        type_registry,
        *,
        resource_resolver: ResourceResolver | None = None,
        game_version: str = "",
        max_depth: int = 8,
    ):
        self.type_registry = type_registry
        self.resource_resolver = resource_resolver
        self.game_version = game_version
        self.max_depth = max(0, int(max_depth))
        self._document_source_cache: dict[str, ScnSceneDocument] = {}
        self._type_name_cache: dict[tuple[int, int], str] = {}
        self._type_parent_cache: dict[str, frozenset[str]] = {}

    def build(self, root_scn, *, root_path: str | None = None) -> ScnSceneGraph:
        source_path = root_path or getattr(root_scn, "filepath", "") or "<memory>"
        root_document_id = normalize_document_id(source_path)
        root_instance_id = self._document_instance_id("root", root_document_id, None)
        graph = ScnSceneGraph(root_document_id, root_instance_id)
        self._parse_source_document(root_scn, source_path, root_document_id, graph)
        self._instantiate_document(graph, root_document_id, root_instance_id, None, None, _identity(), (root_document_id,), 0)
        self._hide_ancestor_copies(graph)
        return graph

    @staticmethod
    def _hide_ancestor_copies(graph: ScnSceneGraph) -> None:
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
                if component is None or _bool_field(component.fields, "Enabled", True):
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
        self._collect_folders(document, graph)
        self._compute_document_world_matrices(document, graph)
        self._collect_folder_references(document, graph)
        return document

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
        cache_key = (id(scn), int(instance_id))
        cached = self._type_name_cache.get(cache_key)
        if cached is not None:
            return cached
        type_id = getattr(scn.instance_infos[instance_id], "type_id", 0)
        type_name = str(self._type_info(type_id).get("name", "") or "")
        self._type_name_cache[cache_key] = type_name
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
            draw_self=_bool_field(fields, "DrawSelf", True),
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

    def _extract_transform(self, component, graph, object_id) -> ScnTransform | None:
        from .scn_scene_adapters import TransformAdapter

        try:
            return TransformAdapter(component.fields).read()
        except ValueError:
            self._warn(
                graph, "invalid_transform",
                "via.Transform does not have usable TRS fields.",
                object_id.document_id, object_id=object_id, component_id=component.id,
            )
            return None

    def _compute_document_world_matrices(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        visiting: set[ScnObjectId] = set()
        visited: set[ScnObjectId] = set()

        def compute(scene_object: ScnSceneObject) -> np.ndarray:
            if scene_object.id in visited:
                return scene_object.document_world_matrix
            if scene_object.id in visiting:
                self._warn(
                    graph, "transform_cycle",
                    "Cycle detected while composing SCN object transforms.",
                    document.document_id, object_id=scene_object.id,
                )
                scene_object.document_world_matrix = scene_object.transform.local_matrix if scene_object.transform else _identity()
                visited.add(scene_object.id)
                return scene_object.document_world_matrix

            visiting.add(scene_object.id)
            local_matrix = scene_object.transform.local_matrix if scene_object.transform else _identity()
            parent_object_id = document.object_by_local_id.get(scene_object.parent_id)
            if parent_object_id is not None and parent_object_id in document.objects:
                parent_world = compute(document.objects[parent_object_id])
                scene_object.document_world_matrix = (
                    compose_parented_transform(parent_world, scene_object.transform)
                    if scene_object.transform is not None
                    else parent_world.copy()
                )
            else:
                scene_object.document_world_matrix = local_matrix.copy()
            scene_object.world_matrix = scene_object.document_world_matrix.copy()
            visiting.discard(scene_object.id)
            visited.add(scene_object.id)
            return scene_object.document_world_matrix

        for scene_object in document.objects.values():
            compute(scene_object)

    def _collect_folder_references(self, document: ScnSceneDocument, graph: ScnSceneGraph) -> None:
        spawners = spawned_prefabs()
        for scene_object in document.objects.values():
            if scene_object.kind != "folder":
                self._collect_spawned_prefabs(document, scene_object, spawners)
                continue
            if scene_object.type_name != VIA_FOLDER:
                self._warn(
                    graph, "unsupported_folder_type",
                    f"Folder object uses unsupported type '{scene_object.type_name}'.",
                    document.document_id, object_id=scene_object.id,
                )
                continue
            linked_path = self._folder_link_path(scene_object.fields)
            if not linked_path:
                continue
            document.folder_references.append(
                ScnFolderReference(scene_object.id, linked_path, self._folder_offset(scene_object.fields))
            )

    def _collect_spawned_prefabs(self, document: ScnSceneDocument, scene_object: ScnSceneObject, spawners) -> None:
        for component_id in scene_object.components:
            component = document.components.get(component_id)
            spawner = None if component is None else spawners.get(component.type_name)
            if spawner is None or not _bool_field(component.fields, "Enabled", True):
                continue
            path = normalize_scene_path(
                _strip_value(getattr(self._field_at(document, component.fields, spawner["prefab"]), "value", ""))
            )
            if not path:
                continue
            child_draw = []
            children = spawner.get("children")
            if children:
                for entry in getattr(component.fields.get(children["list"]), "values", None) or ():
                    fields = self._fields(document.rsz_file, _object_ref_id(entry))
                    name = _strip_value(getattr(fields.get(children["name"]), "value", ""))
                    if name:
                        child_draw.append((name, _bool_field(fields, children["draw"], True)))
            document.folder_references.append(ScnFolderReference(scene_object.id, path, None, tuple(child_draw)))

    def _field_at(self, document: ScnSceneDocument, fields: Mapping[str, object], steps) -> object:
        for step in steps[:-1]:
            fields = self._fields(document.rsz_file, _object_ref_id(fields.get(step)))
        return fields.get(steps[-1])

    def _folder_link_path(self, fields: Mapping[str, object]) -> str:
        from .scn_scene_adapters import FolderLinkAdapter

        return FolderLinkAdapter(fields).scene_path()

    def _folder_offset(self, fields: Mapping[str, object]) -> tuple[float, float, float] | None:
        return next((_vector3(value) for value in fields.values() if isinstance(value, PositionData)), None)

    def _instantiate_document(
        self, graph, document_id, document_instance_id, parent_instance_id, source_link_index, base_world_matrix, include_chain, depth
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
        )

        root_instance = parent_instance_id is None
        for source_object in document.objects.values():
            source_object.world_matrix = source_object.document_world_matrix.copy() if root_instance else (base_world_matrix @ source_object.document_world_matrix).astype(np.float32)
            self._collect_renderables_for_object(
                graph,
                document,
                source_object,
                document_instance_id,
                base_world_matrix,
            )

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
                priority=int(_numeric_field(component.fields, "Priority", 0.0)),
                intensity=max(0.0, _numeric_field(component.fields, "Intensity", 1.0)),
            )
        )

    def _append_mesh_renderable(self, graph, document, source_object, component, document_instance_id) -> None:
        mesh_path, mdf_path, enabled_parts = self._mesh_from_fields(
            component.fields
        )
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
            visible_by_default=self._renderable_visible(
                document,
                source_object,
                component,
            ),
            enabled_parts=enabled_parts,
            normal_recalculation_enabled=_bool_field(
                component.fields,
                "NormalRecalculationEnable",
                True,
            ),
            normal_recalc_edge_exclusion=_bool_field(
                component.fields,
                "NREdgeExcluded",
                False,
            ),
            skin_weight_source=self._skin_weight_source(
                document,
                source_object,
            ),
            decal_receive_mode=int(_numeric_field(component.fields, "DecalReciveMode", 0)),
            transparent_bias=float(_numeric_field(component.fields, "TransparentBias", 0.0)),
        )

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
            visible_by_default=self._renderable_visible(document, source_object, component),
            speedtree_placement=COMPONENT,
        )

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
                document_world = (source_object.document_world_matrix @ transform.local_matrix).astype(np.float32)
                self._append_renderable(
                    graph,
                    source_object,
                    component,
                    document_instance_id,
                    mesh_path,
                    mdf_path,
                    document_world,
                    (base_world_matrix @ document_world).astype(np.float32),
                    source_kind="composite_mesh",
                    source_group_instance_id=group_instance_id,
                    source_transform_instance_id=transform_instance_id,
                    visible_by_default=self._renderable_visible(
                        document,
                        source_object,
                        component,
                    ),
                    enabled_parts=enabled_parts,
                    normal_recalculation_enabled=_bool_field(
                        group_fields,
                        "NormalRecalculationEnable",
                        _bool_field(
                            component.fields,
                            "NormalRecalculationEnable",
                            True,
                        ),
                    ),
                    normal_recalc_edge_exclusion=_bool_field(
                        group_fields,
                        "NREdgeExcluded",
                        _bool_field(
                            component.fields,
                            "NREdgeExcluded",
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
                document_world = (source_object.document_world_matrix @ transform.local_matrix).astype(np.float32)
                self._append_renderable(
                    graph,
                    source_object,
                    component,
                    document_instance_id,
                    mesh_path,
                    normalize_scene_path(group.material_path),
                    document_world,
                    (base_world_matrix @ document_world).astype(np.float32),
                    source_kind="foliage",
                    source_group_instance_id=group.index,
                    source_transform_instance_id=transform_index,
                    speedtree_placement=FOLIAGE,
                    visible_by_default=self._renderable_visible(
                        document,
                        source_object,
                        component,
                    ),
                )

    @staticmethod
    def _renderable_visible(
        document: ScnSceneDocument,
        source_object: ScnSceneObject,
        component: ScnComponent,
        drawn: Mapping[ScnObjectId, bool] | None = None,
    ) -> bool:
        # ``DrawDefault`` means drawn in the main view; RE9 strand prefabs clear it on hair cards.
        if not (_bool_field(component.fields, "Enabled", True) and _bool_field(component.fields, "DrawDefault", True)):
            return False
        current = source_object
        visited: set[ScnObjectId] = set()
        while current.id not in visited:
            visited.add(current.id)
            if not (drawn.get(current.id, current.draw_self) if drawn else current.draw_self):
                return False
            parent_id = document.object_by_local_id.get(current.parent_id)
            if parent_id is None:
                break
            parent = document.objects.get(parent_id)
            if parent is None:
                break
            current = parent
        return True

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
        decal_receive_mode=0,
        transparent_bias=0.0,
        speedtree_placement=None,
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
                decal_receive_mode=decal_receive_mode,
                transparent_bias=transparent_bias,
                speedtree_placement=speedtree_placement,
            )
        )

    def _nested_entries_from_fields(self, scn, fields, type_name: str, *, last: bool = False) -> list[tuple[int | None, Mapping[str, object]]]:
        fallback: list[tuple[int | None, Mapping[str, object]]] = []
        selected: list[tuple[int | None, Mapping[str, object]]] = []
        for field_name, value in fields.items():
            typed = self._nested_entries(scn, value, type_name)
            any_entries = typed or self._nested_entries(scn, value, "")
            if typed or (last and field_name == "Transform" and any_entries):
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
        from .scn_scene_adapters import CompositeMeshAdapter

        try:
            return CompositeMeshAdapter(fields).read_transform()
        except ValueError:
            return None

    def _mesh_from_fields(
        self,
        fields: Mapping[str, object],
    ) -> tuple[str, str, tuple[bool, ...] | None]:
        from .scn_scene_adapters import MeshAdapter

        adapter = MeshAdapter(fields)
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
        child_base_world = (folder_world @ folder_offset_matrix).astype(np.float32)
        child_instance_id = self._document_instance_id(document_instance_id, child_document_id, folder_reference.source_folder_id)
        self._instantiate_document(
            graph,
            child_document_id,
            child_instance_id,
            document_instance_id,
            link_index,
            child_base_world,
            child_chain,
            depth + 1,
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
        for renderable in graph.renderables:
            if renderable.document_instance_id != document_instance_id:
                continue
            source_object = document.objects.get(renderable.source_object_id)
            component = document.components.get(renderable.source_component_id)
            if source_object is not None and component is not None:
                renderable.visible_by_default = self._renderable_visible(document, source_object, component, drawn)

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
