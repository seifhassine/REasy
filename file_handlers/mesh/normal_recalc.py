from __future__ import annotations

from array import array
from copy import copy
from dataclasses import dataclass, replace
from enum import Enum
from functools import partial
import struct
from collections.abc import Callable, Mapping, Sequence

lazy import numpy as np

from .profiles import (
    MeshGameProfile,
    MeshMainVersion,
    NormalRecalcLayout,
    coerce_mesh_profile,
)
from utils.hex_util import align
from utils.native_build import ensure_fastmesh


VERTEX_INDEX_BITS = 22
VERTEX_INDEX_MASK = (1 << VERTEX_INDEX_BITS) - 1
WORKGROUP_SLOT_BITS = 10
WORKGROUP_SLOT_MASK = (1 << WORKGROUP_SLOT_BITS) - 1
REDIRECT_ANNOTATION_MASK = 1 << 31
REDIRECT_RESERVED_MASK = ((1 << 9) - 1) << VERTEX_INDEX_BITS
INDICES_PER_WORKGROUP = 256 * 6
ALIGNMENT = 16
INDEX_ALIGNMENT = 2
VERTEX_PAGE_SIZE = 0xFFFF
MAX_TWO_PAGE_VERTICES = VERTEX_PAGE_SIZE * 2


class NormalRecalcError(ValueError):
    pass


class LegacyNormalRecalcProfile(Enum):
    STANDARD = "standard"
    TWO_PAGE_16BIT = "two_page_16bit"


@dataclass(frozen=True, slots=True)
class NormalRecalcRedirect:
    target_vertex: int
    annotation: bool = False


@dataclass(frozen=True, slots=True)
class NormalRecalcWorkItem:
    target_vertex: int
    workgroup_slot: int


@dataclass(frozen=True, slots=True)
class NormalRecalcDispatchGroup:
    work_items: tuple[NormalRecalcWorkItem, ...]


@dataclass(frozen=True, slots=True)
class NormalRecalcSubmesh:
    base_vertex: int
    indices: tuple[int, ...]
    vertex_count: int = 0


@dataclass(frozen=True, slots=True)
class NormalRecalcData:
    redirects: tuple[NormalRecalcRedirect, ...] = ()
    lods: tuple["NormalRecalcLOD", ...] = ()
    edge_excluded: bool = False
    reserved: tuple[int, ...] = ()
    declared_lod_count: int = 0
    body_list_offset: int = 0
    body_offsets: tuple[int, ...] = ()
    legacy_profile: LegacyNormalRecalcProfile | None = None


@dataclass(frozen=True, slots=True)
class NormalRecalcLOD:
    lod_index: int
    body_offset: int
    vertices_offset: int
    faces_offset: int
    vertex_entries: tuple[int, ...]
    face_entries: tuple[int, ...]
    work_items: tuple[NormalRecalcWorkItem, ...] = ()
    redirects: tuple[NormalRecalcRedirect, ...] = ()
    vertex_base: int = 0
    vertex_page_entries: tuple[int, ...] = ()
    buffer_index: int = 0


@dataclass(frozen=True, slots=True)
class NormalRecalcLODTopology:
    lod_index: int
    buffer_index: int
    vertex_base: int
    vertex_count: int
    submeshes: tuple[NormalRecalcSubmesh, ...]
    uses_32bit_indices: bool = False


@dataclass(frozen=True, slots=True)
class MeshNormalRecalcPlan:
    """Renderer-facing topology derived from modeled normal-recalc data."""

    redirect_targets: tuple[int, ...]
    triangle_vertices: tuple[int, ...]
    dispatch_groups: tuple[NormalRecalcDispatchGroup, ...] = ()
    edge_exclusion_mask: tuple[bool, ...] = ()
    dispatch_quantization_scale: float = 1023.0


def recalculate_mesh_normals(
    mesh: object,
    positions: np.ndarray,
    rendered_triangle_indices: Sequence[int],
    *,
    edge_exclusion_enabled: bool = False,
    preserved_normals: np.ndarray | None = None,
    lod_index: int = 0,
) -> np.ndarray | None:
    plan = mesh_normal_recalc_plan(mesh, rendered_triangle_indices, lod_index=lod_index)
    if plan is None:
        return None
    return recalculate_normal_plan(
        plan,
        positions,
        edge_exclusion_enabled=edge_exclusion_enabled,
        preserved_normals=preserved_normals,
    )


def recalculate_normal_plan(
    plan: MeshNormalRecalcPlan,
    positions: np.ndarray,
    *,
    edge_exclusion_enabled: bool = False,
    preserved_normals: np.ndarray | None = None,
) -> np.ndarray:
    vertices = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
    return PreparedNormalRecalcPlan(plan, len(vertices)).recalculate(
        vertices, edge_exclusion_enabled=edge_exclusion_enabled,
        preserved_normals=preserved_normals,
    )


class PreparedNormalRecalcPlan:
    def __init__(self, plan: MeshNormalRecalcPlan, vertex_count: int):
        self.targets = np.ascontiguousarray(plan.redirect_targets, dtype=np.intp)
        if self.targets.ndim != 1 or len(self.targets) != vertex_count:
            raise NormalRecalcError("normal-recalculation redirects do not match rendered vertices")
        if np.any((self.targets < 0) | (self.targets >= vertex_count)):
            raise NormalRecalcError("normal-recalculation redirect targets a vertex outside the position buffer")
        self.exclusions = np.ascontiguousarray(plan.edge_exclusion_mask, dtype=np.bool_)
        if self.exclusions.ndim != 1 or len(self.exclusions) not in (0, vertex_count):
            raise NormalRecalcError("normal-recalculation edge annotations do not match rendered vertices")
        self.has_edge_exclusions = bool(np.any(self.exclusions))
        self.modern = bool(plan.dispatch_groups)
        self.scale = plan.dispatch_quantization_scale
        if self.modern:
            if not np.isfinite(self.scale) or self.scale <= 0:
                raise NormalRecalcError("normal-recalculation quantization scale must be positive")
            self.triangles = _normal_dispatch_triangles(plan.dispatch_groups, vertex_count)
        else:
            self.triangles = np.ascontiguousarray(plan.triangle_vertices, dtype=np.intp)
        if self.triangles.ndim != 1 or len(self.triangles) % 3:
            raise NormalRecalcError("normal-recalculation indices do not form complete triangles")
        if np.any((self.triangles < 0) | (self.triangles >= vertex_count)):
            raise NormalRecalcError("normal-recalculation triangle references a vertex outside LOD0")
        native = ensure_fastmesh()
        self._native_recalculate = native.recalculate_dispatch_normals if self.modern else native.recalculate_normals

    def recalculate(self, positions, *, edge_exclusion_enabled=False, preserved_normals=None):
        vertices = np.ascontiguousarray(positions, dtype=np.float32).reshape(-1, 3)
        if len(vertices) != len(self.targets):
            raise NormalRecalcError("posed positions do not match the normal-recalculation vertices")
        scaled = vertices * np.float32(100.0) if self.modern else vertices
        options = (self.scale,) if self.modern else ()
        output = np.frombuffer(self._native_recalculate(scaled, self.triangles, self.targets, *options),
                               dtype=np.float32).reshape(-1, 3)
        if edge_exclusion_enabled and self.has_edge_exclusions:
            if preserved_normals is None:
                raise NormalRecalcError("the edge-excluding normal write requires the existing posed normals")
            preserved = np.asarray(preserved_normals, dtype=np.float32).reshape(-1, 3)
            if len(preserved) != len(vertices):
                raise NormalRecalcError("preserved normals do not match rendered vertices")
            output = output.copy()
            output[self.exclusions] = preserved[self.exclusions]
        return output


def _normal_dispatch_triangles(groups, vertex_count):
    triangles = []
    for group_index, group in enumerate(groups):
        items = group.work_items
        if not items or len(items) % 3:
            raise NormalRecalcError(f"normal dispatch group {group_index} does not contain complete triangles")
        if len(items) > INDICES_PER_WORKGROUP:
            raise NormalRecalcError(f"normal dispatch group {group_index} exceeds {INDICES_PER_WORKGROUP} triangle corners")
        targets = np.fromiter((item.target_vertex for item in items), dtype=np.intp, count=len(items))
        slots = np.fromiter((item.workgroup_slot for item in items), dtype=np.intp, count=len(items))
        if np.any((targets < 0) | (targets >= vertex_count)):
            raise NormalRecalcError(f"normal dispatch group {group_index} targets a vertex outside LOD0")
        if np.any((slots < 0) | (slots > WORKGROUP_SLOT_MASK)):
            raise NormalRecalcError(f"normal dispatch group {group_index} uses a slot outside the shader's 1,024-entry shared arrays")
        # Aliased shared-array slots take their last writer's target (the preview's choice).
        last = np.zeros(WORKGROUP_SLOT_MASK + 1, dtype=np.intp)
        np.maximum.at(last, slots, np.arange(len(items), dtype=np.intp))
        triangles.append(targets[last[slots]])
    # Integer local sums can be accumulated directly into their resolved global targets.
    return np.concatenate(triangles)


def mesh_normal_recalc_plan(
    mesh: object,
    rendered_triangle_indices: Sequence[int] | None,
    *,
    lod_index: int = 0,
) -> MeshNormalRecalcPlan | None:
    data = getattr(mesh, "normal_recalc_data", None)
    if data is None:
        return None
    body, redirects = _lod_redirects(data, lod_index)
    if not redirects:
        return None
    layout = mesh.profile.normal_recalc_layout
    resolver = _NORMAL_RECALC_PLAN_RESOLVERS.get(layout)
    if resolver is None:
        raise NormalRecalcError(
            f"normal-recalculation preview is unsupported for {layout.value}"
        )
    return resolver(_normal_recalc_lod_view(mesh, lod_index, body, redirects), rendered_triangle_indices)


