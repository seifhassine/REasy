from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import struct
from collections.abc import Callable, Mapping, Sequence

from .mesh_file import MeshMainVersion


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


class Dmc5NormalRecalcProfile(Enum):
    STANDARD = "standard"
    TWO_PAGE_16BIT = "two_page_16bit"


@dataclass(frozen=True, slots=True)
class NormalRecalcRedirect:
    target_vertex: int
    annotation: bool = False


@dataclass(frozen=True, slots=True)
class NormalRecalcSubmesh:
    base_vertex: int
    indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class NormalRecalcData:
    redirects: tuple[NormalRecalcRedirect, ...]


@dataclass(frozen=True, slots=True)
class MeshNormalRecalcPlan:
    """Renderer-facing topology derived from modeled normal-recalc data."""

    redirect_targets: tuple[int, ...]
    triangle_vertices: tuple[int, ...]


def mesh_normal_recalc_plan(
    mesh: object,
    rendered_triangle_indices: Sequence[int] | None,
) -> MeshNormalRecalcPlan | None:
    data = getattr(mesh, "normal_recalc_data", None)
    if data is None:
        return None
    version = getattr(getattr(mesh, "mesh_buffer", None), "version", None)
    resolver = _NORMAL_RECALC_PLAN_RESOLVERS.get(version)
    if resolver is None:
        raise NormalRecalcError(
            f"normal-recalculation preview is unsupported for mesh {version!r}"
        )
    return resolver(mesh, rendered_triangle_indices)


def _dmc5_normal_recalc_plan(
    mesh: object,
    rendered_triangle_indices: Sequence[int] | None,
) -> MeshNormalRecalcPlan:
    data = mesh.normal_recalc_data
    if rendered_triangle_indices is None:
        raise NormalRecalcError(
            "normal recalculation requires the rendered LOD0 triangles"
        )
    redirects = tuple(getattr(data, "redirects", ()))
    if not redirects:
        raise NormalRecalcError(
            "normal-recalculation data has no vertex redirects"
        )
    try:
        targets = tuple(int(item.target_vertex) for item in redirects)
    except AttributeError as exc:
        raise NormalRecalcError(
            "normal-recalculation redirect has no semantic target vertex"
        ) from exc

    submeshes = dmc5_normal_recalc_submeshes(mesh)
    rendered = tuple(
        submesh.base_vertex + index
        for submesh in submeshes
        for index in submesh.indices
    )
    if tuple(int(index) for index in rendered_triangle_indices) != rendered:
        raise NormalRecalcError(
            "rendered triangles do not match normal-recalculation topology"
        )
    return MeshNormalRecalcPlan(
        targets,
        derive_dmc5_normal_triangle_vertices(data, submeshes),
    )


NormalRecalcPlanResolver = Callable[
    [object, Sequence[int] | None],
    MeshNormalRecalcPlan,
]

_NORMAL_RECALC_PLAN_RESOLVERS: Mapping[
    MeshMainVersion,
    NormalRecalcPlanResolver,
] = {
    MeshMainVersion.DMC5: _dmc5_normal_recalc_plan,
}


def _align(value: int, alignment: int = ALIGNMENT) -> int:
    return (value + alignment - 1) & -alignment


def _aligned_index_count(index_count: int) -> int:
    return _align(index_count, INDEX_ALIGNMENT)


