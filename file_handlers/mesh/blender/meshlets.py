from __future__ import annotations

from array import array
from collections import Counter
from dataclasses import replace

lazy import numpy as np

lazy from ..conversion_allocations import _WEIGHT_METADATA
from ..mesh_file import MPLYChunk
lazy from ..meshlet_clusters import meshletize as _meshletize

MAX_MESHLET_VERTICES = 128
MAX_MESHLET_TRIANGLES = 126
_VECTOR_PLANES = ("positions", "normals", "tangents", "uv0", "uv1", "uv2")
_BYTE_PLANES = ("normal_ws", "tangent_ws")


def _chunk_starts(chunks):
    return np.cumsum([0] + [int(chunk.vert_count) for chunk in chunks])


def _skin_stream(template, indices, weights):
    count = len(indices)
    width = template.influence_count
    lanes_i = np.zeros((count, width), np.uint16)
    lanes_w = np.zeros((count, width), np.float32)
    lanes_i[:, :min(width, indices.shape[1])] = indices[:, :width]
    lanes_w[:, :min(width, weights.shape[1])] = weights[:, :width]
    metadata = {}
    for name in _WEIGHT_METADATA:
        raw = getattr(template, name)
        stride = len(raw) // template.vertex_count if template.vertex_count else 0
        metadata[name] = bytes(count * stride)
    deform = array("H")
    deform.frombytes(lanes_i.astype("<u2").tobytes())
    values = array("f")
    values.frombytes(lanes_w.astype("<f4").tobytes())
    return replace(template, vertex_count=count, deform_indices=deform, weights=values, **metadata)


def write_in_place(ctx, plans):
    """Only edited rows are written: decoded meshlet values need not survive float32."""
    chunks = ctx.mesh.mesh_buffer.mply_chunks
    starts = _chunk_starts(chunks)
    base = ctx.domains.bases[plans[0].domain]
    for plan in plans:
        build = plan.build
        local = build.identity - base
        chunk_of = np.searchsorted(starts, local, side="right") - 1
        edited = build.changed_vertices | build.retangent
        reweighted = getattr(build, "reweighted", np.zeros(build.count, bool))
        for number in np.unique(chunk_of[edited | reweighted]):
            chunk = chunks[number]
            rows = np.flatnonzero((chunk_of == number) & edited)
            offsets = local[rows] - starts[number]
            for name in _VECTOR_PLANES:
                values = getattr(chunk, name)
                if len(values):
                    values[offsets] = build.planes[name][rows]
            for name in _BYTE_PLANES:
                values = getattr(chunk, name)
                if len(values):
                    values[offsets] = build.planes[name][rows, 0]
            if len(chunk.colors):
                chunk.colors.reshape(-1, 4)[offsets] = build.planes["colors"][rows]
            if chunk.skin_weights is not None and "primary" in build.weights and reweighted[chunk_of == number].any():
                rows = np.flatnonzero(chunk_of == number)
                order = np.argsort(local[rows] - starts[number])
                indices, weights = build.weights["primary"]
                chunk.skin_weights = _skin_stream(chunk.skin_weights, indices[rows[order]], weights[rows[order]])


