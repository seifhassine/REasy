from array import array
from dataclasses import replace

lazy import numpy as np

lazy from .conversion_geometry import geometry_layers, mesh_payloads
from .models import MeshAABB
lazy from .conversion_normals import reflect_normal_work


def transform_coordinates(mesh, axes):
    if (len(axes) != 3 or any(type(value) is not int for value in axes)
            or sorted(abs(value) for value in axes) != [1, 2, 3]):
        raise ValueError("Coordinate order must contain each of ±X, ±Y, ±Z once")
    if tuple(axes) == (1, 2, 3):
        return
    order = np.abs(axes) - 1
    signs = np.sign(axes)
    basis = np.eye(3)[order] * signs[:, None]
    reflected = np.linalg.det(basis) < 0

    def vectors(values):
        values = np.asarray(values, dtype=np.float64).reshape(-1, 3)
        return values[:, order] * signs

    def box(value):
        corners = vectors((value.minimum, value.maximum))
        return replace(value, minimum=tuple(corners.min(axis=0)),
                       maximum=tuple(corners.max(axis=0)))

    def handedness(values):
        return [(-max(value - 256 if value & 128 else value, -127)) & 255 for value in values]

    def winding(values):
        if len(values) % 3:
            raise ValueError("Cannot reflect non-triangle topology")
        return np.asarray(values).reshape(-1, 3)[:, (0, 2, 1)].ravel().tolist()

    for payload in mesh_payloads(mesh):
        for name in ("positions", "normals", "tangents"):
            setattr(payload, name, array("f", vectors(getattr(payload, name)).ravel()))
        if reflected:
            payload.tangent_ws = array("B", handedness(payload.tangent_ws or [127] * payload.vertex_count))
            # Index buffers hold padding between draw ranges, which is not a triangle.

    if reflected:
        draws = {}
        for _layer, groups, owners in geometry_layers(mesh):
            for submesh in (*group.submeshes for group in groups):
                if submesh.is_quad:
                    raise ValueError("Coordinate reflection of quad topology is unsupported")
                payload = owners[submesh.buffer_index]
                faces = payload.index_values
                start, count = submesh.faces_index_offset, submesh.indices_count
                if start < 0 or count % 3 or start + count > len(faces):
                    raise ValueError("Cannot reflect an invalid triangle draw range")
                draws.setdefault(id(faces), (faces, []))[1].append((start, start + count))
        for faces, ranges in draws.values():
            merged = []
            for start, end in sorted(ranges):
                if merged and start < merged[-1][1]:
                    previous, stop = merged[-1]
                    if (start - previous) % 3:
                        raise ValueError("Overlapping draw ranges disagree about triangle boundaries")
                    merged[-1] = (previous, max(stop, end))
                else:
                    merged.append((start, end))
            for start, end in merged:
                faces[start:end] = array(faces.typecode, winding(faces[start:end]))
        reflect_normal_work(mesh)

    for chunk in mesh.mesh_buffer.mply_chunks:
        for name in ("positions", "normals", "tangents"):
            setattr(chunk, name, vectors(getattr(chunk, name)))
        if chunk.cluster_aabb is not None:
            bounds = chunk.cluster_aabb
            chunk.cluster_aabb = replace(bounds, center=tuple(vectors([bounds.center])[0]),
                                         half_extent=tuple(np.asarray(bounds.half_extent)[order]))
        if reflected:
            chunk.faces = winding(chunk.faces)
            chunk.tangent_ws = handedness(chunk.tangent_ws if len(chunk.tangent_ws) else [127] * chunk.vert_count)

    transform = np.eye(4)
    transform[:3, :3] = basis
    # RE matrices are row-vector.
    for name in ("local_matrices", "world_matrices", "inverse_bind_matrices"):
        setattr(mesh, name, [(transform @ np.asarray(value).reshape(4, 4).T @ transform.T)
                            .T.ravel().tolist() for value in getattr(mesh, name)])
    if mesh.group_pivots is not None:
        mesh.group_pivots = replace(mesh.group_pivots, pivots=tuple(
            tuple(row) for row in vectors(mesh.group_pivots.pivots)))
    if mesh.bounding_boxes is not None:
        mesh.bounding_boxes.boxes = [box(value) for value in mesh.bounding_boxes.boxes]
    for data in mesh.meshes:
        bounds = box(MeshAABB(data.bounding_box[0][:3], data.bounding_box[1][:3]))
        data.bounding_box = (bounds.minimum4, bounds.maximum4)
        data.bounding_sphere = (*vectors([data.bounding_sphere[:3]])[0], data.bounding_sphere[3])
    if mesh.blend_shape_data is not None:
        blend = mesh.blend_shape_data
        seen_targets, seen_segments = set(), set()
        for body in blend.bodies:
            for target in (*body.targets, *body.none_targets):
                if target.aabb is not None and id(target) not in seen_targets:
                    target.aabb = box(target.aabb)
                    seen_targets.add(id(target))
            for channel in body.channels:
                for segment in channel.segments:
                    if id(segment) in seen_segments:
                        continue
                    seen_segments.add(id(segment))
                    segment.position_deltas = vectors(segment.position_deltas).astype(np.float32)
                    if segment.target_normals is not None:
                        segment.target_normals = vectors(segment.target_normals).astype(np.float32)
        if blend.bone_modifier is not None:
            blend.bone_modifier.effectors = [replace(e, vertex_positions=vectors(e.vertex_positions))
                                            for e in blend.bone_modifier.effectors]