def _normal_recalc_lod_view(mesh, lod_index, body, redirects):
    view = copy(mesh)
    view.mesh_buffer = copy(mesh.mesh_buffer)
    view.meshes = [copy(mesh.meshes[0])]
    if not 0 <= lod_index < len(view.meshes[0].lods):
        raise NormalRecalcError(f"Missing normal-recalculation LOD {lod_index} geometry")
    lod = copy(view.meshes[0].lods[lod_index])
    view.meshes[0].lods = [lod]
    lod.mesh_groups = [copy(group) for group in lod.mesh_groups]
    draws = [*group.submeshes for group in lod.mesh_groups]
    if not draws or len({draw.buffer_index for draw in draws}) != 1:
        raise NormalRecalcError(f"Normal-recalculation LOD {lod_index} needs one vertex buffer")
    base = min(draw.verts_index_offset for draw in draws)
    payload = copy(mesh.mesh_buffer.buffer_payloads[draws[0].buffer_index])
    source_faces = payload.index_values
    faces = array("I")
    for group in lod.mesh_groups:
        group.submeshes = [copy(draw) for draw in group.submeshes]
        for draw in group.submeshes:
            start, count = draw.faces_index_offset, draw.indices_count
            if start < 0 or start + count > len(source_faces):
                raise NormalRecalcError(f"Invalid normal-recalculation LOD {lod_index} index span")
            draw.faces_index_offset, draw.buffer_index = len(faces), 0
            draw.verts_index_offset -= base
            faces.extend(array("I", source_faces[start:start + count]))
            if count % INDEX_ALIGNMENT:
                faces.append(0)
    payload.integer_faces = faces
    view.mesh_buffer.buffer_payloads = {0: payload}
    view.normal_recalc_data = replace(mesh.normal_recalc_data, redirects=redirects, declared_lod_count=1,
        lods=(replace(body, lod_index=0, vertex_base=0, buffer_index=0),) if body else ())
    return view


def _rendered_submeshes(
    mesh: object,
    rendered_triangle_indices: Sequence[int] | None,
    display_name: str,
) -> tuple[NormalRecalcSubmesh, ...]:
    if rendered_triangle_indices is None:
        raise NormalRecalcError(
            f"{display_name} normal recalculation requires rendered LOD0 triangles"
        )
    submeshes = legacy_normal_recalc_submeshes(mesh)
    rendered = tuple(
        submesh.base_vertex + index
        for submesh in submeshes
        for index in submesh.indices
    )
    if tuple(int(index) for index in rendered_triangle_indices) != rendered:
        raise NormalRecalcError(
            f"rendered triangles do not match the {display_name} "
            "normal-recalculation topology"
        )
    return submeshes


def _legacy_normal_recalc_plan(
    mesh: object,
    rendered_triangle_indices: Sequence[int] | None,
) -> MeshNormalRecalcPlan:
    data = mesh.normal_recalc_data
    layout = mesh.profile.normal_recalc_layout
    submeshes = _rendered_submeshes(mesh, rendered_triangle_indices, layout.value)
    return MeshNormalRecalcPlan(
        tuple(int(item.target_vertex) for item in data.redirects),
        derive_legacy_normal_triangle_vertices(data, submeshes, layout),
    )


def _work_table_normal_recalc_plan(
    mesh: object,
    rendered_triangle_indices: Sequence[int] | None,
    *,
    layout: NormalRecalcLayout,
    display_name: str,
    derive_face_words: Callable[
        [NormalRecalcData, Sequence[NormalRecalcSubmesh]],
        tuple[int, ...],
    ],
    quantization_scale: float = 65536.0,
    use_edge_exclusion: bool = True,
) -> MeshNormalRecalcPlan:
    data = mesh.normal_recalc_data
    submeshes = _rendered_submeshes(mesh, rendered_triangle_indices, display_name)
    face_words = derive_face_words(data, submeshes)
    _validate_topology(data, submeshes, layout)
    dispatch_groups: list[NormalRecalcDispatchGroup] = []
    cursor = 0
    for submesh in submeshes:
        count = len(submesh.indices)
        items = [
            NormalRecalcWorkItem(
                int(word & VERTEX_INDEX_MASK),
                int(word >> VERTEX_INDEX_BITS),
            )
            for word in face_words[cursor : cursor + count]
        ]
        if len(items) != count:
            raise NormalRecalcError(
                f"{display_name} normal face table does not match LOD0 topology"
            )
        dispatch_groups.extend(
            NormalRecalcDispatchGroup(tuple(items[start : start + INDICES_PER_WORKGROUP]))
            for start in range(0, count, INDICES_PER_WORKGROUP)
        )
        cursor += _aligned_index_count(count)
    if cursor != len(face_words):
        raise NormalRecalcError(
            f"{display_name} normal face table does not match LOD0 topology"
        )
    return MeshNormalRecalcPlan(
        redirect_targets=tuple(item.target_vertex for item in data.redirects),
        triangle_vertices=tuple(
            item.target_vertex
            for group in dispatch_groups
            for item in group.work_items
        ),
        dispatch_groups=tuple(dispatch_groups),
        edge_exclusion_mask=(
            tuple(item.annotation for item in data.redirects)
            if use_edge_exclusion
            else ()
        ),
        dispatch_quantization_scale=quantization_scale,
    )


def _sf6_normal_recalc_plan(
    mesh: object,
    rendered_triangle_indices: Sequence[int] | None,
) -> MeshNormalRecalcPlan:
    page_words = (
        _sf6_split_vertex_pages(sf6_normal_recalc_lod_topologies(mesh)[0])
        if mesh.profile.normal_recalc_has_split_vertex_page_plane else ()
    )
    return _work_table_normal_recalc_plan(
        mesh,
        rendered_triangle_indices,
        layout=NormalRecalcLayout.SF6,
        display_name="Street Fighter 6",
        derive_face_words=lambda data, submeshes: derive_sf6_normal_indices(
            data, submeshes, pad_sort_windows=bool(page_words),
        ),
    )


NormalRecalcPlanResolver = Callable[
    [object, Sequence[int] | None],
    MeshNormalRecalcPlan,
]


_SINGLE_REDIRECT_LAYOUTS = frozenset({
    NormalRecalcLayout.SF6,
    NormalRecalcLayout.MHWILDS,
    NormalRecalcLayout.RE9,
    NormalRecalcLayout.ONIMUSHA_WOTS,
    NormalRecalcLayout.DD2_UPDATED,
})


def _aligned_index_count(index_count: int) -> int:
    return align(index_count, INDEX_ALIGNMENT)


def legacy_normal_recalc_submeshes(
    mesh: object,
) -> tuple[NormalRecalcSubmesh, ...]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    meshes = getattr(mesh, "meshes", ())
    if mesh_buffer is None or not meshes or not meshes[0].lods:
        raise NormalRecalcError(
            "legacy normal recalculation requires modeled LOD0 geometry"
        )

    result: list[NormalRecalcSubmesh] = []
    face_cursor = 0
    for group_index, group in enumerate(meshes[0].lods[0].mesh_groups):
        group_start = face_cursor
        for submesh_index, submesh in enumerate(group.submeshes):
            if submesh.faces_index_offset != face_cursor:
                raise NormalRecalcError(
                    f"normal-recalculation submesh {group_index}:"
                    f"{submesh_index} does not follow aligned index topology"
                )
            payload = mesh_buffer.buffer_payloads.get(submesh.buffer_index)
            if payload is None:
                raise NormalRecalcError(
                    f"missing normal-recalculation mesh buffer "
                    f"{submesh.buffer_index}"
                )
            faces = payload.index_values
            end = face_cursor + submesh.indices_count
            if end > len(faces):
                raise NormalRecalcError(
                    f"normal-recalculation index span "
                    f"[{face_cursor}, {end}) is invalid"
                )
            result.append(
                NormalRecalcSubmesh(
                    submesh.verts_index_offset,
                    tuple(int(index) for index in faces[face_cursor:end]),
                )
            )
            face_cursor += _aligned_index_count(submesh.indices_count)
        if face_cursor - group_start != group.face_count:
            raise NormalRecalcError(
                f"normal-recalculation group {group_index} count is not "
                "derived from its aligned submeshes"
            )
    return tuple(result)


def sf6_normal_recalc_lod_topologies(
    mesh: object,
) -> tuple[NormalRecalcLODTopology, ...]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    meshes = getattr(mesh, "meshes", ())
    if mesh_buffer is None or not meshes or not meshes[0].lods:
        raise NormalRecalcError(
            "SF6 normal recalculation requires modeled mesh LOD geometry"
        )

    topologies: list[NormalRecalcLODTopology] = []
    for lod_index, lod in enumerate(meshes[0].lods):
        groups = tuple(lod.mesh_groups)
        submesh_records = tuple(
            submesh
            for group in groups
            for submesh in group.submeshes
        )
        if not submesh_records:
            raise NormalRecalcError(
                f"SF6 normal-recalculation LOD {lod_index} has no submeshes"
            )
        buffer_indices = {int(item.buffer_index) for item in submesh_records}
        if len(buffer_indices) != 1:
            raise NormalRecalcError(
                f"SF6 normal-recalculation LOD {lod_index} spans buffers "
                f"{sorted(buffer_indices)}"
            )
        buffer_index = next(iter(buffer_indices))
        payload = mesh_buffer.buffer_payloads.get(buffer_index)
        if payload is None:
            raise NormalRecalcError(
                f"missing SF6 normal-recalculation mesh buffer {buffer_index}"
            )
        faces = payload.index_values
        vertex_base = min(int(item.verts_index_offset) for item in submesh_records)
        vertex_count = sum(int(group.vertex_count) for group in groups)
        vertex_end = vertex_base + vertex_count
        if max(
            int(item.verts_index_offset) + int(item.vert_count)
            for item in submesh_records
        ) != vertex_end:
            raise NormalRecalcError(
                f"SF6 normal-recalculation LOD {lod_index} vertex span is not "
                "derived from its mesh groups"
            )

        semantic_submeshes: list[NormalRecalcSubmesh] = []
        for group_index, group in enumerate(groups):
            aligned_group_count = sum(
                _aligned_index_count(int(item.indices_count))
                for item in group.submeshes
            )
            if aligned_group_count != int(group.face_count):
                raise NormalRecalcError(
                    f"SF6 normal-recalculation LOD {lod_index} group "
                    f"{group_index} index count is not submesh-derived"
                )
            for submesh_index, submesh in enumerate(group.submeshes):
                start = int(submesh.faces_index_offset)
                end = start + int(submesh.indices_count)
                if start < 0 or end > len(faces):
                    raise NormalRecalcError(
                        f"SF6 normal-recalculation LOD {lod_index} submesh "
                        f"{group_index}:{submesh_index} index span is invalid"
                    )
                base_vertex = int(submesh.verts_index_offset) - vertex_base
                if not 0 <= base_vertex < vertex_count:
                    raise NormalRecalcError(
                        f"SF6 normal-recalculation LOD {lod_index} submesh "
                        f"{group_index}:{submesh_index} vertex base is invalid"
                    )
                semantic_submeshes.append(
                    NormalRecalcSubmesh(
                        base_vertex,
                        tuple(int(index) for index in faces[start:end]),
                        int(submesh.vert_count),
                    )
                )
        topologies.append(
            NormalRecalcLODTopology(
                lod_index,
                buffer_index,
                vertex_base,
                vertex_count,
                tuple(semantic_submeshes),
                bool(mesh_buffer.has_32bit_indices),
            )
        )
    return tuple(topologies)


