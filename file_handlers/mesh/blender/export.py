from __future__ import annotations

lazy import numpy as np

from ..mesh_file import joint_name
from ..profiles import VertexNormalWSemantic
from .package import flip_uv, matrix_to_blender, to_blender
lazy from .scene import UV_PLANES, WEIGHT_SETS, build_scene


def joint_names(mesh):
    return [joint_name(mesh, joint) for joint in range(len(mesh.bone_indices))]


def _bone_lengths(mesh, heads):
    lengths = np.full(len(heads), 0.0)
    children = {}
    for node in mesh.bones:
        children.setdefault(int(node.parent_index), []).append(int(node.index))
    for joint in range(len(heads)):
        distances = [np.linalg.norm(heads[child] - heads[joint]) for child in children.get(joint, ())]
        distances = [d for d in distances if d > 1e-4]
        lengths[joint] = min(distances) if distances else 0.0
    fallback = float(np.median(lengths[lengths > 0])) if np.any(lengths > 0) else 0.05
    for node in mesh.bones:
        joint = int(node.index)
        if lengths[joint] <= 0:
            parent = int(node.parent_index)
            lengths[joint] = 0.5 * lengths[parent] if 0 <= parent < len(lengths) and lengths[parent] > 0 else fallback
    return np.maximum(lengths, 1e-3)


def plane(values, width, dtype):
    return np.asarray(values, dtype=dtype).reshape(-1, width)


def gather(domains, ids, name, width, dtype):
    domain, local = domains.locate(ids)
    result = None
    for number in np.unique(domain):
        values = getattr(domains.payloads[number], name)
        if not len(values):
            return None
        rows = domain == number
        chosen = plane(values, width, dtype)[local[rows]]
        if result is None:
            result = np.zeros((len(ids), width), dtype=dtype)
        result[rows] = chosen
    return result if result is not None else np.zeros((0, width), dtype=dtype)


def vertex_weights(domains, ids, names):
    """Lanes hold joint palette indices, not joints."""
    domain, local = domains.locate(ids)
    lanes = None
    for number in np.unique(domain):
        payload = domains.payloads[number]
        streams = [getattr(payload, name, None) for name in names]
        streams = [s for s in streams if s is not None]
        if not streams:
            continue
        indices = np.concatenate([np.asarray(s.deform_indices, dtype=np.int64).reshape(-1, s.influence_count)
                                  for s in streams], axis=1)
        weights = np.concatenate([np.asarray(s.weights, dtype=np.float32).reshape(-1, s.influence_count)
                                  for s in streams], axis=1)
        rows = domain == number
        if lanes is None:
            lanes = (np.zeros((len(ids), indices.shape[1]), np.int64), np.zeros((len(ids), indices.shape[1]), np.float32))
        if lanes[0].shape[1] < indices.shape[1]:
            pad = indices.shape[1] - lanes[0].shape[1]
            lanes = (np.pad(lanes[0], ((0, 0), (0, pad))), np.pad(lanes[1], ((0, 0), (0, pad))))
        lanes[0][rows, :indices.shape[1]] = indices[local[rows]]
        lanes[1][rows, :indices.shape[1]] = weights[local[rows]]
    return lanes


