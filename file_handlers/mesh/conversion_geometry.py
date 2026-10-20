from array import array
from copy import deepcopy
from dataclasses import replace

lazy import numpy as np

lazy from .contract_utils import layer_groups
lazy from .conversion_allocations import _slice_payload, join_payloads
lazy from .mesh_file import (
    MESH_MAGIC, MPLY_MAGIC, GroupPivotData, MeshBufferPayload, MeshData, MeshGroup, MeshLOD,
    Submesh,
)
lazy from .meshlet_clusters import split_chunk
lazy from .conversion_skin import coalesce_weights


def retains_meshlets(mesh, profile, output_kind="auto"):
    if (mesh.header.magic != MPLY_MAGIC or output_kind != "auto"
            or not profile.meshlet_chunk_payload_is_fully_decoded
            or (mesh.bones and not profile.canonical_mply_supports_skeleton)
            or (mesh.group_pivots and mesh.group_pivots.pivots and not profile.canonical_mply_supports_pivots)):
        return False
    settings = mesh.mesh_buffer.mply_runtime_settings
    if (settings and profile.canonical_mply_raster_thresholds is not None
            and (settings.meshlet_raster_switch_threshold not in profile.canonical_mply_raster_thresholds
                 or int(settings.lod_strategy))):
        return False
    for chunk in mesh.mesh_buffer.mply_chunks:
        if chunk.meshlet_material_flag_extension and not profile.canonical_mply_supports_material_extension:
            return False
        if len(chunk.uv2) and not profile.canonical_mply_supports_uv2:
            return False
        if (not profile.canonical_mply_supports_render_flags and any((chunk.no_ray_tracing,
                chunk.alpha_test, chunk.transparent, chunk.meshlet_material_flag_extension))):
            return False
        stream = chunk.skin_weights
        if stream is not None:
            if not profile.canonical_mply_supports_skinning:
                return False
            weights = np.asarray(stream.weights).reshape(stream.vertex_count, stream.influence_count)
            indices = np.asarray(stream.deform_indices).reshape(weights.shape)
            if stream.influence_count > profile.skin_weight_influence_count:
                if np.any(np.count_nonzero(coalesce_weights(indices, weights), axis=1)
                          > profile.skin_weight_influence_count):
                    return False
            if profile.canonical_mply_requires_shared_skinning:
                indices = np.where(weights > 0, indices, 0)
                if np.any(weights != weights[:1]) or np.any(indices != indices[:1]):
                    return False
    return True


def geometry_layers(mesh):
    for layer in (*mesh.meshes, mesh.shadow_mesh, mesh.occluder_mesh):
        if layer is not None:
            yield layer, layer_groups(layer), getattr(layer, "payloads", mesh.mesh_buffer.buffer_payloads)


def mesh_payloads(mesh):
    values = [payload for _index, payload in sorted(mesh.mesh_buffer.buffer_payloads.items())]
    for layer in (mesh.shadow_mesh, mesh.occluder_mesh):
        if layer is not None:
            values.extend(payload for _index, payload in sorted(layer.payloads.items()))
    return list({id(payload): payload for payload in values}.values())


def materialize_tangents(chunk, loss):
    if len(chunk.tangents) and not (chunk.flags and chunk.flags.is_meshlet_no_tangent):
        return
    loss("Generated stored tangents from meshlet triangles and UVs; runtime derivative tangents may differ")
    faces = np.asarray(chunk.faces, dtype=np.int64).reshape(-1, 3)
    points = np.asarray(chunk.positions, dtype=np.float64)
    uv = np.asarray(chunk.uv0, dtype=np.float64)
    normals = np.asarray(chunk.normals, dtype=np.float64)
    edges = points[faces[:, 1:]] - points[faces[:, :1]]
    delta_uv = uv[faces[:, 1:]] - uv[faces[:, :1]]
    determinant = delta_uv[:, 0, 0] * delta_uv[:, 1, 1] - delta_uv[:, 0, 1] * delta_uv[:, 1, 0]
    valid = np.isfinite(delta_uv).all(axis=(1, 2)) & (np.abs(determinant) > 1e-12)
    tangent = np.zeros((len(faces), 3))
    tangent[valid] = ((edges[valid, 0] * delta_uv[valid, 1, 1, None]
                       - edges[valid, 1] * delta_uv[valid, 0, 1, None]) / determinant[valid, None])
    summed = np.zeros_like(points)
    np.add.at(summed, faces.ravel(), np.repeat(tangent, 3, axis=0))
    normal_length = np.sum(normals * normals, axis=1, keepdims=True)
    summed -= normals * (np.sum(summed * normals, axis=1, keepdims=True) / np.maximum(normal_length, 1e-12))
    lengths = np.linalg.norm(summed, axis=1, keepdims=True)
    fallback = lengths[:, 0] <= 1e-12
    basis = np.eye(3)[np.argmin(np.abs(normals[fallback]), axis=1)]
    summed[fallback] = np.cross(basis, normals[fallback])
    summed[normal_length[:, 0] <= 1e-12] = (1, 0, 0)
    lengths = np.linalg.norm(summed, axis=1, keepdims=True)
    chunk.tangents = summed / np.maximum(lengths, 1e-12)
    if not len(chunk.tangent_ws):
        chunk.tangent_ws = np.full(chunk.vert_count, 127, np.int64)