def _require_range(
    start: int,
    size: int,
    lower: int,
    upper: int,
    label: str,
) -> None:
    end = start + size
    if start < lower or size < 0 or end < start or end > upper:
        raise NormalRecalcError(
            f"{label} range [{start}, {end}) exceeds [{lower}, {upper})"
        )


def _profile_for_layout(
    vertex_count: int,
    layout: NormalRecalcLayout,
) -> LegacyNormalRecalcProfile:
    if layout is NormalRecalcLayout.DMC5:
        if vertex_count <= VERTEX_PAGE_SIZE:
            return LegacyNormalRecalcProfile.STANDARD
        if vertex_count <= MAX_TWO_PAGE_VERTICES:
            return LegacyNormalRecalcProfile.TWO_PAGE_16BIT
        raise NormalRecalcError(
            "DMC5 normal recalculation beyond two 16-bit vertex pages "
            "is not semantically understood"
        )
    if layout not in _NORMAL_PLAN_LAYOUTS:
        raise NormalRecalcError(
            f"{layout.value} is not a legacy packed normal-recalculation layout"
        )
    if vertex_count > VERTEX_INDEX_MASK + 1:
        raise NormalRecalcError(
            f"{layout.value.upper()} normal recalculation exceeds its "
            "22-bit vertex domain"
        )
    return LegacyNormalRecalcProfile.STANDARD


def _validate_topology(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    layout: NormalRecalcLayout,
) -> LegacyNormalRecalcProfile:
    redirects = data.redirects
    if len(redirects) > VERTEX_INDEX_MASK + 1:
        raise NormalRecalcError("normal-recalculation vertex count exceeds 22 bits")
    for vertex, redirect in enumerate(redirects):
        target = redirect.target_vertex
        if type(target) is not int or not 0 <= target < len(redirects):
            raise NormalRecalcError(
                f"normal redirect {vertex} targets vertex {target!r} outside LOD0"
            )
        if type(redirect.annotation) is not bool:
            raise NormalRecalcError(
                f"normal redirect {vertex} annotation must be boolean"
            )
    # SF6's write shader follows exactly one redirect, so A -> B -> C chains must not be collapsed.
    if layout not in _SINGLE_REDIRECT_LAYOUTS:
        for vertex, redirect in enumerate(redirects):
            target = redirect.target_vertex
            if redirects[target].target_vertex != target:
                raise NormalRecalcError(
                    f"normal redirect {vertex} does not resolve to a canonical vertex"
                )

    for submesh_index, submesh in enumerate(submeshes):
        if type(submesh.base_vertex) is not int or submesh.base_vertex < 0:
            raise NormalRecalcError(
                f"normal-recalculation submesh {submesh_index} has an invalid base vertex"
            )
        if len(submesh.indices) % 3:
            raise NormalRecalcError(
                f"normal-recalculation submesh {submesh_index} indices "
                "do not form complete triangles"
            )
        for raw_index in submesh.indices:
            if type(raw_index) is not int or raw_index < 0:
                raise NormalRecalcError(
                    f"normal-recalculation submesh {submesh_index} has "
                    f"invalid vertex index {raw_index!r}"
                )
            vertex = submesh.base_vertex + raw_index
            if not 0 <= vertex < len(redirects):
                raise NormalRecalcError(
                    f"normal-recalculation submesh {submesh_index} references "
                    f"vertex {vertex!r} outside LOD0"
                )

    profile = data.legacy_profile
    if profile is not None and not isinstance(profile, LegacyNormalRecalcProfile):
        raise NormalRecalcError("invalid legacy normal-recalculation profile")
    if profile is LegacyNormalRecalcProfile.TWO_PAGE_16BIT and layout is not NormalRecalcLayout.DMC5:
        raise NormalRecalcError("split-page normal tables are only supported for DMC5")
    if profile is LegacyNormalRecalcProfile.TWO_PAGE_16BIT and not (
        VERTEX_PAGE_SIZE < len(redirects) <= MAX_TWO_PAGE_VERTICES
    ):
        raise NormalRecalcError("split-page normal tables require two 16-bit vertex pages")
    if profile is None:
        profile = _profile_for_layout(len(redirects), layout)
    if profile is LegacyNormalRecalcProfile.TWO_PAGE_16BIT:
        pages = set()
        for submesh_index, submesh in enumerate(submeshes):
            if (
                submesh.base_vertex % VERTEX_PAGE_SIZE
                or submesh.base_vertex > VERTEX_PAGE_SIZE
            ):
                raise NormalRecalcError(
                    f"large-mesh submesh {submesh_index} does not start "
                    "on a supported 16-bit vertex page"
                )
            if len(submesh.indices) % INDEX_ALIGNMENT:
                raise NormalRecalcError(
                    "large-mesh normal recalculation with odd submesh "
                    "index counts is not semantically understood"
                )
            if any(index > 0xFFFF for index in submesh.indices):
                raise NormalRecalcError(
                    f"large-mesh submesh {submesh_index} has a non-16-bit index"
                )
            pages.add(submesh.base_vertex // VERTEX_PAGE_SIZE)
        if pages != {0, 1}:
            raise NormalRecalcError(
                "large-mesh normal recalculation requires both observed "
                "16-bit vertex pages"
            )
        if any(
            redirect.target_vertex != vertex
            for vertex, redirect in enumerate(redirects)
        ):
            raise NormalRecalcError(
                "large-mesh normal recalculation with aliased redirects "
                "is not semantically understood"
            )
    return profile


def _pack_normal_indices(
    canonical: Sequence[int],
    slot_values: Sequence[int],
    slot_domain: Sequence[int],
) -> list[int]:
    slots = {
        value: slot
        for slot, value in enumerate(sorted(set(slot_domain)))
    }
    return [
        (
            vertex | (slots[value] << VERTEX_INDEX_BITS)
            if slots[value] <= WORKGROUP_SLOT_MASK
            else 0
        )
        for vertex, value in zip(canonical, slot_values)
    ]


def _derive_standard_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    *,
    pad_sort_windows: bool = False,
) -> tuple[int, ...]:
    packed: list[int] = []
    redirects = data.redirects
    for submesh in submeshes:
        canonical = tuple(
            redirects[submesh.base_vertex + index].target_vertex
            for index in submesh.indices
        )
        for start in range(0, len(canonical), INDICES_PER_WORKGROUP):
            batch = canonical[start : start + INDICES_PER_WORKGROUP]
            domain = (*batch, 0) if pad_sort_windows and len(batch) < INDICES_PER_WORKGROUP else batch
            packed.extend(_pack_normal_indices(batch, batch, domain))
        if len(canonical) % INDEX_ALIGNMENT:
            packed.append(0)
    return tuple(packed)


def _derive_large_dmc5_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> tuple[int, ...]:
    physical_indices: list[int] = []
    physical_starts: list[int] = []
    for submesh in submeshes:
        physical_starts.append(len(physical_indices))
        physical_indices.extend(submesh.indices)
        if len(submesh.indices) % INDEX_ALIGNMENT:
            physical_indices.append(0)

    packed: list[int] = []
    redirects = data.redirects
    for submesh, physical_start in zip(submeshes, physical_starts):
        canonical = tuple(
            redirects[submesh.base_vertex + index].target_vertex
            for index in submesh.indices
        )
        for start in range(0, len(canonical), INDICES_PER_WORKGROUP):
            batch = canonical[start : start + INDICES_PER_WORKGROUP]
            raw_batch = submesh.indices[
                start : start + INDICES_PER_WORKGROUP
            ]
            window_start = physical_start + start
            window = physical_indices[
                window_start : window_start + INDICES_PER_WORKGROUP
            ]
            if len(window) < INDICES_PER_WORKGROUP:
                window = [
                    *window,
                    *([0] * (INDICES_PER_WORKGROUP - len(window))),
                ]
            packed.extend(
                _pack_normal_indices(batch, raw_batch, window)
            )
        if len(canonical) % INDEX_ALIGNMENT:
            packed.append(0)
    return tuple(packed)


def _derive_large_dmc5_page_plane(
    submeshes: Sequence[NormalRecalcSubmesh],
) -> tuple[int, ...]:
    values: list[int] = []
    for submesh in submeshes:
        page = submesh.base_vertex // VERTEX_PAGE_SIZE
        values.extend(((page - 1) & 0xFFFF,) * len(submesh.indices))
        if len(submesh.indices) % INDEX_ALIGNMENT:
            values.append(0)
    return tuple(values)


def _derive_packed_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    profile: LegacyNormalRecalcProfile,
) -> tuple[int, ...]:
    if profile is LegacyNormalRecalcProfile.TWO_PAGE_16BIT:
        return _derive_large_dmc5_normal_indices(data, submeshes)
    return _derive_standard_normal_indices(data, submeshes)


def derive_legacy_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    layout: NormalRecalcLayout,
) -> tuple[int, ...]:
    profile = _validate_topology(data, submeshes, layout)
    return _derive_packed_normal_indices(data, submeshes, profile)


derive_dmc5_normal_indices = partial(derive_legacy_normal_indices, layout=NormalRecalcLayout.DMC5)
derive_re4_normal_indices = partial(derive_legacy_normal_indices, layout=NormalRecalcLayout.RE4)


def legacy_normal_recalc_words(mesh: object) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The oldest files' section: one word per vertex, then one per index."""
    data = mesh.normal_recalc_data
    layout = mesh.header.profile.normal_recalc_layout
    return _redirect_words(data.redirects), derive_legacy_normal_indices(
        data, legacy_normal_recalc_submeshes(mesh), layout)


def derive_re8_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> tuple[int, ...]:
    _validate_topology(data, submeshes, NormalRecalcLayout.RE8)
    work_items = data.lods[0].work_items if data.lods else ()
    logical_count = sum(len(submesh.indices) for submesh in submeshes)
    if not work_items:
        packed: list[int] = []
        for submesh_index, submesh in enumerate(submeshes):
            canonical = tuple(
                data.redirects[
                    submesh.base_vertex + index
                ].target_vertex
                for index in submesh.indices
            )
            for start in range(0, len(canonical), INDICES_PER_WORKGROUP):
                batch = canonical[start : start + INDICES_PER_WORKGROUP]
                unique_targets = sorted(set(batch))
                if len(unique_targets) > WORKGROUP_SLOT_MASK + 1:
                    raise NormalRecalcError(
                        f"RE8 normal submesh {submesh_index} compute batch "
                        f"{start // INDICES_PER_WORKGROUP} needs "
                        f"{len(unique_targets)} local accumulators; explicit "
                        "producer workgroup slots are required"
                    )
                slots = {
                    target: slot
                    for slot, target in enumerate(unique_targets)
                }
                packed.extend(
                    target | (slots[target] << VERTEX_INDEX_BITS)
                    for target in batch
                )
            if len(canonical) % INDEX_ALIGNMENT:
                packed.append(0)
        return tuple(packed)
    if len(work_items) != logical_count:
        raise NormalRecalcError(
            "RE8 normal work-item count does not match LOD0 topology"
        )

    packed: list[int] = []
    cursor = 0
    for submesh in submeshes:
        count = len(submesh.indices)
        for item in work_items[cursor : cursor + count]:
            if not isinstance(item, NormalRecalcWorkItem):
                raise NormalRecalcError(
                    "RE8 normal work table contains a non-semantic entry"
                )
            if not 0 <= item.target_vertex < len(data.redirects):
                raise NormalRecalcError(
                    f"RE8 normal work item targets vertex "
                    f"{item.target_vertex!r} outside LOD0"
                )
            if not 0 <= item.workgroup_slot <= WORKGROUP_SLOT_MASK:
                raise NormalRecalcError(
                    f"RE8 normal workgroup slot "
                    f"{item.workgroup_slot!r} exceeds 10 bits"
                )
            packed.append(
                item.target_vertex
                | (item.workgroup_slot << VERTEX_INDEX_BITS)
            )
        cursor += count
        if count % INDEX_ALIGNMENT:
            packed.append(0)
    return tuple(packed)