def _sparse_groups(palette_lanes, palette_to_joint):
    indices, weights = palette_lanes
    vertices = np.repeat(np.arange(len(indices)), indices.shape[1])
    joints = np.asarray(palette_to_joint, dtype=np.int64)[np.clip(indices.ravel(), 0, len(palette_to_joint) - 1)]
    valid = (weights.ravel() > 0) & (indices.ravel() < len(palette_to_joint))
    vertices, joints, values = vertices[valid], joints[valid], weights.ravel()[valid].astype(np.float64)
    if not len(vertices):
        return np.zeros(0, np.int32), np.zeros(0, np.int32), np.zeros(0, np.float32)
    pairs = vertices * (int(joints.max()) + 1) + joints
    unique, inverse = np.unique(pairs, return_inverse=True)
    summed = np.zeros(len(unique))
    np.add.at(summed, inverse, values)
    stride = int(joints.max()) + 1
    return (unique // stride).astype(np.int32), (unique % stride).astype(np.int32), summed.astype(np.float32)


def blender_faces(scene_mesh):
    """Blender cannot store a face that repeats a vertex."""
    sizes = scene_mesh.face_sizes
    vertices = scene_mesh.corner_vertices
    hidden = np.zeros(len(sizes), dtype=bool)
    for width in np.unique(sizes):
        rows = np.flatnonzero(sizes == width)
        if not len(rows):
            continue
        starts = np.concatenate(([0], np.cumsum(sizes)[:-1]))[rows]
        corners = vertices[starts[:, None] + np.arange(width)]
        ordered = np.sort(corners, axis=1)
        hidden[rows] = np.any(ordered[:, 1:] == ordered[:, :-1], axis=1)
    return ~np.repeat(hidden, sizes), hidden


def build_export_package(mesh, *, title, source_main, source_streaming, file_version, merge_seams=True):
    scene = build_scene(mesh, merge_seams=merge_seams)
    domains = scene.domains
    arrays = {}
    names = joint_names(mesh)
    palette = list(mesh.bone_remap_indices)
    pivots = mesh.profile.vertex_normal_w_semantic is VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    meshes = []
    for number, scene_mesh in enumerate(scene.meshes.values()):
        prefix = f"m{number}."
        vertex_ids = scene_mesh.representative
        visible, hidden = blender_faces(scene_mesh)
        corner_vertices = scene_mesh.corner_vertices[visible]
        corner_ids = scene_mesh.corner_ids[visible]
        arrays[prefix + "co"] = to_blender(gather(domains, vertex_ids, "positions", 3, np.float32))
        arrays[prefix + "corner_vert"] = corner_vertices.astype(np.int32)
        arrays[prefix + "face_size"] = scene_mesh.face_sizes[~hidden].astype(np.int32)
        arrays[prefix + "vid"] = vertex_ids.astype(np.int32)
        arrays[prefix + "cid"] = corner_ids.astype(np.int32)
        if hidden.any():
            arrays[prefix + "hidden_cid"] = scene_mesh.corner_ids[~visible].astype(np.int32)
            arrays[prefix + "hidden_size"] = scene_mesh.face_sizes[hidden].astype(np.int32)
            arrays[prefix + "hidden_face"] = np.flatnonzero(hidden).astype(np.int32)
        entry = {"key": scene_mesh.key, "uvs": [], "colors": False, "weights": [], "shape_keys": [],
                 "pivots": False}
        point_normals = gather(domains, vertex_ids, "normals", 3, np.float32)
        corner_normals = gather(domains, corner_ids, "normals", 3, np.float32)
        if point_normals is not None:
            same = np.array_equal(point_normals[corner_vertices].view(np.uint32),
                                  corner_normals.view(np.uint32))
            entry["normal_domain"] = "POINT" if same else "CORNER"
            arrays[prefix + "normals"] = to_blender(point_normals if same else corner_normals)
        for layer, name in enumerate(UV_PLANES):
            values = gather(domains, corner_ids, name, 2, np.float64)
            if values is not None and len(values) == len(corner_ids):
                arrays[prefix + f"uv{layer}"] = flip_uv(values)
                entry["uvs"].append([f"UV{layer}", f"uv{layer}"])
        colors = gather(domains, vertex_ids, "colors", 4, np.uint8)
        if colors is not None:
            arrays[prefix + "color"] = colors
            entry["colors"] = True
        if pivots:
            values = gather(domains, vertex_ids, "normal_ws", 1, np.uint8)
            if values is not None and values.any():
                arrays[prefix + "pivot"] = values.ravel().astype(np.int32)
                entry["pivots"] = True
        if palette:
            for label, streams, group_prefix in WEIGHT_SETS:
                lanes = vertex_weights(domains, vertex_ids, streams)
                if lanes is None:
                    continue
                vertices, joints, weights = _sparse_groups(lanes, palette)
                arrays[prefix + f"w.{label}.v"] = vertices
                arrays[prefix + f"w.{label}.j"] = joints
                arrays[prefix + f"w.{label}.w"] = weights
                entry["weights"].append([label, group_prefix])
        if scene_mesh.channels:
            vertex_rows = scene_mesh.vertex_of_gpu
            first = np.full(len(scene_mesh.representative), -1, dtype=np.int64)
            first[vertex_rows[::-1]] = np.arange(len(vertex_rows))[::-1]
            positions = arrays[prefix + "co"]
            for key_number, (_body, _position, key_name, delta, _normals) in enumerate(scene_mesh.channels):
                arrays[prefix + f"sk{key_number}"] = positions + to_blender(delta[first])
                entry["shape_keys"].append(key_name)
        meshes.append(entry)
    mesh_numbers = {key: number for number, key in enumerate(scene.meshes)}

    collections, objects = {}, []
    lod_counts = {}
    for item in scene.objects:
        lod_counts[item.layer] = max(lod_counts.get(item.layer, 0), item.lod + 1)
    for item in scene.objects:
        label = {"main": "Main", "shadow": "Shadow", "occluder": "Occluder"}[item.layer]
        collection = f"{title} · {label} LOD{item.lod}" if item.layer != "occluder" else f"{title} · Occluder"
        collections.setdefault(collection, {"name": collection, "layer": item.layer, "lod": item.lod,
                                            "mesh_index": item.mesh_index,
                                            "hidden": item.layer != "main" or item.lod > 0})
        objects.append({"key": item.key, "name": item.name, "mesh": mesh_numbers[item.mesh_key],
                        "collection": collection, "layer": item.layer, "mesh_index": item.mesh_index,
                        "lod": item.lod, "group": item.group_index, "part": item.part_id, "draw": item.draw,
                        "material": item.material_index, "quad": item.is_quad, "alias": item.alias_of})

    skeleton = None
    if mesh.bones:
        world = [matrix_to_blender(matrix) for matrix in mesh.world_matrices]
        heads = np.array([matrix[:3, 3] for matrix in world]) if world else np.zeros((0, 3))
        lengths = _bone_lengths(mesh, heads)
        arrays["bones.matrix"] = np.array(world, dtype=np.float64).reshape(-1, 16)
        arrays["bones.length"] = lengths
        skeleton = {"names": names, "parents": [int(node.parent_index) for node in mesh.bones],
                    "palette": palette}

    manifest = {
        "title": title,
        "game": mesh.profile.game,
        "file_version": int(file_version),
        "merge_seams": bool(merge_seams),
        "materials": list(mesh.material_names),
        "meshes": meshes,
        "objects": objects,
        "collections": list(collections.values()),
        "skeleton": skeleton,
        "has_streaming": source_streaming is not None,
    }
    sources = {"main": source_main, "streaming": source_streaming}
    return manifest, arrays, sources, scene
