from __future__ import annotations

from array import array
from copy import copy
from dataclasses import dataclass, field, replace

import numpy as np

lazy from ..contract_utils import layer_payloads
lazy from ..conversion_allocations import _WEIGHT_METADATA
lazy from ..conversion_skin import quantize_normalized_weights
from ..mesh_file import MPLY_MAGIC, MeshGroup
lazy from ..normal_recalc import INDICES_PER_WORKGROUP, WORKGROUP_SLOT_MASK, NormalRecalcRedirect
from . import meshlets, morphs
from .export import gather, joint_names, vertex_weights
lazy from .nearest import NearestIndex
lazy from .scene import UV_PLANES, WEIGHT_SETS, layer_lods, main_buffer_index
lazy from ..conversion_morphs import _merge_morph_ranges
lazy from ..profiles import NormalRecalcLayout
lazy from ..conversion_normals import materialize_normal_work

UV_TOLERANCE = 2e-6
NORMAL_TOLERANCE = 2e-3
WEIGHT_TOLERANCE = 2e-6
_PLANES = {"positions": (3, np.float32, "f"), "normals": (3, np.float32, "f"), "tangents": (3, np.float32, "f"),
           "normal_ws": (1, np.uint8, "B"), "tangent_ws": (1, np.uint8, "B"), "uv0": (2, np.float64, "d"),
           "uv1": (2, np.float64, "d"), "uv2": (2, np.float64, "d"), "colors": (4, np.uint8, "B")}


@dataclass
class DrawPlan:
    key: str                     # scene object key, or "" for new geometry
    layer: str
    mesh_index: int
    lod: int
    part_id: int
    material: int
    is_quad: bool
    sources: list                # (Blender object, face indices)
    submesh: tuple = ()
    hidden: object = None        # (corner ids, sizes, face positions) of faces Blender could not store
    original_material: int = -1
    domain: int = -1
    buffer: int = 0
    referencing: bool = False
    build: object = None


@dataclass
class Build:
    identity: np.ndarray          # global id each vertex continues, or -1
    origin: np.ndarray            # global id each vertex derives data and structure from
    planes: dict
    faces: np.ndarray             # draw-local indices
    objects: list = field(default_factory=list)
    object_of: np.ndarray = None
    vertex_of: np.ndarray = None  # Blender vertex of each native vertex (-1 for hidden-only vertices)
    pivots: np.ndarray = None
    weights: dict = field(default_factory=dict)
    morphs: dict = field(default_factory=dict)
    retangent: np.ndarray = None
    in_place: bool = False

    @property
    def count(self):
        return len(self.identity)


def _void_rows(columns):
    data = np.ascontiguousarray(np.concatenate(
        [np.ascontiguousarray(c).reshape(len(c), -1).view(np.uint8) for c in columns], axis=1))
    return data.view(np.dtype((np.void, data.shape[1]))).ravel()