def _lod_redirects(data: NormalRecalcData, lod_index: int) -> tuple[NormalRecalcLOD | None, tuple]:
    body = next((item for item in data.lods if item.lod_index == lod_index), None)
    return body, body.redirects if body is not None and body.redirects else data.redirects if lod_index == 0 else ()


def _sf6_lod_semantic_data(
    data: NormalRecalcData,
    lod_index: int,
) -> NormalRecalcData:
    redirects = _lod_redirects(data, lod_index)[1]
    if not redirects:
        raise NormalRecalcError(
            f"SF6 normal-recalculation LOD {lod_index} has no redirects"
        )
    return NormalRecalcData(redirects=tuple(redirects))


def derive_sf6_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    lod_index: int = 0,
    *,
    pad_sort_windows: bool = False,
) -> tuple[int, ...]:
    local_data = _sf6_lod_semantic_data(data, lod_index)
    _validate_topology(local_data, submeshes, NormalRecalcLayout.SF6)
    return _derive_standard_normal_indices(
        local_data, submeshes, pad_sort_windows=pad_sort_windows,
    )


def _sf6_split_vertex_pages(topology: NormalRecalcLODTopology) -> tuple[int, ...]:
    page_size = 1 << 16
    submeshes = topology.submeshes
    if topology.uses_32bit_indices or not any(s.vertex_count == page_size for s in submeshes):
        return ()
    if (len(submeshes) != 2 or submeshes[0].base_vertex != 0
            or submeshes[0].vertex_count != page_size
            or submeshes[1].base_vertex != page_size
            or not 0 < submeshes[1].vertex_count <= page_size
            or sum(s.vertex_count for s in submeshes) != topology.vertex_count):
        raise NormalRecalcError("SF6 split normal topology is outside the verified two-page domain")
    words = []
    for submesh in submeshes:
        selector = (1 - submesh.base_vertex // page_size) << 16
        words.extend([selector] * len(submesh.indices))
        if len(submesh.indices) % INDEX_ALIGNMENT:
            words.append(0)
    return tuple(words)


def derive_re9_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> tuple[int, ...]:
    if data.lods and data.lods[0].face_entries:
        return data.lods[0].face_entries
    return derive_sf6_normal_indices(data, submeshes)


def _triangle_vertices(
    packed: Sequence[int],
    submeshes: Sequence[NormalRecalcSubmesh],
) -> tuple[int, ...]:
    vertices: list[int] = []
    cursor = 0
    for submesh in submeshes:
        count = len(submesh.indices)
        vertices.extend(word & VERTEX_INDEX_MASK for word in packed[cursor : cursor + count])
        cursor += _aligned_index_count(count)
    return tuple(vertices)


def derive_legacy_normal_triangle_vertices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    layout: NormalRecalcLayout,
) -> tuple[int, ...]:
    return _triangle_vertices(derive_legacy_normal_indices(data, submeshes, layout), submeshes)


def _require_section_offset(section_offset: int, alignment: int = 1, name: str = "") -> None:
    if type(section_offset) is not int or section_offset < 0:
        raise NormalRecalcError(
            "normal-recalculation section offset must be a nonnegative integer"
        )
    if section_offset % alignment:
        raise NormalRecalcError(
            f"{name} normal-recalculation sections must be {alignment}-byte aligned"
        )


def _redirect_words(redirects: Sequence[NormalRecalcRedirect]) -> tuple[int, ...]:
    return tuple(
        redirect.target_vertex | (REDIRECT_ANNOTATION_MASK if redirect.annotation else 0)
        for redirect in redirects
    )


def _pack_words(encoded: bytearray, offset: int, words: Sequence[int]) -> None:
    if words:
        struct.pack_into(f"<{len(words)}I", encoded, offset, *words)


def _serialize_legacy_normal_recalc(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    *,
    section_offset: int = 0,
    layout: NormalRecalcLayout,
) -> bytes:
    _require_section_offset(section_offset)
    if not isinstance(data, NormalRecalcData):
        raise NormalRecalcError("expected legacy normal-recalculation data")
    if (
        data.lods
        or data.edge_excluded
        or data.reserved
        or data.declared_lod_count
        or data.body_list_offset
        or data.body_offsets
    ):
        raise NormalRecalcError(
            "legacy normal-recalculation data contains fields from a modern layout"
        )

    profile = _validate_topology(data, submeshes, layout)
    packed_indices = _derive_packed_normal_indices(data, submeshes, profile)
    redirect_words = _redirect_words(data.redirects)
    redirects_offset = align(section_offset + 16, ALIGNMENT)
    indices_offset = align(redirects_offset + len(redirect_words) * 4, ALIGNMENT)
    indices_end = indices_offset + len(packed_indices) * 4
    page_words = (
        _derive_large_dmc5_page_plane(submeshes)
        if profile is LegacyNormalRecalcProfile.TWO_PAGE_16BIT
        else ()
    )
    object_end = (
        indices_end + len(page_words) * 4
        if page_words
        else align(indices_end, ALIGNMENT)
    )

    encoded = bytearray(object_end - section_offset)
    struct.pack_into("<QQ", encoded, 0, redirects_offset, indices_offset)
    _pack_words(encoded, redirects_offset - section_offset, redirect_words)
    _pack_words(encoded, indices_offset - section_offset, packed_indices)
    _pack_words(encoded, indices_end - section_offset, page_words)
    return encoded.take_bytes()


def _serialize_lod0_normal_recalc(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    *,
    section_offset: int = 0,
    derive_face_words: Callable[
        [NormalRecalcData, Sequence[NormalRecalcSubmesh]],
        tuple[int, ...],
    ],
    alignment: int,
    name: str,
    body_list: bool = True,
) -> bytes:
    _require_section_offset(section_offset, alignment, name)
    if not isinstance(data, NormalRecalcData):
        raise NormalRecalcError(f"expected {name} normal-recalculation data")
    if data.edge_excluded or any(data.reserved):
        raise NormalRecalcError(
            f"{name} normal-recalculation reserved header fields must be zero"
        )
    if data.declared_lod_count not in (0, 1) or (
        data.lods and (len(data.lods) != 1 or data.lods[0].lod_index != 0)
    ):
        raise NormalRecalcError(
            f"{name} normal-recalculation data must contain only one LOD0 body"
        )

    face_words = derive_face_words(data, submeshes)
    redirect_words = _redirect_words(data.redirects)
    if body_list:
        body_offset = align(section_offset + 24, ALIGNMENT)
        redirects_offset = body_offset + 16
    else:
        redirects_offset = section_offset + 16
    faces_offset = align(redirects_offset + len(redirect_words) * 4, ALIGNMENT)
    object_end = align(faces_offset + len(face_words) * 4, ALIGNMENT)

    encoded = bytearray(object_end - section_offset)
    if body_list:
        struct.pack_into("<qqq", encoded, 0, 1, section_offset + 16, body_offset)
    struct.pack_into("<qq", encoded, redirects_offset - 16 - section_offset, redirects_offset, faces_offset)
    _pack_words(encoded, redirects_offset - section_offset, redirect_words)
    _pack_words(encoded, faces_offset - section_offset, face_words)
    return encoded.take_bytes()


serialize_dmc5_normal_recalc = partial(_serialize_legacy_normal_recalc, layout=NormalRecalcLayout.DMC5)
serialize_re2_normal_recalc = partial(_serialize_legacy_normal_recalc, layout=NormalRecalcLayout.RE2)
serialize_re3_normal_recalc = partial(_serialize_legacy_normal_recalc, layout=NormalRecalcLayout.RE3)
serialize_re8_normal_recalc = partial(
    _serialize_lod0_normal_recalc,
    derive_face_words=derive_re8_normal_indices,
    alignment=8,
    name="RE8",
    body_list=False,
)
serialize_re4_normal_recalc = partial(
    _serialize_lod0_normal_recalc,
    derive_face_words=derive_re4_normal_indices,
    alignment=8,
    name="RE4",
)


def _rt_serializer(layout: NormalRecalcLayout, name: str):
    return partial(
        _serialize_lod0_normal_recalc,
        derive_face_words=partial(derive_legacy_normal_indices, layout=layout),
        alignment=ALIGNMENT,
        name=name,
    )


serialize_mhrise_normal_recalc = _rt_serializer(NormalRecalcLayout.MHRISE, "Monster Hunter Rise")
serialize_re2rt_normal_recalc = _rt_serializer(NormalRecalcLayout.RE2_RT, "RE2 RT")
serialize_re3rt_normal_recalc = _rt_serializer(NormalRecalcLayout.RE3_RT, "RE3 RT")


