from __future__ import annotations

import numpy as np

from .scene_model import SceneDrawBatch, SceneDrawMesh


def build_mesh_scene(
    mesh,
    *,
    key: str = "mesh",
    color: tuple[float, float, float] | tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
    force_solid: bool = False,
    ignore_highlight_filter: bool = False,
) -> list[SceneDrawMesh]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return []

    payloads = getattr(mesh_buffer, "buffer_payloads", {}) or {0: mesh_buffer}
    vertex_chunks: list[np.ndarray] = []
    normal_chunks: list[np.ndarray] = []
    color_chunks: list[np.ndarray] = []
    uv_chunks: list[np.ndarray] = []
    payload_base: dict[int, int] = {}
    running_base = 0

    for buffer_index in sorted(payloads.keys()):
        payload = payloads[buffer_index]
        if not getattr(payload, "positions", None):
            continue
        verts = np.asarray(payload.positions, dtype=np.float32).reshape(-1, 3)
        if len(verts) == 0:
            continue

        payload_base[int(buffer_index)] = running_base
        running_base += len(verts)
        vertex_chunks.append(verts)

        normals = getattr(payload, "normals", None)
        if normals is not None and len(normals):
            raw_normals = np.asarray(normals, dtype=np.float32).reshape(-1)
            if raw_normals.size == len(verts) * 3:
                normal_chunks.append(raw_normals.reshape(-1, 3))
            else:
                normal_chunks.append(np.zeros((len(verts), 3), dtype=np.float32))
        else:
            normal_chunks.append(np.zeros((len(verts), 3), dtype=np.float32))

        colors = getattr(payload, "colors", None)
        if colors is not None and len(colors):
            raw_colors = np.asarray(colors, dtype=np.uint8).reshape(-1)
            if raw_colors.size == len(verts) * 4:
                color_chunks.append(raw_colors.reshape(-1, 4).astype(np.float32) / 255.0)
            else:
                color_chunks.append(np.ones((len(verts), 4), dtype=np.float32))
        else:
            color_chunks.append(np.ones((len(verts), 4), dtype=np.float32))

        uv0 = getattr(payload, "uv0", None)
        if uv0 is not None and len(uv0):
            raw_uvs = np.asarray(uv0, dtype=np.float32).reshape(-1)
            if raw_uvs.size == len(verts) * 2:
                uv_chunks.append(1.0 - raw_uvs.reshape(-1, 2))
            else:
                uv_chunks.append(np.zeros((len(verts), 2), dtype=np.float32))
        else:
            uv_chunks.append(np.zeros((len(verts), 2), dtype=np.float32))

    if not vertex_chunks:
        return []

    vertices = np.concatenate(vertex_chunks, axis=0)
    normals = np.concatenate(normal_chunks, axis=0) if normal_chunks else None
    colors = np.concatenate(color_chunks, axis=0) if color_chunks else None
    uvs = np.concatenate(uv_chunks, axis=0) if uv_chunks else None

    index_chunks: list[np.ndarray] = []
    batches: list[SceneDrawBatch] = []
    material_names = list(getattr(mesh, "material_names", []) or [])

    if getattr(mesh, "meshes", None):
        for mesh_data in mesh.meshes:
            if not getattr(mesh_data, "lods", None):
                continue
            lod0 = mesh_data.lods[0]
            for mesh_group in getattr(lod0, "parts", []) or getattr(lod0, "mesh_groups", []):
                for submesh in getattr(mesh_group, "submeshes", []):
                    buffer_index = int(getattr(submesh, "buffer_index", 0))
                    payload = payloads.get(buffer_index, payloads.get(0))
                    if payload is None:
                        continue
                    face_array = payload.integer_faces if getattr(payload, "integer_faces", None) is not None else payload.faces
                    start = int(getattr(submesh, "faces_index_offset", 0))
                    end = start + int(getattr(submesh, "indices_count", 0))
                    if end <= start:
                        continue
                    base = payload_base.get(buffer_index, 0) + int(getattr(submesh, "verts_index_offset", 0))
                    batch_indices = np.asarray(face_array[start:end], dtype=np.uint32) + np.uint32(base)
                    usable = (batch_indices.size // 3) * 3
                    if usable < 3:
                        continue
                    triangles = batch_indices[:usable].reshape(-1, 3)
                    valid = (triangles < running_base).all(axis=1)
                    if not np.any(valid):
                        continue
                    batch_indices = triangles[valid].reshape(-1)
                    index_chunks.append(batch_indices)
                    material_name = ""
                    material_index = int(getattr(submesh, "material_index", -1))
                    if 0 <= material_index < len(material_names):
                        material_name = material_names[material_index]
                    batches.append(SceneDrawBatch(indices=batch_indices, material_name=material_name))

    if not index_chunks:
        payload0 = payloads.get(0)
        if payload0 is not None:
            face_array = payload0.integer_faces if getattr(payload0, "integer_faces", None) is not None else payload0.faces
            fallback_indices = np.asarray(face_array, dtype=np.uint32)
            if fallback_indices.size:
                index_chunks.append(fallback_indices)
                batches.append(SceneDrawBatch(indices=fallback_indices))

    if not index_chunks:
        return []

    return [
        SceneDrawMesh(
            key=key,
            vertices=vertices,
            indices=np.concatenate(index_chunks).astype(np.uint32, copy=False),
            color=color,
            force_solid=force_solid,
            ignore_highlight_filter=ignore_highlight_filter,
            normals=normals,
            uvs=uvs,
            colors=colors,
            batches=batches,
        )
    ]