def _equal_rows(columns):
    data = np.ascontiguousarray(np.concatenate(
        [np.ascontiguousarray(c).reshape(len(c), -1).view(np.uint8) for c in columns], axis=1))
    count, width = data.shape
    padded = np.zeros((count, -(-width // 8) * 8), np.uint8)
    padded[:, :width] = data
    digest = np.full(count, 0xCBF29CE484222325, np.uint64)
    for word in padded.view(np.uint64).T:
        digest = (digest ^ word) * np.uint64(0x100000001B3)
        digest ^= digest >> np.uint64(29)
    _unique, first, inverse = np.unique(digest, return_index=True, return_inverse=True)
    inverse = inverse.ravel()
    if not np.array_equal(data, data[first[inverse]]):
        _unique, first, inverse = np.unique(_void_rows(columns), return_index=True, return_inverse=True)
        inverse = inverse.ravel()
    order = np.argsort(first)
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    inverse = rank[inverse]
    return first[order], inverse, np.bincount(inverse, minlength=len(order))


def _rows_by_object(object_of, count):
    if not count:
        return []
    object_of = np.asarray(object_of)
    order = np.argsort(object_of, kind="stable")
    sizes = np.bincount(object_of[object_of >= 0], minlength=count)[:count]
    start = int(np.count_nonzero(object_of < 0))
    return np.split(order[start:start + int(sizes.sum())], np.cumsum(sizes)[:-1])


def _f32(values):
    return np.ascontiguousarray(values, dtype=np.float32)


def _is_main_payload(mesh, payload):
    return main_buffer_index(mesh, payload) is not None


def _lod(mesh, layer, mesh_index, lod_index):
    for m, l, lod in layer_lods(mesh, layer):
        if (m, l) == (mesh_index, lod_index):
            return lod
    raise ValueError(f"{layer} LOD{lod_index} does not exist in the source mesh")


def _plan_domain(ctx, plan):
    payloads = layer_payloads(ctx.mesh, plan.layer)
    if plan.submesh:
        buffer_index = int(plan.submesh[0].buffer_index)
    else:
        groups = _lod(ctx.mesh, plan.layer, plan.mesh_index, plan.lod).mesh_groups
        group = next((g for g in groups if int(g.group_id) == plan.part_id), groups[0] if groups else None)
        buffer_index = int(group.submeshes[0].buffer_index) if group is not None and group.submeshes else 0
    payload = payloads.get(buffer_index)
    if payload is None:
        raise ValueError(f"{plan.layer} LOD{plan.lod} has no vertex buffer {buffer_index}")
    owner = getattr(ctx.mesh, f"{plan.layer}_mesh", None) if plan.layer != "main" else None
    return ctx.domains.index[id(payload)], buffer_index, owner is not None and owner.shares_main_payload


def _primitive_corners(ctx, plan, obj, faces):
    sizes = obj.face_size[faces]
    if plan.is_quad:
        if np.any(sizes != 4):
            ctx.report.loss(f"{obj.name}: dropped non-quad faces from a quad-topology draw")
        primitives = obj.face_start[faces[sizes == 4], None] + np.arange(4)
    else:
        triangle = sizes == 3
        primitives = obj.face_start[faces, None] + np.arange(3)
        if not triangle.all():
            order = np.argsort(obj.tri_face, kind="stable")
            grouped = obj.tri_face[order]
            slots = np.flatnonzero(~triangle)
            start = np.searchsorted(grouped, faces[slots], side="left")
            count = np.searchsorted(grouped, faces[slots], side="right") - start
            slot = np.repeat(slots, count)
            within = np.arange(count.sum()) - np.repeat(np.cumsum(count) - count, count)
            rows = order[np.repeat(start, count) + within]
            keep = np.flatnonzero(triangle)
            slot = np.concatenate((keep, slot))
            within = np.concatenate((np.zeros(len(keep), np.int64), within))
            primitives = np.concatenate((primitives[keep], obj.tri_loops[rows]))[np.lexsort((within, slot))]
    if obj.mirrored:
        primitives = primitives[:, ::-1]
    return primitives.astype(np.int64).ravel()


def _build(ctx, plan):
    domain = plan.domain
    base, count = ctx.domains.bases[domain], len(ctx.domains.payloads[domain].positions) // 3
    width = 4 if plan.is_quad else 3
    tables = []
    for number, (obj, faces) in enumerate(plan.sources):
        corners = _primitive_corners(ctx, plan, obj, np.asarray(faces, dtype=np.int64))
        vertices = obj.corner_vert[corners]
        ids = obj.corner_ids[corners] if obj.corner_ids is not None else np.full(len(corners), -1, np.int64)
        colors = (np.zeros((len(corners), 4), np.uint8) if obj.colors is None else
                  obj.colors[corners] if obj.color_domain == "CORNER" else obj.colors[vertices])
        uvs = [_f32(obj.uvs[p][corners]) if p in obj.uvs else np.zeros((len(corners), 2), np.float32)
               for p in UV_PLANES]
        tables.append((number, vertices, ids, _f32(obj.corner_normals[corners]), colors, uvs, len(corners)))
        if number == 0 and plan.key:
            used = np.zeros(obj.vertex_count, dtype=bool)
            used[obj.corner_vert] = True
            isolated = np.flatnonzero(~used)
            if len(isolated):
                ids = obj.vertex_ids[isolated] if obj.vertex_ids is not None else np.full(len(isolated), -1)
                colors = (obj.colors[isolated] if obj.colors is not None and obj.color_domain == "POINT"
                          else np.zeros((len(isolated), 4), np.uint8))
                nan = [np.full((len(isolated), 2), np.nan, np.float32) for _ in UV_PLANES]
                normals = (_f32(obj.point_normals[isolated]) if obj.point_normals_known
                           else np.full((len(isolated), 3), np.nan, np.float32))
                tables.append((number, isolated, ids, normals, colors, nan, 0))
    corner_count = sum(t[6] for t in tables)
    objects = [obj for obj, _faces in plan.sources]
    if not tables or not sum(len(t[1]) for t in tables):
        build = Build(np.zeros(0, np.int64), np.zeros(0, np.int64), _empty_planes(), np.zeros(0, np.int64), objects,
                      np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0, np.int64))
        _attach_hidden(ctx, plan, build, width)
        return build
    object_of = np.concatenate([np.full(len(t[1]), t[0]) for t in tables])
    vertex_of = np.concatenate([t[1] for t in tables]).astype(np.int64)
    ids = np.concatenate([t[2] for t in tables]).astype(np.int64)
    normals = np.concatenate([t[3] for t in tables])
    colors = np.concatenate([t[4] for t in tables])
    uvs = [np.concatenate([t[5][k] for t in tables]) for k in range(len(UV_PLANES))]
    is_corner = np.concatenate([np.full(len(t[1]), t[6] > 0) for t in tables])
    total = len(ids)
    ids = np.where((ids >= base) & (ids < base + count), ids, -1)
    # Copies of one source vertex that still agree (a seam left by Separate, Rip or Split) weld back.
    rejoin = (ids >= 0) & is_corner
    points = np.concatenate([_f32(objects[t[0]].positions[t[1]]) for t in tables]).view(np.uint32)
    points[~rejoin] = 0
    keys = _void_rows([np.where(rejoin, -1, object_of).astype(np.int64), np.where(rejoin, -1, vertex_of), ids,
                       points,
                       np.where(is_corner, -1, np.arange(total)).astype(np.int64),
                       normals.view(np.uint32), colors, *(np.nan_to_num(uv).view(np.uint32) for uv in uvs)])
    _unique, first, inverse = np.unique(keys, return_index=True, return_inverse=True)
    inverse = inverse.ravel()
    corner_slots = np.flatnonzero(is_corner)
    planes = {"positions": np.zeros((len(first), 3), np.float32), "normals": normals[first].copy(),
              "colors": colors[first].copy()}
    for plane, values in zip(UV_PLANES, uvs):
        planes[plane] = values[first].astype(np.float64)
    pivots = np.full(len(first), -1, np.int64)
    for obj, rows in zip(objects, _rows_by_object(object_of[first], len(objects))):
        planes["positions"][rows] = obj.positions[vertex_of[first][rows]]
        if obj.pivots is not None:
            pivots[rows] = obj.pivots[vertex_of[first][rows]]
    build = Build(ids[first].copy(), ids[first].copy(), planes, inverse[corner_slots].astype(np.int64), objects,
                  object_of[first].copy(), vertex_of[first].copy(), pivots)
    build.first_use = first
    if corner_count != len(corner_slots):
        raise AssertionError("corner bookkeeping")
    _attach_hidden(ctx, plan, build, width)
    return build


def _empty_planes():
    return {name: np.zeros((0, width), dtype) for name, (width, dtype, _code) in _PLANES.items()
            if name in ("positions", "normals", "colors", *UV_PLANES)}


def _attach_hidden(ctx, plan, build, width):
    build.hidden_faces = None
    if plan.hidden is None:
        return
    ids, sizes, positions = plan.hidden
    lookup = {int(i): n for n, i in enumerate(build.identity) if i >= 0}
    missing = sorted({int(i) for i in ids} - set(lookup))
    if missing:
        extra = np.asarray(missing, dtype=np.int64)
        start = build.count
        build.identity = np.concatenate((build.identity, extra))
        build.origin = np.concatenate((build.origin, extra))
        build.object_of = np.concatenate((build.object_of, np.full(len(extra), -1)))
        build.vertex_of = np.concatenate((build.vertex_of, np.full(len(extra), -1)))
        build.pivots = np.concatenate((build.pivots, np.full(len(extra), -1)))
        for name in build.planes:
            plane_width, dtype, _code = _PLANES[name]
            build.planes[name] = np.concatenate((build.planes[name], ctx.source(name, extra, plane_width, dtype)))
        for number, value in enumerate(missing):
            lookup[value] = start + number
    rows, offset = [], 0
    for size in sizes:
        rows.append([lookup[int(i)] for i in ids[offset:offset + size]])
        offset += size
    if any(len(r) != width for r in rows):
        ctx.report.loss("Dropped zero-area faces whose primitive type no longer matches their draw")
        keep = [len(r) == width for r in rows]
        rows = [r for r, k in zip(rows, keep) if k]
        positions = positions[np.asarray(keep, dtype=bool)]
    build.hidden_faces = (np.asarray(rows, dtype=np.int64).reshape(-1, width), np.asarray(positions, np.int64))


def _merge_faces(build, width):
    visible = build.faces.reshape(-1, width)
    if build.hidden_faces is None or not len(build.hidden_faces[0]):
        return visible.ravel()
    hidden, positions = build.hidden_faces
    rows = list(visible)
    for position, row in sorted(zip(positions.tolist(), hidden), key=lambda item: item[0]):
        rows.insert(min(position, len(rows)), row)
    return np.asarray(rows, np.int64).ravel()


def _sort_build(build, width):
    """So untouched draws keep their layout."""
    faces = _merge_faces(build, width)
    build.hidden_faces = None
    claim = build.identity >= 0
    rows = np.arange(build.count)
    origin = np.where(build.origin >= 0, build.origin, build.origin.max(initial=0) + 1)
    first = np.full(build.count, len(faces), np.int64)
    if len(faces):
        np.minimum.at(first, faces, np.arange(len(faces)))
    order = np.lexsort((np.where(first < len(faces), first, len(faces) + rows), origin * 2 + ~claim))
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    for name in build.planes:
        build.planes[name] = build.planes[name][order]
    for name in ("identity", "origin", "object_of", "vertex_of", "pivots"):
        setattr(build, name, getattr(build, name)[order])
    build.faces = rank[faces] if len(faces) else faces
    return build


def _claim(plans):
    seen = set()
    for plan in plans:
        build = plan.build
        for row in np.flatnonzero(build.identity >= 0):
            value = int(build.identity[row])
            if value in seen:
                build.identity[row] = -1
            else:
                seen.add(value)


def _assign_origins(ctx, plan, scene_mesh):
    build = plan.build
    missing = build.origin < 0
    if not missing.any():
        return
    own = scene_mesh is not None and len(scene_mesh.gpu)
    key = (plan.domain, scene_mesh.key if own else None)
    if key not in ctx.origin_indexes:
        if own:
            candidates = scene_mesh.gpu
        else:
            base = ctx.domains.bases[plan.domain]
            candidates = base + np.arange(len(ctx.domains.payloads[plan.domain].positions) // 3)
        if not len(candidates):
            raise ValueError(f"{plan.layer} LOD{plan.lod} has no source vertices to derive new geometry from")
        positions = ctx.source("positions", candidates, 3, np.float32)
        ctx.origin_indexes[key] = (candidates, NearestIndex(positions))
    candidates, index = ctx.origin_indexes[key]
    rows = np.flatnonzero(missing)
    # Corners of one Blender vertex split by normals, UVs or colors share its position: ask once per vertex.
    vertex = build.vertex_of[rows]
    owner = np.where(vertex >= 0, build.object_of[rows] * (int(vertex.max(initial=0)) + 1) + vertex, -1 - rows)
    _keys, first, inverse = np.unique(owner, return_index=True, return_inverse=True)
    found = index.query(build.planes["positions"][rows[first]])
    build.origin[rows] = candidates[found[inverse.ravel()]]


def _snap_values(ctx, plan):
    build = plan.build
    origin = build.origin
    planes = build.planes
    original = {name: ctx.source(name, origin, width, dtype) for name, (width, dtype, _code) in _PLANES.items()}
    payload = ctx.domains.payloads[plan.domain]
    same_position = np.all(planes["positions"].view(np.uint32) == original["positions"].view(np.uint32), axis=1)
    planes["positions"] = np.where(same_position[:, None], original["positions"], planes["positions"])
    encoded = any(obj.encoded_normals for obj in build.objects)
    tolerance = NORMAL_TOLERANCE if encoded else 0.0
    # Vertices without corners carry no normal unless Blender stores normals per point, and corners of
    # zero-area faces get a zero one.
    unknown_normal = (~np.isfinite(planes["normals"]).all(axis=1) | (build.vertex_of < 0)
                      | ~np.any(planes["normals"], axis=1))
    same_normal = unknown_normal | np.all(
        np.abs(np.nan_to_num(planes["normals"]).astype(np.float64) - original["normals"]) <= tolerance, axis=1)
    planes["normals"] = np.where(same_normal[:, None], original["normals"], _unit(planes["normals"]))
    same_uv0 = np.ones(build.count, bool)
    for plane in UV_PLANES:
        present = bool(build.objects) and all(plane in obj.uvs for obj in build.objects)
        values = planes[plane]
        hidden = build.vertex_of < 0
        unknown = ~np.isfinite(values).all(axis=1) | hidden | (not present)
        with np.errstate(invalid="ignore"):
            close = np.all(np.abs(values - original[plane])
                           <= UV_TOLERANCE * np.maximum(1, np.abs(original[plane])), axis=1)
        keep = unknown | close
        planes[plane] = np.where(keep[:, None], original[plane], values.astype(np.float32).astype(np.float64))
        if plane == "uv0":
            same_uv0 = keep
        if not len(getattr(payload, plane)) and not np.all(keep) and build.count:
            ctx.report.loss(f"{plan.layer} LOD{plan.lod}: this vertex buffer has no {plane.upper()}; "
                            "ignored that UV map")
    has_colors = any(obj.colors is not None for obj in build.objects)
    no_color = (build.vertex_of < 0) | (not has_colors)
    planes["colors"] = np.where(no_color[:, None], original["colors"], planes["colors"])
    if len(payload.colors) == 0 and has_colors and np.any(planes["colors"] != original["colors"]):
        ctx.report.loss(f"{plan.layer} LOD{plan.lod}: this vertex buffer has no vertex colors; ignored them")
    pivots = build.pivots
    planes["normal_ws"] = np.where(pivots[:, None] >= 0, np.clip(pivots, 0, 255)[:, None].astype(np.uint8),
                                   original["normal_ws"])
    same_frame = same_normal & same_uv0 & (same_position | (build.identity >= 0))
    planes["tangents"] = original["tangents"]
    planes["tangent_ws"] = original["tangent_ws"]
    build.retangent = ~same_frame
    build.moved = ~same_position
    build.changed_vertices = ~(same_position & same_normal & same_uv0)
    for plane in ("uv1", "uv2", "colors", "normal_ws"):
        build.changed_vertices |= np.any(planes[plane] != original[plane], axis=1)


def geometric_normals(build, width, positions, seed=None):
    points = np.asarray(positions, np.float64)
    faces = build.faces.reshape(-1, width)
    if width == 4:
        faces = np.concatenate((faces[:, [0, 1, 2]], faces[:, [0, 2, 3]]))
    accumulated = np.zeros_like(points)
    if len(faces):
        normal = np.cross(points[faces[:, 1]] - points[faces[:, 0]], points[faces[:, 2]] - points[faces[:, 0]])
        for corner in range(3):
            np.add.at(accumulated, faces[:, corner], normal)
    current = _unit(build.planes["normals"]).astype(np.float64)
    if seed is None:
        seed = _normal_clusters(current, build.planes["positions"])
    shared = np.zeros_like(accumulated)
    np.add.at(shared, seed, accumulated)
    shared = shared[seed]
    length = np.linalg.norm(shared, axis=1, keepdims=True)
    return np.where(length > 1e-20, shared / np.where(length > 0, length, 1), current)


def _shape_normals(build, width, shape, seed, rest, displaced):
    """Bit-equal to geometric_normals() for ``shape`` only because affected rows sum the same face normals in the same
    order."""
    faces = build.faces.reshape(-1, width)
    if width == 4:
        faces = np.concatenate((faces[:, [0, 1, 2]], faces[:, [0, 2, 3]]))
    shaped = rest.copy()
    touched = np.zeros(build.count, bool)
    touched[faces[displaced[faces].any(axis=1)]] = True
    clusters = np.zeros(build.count, bool)
    clusters[seed[touched]] = True
    affected = clusters[seed]
    if not affected.any():
        return shaped
    points = np.asarray(shape, np.float64)
    local = faces[affected[faces].any(axis=1)]
    accumulated = np.zeros_like(points)
    normal = np.cross(points[local[:, 1]] - points[local[:, 0]], points[local[:, 2]] - points[local[:, 0]])
    for corner in range(3):
        np.add.at(accumulated, local[:, corner], normal)
    rows = np.flatnonzero(affected)
    shared = np.zeros_like(accumulated)
    np.add.at(shared, seed[rows], accumulated[rows])
    shared = shared[seed[rows]]
    current = _unit(build.planes["normals"][rows]).astype(np.float64)
    length = np.linalg.norm(shared, axis=1, keepdims=True)
    shaped[rows] = np.where(length > 1e-20, shared / np.where(length > 0, length, 1), current)
    return shaped


SEED_GROUP_LIMIT = 32


def _normal_clusters(normals, positions):
    seed = np.arange(len(normals))
    _first, inverse, counts = _equal_rows([np.asarray(positions, np.float32)])
    order = np.argsort(inverse, kind="stable")
    starts = np.concatenate(([0], np.cumsum(counts)[:-1]))
    groups = np.flatnonzero((counts > 1) & (counts <= SEED_GROUP_LIMIT))
    if len(groups):
        firsts = np.full((len(groups), SEED_GROUP_LIMIT), -1, np.int64)
        firsts[:, 0] = order[starts[groups]]
        made = np.ones(len(groups), np.int64)
        for rank in range(1, int(counts[groups].max())):
            active = np.flatnonzero(counts[groups] > rank)
            member = order[starts[groups[active]] + rank]
            width = int(made[active].max())
            candidates = firsts[active, :width]
            dots = np.einsum("aj,awj->aw", normals[member], normals[np.maximum(candidates, 0)])
            agree = (candidates >= 0) & (dots > 0.5)
            joins = agree.any(axis=1)
            seed[member[joins]] = candidates[joins, agree[joins].argmax(axis=1)]
            fresh = active[~joins]
            firsts[fresh, made[fresh]] = member[~joins]
            made[fresh] += 1
    for group in np.flatnonzero(counts > SEED_GROUP_LIMIT):
        clusters = []
        for member in order[starts[group]:starts[group] + counts[group]]:
            for first in clusters:
                if np.dot(normals[member], normals[first]) > 0.5:
                    seed[member] = first
                    break
            else:
                clusters.append(member)
    return seed


def rotate_between(source, target, vectors):
    """All inputs are unit rows."""
    axis = np.cross(source, target)
    sine = np.linalg.norm(axis, axis=1, keepdims=True)
    cosine = np.sum(source * target, axis=1, keepdims=True)
    unit_axis = np.divide(axis, sine, out=np.zeros_like(axis), where=sine > 1e-12)
    rotated = (vectors * cosine + np.cross(unit_axis, vectors) * sine
               + unit_axis * np.sum(unit_axis * vectors, axis=1, keepdims=True) * (1 - cosine))
    return _unit(rotated)


def _recompute_normals(ctx, plan):
    build = plan.build
    rows = getattr(build, "moved", np.zeros(build.count, bool)) & (build.vertex_of >= 0)
    if not rows.any():
        return
    normals = geometric_normals(build, 4 if plan.is_quad else 3, build.planes["positions"])
    build.planes["normals"][rows] = normals[rows].astype(np.float32)
    build.retangent = build.retangent | rows
    build.changed_vertices = build.changed_vertices | rows
    ctx.normals_recomputed += int(rows.sum())


def _unit(values):
    values = np.asarray(values, dtype=np.float64)
    length = np.linalg.norm(values, axis=1, keepdims=True)
    return np.where(length > 1e-12, values / np.where(length > 0, length, 1), (0.0, 1.0, 0.0)).astype(np.float32)


def _tangents(build, width):
    if build.retangent is None or not build.retangent.any() or not build.count:
        return 0
    faces = build.faces.reshape(-1, width)
    if width == 4:
        faces = np.concatenate((faces[:, [0, 1, 2]], faces[:, [0, 2, 3]]))
    points = build.planes["positions"].astype(np.float64)
    uv = build.planes["uv0"].astype(np.float64)
    normals = build.planes["normals"].astype(np.float64)
    tangent_sum = np.zeros_like(points)
    bitangent_sum = np.zeros_like(points)
    if len(faces):
        edges = points[faces[:, 1:]] - points[faces[:, :1]]
        delta = uv[faces[:, 1:]] - uv[faces[:, :1]]
        det = delta[:, 0, 0] * delta[:, 1, 1] - delta[:, 1, 0] * delta[:, 0, 1]
        valid = np.isfinite(det) & (np.abs(det) > 1e-20)
        inverse = np.where(valid, 1.0 / np.where(valid, det, 1.0), 0.0)
        tangent = (edges[:, 0] * delta[:, 1, 1, None] - edges[:, 1] * delta[:, 0, 1, None]) * inverse[:, None]
        bitangent = (edges[:, 1] * delta[:, 0, 0, None] - edges[:, 0] * delta[:, 1, 0, None]) * inverse[:, None]
        for corner in range(3):
            np.add.at(tangent_sum, faces[:, corner], tangent)
            np.add.at(bitangent_sum, faces[:, corner], bitangent)
    rows = np.flatnonzero(build.retangent)
    n = normals[rows]
    t = tangent_sum[rows] - n * np.sum(n * tangent_sum[rows], axis=1, keepdims=True)
    length = np.linalg.norm(t, axis=1, keepdims=True)
    fallback = np.cross(n, np.where(np.abs(n[:, :1]) < 0.9, (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)))
    t = np.where(length > 1e-12, t / np.where(length > 0, length, 1), _unit(fallback))
    handed = np.sum(np.cross(n, t) * bitangent_sum[rows], axis=1)
    build.planes["tangents"][rows] = t.astype(np.float32)
    # The engine's V runs downward; MikkTSpace handedness flips with it.
    build.planes["tangent_ws"][rows, 0] = np.where(handed < 0, 127, 129).astype(np.uint8)
    return len(rows)


def _group_lookup(ctx, obj):
    result = []
    for name in obj.group_names:
        label, stripped = "primary", name
        for set_label, _streams, prefix in WEIGHT_SETS:
            if prefix and name.startswith(prefix):
                label, stripped = set_label, name[len(prefix):]
        joint = ctx.group_joints.get(stripped)
        if joint is None and name not in ctx.unknown_groups:
            ctx.unknown_groups.add(name)
            ctx.report.loss(f"Vertex group {name!r} matches no joint; ignored it")
        result.append(None if joint is None else (label, joint))
    return result


def _blender_triples(ctx, build, label):
    rows_out, joints_out, weights_out = [], [], []
    for obj, rows in zip(build.objects, _rows_by_object(build.object_of, len(build.objects))):
        lookup = _group_lookup(ctx, obj)
        if not len(obj.weight_vertex):
            continue
        valid = np.array([item is not None and item[0] == label for item in lookup], dtype=bool)
        joint_of = np.array([item[1] if item is not None else -1 for item in lookup], dtype=np.int64)
        entries = valid[obj.weight_group] & (obj.weight_value > 0)
        vertex, joint, weight = obj.weight_vertex[entries], joint_of[obj.weight_group[entries]], obj.weight_value[entries]
        if not len(rows) or not len(vertex):
            continue
        order = np.argsort(vertex, kind="stable")
        vertex, joint, weight = vertex[order], joint[order], weight[order]
        targets = build.vertex_of[rows]
        start = np.searchsorted(vertex, targets, side="left")
        end = np.searchsorted(vertex, targets, side="right")
        counts = end - start
        if not counts.sum():
            continue
        row_index = np.repeat(rows, counts)
        entry_index = np.concatenate([np.arange(s, e) for s, e in zip(start, end) if e > s])
        rows_out.append(row_index)
        joints_out.append(joint[entry_index])
        weights_out.append(weight[entry_index])
    if not rows_out:
        return np.zeros(0, np.int64), np.zeros(0, np.int64), np.zeros(0)
    return _combine(np.concatenate(rows_out), np.concatenate(joints_out), np.concatenate(weights_out))


def _combine(rows, joints, weights):
    if not len(rows):
        return rows, joints, weights
    stride = int(joints.max()) + 1
    unique, inverse = np.unique(rows * stride + joints, return_inverse=True)
    total = np.zeros(len(unique))
    np.add.at(total, inverse, weights)
    return unique // stride, unique % stride, total


def _source_triples(palette, lanes):
    indices, weights = lanes
    rows = np.repeat(np.arange(len(indices)), indices.shape[1])
    flat = indices.ravel()
    valid = (weights.ravel() > 0) & (flat >= 0) & (flat < len(palette))
    joints = np.asarray(palette, dtype=np.int64)[np.where(valid, flat, 0)]
    return _combine(rows[valid], joints[valid], weights.ravel()[valid].astype(np.float64))


def _changed_rows(count, left, right, tolerance):
    changed = np.zeros(count, bool)
    rows = np.concatenate((left[0], right[0]))
    if not len(rows):
        return changed
    stride = int(max(left[1].max(initial=0), right[1].max(initial=0))) + 1
    keys = np.concatenate((left[0] * stride + left[1], right[0] * stride + right[1]))
    values = np.concatenate((left[2], -right[2]))
    unique, inverse = np.unique(keys, return_inverse=True)
    difference = np.zeros(len(unique))
    np.add.at(difference, inverse, values)
    bad = np.abs(difference) > tolerance
    changed[(unique[bad] // stride).astype(np.int64)] = True
    return changed


def _weight_streams(payload, names):
    return [getattr(payload, name, None) for name in names if getattr(payload, name, None) is not None]


INHERIT_DISTANCE = 0.1  # metres: a fitted accessory sits closer than this to the mesh it copies weights from


def _check_inherited_distance(ctx, build, rows):
    origins = gather(ctx.domains, build.origin[rows], "positions", 3, np.float64)
    if origins is None or not len(origins):
        return
    distance = float(np.median(np.linalg.norm(build.planes["positions"][rows] - origins, axis=1)))
    if distance > INHERIT_DISTANCE:
        ctx.report.loss(f"Vertices that copied their weights lie {distance:.2f} m from the vertices they copied "
                        "from (median); fit the model onto the mesh in Blender so it takes the right weights")


def _rigid_unweighted(ctx, build, inherited, listed, indices, weights):
    """Copying each vertex's nearest weights would bend a rigid accessory such as a bracelet like skin."""
    positions = np.asarray(build.planes["positions"], np.float64)
    names, followed = joint_names(ctx.mesh), {}
    for obj, rows in zip(build.objects, _rows_by_object(build.object_of, len(build.objects))):
        rows = rows[build.vertex_of[rows] >= 0]
        if not len(rows) or listed[rows].any() or not inherited[rows].all():
            continue
        origins = gather(ctx.domains, build.origin[rows], "positions", 3, np.float64)
        chosen = rows[int(np.argmin(np.linalg.norm(origins - positions[rows].mean(0), axis=1)))]
        joint = indices[chosen, int(np.argmax(weights[chosen]))]
        indices[rows], weights[rows] = 0, 0
        indices[rows, 0], weights[rows, 0] = joint, 1.0
        followed[obj.name] = names[ctx.mesh.bone_remap_indices[int(joint)]]
    if followed:
        listing = ", ".join(f"{name} → {joint}" for name, joint in list(followed.items())[:3])
        more = f" and {len(followed) - 3} more" if len(followed) > 3 else ""
        ctx.report.change(f"Objects without weights move rigidly with one joint: {listing}{more}")


def _snap_weights(ctx, plan):
    build = plan.build
    payload = ctx.domains.payloads[plan.domain]
    palette = ctx.mesh.bone_remap_indices
    if not build.count:
        return
    for label, names, _prefix in WEIGHT_SETS:
        streams = _weight_streams(payload, names)
        new = _blender_triples(ctx, build, label)
        if not streams:
            if len(new[0]) and label == "primary" and not ctx.mesh.bones:
                ctx.report.loss("The source mesh is not skinned; ignored vertex weights")
            elif len(new[0]):
                ctx.report.loss(f"This vertex buffer stores no {label} weights; ignored them")
            continue
        capacity = sum(s.influence_count for s in streams)
        original = vertex_weights(ctx.domains, build.origin, names)
        if not len(palette):
            # Weight streams without a deform palette have no Blender representation.
            build.weights[label] = original
            continue
        source = _source_triples(palette, original)
        changed = _changed_rows(build.count, source, new, WEIGHT_TOLERANCE)
        changed &= build.vertex_of >= 0
        # Unweighted skinned vertices would collapse onto the skeleton's origin in game.
        listed = np.zeros(build.count, bool)
        listed[new[0]] = True
        inherited = changed & ~listed & (build.identity < 0)
        if inherited.any():
            ctx.report.change(f"Copied {label} weights from the nearest source vertices for "
                              f"{int(inherited.sum())} new vertices without {label} weights")
            if label == "primary":
                _check_inherited_distance(ctx, build, np.flatnonzero(inherited))
        changed &= ~inherited
        indices, weights = original[0].copy(), original[1].copy()
        if indices.shape[1] < capacity:
            pad = capacity - indices.shape[1]
            indices, weights = np.pad(indices, ((0, 0), (0, pad))), np.pad(weights, ((0, 0), (0, pad)))
        if label == "primary" and inherited.any() and not ctx.skin_unweighted:
            _rigid_unweighted(ctx, build, inherited, listed, indices, weights)
        if label == "primary":
            build.reweighted = changed
        if changed.any():
            ctx.weight_edits += int(changed.sum())
            rows, joints, values = new
            keep = changed[rows]
            rows, joints, values = rows[keep], joints[keep], values[keep]
            indices[changed] = 0
            weights[changed] = 0
            empty = changed.copy()
            if len(rows):
                order = np.lexsort((-values, rows))
                rows, joints, values = rows[order], joints[order], values[order]
                starts = np.searchsorted(rows, rows, side="left")
                rank = np.arange(len(rows)) - starts
                dropped = rank >= capacity
                if dropped.any():
                    ctx.report.loss(f"{int(np.unique(rows[dropped]).size)} vertices have more than {capacity} "
                                    f"{label} influences; kept the strongest and renormalized")
                rows, joints, values, rank = rows[~dropped], joints[~dropped], values[~dropped], rank[~dropped]
                touched, slot = np.unique(rows, return_inverse=True)
                lanes = np.zeros((len(touched), capacity))
                lanes[slot, rank] = values
                unique, inverse = np.unique(joints, return_inverse=True)
                indices[rows, rank] = np.array([ctx.palette_index(int(j)) for j in unique], np.int64)[inverse]
                weights[rows, rank] = quantize_normalized_weights(lanes)[slot, rank]
                empty[rows] = False
            if (empty & changed).any():
                ctx.report.loss(f"{int((empty & changed).sum())} skinned vertices have no {label} weights")
        build.weights[label] = (indices, weights.astype(np.float32))


def _snap_morphs(ctx, plan, scene_mesh):
    build = plan.build
    buffer_index = main_buffer_index(ctx.mesh, ctx.domains.payloads[plan.domain])
    blend = ctx.mesh.blend_shape_data
    if buffer_index is None or blend is None or plan.layer != "main":
        return
    names = ctx.channel_names_for(plan, scene_mesh)
    used = set()
    for obj in build.objects:
        for name in obj.shape_keys:
            if name not in names:
                ctx.report.loss(f"{obj.name}: new shape key {name!r} has no native morph channel; ignored it")
    if scene_mesh is not None:
        present = set().union(*(obj.shape_keys for obj in build.objects)) if build.objects else set()
        for _body, _position, name, _delta, _normals in scene_mesh.channels:
            if name not in present and build.objects:
                ctx.report.change(f"Shape key {name!r} was deleted; its morph channel is now inert there")
    positions = build.planes["positions"].astype(np.float64)
    rest = seed = unturned = None
    object_rows = _rows_by_object(build.object_of, len(build.objects))
    for name, channel in names.items():
        original, original_normals = ctx.original_morph(channel, build.origin)
        delta = np.zeros((build.count, 3), np.float32)
        for obj, rows in zip(build.objects, object_rows):
            values = obj.shape_keys.get(name)
            if values is None:
                continue
            delta[rows] = values[build.vertex_of[rows]]
        hidden = build.vertex_of < 0
        scale = np.maximum(np.abs(positions), np.abs(positions + original)).max(axis=1, initial=0) + 1.0
        close = np.all(np.abs(delta.astype(np.float64) - original) <= 4 * np.finfo(np.float32).eps * scale[:, None],
                       axis=1) | hidden
        values = np.where(close[:, None], original, delta).astype(np.float32)
        build.morph_changed = getattr(build, "morph_changed", np.zeros(build.count, bool)) | ~close
        if not close.all():
            ctx.morph_edits += int((~close).sum())
        if original_normals is not None:
            target_normals = original_normals.copy()
            moved = (~close) | build.changed_vertices
            if moved.any():
                width = 4 if plan.is_quad else 3
                stored = build.planes["normals"].astype(np.float64)
                if rest is None:
                    seed = _normal_clusters(_unit(stored).astype(np.float64), build.planes["positions"])
                    rest = geometric_normals(build, width, build.planes["positions"], seed)
                    unturned = rotate_between(rest, rest, stored)
                shape = build.planes["positions"].astype(np.float64) + values
                shaped = _shape_normals(build, width, shape, seed, rest, values.any(axis=1))
                target_normals[moved] = unturned[moved]
                turned = moved & np.any(shaped != rest, axis=1)
                target_normals[turned] = rotate_between(rest[turned], shaped[turned], stored[turned])
                corners = build.faces.reshape(-1, width)
                ring = np.zeros(build.count, bool)
                ring[corners[moved[corners].any(axis=1)]] = True
                ring &= ~moved
                ring &= np.linalg.norm(original_normals, axis=1) > 0.5
                if ring.any():
                    before = geometric_normals(build, width, positions + original, seed)
                    target_normals[ring] = rotate_between(before[ring], shaped[ring],
                                                          original_normals[ring].astype(np.float64))
                ctx.report.change("Recomputed explicit morph target normals for edited vertices")
        else:
            target_normals = None
        if values.any() or original.any():
            build.morphs[channel] = (values, target_normals)
            used.add(channel)
    build.morph_channels = used


def _check_in_place(plan, scene_mesh):
    build = plan.build
    if not plan.key or scene_mesh is None or len(plan.sources) != 1:
        return False
    if plan.material != plan.original_material:
        return False
    if build.count != len(scene_mesh.gpu) or not np.array_equal(build.identity, scene_mesh.gpu):
        return False
    return np.array_equal(build.faces, scene_mesh.corners)


def apply_geometry(ctx):
    mesh = ctx.mesh
    for plan in ctx.plans:
        plan.domain, plan.buffer, plan.referencing = _plan_domain(ctx, plan)
    morphs.add_channels(ctx)
    for plan in ctx.plans:
        plan.build = _sort_build(_build(ctx, plan), 4 if plan.is_quad else 3)
    owning = {}
    for plan in ctx.plans:
        if not plan.referencing:
            owning.setdefault(plan.domain, []).append(plan)
    ctx.weight_edits = ctx.morph_edits = ctx.normals_recomputed = ctx.tangents_recomputed = 0
    ctx.origin_indexes = {}  # (domain, scene mesh key) -> (candidate ids, NearestIndex of their positions)
    rebuilt = {}
    for domain, plans in owning.items():
        plans = _canonical_order(ctx, plans)
        _claim(plans)
        for plan in plans:
            scene_mesh = ctx.scene.meshes.get(ctx.scene_object(plan.key).mesh_key) if plan.key else None
            _assign_origins(ctx, plan, scene_mesh)
            _sort_build(plan.build, 4 if plan.is_quad else 3)
            _snap_values(ctx, plan)
            if ctx.recompute_normals:
                _recompute_normals(ctx, plan)
            _snap_weights(ctx, plan)
            _snap_morphs(ctx, plan, scene_mesh)
            plan.build.in_place = _check_in_place(plan, scene_mesh)
        in_place = all(plan.build.in_place for plan in plans)
        for plan in plans:
            ctx.tangents_recomputed += _tangents(plan.build, 4 if plan.is_quad else 3)
        if mesh.header.magic == MPLY_MAGIC and _is_main_payload(mesh, ctx.domains.payloads[domain]):
            if in_place:
                meshlets.write_in_place(ctx, plans)
            else:
                meshlets.rebuild(ctx, plans)
            continue
        if in_place:
            _write_in_place(ctx, domain, plans)
        else:
            rebuilt[domain] = _rebuild_domain(ctx, domain, plans)
    _apply_morphs(ctx, owning, rebuilt)
    _apply_normal_redirects(ctx, rebuilt)
    _apply_bone_modifier(ctx, rebuilt)
    _apply_references(ctx, [plan for plan in ctx.plans if plan.referencing], rebuilt)
    if ctx.tangents_recomputed:
        ctx.report.change(f"Recomputed tangents for {ctx.tangents_recomputed} edited vertices")
    if ctx.normals_recomputed:
        ctx.report.change(f"Recomputed normals of {ctx.normals_recomputed} moved vertices from geometry")
    if ctx.weight_edits:
        ctx.report.change(f"Updated weights of {ctx.weight_edits} vertices")
    if ctx.morph_edits:
        ctx.report.change(f"Updated {ctx.morph_edits} morph deltas")
    return rebuilt


def _canonical_order(ctx, plans):
    by_submesh = {id(s): plan for plan in plans for s in plan.submesh}
    new = [plan for plan in plans if not plan.submesh]
    ordered, seen_lods = [], set()
    layer = plans[0].layer
    for mesh_index, lod_index, lod in layer_lods(ctx.mesh, layer):
        if id(lod) in seen_lods:
            continue
        seen_lods.add(id(lod))
        for group in lod.mesh_groups:
            for submesh in group.submeshes:
                plan = by_submesh.get(id(submesh))
                if plan is not None and plan not in ordered:
                    ordered.append(plan)
            ordered.extend(p for p in new if (p.mesh_index, p.lod, p.part_id) == (mesh_index, lod_index, int(group.group_id))
                           and p not in ordered)
        ordered.extend(p for p in new if (p.mesh_index, p.lod) == (mesh_index, lod_index) and p not in ordered)
    ordered.extend(p for p in plans if p not in ordered)
    return ordered


def _write_plane(payload, name, rows, values):
    width, dtype, code = _PLANES[name]
    current = getattr(payload, name)
    if not len(current):
        return
    data = np.asarray(current, dtype=dtype).reshape(-1, width)
    data[rows] = np.asarray(values, dtype=dtype).reshape(-1, width)
    setattr(payload, name, _to_array(code, data))


def _to_array(code, data):
    result = array(code)
    result.frombytes(np.ascontiguousarray(data, dtype={"f": np.float32, "d": np.float64, "B": np.uint8,
                                                       "H": np.uint16, "I": np.uint32}[code]).tobytes())
    return result


def _write_in_place(ctx, domain, plans):
    payload = ctx.domains.payloads[domain]
    base = ctx.domains.bases[domain]
    for plan in plans:
        build = plan.build
        rows = build.identity - base
        for name in _PLANES:
            if name in build.planes:
                _write_plane(payload, name, rows, build.planes[name])
        for label, names, _prefix in WEIGHT_SETS:
            if label not in build.weights:
                continue
            indices, weights = build.weights[label]
            offset = 0
            for stream_name in names:
                stream = getattr(payload, stream_name, None)
                if stream is None:
                    continue
                k = stream.influence_count
                current_i = np.asarray(stream.deform_indices, dtype=np.uint16).reshape(-1, k).copy()
                current_w = np.asarray(stream.weights, dtype=np.float32).reshape(-1, k).copy()
                current_i[rows] = indices[:, offset:offset + k]
                current_w[rows] = weights[:, offset:offset + k]
                setattr(payload, stream_name, replace(stream, deform_indices=_to_array("H", current_i),
                                                      weights=_to_array("f", current_w)))
                offset += k
        if plan.submesh and plan.material != int(plan.submesh[0].material_index):
            for submesh in plan.submesh:
                submesh.material_index = plan.material


def _new_submesh(template, material, is_quad):
    submesh = copy(template)
    submesh.material_index = material
    submesh.is_quad = int(is_quad)
    submesh.indices_count = submesh.faces_index_offset = submesh.verts_index_offset = submesh.vert_count = 0
    for name in ("no_ray_tracing", "alpha_test", "transparent"):
        if hasattr(submesh, name):
            setattr(submesh, name, False)
    return submesh


def _normal_window_cuts(faces, width):
    cuts, window, used = [], 0, set()
    for primitive, corners in enumerate(faces.reshape(-1, width).tolist()):
        if window + width > INDICES_PER_WORKGROUP:
            window, used = 0, set()
        if len(used.union(corners)) > WORKGROUP_SLOT_MASK + 1:
            cuts.append(primitive)
            window, used = 0, set()
        used.update(corners)
        window += width
    return cuts


def _vertex_limit_cuts(faces, width, limit=0x10000):
    """Local indices must fit 16 bits."""
    cuts, used = [], set()
    for primitive, corners in enumerate(faces.reshape(-1, width).tolist()):
        if len(used) + sum(corner not in used for corner in set(corners)) > limit:
            cuts.append(primitive)
            used = set()
        used.update(corners)
    return cuts


def _slice_build(build, primitives, width, *, keep_isolated):
    faces = build.faces.reshape(-1, width)[primitives].ravel()
    vertices = np.unique(faces)
    if keep_isolated:
        used = np.zeros(build.count, bool)
        used[build.faces] = True
        vertices = np.union1d(vertices, np.flatnonzero(~used))
    lookup = np.full(build.count, -1, np.int64)
    lookup[vertices] = np.arange(len(vertices))
    part = copy(build)
    part.planes = {name: values[vertices] for name, values in build.planes.items()}
    for name in ("identity", "origin", "object_of", "vertex_of", "pivots", "retangent", "changed_vertices",
                 "moved", "reweighted", "morph_changed"):
        value = getattr(build, name, None)
        if value is not None:
            setattr(part, name, value[vertices])
    part.weights = {label: (lanes[0][vertices], lanes[1][vertices]) for label, lanes in build.weights.items()}
    part.morphs = {key: (delta[vertices], None if normals is None else normals[vertices])
                   for key, (delta, normals) in build.morphs.items()}
    part.faces = lookup[faces]
    return part, vertices


def _split_normal_windows(ctx, order):
    return _split_draws(ctx, order, _normal_window_cuts, "to fit the normal-recalculation work windows")


def _split_large_draws(ctx, order):
    return _split_draws(ctx, order, _vertex_limit_cuts, "to fit 16-bit indices",
                        when=lambda build: build.count > 0x10000)


def _split_draws(ctx, order, cuts_of, reason, when=None):
    result = []
    for group, submesh, plan in order:
        width = 4 if plan.is_quad else 3
        wanted = len(plan.build.faces) and (when is None or when(plan.build))
        cuts = cuts_of(plan.build.faces, width) if wanted else []
        if not cuts:
            result.append((group, submesh, plan))
            continue
        bounds = [0, *cuts, len(plan.build.faces) // width]
        claimed = set()
        position = group.submeshes.index(submesh)
        for number, (start, end) in enumerate(zip(bounds, bounds[1:])):
            part, _vertices = _slice_build(plan.build, np.arange(start, end), width, keep_isolated=number == 0)
            duplicate = np.array([value in claimed for value in part.identity.tolist()], dtype=bool)
            part.identity = np.where(duplicate, -1, part.identity)
            claimed.update(int(v) for v in part.identity if v >= 0)
            piece = copy(plan)
            piece.build = part
            if number == 0:
                result.append((group, submesh, piece))
                continue
            extra = _new_submesh(submesh, plan.material, plan.is_quad)
            extra.buffer_index = submesh.buffer_index
            position += 1
            group.submeshes.insert(position, extra)
            result.append((group, extra, piece))
        group.cluster_count = len(group.submeshes)
        ctx.report.change(f"Split a {plan.layer} LOD{plan.lod} draw into {len(bounds) - 1} draws {reason}")
    return result


def _rebuild_domain(ctx, domain, plans):
    mesh = ctx.mesh
    payload = ctx.domains.payloads[domain]
    base = ctx.domains.bases[domain]
    layer = plans[0].layer
    buffer_index = plans[0].buffer
    plan_of = {id(s): plan for plan in plans for s in plan.submesh}
    new_plans = [plan for plan in plans if not plan.submesh]
    template = next((*plan.submesh for plan in plans), None)
    order, seen = [], set()
    for mesh_index, lod_index, lod in layer_lods(mesh, layer):
        if id(lod) in seen:
            continue
        seen.add(id(lod))
        groups = []
        lod_template = None
        for group in lod.mesh_groups:
            in_domain = any(int(s.buffer_index) == buffer_index for s in group.submeshes)
            if not in_domain:
                groups.append(group)
                continue
            submeshes = []
            for submesh in group.submeshes:
                plan = plan_of.get(id(submesh))
                lod_template = lod_template or submesh
                if plan is None or (not plan.build.count and not len(plan.build.faces)):
                    continue
                if plan.submesh[0] is submesh:
                    submeshes.append((submesh, plan))
            for plan in [p for p in new_plans if (p.mesh_index, p.lod, p.part_id) ==
                         (mesh_index, lod_index, int(group.group_id))]:
                if plan.build.count:
                    submeshes.append((_new_submesh(group.submeshes[0] if group.submeshes else template,
                                                   plan.material, plan.is_quad), plan))
                new_plans.remove(plan)
            if submeshes:
                group.submeshes[:] = [s for s, _p in submeshes]
                group.cluster_count = len(group.submeshes)
                groups.append(group)
                order.extend((group, s, p) for s, p in submeshes)
            else:
                if not ctx.reference:
                    ctx.report.change(f"Removed empty part {int(group.group_id)} from {layer} LOD{lod_index}")
        for part in sorted({p.part_id for p in new_plans if (p.mesh_index, p.lod) == (mesh_index, lod_index)}):
            source = lod_template or template
            if source is None:
                raise ValueError(f"{layer} LOD{lod_index} has no draw to copy for new geometry")
            group = MeshGroup(source.buffer)
            group.group_id = part
            group.reserve = bytes(6)
            for plan in [p for p in new_plans if (p.mesh_index, p.lod, p.part_id) == (mesh_index, lod_index, part)]:
                if plan.build.count:
                    submesh = _new_submesh(source, plan.material, plan.is_quad)
                    submesh.buffer_index = buffer_index
                    group.submeshes.append(submesh)
                    order.append((group, submesh, plan))
                new_plans.remove(plan)
            if group.submeshes:
                group.cluster_count = len(group.submeshes)
                groups.append(group)
                ctx.report.change(f"Added part {part} to {layer} LOD{lod_index}")
        if not groups:
            raise ValueError(f"{layer} LOD{lod_index} would have no geometry; keep at least one face")
        lod.mesh_groups[:] = groups
        lod.part_count = len(groups)
    if new_plans:
        raise ValueError("New geometry could not be placed in a LOD collection")
    if not mesh.profile.canonical_ordinary_supports_32bit_indices:
        order = _split_large_draws(ctx, order)
    if mesh.normal_recalc_data is not None and layer == "main":
        order = _split_normal_windows(ctx, order)
    cursor = 0
    faces, origin, identity = [], [], []
    planes = {name: [] for name in _PLANES}
    weights = {}
    counted = {}
    plane_present = {name: bool(len(getattr(payload, name))) for name in _PLANES}
    for group, submesh, plan in order:
        build = plan.build
        submesh.buffer_index = buffer_index
        submesh.verts_index_offset = cursor
        submesh.vert_count = build.count
        submesh.faces_index_offset = sum(len(f) for f in faces)
        submesh.indices_count = len(build.faces)
        counted[id(group)] = counted.get(id(group), 0) + build.count
        faces.append(build.faces)
        origin.append(build.origin)
        identity.append(build.identity)
        for name in _PLANES:
            planes[name].append(build.planes[name])
        for label, value in build.weights.items():
            weights.setdefault(label, []).append(value)
        cursor += build.count
    for group, _submesh, _plan in order:
        group.vertex_count = counted[id(group)]
    origin = np.concatenate(origin) if origin else np.zeros(0, np.int64)
    identity = np.concatenate(identity) if identity else np.zeros(0, np.int64)
    faces = np.concatenate(faces) if faces else np.zeros(0, np.int64)
    if faces.size and faces.max() > 0xFFFF and not mesh.profile.canonical_ordinary_supports_32bit_indices:
        raise ValueError("A draw needs more than 65,536 vertices; split it into several objects")
    old_local = origin - base
    _replace_payload(payload, cursor, planes, plane_present, weights, identity - base, old_local, identity >= 0)
    payload.integer_faces = _to_array("I", faces.astype(np.uint32))
    payload.faces = array("H")
    ctx.report.change(f"Rebuilt {layer} vertex buffer {buffer_index} ({cursor:,} vertices, "
                      f"{len(faces) // 3:,} index triples)")
    return {"base": base, "buffer": buffer_index, "layer": layer, "origin": old_local,
            "identity": np.where(identity >= 0, identity - base, -1), "plans": plans, "order": order,
            "old_count": ctx.original_counts[domain], "count": cursor,
            "draw_of": np.repeat(np.arange(len(order)), [p.build.count for _g, _s, p in order]).astype(np.int64)}


def _replace_payload(payload, count, planes, present, weights, identity_local, origin_local, exact):
    for name, (width, dtype, code) in _PLANES.items():
        if not present[name]:
            continue
        values = np.concatenate(planes[name]) if planes[name] else np.zeros((0, width), dtype)
        setattr(payload, name, _to_array(code, values.astype(dtype)))
    for label, names, _prefix in WEIGHT_SETS:
        streams = [(n, getattr(payload, n)) for n in names if getattr(payload, n, None) is not None]
        if not streams:
            continue
        indices = np.concatenate([w[0] for w in weights.get(label, [])]) if weights.get(label) else None
        values = np.concatenate([w[1] for w in weights.get(label, [])]) if weights.get(label) else None
        offset = 0
        for stream_name, stream in streams:
            k = stream.influence_count
            metadata = {}
            for field_name in _WEIGHT_METADATA:
                raw = getattr(stream, field_name)
                width = len(raw) // stream.vertex_count if stream.vertex_count else 0
                if not width:
                    metadata[field_name] = raw[:0]
                    continue
                table = np.frombuffer(raw, dtype=np.uint8).reshape(-1, width)
                rows = np.where(exact, identity_local, origin_local)
                metadata[field_name] = table[np.clip(rows, 0, len(table) - 1)].tobytes()
            lane_i = indices[:, offset:offset + k] if indices is not None else np.zeros((count, k), np.uint16)
            lane_w = values[:, offset:offset + k] if values is not None else np.zeros((count, k), np.float32)
            setattr(payload, stream_name, replace(stream, vertex_count=count,
                                                  deform_indices=_to_array("H", lane_i.astype(np.uint16)),
                                                  weights=_to_array("f", lane_w.astype(np.float32)), **metadata))
            offset += k
    payload.vertex_count = count


def _lookup(rebuilt):
    old_count = rebuilt["old_count"]
    lookup = np.full(old_count, -1, np.int64)
    origin = rebuilt["origin"]
    derived = (origin >= 0) & (origin < old_count)
    rows = np.flatnonzero(derived)[::-1]
    lookup[origin[rows]] = rows
    identity = rebuilt["identity"]
    rows = np.flatnonzero((identity >= 0) & (identity < old_count))
    lookup[identity[rows]] = rows
    return lookup


def _apply_morphs(ctx, owning, rebuilt):
    blend = ctx.mesh.blend_shape_data
    if blend is None or not blend.bodies:
        return
    changed_structure = False
    for domain, plans in owning.items():
        payload = ctx.domains.payloads[domain]
        buffer_index = main_buffer_index(ctx.mesh, payload)
        if buffer_index is None:
            continue
        info = rebuilt.get(domain)
        base = ctx.domains.bases[domain]
        builds = [p.build for p in (info["plans"] if info else plans) if p.build.count]
        if info is not None:
            new_order = [p.build for _g, _s, p in info["order"]]
            builds = new_order
            origin = info["origin"]
            count = info["count"]
        else:
            origin = np.concatenate([b.identity for b in builds]) - base if builds else np.zeros(0, np.int64)
            count = len(payload.positions) // 3
        channels = set()
        for build in builds:
            channels |= set(build.morphs)
        if info is None and not ctx.morph_edits:
            continue
        values = {}
        normals = {}
        cursor = 0
        positions_of_rows = []
        for build in builds:
            rows = np.arange(cursor, cursor + build.count) if info is not None else build.identity - base
            positions_of_rows.append(rows)
            for channel in channels:
                if channel not in build.morphs:
                    continue
                delta, target_normals = build.morphs[channel]
                values.setdefault(channel, []).append((rows, delta))
                if target_normals is not None:
                    normals.setdefault(channel, []).append((rows, target_normals))
            cursor += build.count
        changed_structure |= _rebuild_morph_ranges(ctx, blend, buffer_index, origin, count, values, normals,
                                                   structural=info is not None,
                                                   identity=info["identity"] if info is not None else None)
    if changed_structure and not ctx.mesh.profile.has_modern_blend_shape_targets:
        # Older layouts store one contiguous range per target; gaps get zero deltas.
        for body in blend.bodies:
            for index in range(len(body.targets)):
                _merge_morph_ranges(ctx.mesh, body, index)
            for index, target in enumerate(body.targets):
                _fit_quantization_box(ctx, body, index, target)


def _fit_quantization_box(ctx, body, index, target):
    if target.aabb is None:
        return
    samples = [np.asarray(s.position_deltas).reshape(-1, 3) for c in body.channels for s in c.segments
               if s.target_index == index and len(s.position_deltas)]
    if not samples:
        return
    combined = np.concatenate(samples)
    low, high = np.asarray(target.aabb.minimum, np.float64), np.asarray(target.aabb.maximum, np.float64)
    # Packed codes decode to bin centers, up to half a step past the stored box.
    divisor = {0: np.array((2047, 1023, 2047)), 3: np.array((255, 255, 255))}.get(body.compression_type)
    slack = 0.5 * (high - low) / divisor + 1e-6 if divisor is not None else 0.0
    if np.any(combined < low - slack) or np.any(combined > high + slack):
        target.aabb = None
        if body.compression_type in (0, 3):
            ctx.report.change("Edited morph deltas left their quantization box; the morph target is requantized")


def _dense(entries, count, width=3, fill=None):
    result = np.zeros((count, width), np.float32) if fill is None else fill.copy()
    for rows, values in entries:
        result[rows] = values
    return result


MAX_TARGET_RANGES = 0xFF
RANGE_GAP = 0
_SEGMENT_EXTRAS = ("position_w", "normal_w", "deform_percent", "vertex_cull_mask")


def _runs(mask):
    rows = np.flatnonzero(mask)
    if not len(rows):
        return []
    breaks = np.flatnonzero(np.diff(rows) > RANGE_GAP + 1) + 1
    starts = np.concatenate(([rows[0]], rows[breaks]))
    ends = np.concatenate((rows[breaks - 1], [rows[-1]])) + 1
    spans = list(zip(starts.tolist(), ends.tolist()))
    while len(spans) > MAX_TARGET_RANGES:
        gaps = [spans[i + 1][0] - spans[i][1] for i in range(len(spans) - 1)]
        i = int(np.argmin(gaps))
        spans[i:i + 2] = [(spans[i][0], spans[i + 1][1])]
    return [np.arange(start, end) for start, end in spans]


def _old_dense(segments, name, size):
    result = None
    for segment in segments:
        value = getattr(segment, name)
        if value is None:
            continue
        value = np.asarray(value)
        if result is None:
            result = np.zeros((size, *value.shape[1:]), dtype=value.dtype)
        first = int(segment.base_vertex_location)
        result[first:first + len(value)] = value[:max(0, min(len(value), size - first))]
    return result


def _rebuild_morph_ranges(ctx, blend, buffer_index, origin, count, values, normals, *, structural, identity=None):
    payload = ctx.mesh.mesh_buffer.buffer_payloads[buffer_index]
    identity = origin if identity is None else np.asarray(identity, dtype=np.int64)
    bind_normals = _bind(np.asarray(payload.normals, dtype=np.float32).reshape(-1, 3), count)
    origin = np.asarray(origin, dtype=np.int64)
    old_size = int(max(origin.max(initial=0) + 1, ctx.original_counts[ctx.domains.index[id(payload)]]))
    structure_changed = structural
    for body_index, body in enumerate(blend.bodies):
        if body.alias_of_lod is not None:
            continue
        if not any(s.buffer_index == buffer_index for c in body.channels for s in c.segments):
            continue
        dense = {}
        for position, channel in enumerate(body.channels):
            key = (body_index, position)
            fill = None if structural else _current_deltas(channel, buffer_index, count)
            if key in values or fill is not None:
                dense[key] = _dense(values.get(key, ()), count, fill=fill)
        targets = (*body.targets, *body.none_targets)
        target_runs = {}
        for target_index, target in enumerate(targets):
            covered = np.zeros(count, bool)
            if structural:
                for item in target.ranges:
                    covered |= (identity >= item.base_vertex_location) & (
                        identity < item.base_vertex_location + item.vertex_count)
            else:
                for item in target.ranges:
                    covered[item.base_vertex_location:item.base_vertex_location + item.vertex_count] = True
            need = np.zeros(count, bool)
            if target_index < len(body.targets):
                for position, channel in enumerate(body.channels):
                    key = (body_index, position)
                    if (target.channel_location <= channel.index < target.channel_location + target.channel_count
                            and key in dense):
                        need |= np.any(dense[key] != 0, axis=1)
            extra = need & ~covered
            if not structural and not extra.any():
                target_runs[target_index] = None
                continue
            if extra.any():
                structure_changed = True
                ctx.report.change(f"Extended morph target {target_index} of LOD{body.lod_index} to "
                                  f"{int(extra.sum())} more vertices")
            runs = _runs(covered | need)
            template = target.ranges[0] if target.ranges else None
            if template is None:
                if runs:
                    raise ValueError("A morph target without ranges cannot gain vertices")
                target_runs[target_index] = []
                continue
            ranges = []
            for run in runs:
                source = next((item for item in target.ranges if structural and np.any(
                    (origin[run] >= item.base_vertex_location)
                    & (origin[run] < item.base_vertex_location + item.vertex_count))), template)
                ranges.append(replace(source, base_vertex_location=int(run[0]), vertex_count=len(run),
                                      blend_shape_location=0))
            target.ranges = ranges or [replace(template, vertex_count=0, blend_shape_location=0)]
            target_runs[target_index] = runs
        changed_targets = set()
        for position, channel in enumerate(body.channels):
            key = (body_index, position)
            local = [s for s in channel.segments if s.buffer_index == buffer_index]
            others = [s for s in channel.segments if s.buffer_index != buffer_index]
            rebuilt = []
            for target_index in sorted({s.target_index for s in local}):
                segments = [s for s in local if s.target_index == target_index]
                runs = target_runs.get(target_index)
                if runs is None:
                    for segment in segments:
                        first = int(segment.base_vertex_location)
                        size = len(segment.position_deltas)
                        new = dense[key][first:first + size] if key in dense else segment.position_deltas
                        if not np.array_equal(np.asarray(segment.position_deltas, np.float32), new):
                            changed_targets.add(target_index)
                        target_normals = segment.target_normals
                        if target_normals is not None and key in normals:
                            fill = bind_normals.copy()
                            fill[first:first + size] = np.asarray(segment.target_normals, np.float32).reshape(-1, 3)
                            target_normals = _dense(normals[key], count, fill=fill)[first:first + size]
                        rebuilt.append(replace(segment, position_deltas=np.asarray(new, np.float32),
                                               target_normals=target_normals))
                    continue
                template = segments[0]
                rows = origin if structural else np.arange(count)
                extras = {name: _old_dense(segments, name, old_size) for name in _SEGMENT_EXTRAS}
                old_normals = _old_dense(segments, "target_normals", old_size)
                changed_targets.add(target_index)
                for range_index, run in enumerate(runs):
                    source_rows = rows[run]
                    valid = (source_rows >= 0) & (source_rows < old_size)
                    planes = {}
                    for name, table in extras.items():
                        if table is None:
                            planes[name] = None
                            continue
                        result = np.zeros((len(run), *table.shape[1:]), dtype=table.dtype)
                        result[valid] = table[source_rows[valid]]
                        planes[name] = result
                    target_normals = None
                    if template.target_normals is not None:
                        fill = bind_normals.copy()
                        if old_normals is not None:
                            fill[run[valid]] = old_normals[source_rows[valid]]
                        target_normals = (_dense(normals[key], count, fill=fill) if key in normals else fill)[run]
                    deltas = dense[key][run] if key in dense else np.zeros((len(run), 3), np.float32)
                    rebuilt.append(replace(template, range_index=range_index, base_vertex_location=int(run[0]),
                                           position_deltas=np.asarray(deltas, np.float32),
                                           target_normals=target_normals, **planes))
            channel.segments = others + rebuilt
        for target_index in changed_targets:
            _fit_quantization_box(ctx, body, target_index, targets[target_index])
        for alias in blend.bodies:
            if alias.alias_of_lod == body.lod_index and (structure_changed or changed_targets):
                alias.alias_of_lod = None
                alias.targets = [copy(t) for t in body.targets]
                alias.none_targets = [copy(t) for t in body.none_targets]
                for mine, theirs in zip(alias.channels, body.channels):
                    mine.segments = [replace(s, lod_index=alias.lod_index) for s in theirs.segments]
                structure_changed = True
    return structure_changed


def _current_deltas(channel, buffer_index, count):
    result = np.zeros((count, 3), np.float32)
    for segment in channel.segments:
        if segment.buffer_index == buffer_index:
            first = int(segment.base_vertex_location)
            values = np.asarray(segment.position_deltas, np.float32).reshape(-1, 3)
            result[first:first + len(values)] += values
    return result


def _bind(bind_normals, count):
    result = np.zeros((count, 3), np.float32)
    result[:min(count, len(bind_normals))] = bind_normals[:count]
    return result


def _apply_normal_redirects(ctx, rebuilt):
    data = ctx.mesh.normal_recalc_data
    if data is None:
        return
    main = {info["buffer"]: info for info in rebuilt.values() if info["layer"] == "main"}
    if not main:
        return
    lods = list(data.lods)
    legacy = not lods or not any(lod.redirects for lod in lods)
    payloads = ctx.mesh.mesh_buffer.buffer_payloads
    main_lods = ctx.mesh.meshes[0].lods if ctx.mesh.meshes else []

    def lod_span(lod_index, buffer_index):
        groups = [g for g in main_lods[lod_index].mesh_groups
                  if g.submeshes and int(g.submeshes[0].buffer_index) == buffer_index]
        if not groups:
            return 0, 0
        start = min(int(g.submeshes[0].verts_index_offset) for g in groups)
        return start, sum(int(g.vertex_count) for g in groups)

    def redirects_for(info, old_redirects, old_base, new_base, new_count):
        payload = payloads[info["buffer"]]
        positions = np.asarray(payload.positions, np.float32).reshape(-1, 3)[new_base:new_base + new_count]
        normals = np.asarray(payload.normals, np.float32).reshape(-1, 3)[new_base:new_base + new_count]
        # Native redirects stay within their draw.
        draws = info["draw_of"][new_base:new_base + new_count]
        first, inverse, _counts = _equal_rows([draws, positions, normals])
        rule = first[inverse]
        lookup = _lookup(info)
        identity = info["identity"][new_base:new_base + new_count]
        origin = info["origin"][new_base:new_base + new_count]
        targets = rule.copy()
        annotations = np.zeros(new_count, bool)
        old_targets = np.array([r.target_vertex for r in old_redirects], dtype=np.int64)
        old_annotations = np.array([r.annotation for r in old_redirects], dtype=bool)
        source = np.where(identity >= 0, identity, origin) - old_base
        valid = (source >= 0) & (source < len(old_targets))
        rows = np.flatnonzero(valid)
        annotations[rows] = old_annotations[source[rows]]
        keep = rows[identity[rows] >= 0]
        old_target = old_targets[source[keep]] + old_base
        mapped = np.where((old_target >= 0) & (old_target < len(lookup)), lookup[np.clip(old_target, 0, len(lookup) - 1)], -1)
        mapped_local = mapped - new_base
        good = (mapped_local >= 0) & (mapped_local < new_count)
        good[good] &= inverse[keep[good]] == inverse[mapped_local[good]]
        targets[keep[good]] = mapped_local[good]
        # Every redirect target must redirect to itself.
        for _ in range(32):
            following = targets[targets]
            if np.array_equal(following, targets):
                break
            targets = following
        unresolved = targets[targets] != targets
        targets[unresolved] = rule[unresolved]
        return tuple(NormalRecalcRedirect(int(t), bool(a)) for t, a in zip(targets, annotations))

    if legacy:
        info = main.get(0)
        if info is None:
            return
        _start, count = lod_span(0, 0)
        redirects = redirects_for(info, data.redirects, 0, 0, count)
        ctx.mesh.normal_recalc_data = replace(data, redirects=redirects)
        lods = [replace(lod, redirects=(), work_items=(), face_entries=()) for lod in lods]
        ctx.mesh.normal_recalc_data = replace(ctx.mesh.normal_recalc_data, lods=tuple(lods))
    else:
        new_lods = []
        for lod in lods:
            info = main.get(int(lod.buffer_index))
            if info is None or lod.lod_index >= len(main_lods):
                new_lods.append(lod)
                continue
            start, count = lod_span(lod.lod_index, int(lod.buffer_index))
            old = lod.redirects or (data.redirects if lod.lod_index == 0 else ())
            new_lods.append(replace(lod, redirects=redirects_for(info, old, int(lod.vertex_base), start, count),
                                    vertex_base=start, work_items=(), face_entries=()))
        first = next((l for l in new_lods if l.lod_index == 0), None)
        ctx.mesh.normal_recalc_data = replace(data, lods=tuple(new_lods),
                                              redirects=first.redirects if first is not None and data.redirects else data.redirects)
    ctx.report.change("Rebuilt normal-recalculation redirects for the edited vertex buffers")
    if ctx.mesh.profile.normal_recalc_layout is NormalRecalcLayout.RE8:
        materialize_normal_work(ctx.mesh)


def _apply_bone_modifier(ctx, rebuilt):
    blend = ctx.mesh.blend_shape_data
    modifier = getattr(blend, "bone_modifier", None) if blend is not None else None
    if not modifier or not modifier.effectors:
        return
    info = next((i for i in rebuilt.values() if i["layer"] == "main" and i["buffer"] == 0), None)
    if info is None:
        return
    lookup = _lookup(info)
    effectors, dropped = [], 0
    for effector in modifier.effectors:
        indices, positions = [], []
        for vertex, position in zip(effector.vertex_indices, effector.vertex_positions):
            new = lookup[vertex] if 0 <= vertex < len(lookup) else -1
            if new < 0:
                dropped += 1
                continue
            indices.append(int(new))
            positions.append(position)
        effectors.append(replace(effector, vertex_indices=tuple(indices),
                                 vertex_positions=np.asarray(positions, dtype=np.float32).reshape(-1, 3)))
    modifier.effectors = effectors
    if dropped:
        ctx.report.loss(f"Removed {dropped} bone-modifier vertices that no longer exist")


def _apply_references(ctx, plans, rebuilt):
    for plan in plans:
        build = plan.build
        info = rebuilt.get(plan.domain)
        base = ctx.domains.bases[plan.domain]
        changed_topology = not (plan.key and _check_in_place(plan, ctx.scene.meshes.get(
            ctx.scene_object(plan.key).mesh_key)))
        if info is None and not changed_topology:
            continue
        lookup = _lookup(info) if info is not None else np.arange(len(ctx.domains.payloads[plan.domain].positions) // 3)
        identity = build.identity - base
        width = 4 if plan.is_quad else 3
        mapped = np.where(identity >= 0, lookup[np.clip(identity, 0, len(lookup) - 1)], -1)
        faces = build.faces.reshape(-1, width)
        vertices = mapped[faces] if len(faces) else faces
        keep = np.all(vertices >= 0, axis=1) if len(faces) else np.zeros(0, bool)
        if len(faces) and not keep.all():
            ctx.report.loss(f"{plan.layer} LOD{plan.lod}: dropped {int((~keep).sum())} faces whose shared vertices "
                            "were removed")
        vertices = vertices[keep]
        if not plan.submesh:
            ctx.report.loss(f"New {plan.layer} geometry cannot share another layer's vertices; ignored it")
            continue
        submesh = plan.submesh[0]
        payload = ctx.domains.payloads[plan.domain]
        if len(vertices):
            low = int(vertices.min())
            local = (vertices - low).ravel()
        else:
            low, local = 0, np.zeros(0, np.int64)
        current = np.asarray(payload.index_values, dtype=np.int64)
        submesh.verts_index_offset = low
        submesh.vert_count = int(local.max()) + 1 if len(local) else 0
        submesh.faces_index_offset = len(current)
        submesh.indices_count = len(local)
        payload.integer_faces = _to_array("I", np.concatenate((current, local)).astype(np.uint32))
        payload.faces = array("H")
        ctx.report.change(f"Updated shared {plan.layer} LOD{plan.lod} draws")