def serialize_sf6_normal_recalc(
    data: NormalRecalcData,
    lod_topologies: Sequence[NormalRecalcLODTopology],
    *,
    section_offset: int = 0,
    section_alignment: int = 8,
    split_vertex_page_plane: bool = True,
) -> bytes:
    if section_alignment <= 0 or section_alignment & (section_alignment - 1):
        raise NormalRecalcError(
            "normal-recalculation section alignment must be a positive power of two"
        )
    _require_section_offset(section_offset, section_alignment, "SF6-family")
    if not isinstance(data, NormalRecalcData):
        raise NormalRecalcError("expected SF6 normal-recalculation data")
    topologies = tuple(lod_topologies)
    if not topologies:
        raise NormalRecalcError("SF6 normal-recalculation data has no LODs")
    if tuple(item.lod_index for item in topologies) != tuple(range(len(topologies))):
        raise NormalRecalcError(
            "SF6 normal-recalculation topologies must be ordered from LOD0"
        )
    if data.edge_excluded or any(data.reserved):
        raise NormalRecalcError(
            "SF6 normal-recalculation reserved header fields must be zero"
        )
    body_count = data.declared_lod_count or (
        len(data.lods) if data.lods else len(topologies)
    )
    if not 0 < body_count <= len(topologies):
        raise NormalRecalcError(
            "SF6 normal-recalculation LOD count exceeds its mesh topology"
        )
    topologies = topologies[:body_count]
    if data.lods and tuple(item.lod_index for item in data.lods) != tuple(
        range(body_count)
    ):
        raise NormalRecalcError(
            "SF6 normal-recalculation data must contain its leading LOD bodies"
        )

    redirect_tables: list[tuple[int, ...]] = []
    face_tables: list[tuple[int, ...]] = []
    page_tables: list[tuple[int, ...]] = []
    for topology in topologies:
        local_data = _sf6_lod_semantic_data(data, topology.lod_index)
        if len(local_data.redirects) != topology.vertex_count:
            raise NormalRecalcError(
                f"SF6 normal-recalculation LOD {topology.lod_index} redirect "
                "count does not match its vertex domain"
            )
        redirect_tables.append(_redirect_words(local_data.redirects))
        page_words = _sf6_split_vertex_pages(topology) if split_vertex_page_plane else ()
        page_tables.append(page_words)
        face_tables.append(
            derive_sf6_normal_indices(
                data,
                topology.submeshes,
                topology.lod_index,
                pad_sort_windows=bool(page_words),
            )
        )

    body_list_offset = section_offset + 16
    cursor = align(body_list_offset + len(topologies) * 8, ALIGNMENT)
    body_offsets: list[int] = []
    redirects_offsets: list[int] = []
    faces_offsets: list[int] = []
    for redirect_words, face_words, page_words in zip(
        redirect_tables,
        face_tables,
        page_tables,
    ):
        body_offset = cursor
        redirects_offset = body_offset + 16
        faces_offset = align(redirects_offset + len(redirect_words) * 4, ALIGNMENT)
        cursor = align(faces_offset + (len(face_words) + len(page_words)) * 4, ALIGNMENT)
        body_offsets.append(body_offset)
        redirects_offsets.append(redirects_offset)
        faces_offsets.append(faces_offset)

    encoded = bytearray(cursor - section_offset)
    struct.pack_into(
        "<BBHIq",
        encoded,
        0,
        body_count,
        0,
        0,
        0,
        body_list_offset,
    )
    struct.pack_into(
        f"<{len(body_offsets)}q",
        encoded,
        body_list_offset - section_offset,
        *body_offsets,
    )
    for body_offset, redirects_offset, faces_offset, redirect_words, face_words, page_words in zip(
        body_offsets,
        redirects_offsets,
        faces_offsets,
        redirect_tables,
        face_tables,
        page_tables,
    ):
        struct.pack_into(
            "<qq",
            encoded,
            body_offset - section_offset,
            redirects_offset,
            faces_offset,
        )
        _pack_words(encoded, redirects_offset - section_offset, redirect_words)
        _pack_words(encoded, faces_offset - section_offset, face_words)
        _pack_words(encoded, faces_offset + len(face_words) * 4 - section_offset, page_words)
    return encoded.take_bytes()


def _unpack_words(view, offset: int, count: int) -> tuple[int, ...]:
    return struct.unpack_from(f"<{count}I", view, offset) if count else ()


def _decode_redirects(
    words: Sequence[int],
    display_name: str = "",
    vertex_count: int | None = None,
) -> tuple[NormalRecalcRedirect, ...]:
    redirects: list[NormalRecalcRedirect] = []
    for vertex, word in enumerate(words):
        if word & REDIRECT_RESERVED_MASK:
            raise NormalRecalcError(
                f"{display_name} normal redirect {vertex} has unsupported bits 22-30".lstrip()
            )
        target_vertex = int(word & VERTEX_INDEX_MASK)
        if vertex_count is not None and target_vertex >= vertex_count:
            raise NormalRecalcError(
                f"{display_name} normal redirect {vertex} targets vertex "
                f"{target_vertex} outside LOD0"
            )
        redirects.append(NormalRecalcRedirect(target_vertex, bool(word & REDIRECT_ANNOTATION_MASK)))
    return tuple(redirects)


def _require_derived(stored: Sequence[int], derived: Sequence[int], message: str) -> None:
    if tuple(stored) != tuple(derived):
        mismatch = next(
            (
                index
                for index, (actual, expected) in enumerate(zip(stored, derived))
                if actual != expected
            ),
            min(len(stored), len(derived)),
        )
        raise NormalRecalcError(message.format(mismatch))


def _parse_legacy_normal_recalc(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    section_end: int,
    vertex_count: int,
    submeshes: Sequence[NormalRecalcSubmesh],
    layout: NormalRecalcLayout,
) -> NormalRecalcData:
    view = memoryview(file_bytes)
    if not 0 <= section_offset <= section_end <= len(view):
        raise NormalRecalcError("normal-recalculation section bounds are invalid")
    if type(vertex_count) is not int or vertex_count < 0:
        raise NormalRecalcError("normal-recalculation vertex count is invalid")
    if vertex_count > VERTEX_INDEX_MASK + 1:
        raise NormalRecalcError("normal-recalculation vertex count exceeds 22 bits")
    _require_range(section_offset, 16, section_offset, section_end, "header")
    redirects_offset, indices_offset = struct.unpack_from(
        "<QQ", view, section_offset
    )
    index_count = sum(
        _aligned_index_count(len(submesh.indices))
        for submesh in submeshes
    )
    redirect_size = vertex_count * 4
    index_size = index_count * 4
    if vertex_count and not redirects_offset:
        raise NormalRecalcError("normal-recalculation redirect pointer is null")
    if index_count and not indices_offset:
        raise NormalRecalcError("normal-recalculation index pointer is null")
    if redirects_offset % ALIGNMENT or indices_offset % ALIGNMENT:
        raise NormalRecalcError("normal-recalculation arrays are not 16-byte aligned")
    if redirects_offset != align(section_offset + 16, ALIGNMENT):
        raise NormalRecalcError(
            "normal-recalculation redirects do not follow the aligned header"
        )
    _require_range(redirects_offset, redirect_size, section_offset + 16, section_end, "redirect table")
    _require_range(indices_offset, index_size, section_offset + 16, section_end, "index table")

    redirect_end = redirects_offset + redirect_size
    if indices_offset != align(redirect_end, ALIGNMENT):
        raise NormalRecalcError(
            "normal-recalculation index table does not follow the aligned redirects"
        )
    index_end = indices_offset + index_size
    profile = (
        LegacyNormalRecalcProfile.STANDARD
        if layout is NormalRecalcLayout.DMC5 and section_end == align(index_end, ALIGNMENT)
        else _profile_for_layout(vertex_count, layout)
    )
    paged = profile is LegacyNormalRecalcProfile.TWO_PAGE_16BIT
    object_end = index_end + index_size if paged else align(index_end, ALIGNMENT)
    if paged:
        _require_range(index_end, index_size, section_offset + 16, section_end, "vertex-page table")
    if section_end != object_end:
        raise NormalRecalcError(
            f"normal-recalculation section does not match the "
            f"{profile.value} profile"
        )
    if (
        any(view[section_offset + 16:redirects_offset])
        or any(view[redirect_end:indices_offset])
        or (not paged and any(view[index_end:object_end]))
    ):
        raise NormalRecalcError("normal-recalculation alignment padding is nonzero")

    data = NormalRecalcData(
        _decode_redirects(_unpack_words(view, redirects_offset, vertex_count)),
        legacy_profile=(
            profile
            if layout is NormalRecalcLayout.DMC5
            and vertex_count > VERTEX_PAGE_SIZE
            and profile is LegacyNormalRecalcProfile.STANDARD
            else None
        ),
    )
    if _validate_topology(data, submeshes, layout) is not profile:
        raise NormalRecalcError(
            "normal-recalculation topology does not match its derived profile"
        )
    _require_derived(
        _unpack_words(view, indices_offset, index_count),
        _derive_packed_normal_indices(data, submeshes, profile),
        "normal-recalculation index {} is not topology-derived",
    )
    if paged:
        _require_derived(
            _unpack_words(view, index_end, index_count),
            _derive_large_dmc5_page_plane(submeshes),
            "normal-recalculation vertex-page entry {} is not topology-derived",
        )
    return data


parse_dmc5_normal_recalc = partial(_parse_legacy_normal_recalc, layout=NormalRecalcLayout.DMC5)
parse_re2_normal_recalc = partial(_parse_legacy_normal_recalc, layout=NormalRecalcLayout.RE2)
parse_re3_normal_recalc = partial(_parse_legacy_normal_recalc, layout=NormalRecalcLayout.RE3)


