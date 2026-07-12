from __future__ import annotations

import numpy as np

from .scene_model import SceneDrawBatch, SceneDrawMesh


def _merge_attribute(records, name: str, width: int, dtype) -> np.ndarray | None:
    chunks = []
    missing = []
    for buffer_index, payload, vertex_count in records:
        values = getattr(payload, name)
        if not values:
            missing.append(buffer_index)
            continue
        data = np.asarray(values, dtype=dtype).reshape(-1)
        if data.size != vertex_count * width:
            raise ValueError(f"Malformed {name} in mesh buffer {buffer_index}")
        chunks.append(data.reshape(-1, width))
    if not chunks:
        return None
    if missing:
        raise ValueError(f"Missing {name} in mesh buffers {missing}")
    return np.concatenate(chunks)


def build_mesh_scene(
    mesh,
    *,
    key: str = "mesh",
    color: tuple[float, float, float] | tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0),
    force_solid: bool = False,
    ignore_highlight_filter: bool = False,
    include_vertex_colors: bool = True,
) -> list[SceneDrawMesh]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return []

    payloads = mesh_buffer.buffer_payloads
    submeshes = [
        submesh
        for mesh_data in mesh.meshes
        if mesh_data.lods
        for group in mesh_data.lods[0].mesh_groups
        for submesh in group.submeshes
    ]
    if not payloads or not submeshes:
        return []
    vertex_chunks: list[np.ndarray] = []
    records = []
    payload_base: dict[int, int] = {}
    running_base = 0

    for buffer_index in sorted({submesh.buffer_index for submesh in submeshes}):
        payload = payloads[buffer_index]
        if not payload.positions:
            continue
        positions = np.asarray(payload.positions, dtype=np.float32).reshape(-1)
        if positions.size % 3:
            raise ValueError(f"Malformed positions in mesh buffer {buffer_index}")
        verts = positions.reshape(-1, 3)
        payload_base[buffer_index] = running_base
        running_base += len(verts)
        vertex_chunks.append(verts)
        records.append((buffer_index, payload, len(verts)))

    if not vertex_chunks:
        return []

    vertices = np.concatenate(vertex_chunks)
    normals = _merge_attribute(records, "normals", 3, np.float32)
    colors = _merge_attribute(records, "colors", 4, np.uint8) if include_vertex_colors else None
    if colors is not None:
        colors = colors.astype(np.float32) / 255.0
    uvs = _merge_attribute(records, "uv0", 2, np.float32)

    index_chunks: list[np.ndarray] = []
    batches: list[SceneDrawBatch] = []
    material_names = mesh.material_names

    for submesh in submeshes:
        buffer_index = submesh.buffer_index
        payload = payloads[buffer_index]
        if buffer_index not in payload_base:
            raise ValueError(f"No positions in mesh buffer {buffer_index}")
        face_array = (
            payload.integer_faces
            if payload.integer_faces is not None
            else payload.faces
        )
        start, count = submesh.faces_index_offset, submesh.indices_count
        if not count:
            continue
        end = start + count
        if start < 0 or end > len(face_array) or count % 3:
            raise ValueError(f"Invalid index span [{start}, {end})")
        vertex_offset = submesh.verts_index_offset
        if vertex_offset < 0:
            raise ValueError(f"Negative vertex offset {vertex_offset}")
        local_indices = np.asarray(face_array[start:end], dtype=np.uint64) + vertex_offset
        if np.any(local_indices >= len(payload.positions) // 3):
            raise ValueError(f"Vertex outside mesh buffer {buffer_index}")
        batch_indices = (local_indices + payload_base[buffer_index]).astype(np.uint32)
        index_chunks.append(batch_indices)
        material_index = submesh.material_index
        material_name = (
            material_names[material_index]
            if 0 <= material_index < len(material_names)
            else ""
        )
        batches.append(SceneDrawBatch(indices=batch_indices, material_name=material_name))

    if not index_chunks:
        return []

    return [
        SceneDrawMesh(
            key=key,
            vertices=vertices,
            indices=np.concatenate(index_chunks),
            color=color,
            force_solid=force_solid,
            ignore_highlight_filter=ignore_highlight_filter,
            normals=normals,
            uvs=uvs,
            colors=colors,
            batches=batches,
        )
    ]