def detach_topology(layer, memo):
    name = "lods" if hasattr(layer, "lods") else "mesh_groups"
    setattr(layer, name, deepcopy(getattr(layer, name), memo))


def separate_normal_lods(mesh):
    buffer = mesh.mesh_buffer
    primary = {id(s) for lod in mesh.meshes[0].lods for g in lod.mesh_groups for s in g.submeshes}
    auxiliary = [(s, s.buffer_index, s.verts_index_offset) for layer in (mesh.shadow_mesh, mesh.occluder_mesh)
                 if layer is not None and layer.shares_main_payload
                 for lod in getattr(layer, "lods", (layer,)) for g in lod.mesh_groups for s in g.submeshes
                 if id(s) not in primary]
    allocations = []
    plans = {}
    for lod in mesh.meshes[0].lods:
        submeshes = [*g.submeshes for g in lod.mesh_groups]
        indices = {s.buffer_index for s in submeshes}
        if len(indices) != 1:
            raise ValueError("Normal-recalculation LOD spans multiple vertex buffers")
        source = next(iter(indices))
        plans.setdefault(source, {})[id(lod)] = lod
    moved = {}
    for source, lods in plans.items():
        if len(lods) == 1:
            continue
        original = buffer.buffer_payloads[source]
        for index, lod in enumerate(lods.values()):
            submeshes = [*g.submeshes for g in lod.mesh_groups]
            start = min(s.verts_index_offset for s in submeshes)
            end = max(s.verts_index_offset + s.vert_count for s in submeshes)
            target = source if index == 0 else max(buffer.buffer_payloads) + 1
            payload = _slice_payload(original, start, end)
            payload.faces, payload.integer_faces = original.faces, original.integer_faces
            buffer.buffer_payloads[target] = payload
            for submesh in submeshes:
                submesh.buffer_index = target
                submesh.verts_index_offset -= start
            moved[id(lod)] = (target, start)
            allocations.append((source, start, end, target))
    for submesh, source, start in auxiliary:
        if source not in {index for index, *_ in allocations}:
            continue
        match = next(((low, target) for index, low, high, target in allocations
                      if index == source and low <= start and start + submesh.vert_count <= high), None)
        if match is None:
            raise ValueError("Auxiliary draw crosses the target's separate normal LOD allocations")
        low, target = match
        submesh.buffer_index, submesh.verts_index_offset = target, start - low
    if mesh.blend_shape_data is not None:
        for index, body in enumerate(mesh.blend_shape_data.bodies):
            move = moved.get(id(mesh.meshes[0].lods[body.lod_index]))
            if move is None:
                continue
            target_index, offset = move
            body = mesh.blend_shape_data.bodies[index] = deepcopy(body)
            body.alias_of_lod = None
            for target in (*body.targets, *body.none_targets):
                target.ranges = [replace(r, base_vertex_location=r.base_vertex_location - offset)
                                 for r in target.ranges]
            for channel in body.channels:
                for segment in channel.segments:
                    segment.buffer_index = target_index
                    segment.base_vertex_location -= offset
        modifier = mesh.blend_shape_data.bone_modifier
        if modifier is not None:
            _target, offset = moved.get(id(mesh.meshes[0].lods[0]), (0, 0))
            count = buffer.buffer_payloads[0].vertex_count
            for effector in modifier.effectors:
                if any(not offset <= index < offset + count for index in effector.vertex_indices):
                    raise ValueError("Bone modifier references vertices outside the resident LOD")
            modifier.effectors = [replace(e, vertex_indices=tuple(i - offset for i in e.vertex_indices))
                                  for e in modifier.effectors]
    if moved:
        mesh.streaming_info = None
        for layer in (mesh.shadow_mesh, mesh.occluder_mesh):
            if layer is not None and layer.shares_main_payload:
                layer.payloads = dict(buffer.buffer_payloads)


def split_large_meshlets(mesh, profile, loss):
    if profile.meshlet_max_triangles is None:
        return
    buffer = mesh.mesh_buffer
    split = [split_chunk(chunk, profile.meshlet_max_vertices, profile.meshlet_max_triangles)
             for chunk in buffer.mply_chunks]
    if len(split) < sum(map(len, split)):
        buffer.mply_chunks = [*parts for parts in split]
        loss("Split meshlets larger than the target draws; their positions are requantized")