def _parse_lod0_normal_recalc(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    section_end: int,
    vertex_count: int,
    submeshes: Sequence[NormalRecalcSubmesh],
    *,
    layout: NormalRecalcLayout,
    name: str,
    alignment: int,
    body_list: bool = True,
    producer_slots: bool = False,
    keep_work_items: bool = True,
) -> NormalRecalcData:
    view = memoryview(file_bytes)
    if not 0 <= section_offset <= section_end <= len(view):
        raise NormalRecalcError(f"{name} normal-recalculation bounds are invalid")
    if section_offset % alignment:
        raise NormalRecalcError(
            f"{name} normal-recalculation section is not {alignment}-byte aligned"
        )
    if type(vertex_count) is not int or not 0 <= vertex_count <= VERTEX_INDEX_MASK + 1:
        raise NormalRecalcError(f"{name} normal-recalculation vertex count is invalid")
    _require_range(section_offset, 16, section_offset, section_end, "header")
    padding: list[tuple[int, int]] = []
    body_list_offset = 0
    body_offset = section_offset
    if body_list:
        # lodCount is one byte followed by zero reserved fields, so one LOD reads as int64 1.
        lod_header, body_list_offset = struct.unpack_from("<qq", view, section_offset)
        if lod_header != 1:
            raise NormalRecalcError(
                f"{name} normal-recalculation header {lod_header:#x} is not the "
                "shipped one-LOD layout"
            )
        if body_list_offset != section_offset + 16:
            raise NormalRecalcError(
                f"{name} normal-recalculation body list does not follow its header"
            )
        _require_range(body_list_offset, 8, section_offset, section_end, "body list")
        body_offset = struct.unpack_from("<q", view, body_list_offset)[0]
        if body_offset != align(body_list_offset + 8, ALIGNMENT):
            raise NormalRecalcError(
                f"{name} normal-recalculation body is not at its canonical "
                "aligned location"
            )
        _require_range(body_offset, 16, section_offset, section_end, "LOD0 body")
        padding.append((body_list_offset + 8, body_offset))
    redirects_offset, faces_offset = struct.unpack_from("<qq", view, body_offset)
    if redirects_offset != body_offset + 16:
        raise NormalRecalcError(f"{name} normal redirects do not follow the LOD0 body")

    face_count = sum(_aligned_index_count(len(submesh.indices)) for submesh in submeshes)
    redirects_end = redirects_offset + vertex_count * 4
    if faces_offset != align(redirects_end, ALIGNMENT):
        raise NormalRecalcError(
            f"{name} normal face table does not follow the aligned redirect table"
        )
    faces_end = faces_offset + face_count * 4
    padding.append((redirects_end, faces_offset))
    if body_list:
        if align(faces_end, ALIGNMENT) != section_end:
            raise NormalRecalcError(
                f"{name} normal-recalculation section size is not topology-derived"
            )
        padding.append((faces_end, section_end))
    elif faces_end > section_end:
        raise NormalRecalcError(
            f"{name} normal-recalculation section is smaller than its semantic tables"
        )
    # RE8 hair meshes can have an unread scratch tail after the two tables.
    _require_range(redirects_offset, vertex_count * 4, section_offset, section_end, "redirect table")
    _require_range(faces_offset, face_count * 4, section_offset, section_end, "face table")
    if any(any(view[start:end]) for start, end in padding):
        raise NormalRecalcError(f"{name} normal-recalculation alignment padding is nonzero")

    redirects, redirect_words, face_words, work_items = _decode_normal_work_tables(
        view, redirects_offset, faces_offset, vertex_count, submeshes, display_name=name,
    )
    data = NormalRecalcData(
        redirects=redirects,
        lods=(
            NormalRecalcLOD(
                0,
                body_offset,
                redirects_offset,
                faces_offset,
                redirect_words,
                face_words,
                work_items if keep_work_items else (),
            ),
        ),
        declared_lod_count=1,
        body_list_offset=body_list_offset,
        body_offsets=(body_offset,),
    )
    if producer_slots:
        _validate_topology(data, submeshes, layout)
    else:
        _require_derived(
            face_words,
            derive_legacy_normal_indices(data, submeshes, layout),
            f"{name} normal face word {{}} is not derived from redirects and LOD0 topology",
        )
    return data


def _rt_parser(layout: NormalRecalcLayout, name: str):
    return partial(
        _parse_lod0_normal_recalc,
        layout=layout,
        name=name,
        alignment=ALIGNMENT,
        keep_work_items=False,
    )


parse_mhrise_normal_recalc = _rt_parser(NormalRecalcLayout.MHRISE, "Monster Hunter Rise")
parse_re2rt_normal_recalc = _rt_parser(NormalRecalcLayout.RE2_RT, "RE2 RT")
parse_re3rt_normal_recalc = _rt_parser(NormalRecalcLayout.RE3_RT, "RE3 RT")
parse_re4_normal_recalc = partial(
    _parse_lod0_normal_recalc, layout=NormalRecalcLayout.RE4, name="RE4", alignment=8,
)
parse_re8_normal_recalc = partial(
    _parse_lod0_normal_recalc,
    layout=NormalRecalcLayout.RE8,
    name="RE8",
    alignment=1,
    body_list=False,
    producer_slots=True,
)


def _decode_normal_work_tables(
    view: memoryview,
    redirects_offset: int,
    faces_offset: int,
    vertex_count: int,
    submeshes: Sequence[NormalRecalcSubmesh],
    *,
    display_name: str,
    require_zero_index_padding: bool = True,
) -> tuple[
    tuple[NormalRecalcRedirect, ...],
    tuple[int, ...],
    tuple[int, ...],
    tuple[NormalRecalcWorkItem, ...],
]:
    redirect_words = _unpack_words(view, redirects_offset, vertex_count)
    redirects = _decode_redirects(redirect_words, display_name, vertex_count)
    face_count = sum(_aligned_index_count(len(submesh.indices)) for submesh in submeshes)
    face_words = _unpack_words(view, faces_offset, face_count)
    work_items: list[NormalRecalcWorkItem] = []
    face_cursor = 0
    for submesh_index, submesh in enumerate(submeshes):
        index_count = len(submesh.indices)
        for word in face_words[face_cursor : face_cursor + index_count]:
            target_vertex = word & VERTEX_INDEX_MASK
            if target_vertex >= vertex_count:
                raise NormalRecalcError(
                    f"{display_name} normal work item targets vertex "
                    f"{target_vertex} outside LOD0"
                )
            work_items.append(NormalRecalcWorkItem(target_vertex, word >> VERTEX_INDEX_BITS))
        aligned_count = _aligned_index_count(index_count)
        if require_zero_index_padding and any(
            face_words[face_cursor + index_count : face_cursor + aligned_count]
        ):
            raise NormalRecalcError(
                f"{display_name} normal submesh {submesh_index} has nonzero "
                "index padding"
            )
        face_cursor += aligned_count
    return redirects, redirect_words, face_words, tuple(work_items)


def _vertex_attribute_end(mesh, buffer_index):
    payloads = [mesh.mesh_buffer.buffer_payloads.get(buffer_index)]
    for layer in (getattr(mesh, "shadow_mesh", None), getattr(mesh, "occluder_mesh", None)):
        if layer is not None and not layer.shares_main_payload:
            payloads.append(layer.payloads.get(buffer_index))
    return max((int(item.offset) + int(item.size) * int(payload.vertex_count)
                for payload in payloads if payload is not None for item in payload.buffer_headers), default=0)


def _parse_buffer_owned_normal_header(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    section_end: int,
    mesh: object,
    lod_topologies: Sequence[NormalRecalcLODTopology],
    display_name: str,
) -> tuple[int, tuple[NormalRecalcLODTopology, ...], object]:
    view = memoryview(file_bytes)
    if not 0 <= section_offset <= section_end <= len(view):
        raise NormalRecalcError(f"{display_name} normal-recalculation bounds are invalid")
    section_alignment = mesh.profile.normal_recalc_section_alignment
    if section_offset % section_alignment:
        raise NormalRecalcError(
            f"{display_name} normal header is not {section_alignment}-byte aligned"
        )
    _require_range(section_offset, 16, section_offset, section_end, "header")
    declared_lod_count, body_list_offset = struct.unpack_from("<qq", view, section_offset)
    topologies = tuple(lod_topologies)
    if not 0 < declared_lod_count <= len(topologies):
        raise NormalRecalcError(
            f"{display_name} declares {declared_lod_count} normal LODs for "
            f"{len(topologies)} unique mesh LODs"
        )
    if body_list_offset != 0:
        raise NormalRecalcError(f"{display_name}'s legacy normal body pointer must be null")
    if any(view[section_offset + 16 : section_end]):
        raise NormalRecalcError(f"{display_name} normal-header alignment tail is nonzero")
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None or not getattr(mesh, "meshes", ()):
        raise NormalRecalcError(f"{display_name} has no mesh buffer/topology")
    return int(declared_lod_count), topologies[:declared_lod_count], mesh_buffer


def _normal_buffer_owner(mesh_buffer: object, buffer_index: int, display_name: str):
    payload = mesh_buffer.buffer_payloads.get(buffer_index)
    owner = (
        mesh_buffer
        if buffer_index == 0
        else mesh_buffer.streaming_buffer_headers.get(buffer_index)
    )
    if payload is None or owner is None:
        raise NormalRecalcError(
            f"{display_name} normal tables have no buffer/header {buffer_index}"
        )
    return payload, owner


def _check_normal_buffer_tail(
    payload: object,
    owner: object,
    payload_end: int,
    padding: Sequence[tuple[str, int, int]],
    label: str,
) -> None:
    if payload_end > len(payload.vertex_bytes):
        raise NormalRecalcError(f"{label} normal tables exceed their vertex buffer")
    blend_shape_offset = int(owner.blend_shape_offset)
    if blend_shape_offset > 0 and blend_shape_offset != payload_end:
        raise NormalRecalcError(
            f"{label} blend-shape payload does not follow its aligned redirect tables"
        )
    if blend_shape_offset <= 0 and len(payload.vertex_bytes) != payload_end:
        raise NormalRecalcError(
            f"{label} has unmodeled vertex-buffer bytes after its redirect tables"
        )
    for name, start, end in padding:
        if any(payload.vertex_bytes[start:end]):
            raise NormalRecalcError(f"{label} {name} alignment padding is nonzero")


def _aligned_padding_words(face_words: Sequence[int], submeshes: Sequence[NormalRecalcSubmesh]) -> list[int]:
    words: list[int] = []
    cursor = 0
    for submesh in submeshes:
        count = len(submesh.indices)
        aligned_count = _aligned_index_count(count)
        words.extend(face_words[cursor + count : cursor + aligned_count])
        cursor += aligned_count
    return words


def _buffer_owned_normal_data(lods: Sequence[NormalRecalcLOD], declared_lod_count: int) -> NormalRecalcData:
    return NormalRecalcData(
        redirects=lods[0].redirects,
        lods=tuple(lods),
        edge_excluded=any(redirect.annotation for lod in lods for redirect in lod.redirects),
        declared_lod_count=declared_lod_count,
        body_list_offset=0,
        body_offsets=tuple(lod.body_offset for lod in lods),
    )


