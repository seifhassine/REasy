from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from .scene_model import SceneDrawBatch, SceneDrawMesh


@dataclass(frozen=True, slots=True)
class MeshScenePayload:
    buffer_index: int
    payload: object
    vertex_count: int
    vertex_base: int


def mesh_lod0_submeshes(mesh) -> list[object]:
    return [submesh for _part, submesh in mesh_lod0_parts(mesh)]


def mesh_lod0_parts(mesh) -> list[tuple[int, object]]:
    parts = []
    for mesh_data in mesh.meshes:
        if not mesh_data.lods:
            continue
        for group_index, group in enumerate(mesh_data.lods[0].mesh_groups):
            part_index = int(getattr(group, "group_id", group_index))
            parts.extend((part_index, submesh) for submesh in group.submeshes)
    return parts


def mesh_scene_payloads(
    mesh,
    submeshes: list[object] | None = None,
) -> list[MeshScenePayload]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return []
    submeshes = mesh_lod0_submeshes(mesh) if submeshes is None else submeshes
    vertex_counts: dict[int, int] = {}
    for submesh in submeshes:
        buffer_index = int(submesh.buffer_index)
        payload = mesh_buffer.buffer_payloads.get(buffer_index)
        if payload is None:
            raise ValueError(f"Missing mesh buffer {buffer_index}")
        available = len(payload.positions) // 3
        base = int(submesh.verts_index_offset)
        count = int(getattr(submesh, "vert_count", 0))
        end = base + count if count > 0 else available
        if base < 0 or end < base or end > available:
            raise ValueError(
                f"Invalid LOD0 vertex span [{base}, {end}) "
                f"in mesh buffer {buffer_index}"
            )
        vertex_counts[buffer_index] = max(
            vertex_counts.get(buffer_index, 0),
            end,
        )
    records: list[MeshScenePayload] = []
    vertex_base = 0
    for buffer_index in sorted(vertex_counts):
        if buffer_index not in mesh_buffer.buffer_payloads:
            raise ValueError(f"Missing mesh buffer {buffer_index}")
        payload = mesh_buffer.buffer_payloads[buffer_index]
        positions = np.asarray(payload.positions, dtype=np.float32).reshape(-1)
        if positions.size % 3:
            raise ValueError(f"Malformed positions in mesh buffer {buffer_index}")
        vertex_count = vertex_counts[buffer_index]
        if not vertex_count:
            continue
        records.append(MeshScenePayload(
            buffer_index,
            payload,
            vertex_count,
            vertex_base,
        ))
        vertex_base += vertex_count
    return records


def _merge_attribute(records, name: str, width: int, dtype) -> np.ndarray | None:
    chunks = []
    missing = []
    for buffer_index, payload, vertex_count in records:
        values = getattr(payload, name)
        if not values:
            missing.append(buffer_index)
            continue
        data = np.asarray(values, dtype=dtype).reshape(-1)
        required = vertex_count * width
        if data.size < required:
            raise ValueError(f"Malformed {name} in mesh buffer {buffer_index}")
        chunks.append(data[:required].reshape(-1, width))
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
    material_key: Callable[[str], str] | None = None,
) -> list[SceneDrawMesh]:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return []

    payloads = mesh_buffer.buffer_payloads
    parts = mesh_lod0_parts(mesh)
    submeshes = [submesh for _part, submesh in parts]
    if not payloads or not submeshes:
        return []
    vertex_chunks: list[np.ndarray] = []
    scene_payloads = mesh_scene_payloads(mesh, submeshes)
    records = []
    payload_base = {record.buffer_index: record.vertex_base for record in scene_payloads}

    for record in scene_payloads:
        buffer_index, payload = record.buffer_index, record.payload
        verts = np.asarray(payload.positions, dtype=np.float32).reshape(-1, 3)[
            : record.vertex_count
        ]
        vertex_chunks.append(verts)
        records.append((buffer_index, payload, record.vertex_count))

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

    for part_index, submesh in parts:
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
        if material_key is not None:
            material_name = material_key(material_name)
        batches.append(
            SceneDrawBatch(
                indices=batch_indices,
                material_name=material_name,
                part_index=part_index,
            )
        )

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