def dmc5_normal_recalc_submeshes(
    mesh: object,
) -> tuple[NormalRecalcSubmesh, ...]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    meshes = getattr(mesh, "meshes", ())
    if mesh_buffer is None or not meshes or not meshes[0].lods:
        raise NormalRecalcError(
            "DMC5 normal recalculation requires modeled LOD0 geometry"
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
            faces = (
                payload.integer_faces
                if payload.integer_faces is not None
                else payload.faces
            )
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


def _profile_for_vertex_count(
    vertex_count: int,
) -> Dmc5NormalRecalcProfile:
    if vertex_count <= VERTEX_PAGE_SIZE:
        return Dmc5NormalRecalcProfile.STANDARD
    if vertex_count <= MAX_TWO_PAGE_VERTICES:
        return Dmc5NormalRecalcProfile.TWO_PAGE_16BIT
    raise NormalRecalcError(
        "DMC5 normal recalculation beyond two 16-bit vertex pages "
        "is not semantically understood"
    )


def _validate_topology(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> Dmc5NormalRecalcProfile:
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

    profile = _profile_for_vertex_count(len(redirects))
    if profile is Dmc5NormalRecalcProfile.TWO_PAGE_16BIT:
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


def dmc5_normal_recalc_profile(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> Dmc5NormalRecalcProfile:
    if not isinstance(data, NormalRecalcData):
        raise NormalRecalcError("expected DMC5 normal-recalculation data")
    return _validate_topology(data, submeshes)


def _pack_dmc5_normal_indices(
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


def _derive_standard_dmc5_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
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
            packed.extend(_pack_dmc5_normal_indices(batch, batch, batch))
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
                _pack_dmc5_normal_indices(batch, raw_batch, window)
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


def _derive_dmc5_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
    profile: Dmc5NormalRecalcProfile,
) -> tuple[int, ...]:
    if profile is Dmc5NormalRecalcProfile.TWO_PAGE_16BIT:
        return _derive_large_dmc5_normal_indices(data, submeshes)
    return _derive_standard_dmc5_normal_indices(data, submeshes)


def derive_dmc5_normal_indices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> tuple[int, ...]:
    profile = dmc5_normal_recalc_profile(data, submeshes)
    return _derive_dmc5_normal_indices(data, submeshes, profile)


def derive_dmc5_normal_triangle_vertices(
    data: NormalRecalcData,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> tuple[int, ...]:
    packed = derive_dmc5_normal_indices(data, submeshes)
    vertices: list[int] = []
    cursor = 0
    for submesh in submeshes:
        end = cursor + len(submesh.indices)
        vertices.extend(word & VERTEX_INDEX_MASK for word in packed[cursor:end])
        cursor += _aligned_index_count(len(submesh.indices))
    return tuple(vertices)


def parse_dmc5_normal_recalc(
    file_bytes: bytes | bytearray | memoryview,
    section_offset: int,
    section_end: int,
    vertex_count: int,
    submeshes: Sequence[NormalRecalcSubmesh],
) -> NormalRecalcData:
    view = memoryview(file_bytes)
    if not 0 <= section_offset <= section_end <= len(view):
        raise NormalRecalcError("normal-recalculation section bounds are invalid")
    if type(vertex_count) is not int or vertex_count < 0:
        raise NormalRecalcError("normal-recalculation vertex count is invalid")
    if vertex_count > VERTEX_INDEX_MASK + 1:
        raise NormalRecalcError("normal-recalculation vertex count exceeds 22 bits")
    profile = _profile_for_vertex_count(vertex_count)

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
    if redirects_offset != _align(section_offset + 16):
        raise NormalRecalcError(
            "normal-recalculation redirects do not follow the aligned header"
        )
    _require_range(
        redirects_offset,
        redirect_size,
        section_offset + 16,
        section_end,
        "redirect table",
    )
    _require_range(
        indices_offset,
        index_size,
        section_offset + 16,
        section_end,
        "index table",
    )

    redirect_end = redirects_offset + redirect_size
    if indices_offset != _align(redirect_end):
        raise NormalRecalcError(
            "normal-recalculation index table does not follow the aligned redirects"
        )
    index_end = indices_offset + index_size
    page_plane_offset = index_end
    object_end = (
        page_plane_offset + index_size
        if profile is Dmc5NormalRecalcProfile.TWO_PAGE_16BIT
        else _align(index_end)
    )
    if profile is Dmc5NormalRecalcProfile.TWO_PAGE_16BIT:
        _require_range(
            page_plane_offset,
            index_size,
            section_offset + 16,
            section_end,
            "vertex-page table",
        )
    if section_end != object_end:
        raise NormalRecalcError(
            f"normal-recalculation section does not match the "
            f"{profile.value} profile"
        )
    if (
        any(view[section_offset + 16:redirects_offset])
        or any(view[redirect_end:indices_offset])
        or (
            profile is Dmc5NormalRecalcProfile.STANDARD
            and any(view[index_end:object_end])
        )
    ):
        raise NormalRecalcError("normal-recalculation alignment padding is nonzero")

    redirect_words = (
        struct.unpack_from(f"<{vertex_count}I", view, redirects_offset)
        if vertex_count
        else ()
    )
    redirects = []
    for vertex, word in enumerate(redirect_words):
        if word & REDIRECT_RESERVED_MASK:
            raise NormalRecalcError(
                f"normal redirect {vertex} has unsupported bits 22-30"
            )
        redirects.append(
            NormalRecalcRedirect(
                word & VERTEX_INDEX_MASK,
                bool(word & REDIRECT_ANNOTATION_MASK),
            )
        )
    data = NormalRecalcData(tuple(redirects))
    validated_profile = _validate_topology(data, submeshes)
    if validated_profile is not profile:
        raise NormalRecalcError(
            "normal-recalculation topology does not match its derived profile"
        )

    stored = (
        struct.unpack_from(f"<{index_count}I", view, indices_offset)
        if index_count
        else ()
    )
    derived = _derive_dmc5_normal_indices(data, submeshes, profile)
    if stored != derived:
        mismatch = next(
            index
            for index, (actual, expected) in enumerate(zip(stored, derived))
            if actual != expected
        )
        raise NormalRecalcError(
            f"normal-recalculation index {mismatch} is not topology-derived"
        )
    if profile is Dmc5NormalRecalcProfile.TWO_PAGE_16BIT:
        stored_pages = (
            struct.unpack_from(
                f"<{index_count}I",
                view,
                page_plane_offset,
            )
            if index_count
            else ()
        )
        derived_pages = _derive_large_dmc5_page_plane(submeshes)
        if stored_pages != derived_pages:
            mismatch = next(
                index
                for index, pair in enumerate(zip(stored_pages, derived_pages))
                if pair[0] != pair[1]
            )
            raise NormalRecalcError(
                f"normal-recalculation vertex-page entry {mismatch} "
                "is not topology-derived"
            )
    return data