def parse_mhwilds_normal_recalc(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    section_end: int,
    mesh: object,
    lod_topologies: Sequence[NormalRecalcLODTopology],
) -> NormalRecalcData:
    display_name = "Monster Hunter Wilds"
    declared_lod_count, topologies, mesh_buffer = _parse_buffer_owned_normal_header(
        file_bytes, section_offset, section_end, mesh, lod_topologies, display_name,
    )
    lods: list[NormalRecalcLOD] = []
    for lod_index, topology in enumerate(topologies):
        label = f"{display_name} normal LOD {lod_index}"
        buffer_index = int(topology.buffer_index)
        payload, owner = _normal_buffer_owner(mesh_buffer, buffer_index, label)
        local_index_offset = int(owner.normal_recalc_local_index_buffer_offset)
        redirected_list_offset = int(owner.normal_recalc_redirected_list_offset)

        attribute_end = _vertex_attribute_end(mesh, buffer_index)
        if local_index_offset != align(attribute_end, ALIGNMENT):
            raise NormalRecalcError(
                f"{label} local-index table does not follow its aligned vertex attributes"
            )
        face_word_count = sum(
            _aligned_index_count(len(submesh.indices))
            for submesh in topology.submeshes
        )
        face_words_end = local_index_offset + face_word_count * 4
        expected_redirected_list_offset = align(face_words_end, ALIGNMENT)
        if (
            redirected_list_offset < expected_redirected_list_offset
            or redirected_list_offset % ALIGNMENT
        ):
            raise NormalRecalcError(
                f"{label} redirect table overlaps or misaligns its runtime local-index table"
            )
        redirects_end = redirected_list_offset + int(topology.vertex_count) * 4
        payload_end = align(redirects_end, ALIGNMENT)
        _check_normal_buffer_tail(payload, owner, payload_end, (
            ("attribute", attribute_end, local_index_offset),
            ("local-index", face_words_end, expected_redirected_list_offset),
            ("redirect", redirects_end, payload_end),
        ), label)

        redirects, redirect_words, face_words, work_items = _decode_normal_work_tables(
            memoryview(payload.vertex_bytes),
            redirected_list_offset,
            local_index_offset,
            int(topology.vertex_count),
            topology.submeshes,
            display_name=f"{display_name} LOD {lod_index}",
            require_zero_index_padding=False,
        )
        _validate_topology(
            NormalRecalcData(redirects=redirects),
            topology.submeshes,
            NormalRecalcLayout.MHWILDS,
        )
        face_cursor = 0
        for submesh_index, submesh in enumerate(topology.submeshes):
            count = len(submesh.indices)
            for corner_index, (word, raw_index) in enumerate(
                zip(face_words[face_cursor : face_cursor + count], submesh.indices)
            ):
                expected_target = redirects[submesh.base_vertex + raw_index].target_vertex
                actual_target = word & VERTEX_INDEX_MASK
                if actual_target != expected_target:
                    raise NormalRecalcError(
                        f"{label} submesh {submesh_index} corner {corner_index} targets "
                        f"{actual_target}, expected redirected vertex {expected_target}"
                    )
            face_cursor += _aligned_index_count(count)

        payload.normal_recalc_local_index_buffer_offset = local_index_offset
        payload.normal_recalc_redirected_list_offset = redirected_list_offset
        payload.normal_recalc_payload_end = payload_end
        payload.normal_recalc_face_words = face_words
        payload.normal_recalc_redirect_words = redirect_words
        payload.normal_recalc_runtime_ignored_work_padding_words = tuple(
            _aligned_padding_words(face_words, topology.submeshes)
        )
        payload.normal_recalc_runtime_ignored_capacity_slack = bytes(
            payload.vertex_bytes[expected_redirected_list_offset:redirected_list_offset]
        )
        lods.append(
            NormalRecalcLOD(
                lod_index,
                local_index_offset,
                redirected_list_offset,
                local_index_offset,
                redirect_words,
                face_words,
                work_items,
                redirects,
                topology.vertex_base,
                (),
                buffer_index,
            )
        )
    return _buffer_owned_normal_data(lods, declared_lod_count)