def meshlets_to_mesh(mesh, loss):
    buffer = mesh.mesh_buffer
    lod_groups = {}
    for chunk in buffer.mply_chunks:
        lod_groups.setdefault(chunk.lod_index, {}).setdefault(chunk.part_id, {}).setdefault(
            chunk.material_id, []).append(chunk)
    chunks = [chunk for lod in sorted(lod_groups) for part in lod_groups[lod].values()
              for batch in part.values() for chunk in batch]
    if (any(chunk.transparent for chunk in chunks)
            and [id(chunk) for chunk in chunks] != [id(chunk) for chunk in buffer.mply_chunks]):
        loss("Ordinary MESH groups meshlets by material; transparent draw ordering may differ")
    payloads = {}
    rounding_errors = {}
    for index, chunk in enumerate(chunks):
        materialize_tangents(chunk, loss)
        if any((chunk.no_ray_tracing, chunk.alpha_test, chunk.transparent,
                chunk.meshlet_material_flag_extension)):
            loss("Ordinary MESH cannot retain per-meshlet rendering flags")
        payload = MeshBufferPayload(vertex_count=chunk.vert_count,
                                    integer_faces=array("I", chunk.faces), skin_weights=chunk.skin_weights)
        for name in ("positions", "normals", "tangents", "uv0", "uv1", "uv2"):
            getattr(payload, name).extend(v for row in getattr(chunk, name) for v in row)
            before = np.asarray(getattr(chunk, name), dtype=np.float64).ravel()
            after = np.asarray(getattr(payload, name), dtype=np.float64)
            finite = np.isfinite(before) & np.isfinite(after)
            error = np.max(np.abs(before[finite] - after[finite]), initial=0)
            if error:
                rounding_errors[name] = max(rounding_errors.get(name, 0), error)
        for name in ("normal_ws", "tangent_ws", "colors"):
            getattr(payload, name).extend(getattr(chunk, name))
        if chunk.flags is not None and chunk.flags.is_meshlet_no_tangent:
            loss("Ordinary MESH replaces the meshlet derivative-tangent path with stored tangents")
        payloads[index] = payload
    for name, error in rounding_errors.items():
        loss(f"Ordinary MESH quantizes {name} to native floats (maximum component error {error:.9g})")
    payload, bases = join_payloads(payloads, loss)
    data = MeshData(buffer)
    settings = buffer.mply_runtime_settings
    for lod_index in range(max(lod_groups) + 1):
        lod = MeshLOD(buffer)
        lod.lod_level = lod_index
        lod.lod_factor = settings.lod_factors[lod_index] if settings and settings.lod_factors else 0.0
        groups = {}
        for index, chunk in enumerate(chunks):
            if chunk.lod_index != lod_index:
                continue
            if chunk.part_id not in groups:
                group = MeshGroup(buffer)
                group.group_id = chunk.part_id
                groups[chunk.part_id] = group
            group = groups[chunk.part_id]
            submesh = group.submeshes[-1] if group.submeshes else None
            if (submesh is None or submesh.material_index != chunk.material_id
                    or submesh.vert_count + chunk.vert_count > 0x10000):
                submesh = Submesh(buffer)
                submesh.material_index = chunk.material_id
                submesh.verts_index_offset, submesh.faces_index_offset = bases[index]
                group.submeshes.append(submesh)
            vertex_start, face_start = bases[index]
            offset = vertex_start - submesh.verts_index_offset
            payload.integer_faces[face_start:face_start + len(chunk.faces)] = array(
                "I", (int(value) + offset for value in chunk.faces))
            submesh.vert_count += chunk.vert_count
            submesh.indices_count += len(chunk.faces)
            group.vertex_count += chunk.vert_count
        lod.mesh_groups = list(groups.values())
        data.lods.append(lod)
    points = np.asarray(payload.positions).reshape(-1, 3)
    low, high = points.min(axis=0), points.max(axis=0)
    center = (low + high) * 0.5
    data.bounding_box = ((*low, 0.0), (*high, 0.0))
    data.bounding_sphere = (*center, float(np.linalg.norm(points - center, axis=1).max()))
    # MPLY material IDs address the name partition directly; ordinary MESH goes through the index table.
    data.material_count = len(mesh.material_names)
    mesh.material_indices = list(range(data.material_count))
    data.uv_count = sum(bool(len(getattr(payload, name))) for name in ("uv0", "uv1", "uv2"))
    if settings and (settings.meshlet_raster_switch_threshold or int(settings.lod_strategy)):
        loss("Ordinary MESH cannot retain meshlet raster/LOD strategy controls")
    mesh.meshes = [data]
    mesh.header.magic = MESH_MAGIC
    if mesh.group_pivots is not None:
        mesh.group_pivots = GroupPivotData(pivots=list(mesh.group_pivots.pivots))
    buffer.buffer_payloads = {0: payload}
    buffer.mply_chunks = []
    buffer.streaming_buffer_headers = {}
    buffer.streaming_buffer_count = 0
    mesh.streaming_info = None