def _limits(chunks, profile):
    vertices = max((int(c.vert_count) for c in chunks), default=MAX_MESHLET_VERTICES)
    triangles = max((len(c.faces) // 3 for c in chunks), default=MAX_MESHLET_TRIANGLES)
    return (min(max(vertices, 3), profile.meshlet_max_vertices or 255),
            min(max(triangles, 1), profile.meshlet_max_triangles or 170))


def _partition(ctx, triangles, points, limits):
    """A reference import copies one model into every LOD."""
    done = ctx.meshlet_partitions
    for seen_triangles, seen_points, seen_limits, clusters in done:
        if seen_limits == limits and np.array_equal(seen_triangles, triangles) and np.array_equal(seen_points, points):
            return clusters
    clusters = _meshletize(triangles, points, *limits)
    done.append((triangles, points, limits, clusters))
    return clusters


def _new_chunks(ctx, plan, template, limits):
    build = plan.build
    triangles = build.faces.reshape(-1, 3)
    location = np.where(build.vertex_of >= 0, build.object_of * (1 << 32) + build.vertex_of, -1 - np.arange(build.count))
    clusters = _partition(ctx, triangles, location[triangles], limits)
    if not clusters:
        return []
    sizes = np.array([len(members) for members in clusters], np.int64) * 3
    corners = triangles[np.concatenate(clusters)].ravel()
    meshlet = np.repeat(np.arange(len(clusters)), sizes)
    pairs, first, local = np.unique(meshlet * build.count + corners, return_index=True, return_inverse=True)
    local = local.ravel()
    order = np.lexsort((first, pairs // build.count))
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    vertex_order = (pairs % build.count)[order]
    counts = np.bincount(pairs // build.count, minlength=len(clusters))
    vertex_starts = np.concatenate(([0], np.cumsum(counts)))
    faces = (rank[local] - np.repeat(vertex_starts[:-1], sizes)).astype(np.int64)
    face_starts = np.concatenate(([0], np.cumsum(sizes))).tolist()

    def rows(name, wanted=True):
        if not wanted:
            return None
        values = build.planes[name][vertex_order]
        return values.astype(np.float64) if values.dtype.kind == "f" else values.astype(np.int64)

    positions, normals, uv0 = rows("positions"), rows("normals"), rows("uv0")
    uv1, uv2 = rows("uv1", bool(len(template.uv1))), rows("uv2", bool(len(template.uv2)))
    tangents = rows("tangents", bool(len(template.tangents)))
    normal_ws = rows("normal_ws", bool(len(template.normal_ws)))
    tangent_ws = rows("tangent_ws", bool(len(template.tangent_ws)))
    colors = rows("colors", bool(len(template.colors)))
    skin = template.skin_weights is not None and "primary" in build.weights
    chunks = []
    for number in range(len(clusters)):
        start, end = int(vertex_starts[number]), int(vertex_starts[number + 1])
        span = slice(start, end)
        chunk = MPLYChunk(
            vert_count=end - start,
            positions=positions[span],
            faces=faces[face_starts[number]:face_starts[number + 1]],
            normals=normals[span],
            uv0=uv0[span],
            uv1=uv1[span] if uv1 is not None else (),
            uv2=uv2[span] if uv2 is not None else (),
            colors=colors[span].ravel() if colors is not None else (),
            normal_ws=normal_ws[span, 0] if normal_ws is not None else (),
            tangents=tangents[span] if tangents is not None else (),
            tangent_ws=tangent_ws[span, 0] if tangent_ws is not None else (),
            material_id=int(plan.material), part_id=int(plan.part_id), flags=template.flags,
            lod_index=int(plan.lod), no_ray_tracing=template.no_ray_tracing, alpha_test=template.alpha_test,
            transparent=template.transparent,
            meshlet_material_flag_extension=template.meshlet_material_flag_extension,
        )
        if skin:
            indices, weights = build.weights["primary"]
            vertices = vertex_order[span]
            chunk.skin_weights = _skin_stream(template.skin_weights, indices[vertices], weights[vertices])
        chunks.append(chunk)
    return chunks


def _check_meshlet_count(ctx, per_lod, qualifier=""):
    # The meshlet BVH stores each LOD's meshlet count in 16 bits where meshlets have extended indices, else 32.
    limit = 0xFFFF if getattr(ctx.mesh.profile, "meshlet_has_extended_indices", False) else 0xFFFFFFFF
    if per_lod:
        lod, count = per_lod.most_common(1)[0]
        if count > limit:
            raise ValueError(f"LOD{lod} needs {qualifier}{count:,} meshlets; the format holds {limit:,} per LOD. "
                             "Decimate the model, or apply a lower Multires/Subdivision level")


def _draw(ctx, plan):
    names = ctx.mesh.material_names
    return f"LOD{plan.lod} part {plan.part_id} ({names[plan.material] if plan.material < len(names) else plan.material})"


def rebuild(ctx, plans):
    buffer = ctx.mesh.mesh_buffer
    chunks = list(buffer.mply_chunks)
    starts = _chunk_starts(chunks)
    limits = _limits(chunks, ctx.mesh.profile)
    ctx.meshlet_partitions = []
    least = Counter()
    for plan in plans:
        if plan.build.count:
            least[plan.lod] += -(-len(plan.build.faces) // 3 // limits[1])
    _check_meshlet_count(ctx, least, "at least ")
    owner = {}
    for plan in plans:
        for submesh in plan.submesh:
            number = int(np.searchsorted(starts, int(submesh.verts_index_offset), side="right") - 1)
            owner[number] = plan
    for plan in plans:
        if plan.build.in_place:
            write_in_place(ctx, [plan])
    output, emitted = [], set()
    for number, chunk in enumerate(chunks):
        plan = owner.get(number)
        if plan is None or plan.build.in_place:
            output.append(chunk)
            continue
        if id(plan) in emitted:
            continue
        emitted.add(id(plan))
        if plan.build.count:
            output.extend(_new_chunks(ctx, plan, chunk, limits))
            ctx.report.change(f"Rebuilt the meshlets of {_draw(ctx, plan)}; their positions are requantized to the "
                              "new meshlet bounds")
        elif not ctx.reference:
            ctx.report.change(f"Removed the meshlets of {_draw(ctx, plan)}")
    added = {}
    for plan in plans:
        if plan.submesh or not plan.build.count:
            continue
        template = next((c for c in chunks if c.lod_index == plan.lod and c.part_id == plan.part_id),
                        next((c for c in chunks if c.lod_index == plan.lod), chunks[0]))
        new = _new_chunks(ctx, plan, template, limits)
        position = max((i for i, c in enumerate(output) if c.lod_index <= plan.lod), default=len(output) - 1) + 1
        output[position:position] = new
        key = (plan.lod, plan.part_id, _draw(ctx, plan))
        added[key] = added.get(key, 0) + len(new)
    for (_lod, _part, draw), count in sorted(added.items()):
        ctx.report.change(f"Added {count} meshlet{'s' if count != 1 else ''} to {draw}")
    _check_meshlet_count(ctx, Counter(int(c.lod_index) for c in output))
    lods = sorted({int(c.lod_index) for c in chunks})
    if any(not any(int(c.lod_index) == lod for c in output) for lod in lods):
        raise ValueError("A meshlet LOD would have no geometry; keep at least one face per LOD")
    buffer.mply_chunks = output