def _parse_late_multilod_normal_recalc(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    section_end: int,
    mesh: object,
    lod_topologies: Sequence[NormalRecalcLODTopology],
    *,
    display_name: str,
) -> NormalRecalcData:
    declared_lod_count, selected, mesh_buffer = _parse_buffer_owned_normal_header(
        file_bytes, section_offset, section_end, mesh, lod_topologies, display_name,
    )
    serialized_lod_offsets = tuple(getattr(mesh.meshes[0], "lod_offsets", ()))

    def segment_key(topology: NormalRecalcLODTopology) -> tuple[int, int]:
        lod_index = int(topology.lod_index)
        lod_pointer = (
            int(serialized_lod_offsets[lod_index])
            if lod_index < len(serialized_lod_offsets)
            else -(lod_index + 1)
        )
        return int(topology.buffer_index), lod_pointer

    unique_by_buffer: dict[
        int,
        list[tuple[tuple[int, int], NormalRecalcLODTopology]],
    ] = {}
    topology_by_key: dict[tuple[int, int], NormalRecalcLODTopology] = {}
    for topology in selected:
        key = segment_key(topology)
        previous = topology_by_key.get(key)
        if previous is not None:
            if (
                previous.vertex_count != topology.vertex_count
                or previous.submeshes != topology.submeshes
            ):
                raise NormalRecalcError(
                    f"{display_name} aliased MeshLOD 0x{key[1]:X} has "
                    "inconsistent semantic topology"
                )
            continue
        topology_by_key[key] = topology
        unique_by_buffer.setdefault(key[0], []).append((key, topology))

    segment_offsets: dict[tuple[int, int], tuple[int, int]] = {}
    for buffer_index, entries in unique_by_buffer.items():
        label = f"{display_name} buffer {buffer_index}"
        payload, owner = _normal_buffer_owner(mesh_buffer, buffer_index, label)
        local_base = int(owner.normal_recalc_local_index_buffer_offset)
        redirect_base = int(owner.normal_recalc_redirected_list_offset)
        attribute_end = _vertex_attribute_end(mesh, buffer_index)
        if local_base != align(attribute_end, ALIGNMENT):
            raise NormalRecalcError(
                f"{label} local-index tables do not follow its aligned vertex attributes"
            )

        local_cursor = local_base
        local_offsets: dict[tuple[int, int], int] = {}
        for key, topology in entries:
            local_offsets[key] = local_cursor
            local_cursor += sum(
                _aligned_index_count(len(submesh.indices))
                for submesh in topology.submeshes
            ) * 4
        if redirect_base != align(local_cursor, ALIGNMENT):
            raise NormalRecalcError(
                f"{label} redirect tables do not follow the topology-counted face tables"
            )

        redirect_cursor = redirect_base
        for key, topology in entries:
            segment_offsets[key] = (local_offsets[key], redirect_cursor)
            redirect_cursor += int(topology.vertex_count) * 4
        payload_end = align(redirect_cursor, ALIGNMENT)
        _check_normal_buffer_tail(payload, owner, payload_end, (
            ("attribute", attribute_end, local_base),
            ("face-table", local_cursor, redirect_base),
            ("redirect-table", redirect_cursor, payload_end),
        ), label)

        payload.normal_recalc_local_index_buffer_offset = local_base
        payload.normal_recalc_redirected_list_offset = redirect_base
        payload.normal_recalc_payload_end = payload_end
        payload.normal_recalc_face_words = _unpack_words(
            payload.vertex_bytes, local_base, (local_cursor - local_base) // 4)
        payload.normal_recalc_redirect_words = _unpack_words(
            payload.vertex_bytes, redirect_base, (redirect_cursor - redirect_base) // 4)
        payload.normal_recalc_runtime_ignored_capacity_slack = b""

    lods: list[NormalRecalcLOD] = []
    padding_by_buffer: dict[int, list[int]] = {}
    for topology in selected:
        buffer_index = int(topology.buffer_index)
        payload = mesh_buffer.buffer_payloads[buffer_index]
        local_offset, redirect_offset = segment_offsets[segment_key(topology)]
        redirects, redirect_words, face_words, work_items = _decode_normal_work_tables(
            memoryview(payload.vertex_bytes),
            redirect_offset,
            local_offset,
            int(topology.vertex_count),
            topology.submeshes,
            display_name=f"{display_name} LOD {topology.lod_index}",
            require_zero_index_padding=False,
        )

        _require_derived(
            face_words,
            derive_sf6_normal_indices(NormalRecalcData(redirects=redirects), topology.submeshes),
            f"{display_name} LOD {topology.lod_index} face word {{}} does not match "
            "its semantic producer",
        )

        padding_by_buffer.setdefault(buffer_index, []).extend(
            _aligned_padding_words(face_words, topology.submeshes)
        )

        lods.append(
            NormalRecalcLOD(
                int(topology.lod_index),
                local_offset,
                redirect_offset,
                local_offset,
                redirect_words,
                face_words,
                work_items,
                redirects,
                int(topology.vertex_base),
                (),
                buffer_index,
            )
        )

    for buffer_index, padding_words in padding_by_buffer.items():
        mesh_buffer.buffer_payloads[
            buffer_index
        ].normal_recalc_runtime_ignored_work_padding_words = tuple(padding_words)
    return _buffer_owned_normal_data(lods, declared_lod_count)


parse_re9_normal_recalc = partial(
    _parse_late_multilod_normal_recalc, display_name="Resident Evil Requiem"
)
parse_onimusha_wots_normal_recalc = partial(
    _parse_late_multilod_normal_recalc, display_name="Onimusha: Way of the Sword"
)
parse_dd2_updated_normal_recalc = partial(
    _parse_late_multilod_normal_recalc, display_name="Dragon's Dogma 2 (updated)"
)


def parse_sf6_normal_recalc(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    section_end: int,
    lod_topologies: Sequence[NormalRecalcLODTopology],
    *,
    section_alignment: int = 8,
    split_vertex_page_plane: bool = True,
) -> NormalRecalcData:
    view = memoryview(file_bytes)
    if not 0 <= section_offset <= section_end <= len(view):
        raise NormalRecalcError(
            "SF6 normal-recalculation bounds are invalid"
        )
    if section_alignment <= 0 or section_alignment & (section_alignment - 1):
        raise NormalRecalcError(
            "normal-recalculation section alignment must be a positive power of two"
        )
    if section_offset % section_alignment:
        raise NormalRecalcError(
            f"SF6-family normal-recalculation section is not "
            f"{section_alignment}-byte aligned"
        )
    topologies = tuple(lod_topologies)
    if not topologies:
        raise NormalRecalcError(
            "SF6 normal-recalculation has no modeled LOD topology"
        )
    if tuple(item.lod_index for item in topologies) != tuple(range(len(topologies))):
        raise NormalRecalcError(
            "SF6 normal-recalculation topologies are not ordered from LOD0"
        )

    _require_range(section_offset, 16, section_offset, section_end, "header")
    (
        lod_count,
        reserved_byte,
        reserve1,
        reserve2,
        body_list_offset,
    ) = struct.unpack_from("<BBHIq", view, section_offset)
    if not 0 < lod_count <= len(topologies):
        raise NormalRecalcError(
            f"SF6 normal-recalculation LOD count {lod_count} exceeds "
            f"the {len(topologies)} modeled LODs"
        )
    topologies = topologies[:lod_count]
    if reserved_byte or reserve1 or reserve2:
        raise NormalRecalcError(
            "SF6 normal-recalculation reserved header fields must be zero"
        )
    if body_list_offset != section_offset + 16:
        raise NormalRecalcError(
            "SF6 normal-recalculation body list does not follow its header"
        )
    _require_range(
        int(body_list_offset),
        lod_count * 8,
        section_offset,
        section_end,
        "body list",
    )
    body_offsets = struct.unpack_from(f"<{lod_count}q", view, body_list_offset)
    expected_body_offset = align(body_list_offset + lod_count * 8, ALIGNMENT)
    if any(view[body_list_offset + lod_count * 8 : expected_body_offset]):
        raise NormalRecalcError(
            "SF6 normal-recalculation body-list padding is nonzero"
        )

    lods: list[NormalRecalcLOD] = []
    for lod_index, (body_offset, topology) in enumerate(
        zip(body_offsets, topologies)
    ):
        if body_offset != expected_body_offset:
            raise NormalRecalcError(
                f"SF6 normal-recalculation LOD {lod_index} body is not at "
                "its canonical aligned location"
            )
        _require_range(
            body_offset,
            16,
            section_offset,
            section_end,
            f"LOD {lod_index} body",
        )
        redirects_offset, faces_offset = struct.unpack_from(
            "<qq",
            view,
            body_offset,
        )
        if redirects_offset != body_offset + 16:
            raise NormalRecalcError(
                f"SF6 normal redirects do not follow LOD {lod_index}'s body"
            )
        vertex_count = int(topology.vertex_count)
        if not 0 <= vertex_count <= VERTEX_INDEX_MASK + 1:
            raise NormalRecalcError(
                f"SF6 normal-recalculation LOD {lod_index} vertex count is invalid"
            )
        face_count = sum(
            _aligned_index_count(len(submesh.indices))
            for submesh in topology.submeshes
        )
        redirects_end = redirects_offset + vertex_count * 4
        expected_faces_offset = align(redirects_end, ALIGNMENT)
        if faces_offset != expected_faces_offset:
            raise NormalRecalcError(
                f"SF6 normal face table for LOD {lod_index} does not follow "
                "its aligned redirect table"
            )
        faces_end = faces_offset + face_count * 4
        page_words = _sf6_split_vertex_pages(topology) if split_vertex_page_plane else ()
        pages_end = faces_end + len(page_words) * 4
        body_boundary = (
            body_offsets[lod_index + 1]
            if lod_index + 1 < len(body_offsets)
            else section_end
        )
        expected_boundary = align(pages_end, ALIGNMENT)
        if body_boundary != expected_boundary:
            raise NormalRecalcError(
                f"SF6 normal-recalculation LOD {lod_index} serialized "
                "boundary does not match its topology-derived allocation"
            )
        expected_body_offset = expected_boundary
        _require_range(
            int(redirects_offset),
            vertex_count * 4,
            section_offset,
            section_end,
            f"LOD {lod_index} redirect table",
        )
        _require_range(
            int(faces_offset),
            (face_count + len(page_words)) * 4,
            section_offset,
            section_end,
            f"LOD {lod_index} face table",
        )
        if page_words:
            stored_pages = struct.unpack_from(f"<{len(page_words)}I", view, faces_end)
            if stored_pages != page_words:
                raise NormalRecalcError(f"SF6 LOD {lod_index} page selectors are not topology-derived")
        for start, end in (
            (redirects_end, faces_offset),
            (pages_end, expected_body_offset),
        ):
            if any(view[start:end]):
                raise NormalRecalcError(
                    f"SF6 normal-recalculation LOD {lod_index} alignment "
                    "padding is nonzero"
                )

        redirects, redirect_words, face_words, work_items = (
            _decode_normal_work_tables(
                view,
                int(redirects_offset),
                int(faces_offset),
                vertex_count,
                topology.submeshes,
                display_name=f"SF6 LOD {lod_index}",
            )
        )
        _require_derived(
            face_words,
            derive_sf6_normal_indices(
                NormalRecalcData(redirects=redirects),
                topology.submeshes,
                pad_sort_windows=bool(page_words),
            ),
            f"SF6 normal face word {{}} in LOD {lod_index} is not derived "
            "from redirects and draw topology",
        )
        lods.append(
            NormalRecalcLOD(
                lod_index,
                body_offset,
                int(redirects_offset),
                int(faces_offset),
                redirect_words,
                face_words,
                work_items,
                redirects,
                topology.vertex_base,
                page_words,
            )
        )

    if expected_body_offset != section_end:
        raise NormalRecalcError(
            "SF6 normal-recalculation section size is not topology-derived"
        )
    return NormalRecalcData(
        redirects=lods[0].redirects,
        lods=tuple(lods),
        reserved=(reserved_byte, reserve1, reserve2),
        declared_lod_count=lod_count,
        body_list_offset=int(body_list_offset),
        body_offsets=body_offsets,
    )


def parse_modern_normal_recalc(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    profile: MeshGameProfile | MeshMainVersion | int,
    lod_counts: Sequence[tuple[int, int]],
) -> NormalRecalcData:
    profile = coerce_mesh_profile(profile)
    view = memoryview(file_bytes)
    if not 0 < section_offset < len(view):
        raise NormalRecalcError("normal-recalculation section offset is invalid")

    body_list_offset = 0
    if profile.normal_recalc_layout is NormalRecalcLayout.RE_RT:
        _require_range(section_offset, 16, 0, len(view), "normal-recalc header")
        lod_count, bodies_ptr = struct.unpack_from("<qq", view, section_offset)
        if lod_count < 0 or lod_count > 128:
            raise NormalRecalcError(
                f"normal-recalc body count {lod_count} is invalid"
            )
        body_list_offset = bodies_ptr
        if lod_count and bodies_ptr > 0:
            _require_range(
                bodies_ptr,
                lod_count * 8,
                0,
                len(view),
                "normal-recalc body list",
            )
            body_offsets = struct.unpack_from(
                f"<{lod_count}q",
                view,
                bodies_ptr,
            )
        else:
            body_offsets = ()
    else:
        raise NormalRecalcError(
            "modern normal-recalc parser does not support "
            f"{profile.display_name} ({profile.normal_recalc_layout.value})"
        )

    lods: list[NormalRecalcLOD] = []
    for lod_index, body_offset in enumerate(body_offsets):
        if body_offset <= 0:
            continue
        _require_range(body_offset, 16, 0, len(view), "normal-recalc body")
        vertices_offset, faces_offset = struct.unpack_from(
            "<qq", view, body_offset
        )
        if lod_index >= len(lod_counts):
            raise NormalRecalcError(
                f"normal-recalc LOD {lod_index} has no modeled mesh topology"
            )
        vertex_count, face_count = lod_counts[lod_index]
        if min(vertex_count, face_count) < 0:
            raise NormalRecalcError("normal-recalc semantic counts are invalid")
        if vertex_count and vertices_offset <= 0:
            raise NormalRecalcError(
                f"normal-recalc LOD {lod_index} has no vertex table"
            )
        if face_count and faces_offset <= 0:
            raise NormalRecalcError(
                f"normal-recalc LOD {lod_index} has no face table"
            )
        _require_range(
            vertices_offset,
            vertex_count * 4,
            0,
            len(view),
            f"normal-recalc LOD {lod_index} vertices",
        )
        _require_range(
            faces_offset,
            face_count * 4,
            0,
            len(view),
            f"normal-recalc LOD {lod_index} faces",
        )
        vertices = _unpack_words(view, vertices_offset, vertex_count)
        faces = _unpack_words(view, faces_offset, face_count)
        lods.append(
            NormalRecalcLOD(
                lod_index,
                body_offset,
                vertices_offset,
                faces_offset,
                vertices,
                faces,
            )
        )
    return NormalRecalcData(
        lods=tuple(lods),
        declared_lod_count=int(lod_count),
        body_list_offset=int(body_list_offset),
        body_offsets=tuple(int(offset) for offset in body_offsets),
    )


_NORMAL_RECALC_PLAN_RESOLVERS: Mapping[NormalRecalcLayout, NormalRecalcPlanResolver] = {
    NormalRecalcLayout.DMC5: _legacy_normal_recalc_plan,
    NormalRecalcLayout.RE2: _legacy_normal_recalc_plan,
    NormalRecalcLayout.RE3: _legacy_normal_recalc_plan,
    NormalRecalcLayout.RE2_RT: _legacy_normal_recalc_plan,
    NormalRecalcLayout.RE3_RT: _legacy_normal_recalc_plan,
    NormalRecalcLayout.MHRISE: _legacy_normal_recalc_plan,
    NormalRecalcLayout.SF6: _sf6_normal_recalc_plan,
    **{
        layout: partial(
            _work_table_normal_recalc_plan,
            layout=layout,
            display_name=name,
            derive_face_words=derive,
            **options,
        )
        for layout, name, derive, options in (
            (NormalRecalcLayout.RE8, "RE8", derive_re8_normal_indices, {"quantization_scale": 1023.0}),
            (NormalRecalcLayout.RE4, "RE4", derive_re4_normal_indices, {"use_edge_exclusion": False}),
            (NormalRecalcLayout.MHWILDS, "Monster Hunter Wilds", derive_re9_normal_indices, {}),
            (NormalRecalcLayout.RE9, "Resident Evil Requiem", derive_re9_normal_indices, {}),
            (NormalRecalcLayout.ONIMUSHA_WOTS, "Onimusha: Way of the Sword", derive_re9_normal_indices, {}),
            (NormalRecalcLayout.DD2_UPDATED, "Dragon's Dogma 2 (updated)", derive_sf6_normal_indices, {}),
        )
    },
}
_NORMAL_PLAN_LAYOUTS = frozenset(_NORMAL_RECALC_PLAN_RESOLVERS)

LOD0_NORMAL_RECALC_PARSERS = {
    NormalRecalcLayout.DMC5: parse_dmc5_normal_recalc,
    NormalRecalcLayout.RE2: parse_re2_normal_recalc,
    NormalRecalcLayout.RE3: parse_re3_normal_recalc,
    NormalRecalcLayout.RE8: parse_re8_normal_recalc,
    NormalRecalcLayout.RE4: parse_re4_normal_recalc,
    NormalRecalcLayout.RE2_RT: parse_re2rt_normal_recalc,
    NormalRecalcLayout.RE3_RT: parse_re3rt_normal_recalc,
    NormalRecalcLayout.MHRISE: parse_mhrise_normal_recalc,
}
BUFFER_OWNED_NORMAL_RECALC_PARSERS = {
    NormalRecalcLayout.MHWILDS: parse_mhwilds_normal_recalc,
    NormalRecalcLayout.RE9: parse_re9_normal_recalc,
    NormalRecalcLayout.ONIMUSHA_WOTS: parse_onimusha_wots_normal_recalc,
    NormalRecalcLayout.DD2_UPDATED: parse_dd2_updated_normal_recalc,
}
LOD0_NORMAL_RECALC_SERIALIZERS = {
    NormalRecalcLayout.DMC5: serialize_dmc5_normal_recalc,
    NormalRecalcLayout.RE2: serialize_re2_normal_recalc,
    NormalRecalcLayout.RE3: serialize_re3_normal_recalc,
    NormalRecalcLayout.RE8: serialize_re8_normal_recalc,
    NormalRecalcLayout.RE4: serialize_re4_normal_recalc,
    NormalRecalcLayout.RE2_RT: serialize_re2rt_normal_recalc,
    NormalRecalcLayout.RE3_RT: serialize_re3rt_normal_recalc,
    NormalRecalcLayout.MHRISE: serialize_mhrise_normal_recalc,
}
