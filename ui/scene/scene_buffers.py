from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np

from .scene_model import SceneDrawBatch, SceneDrawMesh


@dataclass(slots=True)
class SceneBufferSet:
    vertices: np.ndarray
    normals: np.ndarray | None
    base_colors: np.ndarray | None
    uvs: np.ndarray | None
    indices: np.ndarray
    batches: list[tuple[str, np.ndarray]]
    triangle_chunks: list[tuple[str, str, np.ndarray]]
    key_spans: dict[str, list[tuple[int, int]]]


def scene_bounds(meshes: Iterable[SceneDrawMesh]) -> tuple[np.ndarray, float]:
    return point_bounds(mesh_bounds_points(meshes))


def mesh_bounds_points(meshes: Iterable[SceneDrawMesh]) -> np.ndarray:
    chunks = []
    local_corners: dict[int, np.ndarray] = {}
    for mesh in meshes:
        if not len(mesh.vertices):
            continue
        cache_key = id(mesh.vertices)
        corners = local_corners.get(cache_key)
        if corners is None:
            vertices = np.asarray(mesh.vertices, dtype=np.float32).reshape(-1, 3)
            mins, maxs = vertices.min(axis=0), vertices.max(axis=0)
            corners = np.array(
                [(x, y, z) for x in (mins[0], maxs[0]) for y in (mins[1], maxs[1]) for z in (mins[2], maxs[2])],
                dtype=np.float32,
            )
            local_corners[cache_key] = corners
        chunks.append(transform_points(corners, mesh.transform_matrix) if mesh.transform_matrix is not None else corners)
    return np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 3), dtype=np.float32)


def point_bounds(points: np.ndarray) -> tuple[np.ndarray, float]:
    points = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    if not len(points):
        return np.zeros(3, dtype=np.float32), 1.0
    mins = points.min(axis=0)
    maxs = points.max(axis=0)
    return (mins + maxs) / 2.0, max(float(np.max(maxs - mins)), 1.0)


def display_colors(
    buffer_set: SceneBufferSet,
    *,
    color_source: str,
    lighting_mode: str,
    ambient: float,
    diffuse: float,
) -> np.ndarray | None:
    return display_vertex_colors(
        buffer_set.vertices,
        buffer_set.normals,
        buffer_set.base_colors if color_source == "vertex" else None,
        lighting_mode=lighting_mode,
        ambient=ambient,
        diffuse=diffuse,
    )


def display_vertex_colors(
    vertices: np.ndarray,
    normals: np.ndarray | None,
    base: np.ndarray | None,
    *,
    lighting_mode: str,
    ambient: float,
    diffuse: float,
) -> np.ndarray | None:
    if base is None:
        if lighting_mode != "software":
            return None
        base = np.ones((len(vertices), 4), dtype=np.float32)
    if lighting_mode != "software":
        return base
    if normals is None or not np.isfinite(normals).all():
        normals = np.tile(np.array([0.0, 0.0, 1.0], dtype=np.float32), (len(vertices), 1))
    light_dir = np.array([0.4, 0.8, 0.4], dtype=np.float32)
    light_dir /= np.linalg.norm(light_dir) or 1.0
    intensity = np.clip((normals @ light_dir) * float(diffuse) + float(ambient), 0.0, 1.0).astype(np.float32)
    colors = base.copy()
    colors[:, :3] *= intensity[:, np.newaxis]
    colors[:, 3] = 1.0
    return colors


def build_scene_buffer_set(
    meshes: Iterable[SceneDrawMesh],
    highlighted_keys: set[str],
    *,
    show_only_highlighted: bool,
    force_solid: bool,
) -> SceneBufferSet | None:
    return _SceneBufferBuilder(highlighted_keys, show_only_highlighted, force_solid).build(meshes)


def scene_index_buffers(buffer_set: SceneBufferSet, hidden_keys: set[str], *, include_lines: bool = True) -> tuple[np.ndarray, list[tuple[str, np.ndarray]], np.ndarray]:
    if not hidden_keys:
        return buffer_set.indices, buffer_set.batches, triangle_line_indices(buffer_set.indices) if include_lines else np.zeros((0,), dtype=np.uint32)
    return _chunk_index_buffers(
        ((material_name, indices) for key, material_name, indices in buffer_set.triangle_chunks if key not in hidden_keys),
        include_lines=include_lines,
    )


def scene_key_index_buffers(buffer_set: SceneBufferSet, keys: set[str], *, include_lines: bool = True) -> tuple[np.ndarray, list[tuple[str, np.ndarray]], np.ndarray]:
    return _chunk_index_buffers(
        ((material_name, indices) for key, material_name, indices in buffer_set.triangle_chunks if key in keys),
        include_lines=include_lines,
    )


def _chunk_index_buffers(chunks, *, include_lines: bool) -> tuple[np.ndarray, list[tuple[str, np.ndarray]], np.ndarray]:
    by_material: dict[str, list[np.ndarray]] = {}
    for material_name, indices in chunks:
        if not len(indices):
            continue
        by_material.setdefault(material_name, []).append(indices)
    if not by_material:
        empty = np.zeros((0,), dtype=np.uint32)
        return empty, [], empty
    indices = np.concatenate([indices for chunks in by_material.values() for indices in chunks]).astype(np.uint32, copy=False)
    batches = [(name, np.concatenate(parts).astype(np.uint32, copy=False)) for name, parts in by_material.items()]
    return indices, batches, triangle_line_indices(indices) if include_lines else np.zeros((0,), dtype=np.uint32)


def transform_points(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    matrix = np.asarray(matrix, dtype=np.float32)
    return (points @ matrix[:3, :3].T + matrix[:3, 3]).astype(np.float32)


def _normalized_normals(normals: np.ndarray) -> np.ndarray:
    lengths = np.linalg.norm(normals, axis=1)
    safe = np.zeros_like(normals, dtype=np.float32)
    np.divide(normals, lengths[:, np.newaxis], out=safe, where=lengths[:, np.newaxis] > 0)
    invalid = ~np.isfinite(safe).all(axis=1)
    if np.any(invalid):
        safe[invalid] = np.array([0.0, 0.0, 1.0], dtype=np.float32)
    return safe


def _computed_normals(vertices: np.ndarray, indices: np.ndarray) -> np.ndarray:
    if len(vertices) == 0 or len(indices) < 3:
        return np.zeros((len(vertices), 3), dtype=np.float32)
    tris = indices[:(len(indices) // 3) * 3].reshape(-1, 3)
    tris = tris[(tris < len(vertices)).all(axis=1)]
    normals = np.zeros_like(vertices, dtype=np.float32)
    if len(tris):
        face_normals = np.cross(vertices[tris[:, 1]] - vertices[tris[:, 0]], vertices[tris[:, 2]] - vertices[tris[:, 0]])
        for column in range(3):
            np.add.at(normals, tris[:, column], face_normals)
    return _normalized_normals(normals)


def triangle_line_indices(indices: np.ndarray) -> np.ndarray:
    usable = (len(indices) // 3) * 3
    if usable == 0:
        return np.zeros((0,), dtype=np.uint32)
    tris = indices[:usable].reshape(-1, 3)
    return np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]], axis=0).astype(np.uint32).reshape(-1)


def transform_normals(normals: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    try:
        normal_matrix = np.linalg.inv(np.asarray(matrix, dtype=np.float32)[:3, :3]).T
    except np.linalg.LinAlgError:
        normal_matrix = np.identity(3, dtype=np.float32)
    return (normals @ normal_matrix.T).astype(np.float32)


class _SceneBufferBuilder:
    def __init__(self, highlighted_keys: set[str], show_only_highlighted: bool, force_solid: bool):
        self.highlighted_keys = highlighted_keys
        self.show_only_highlighted = show_only_highlighted
        self.force_solid = force_solid
        self.vertex_chunks: list[np.ndarray] = []
        self.normal_chunks: list[np.ndarray] = []
        self.color_chunks: list[np.ndarray | None] = []
        self.uv_chunks: list[np.ndarray] = []
        self.index_chunks: list[np.ndarray] = []
        self.draw_batches: dict[str, list[np.ndarray]] = {}
        self.triangle_chunks: list[tuple[str, str, np.ndarray]] = []
        self.key_spans: dict[str, list[tuple[int, int]]] = {}
        self.any_uvs = False
        self.base = self.index_base = 0

    def build(self, meshes: Iterable[SceneDrawMesh]) -> SceneBufferSet | None:
        singles, groups = self._split_meshes(meshes)
        for group in groups.values():
            self._append_group(group) if len(group) > 1 else singles.extend(group)
        for mesh in singles:
            self._append_single(mesh)
        if not self.vertex_chunks or not self.index_chunks:
            return None
        indices = np.concatenate(self.index_chunks).astype(np.uint32, copy=False)
        return SceneBufferSet(
            vertices=np.concatenate(self.vertex_chunks, axis=0).astype(np.float32, copy=False),
            normals=np.concatenate(self.normal_chunks, axis=0).astype(np.float32, copy=False),
            base_colors=self._base_colors(),
            uvs=np.concatenate(self.uv_chunks, axis=0).astype(np.float32, copy=False) if self.any_uvs else None,
            indices=indices,
            batches=[(name, np.concatenate(chunks)) for name, chunks in self.draw_batches.items()],
            triangle_chunks=self.triangle_chunks,
            key_spans=self.key_spans,
        )

    def _split_meshes(self, meshes: Iterable[SceneDrawMesh]) -> tuple[list[SceneDrawMesh], dict[str, list[SceneDrawMesh]]]:
        singles: list[SceneDrawMesh] = []
        groups: dict[str, list[SceneDrawMesh]] = {}
        for mesh in meshes:
            if not self._visible(mesh):
                continue
            key = mesh.geometry_key if mesh.transform_matrix is not None else ""
            groups.setdefault(key, []).append(mesh) if key else singles.append(mesh)
        return singles, groups

    def _visible(self, mesh: SceneDrawMesh) -> bool:
        if bool(mesh.force_solid) != self.force_solid or not len(mesh.vertices):
            return False
        return not (
            self.show_only_highlighted
            and self.highlighted_keys
            and mesh.key not in self.highlighted_keys
            and not mesh.ignore_highlight_filter
        )

    @staticmethod
    def _batches(mesh: SceneDrawMesh) -> list[SceneDrawBatch]:
        return list(mesh.batches) if mesh.batches else [SceneDrawBatch(indices=mesh.indices, material_name=mesh.material_name)]

    def _valid_batches(self, mesh: SceneDrawMesh, vertex_count: int) -> list[tuple[str, np.ndarray]]:
        batches = []
        for batch in self._batches(mesh):
            indices = np.asarray(batch.indices, dtype=np.uint32).reshape(-1)
            triangles = indices[:(len(indices) // 3) * 3].reshape(-1, 3)
            valid = (triangles < vertex_count).all(axis=1) if len(triangles) else []
            if np.any(valid):
                batches.append((batch.material_name, triangles[valid].reshape(-1)))
        return batches

    @staticmethod
    def _source_normals(mesh: SceneDrawMesh, vertex_count: int) -> np.ndarray | None:
        if mesh.normals is None:
            return None
        raw = np.asarray(mesh.normals, dtype=np.float32).reshape(-1)
        return raw.reshape(-1, 3) if raw.size == vertex_count * 3 else None

    def _source_colors(self, mesh: SceneDrawMesh, vertex_count: int) -> np.ndarray | None:
        if mesh.colors is not None and mesh.key not in self.highlighted_keys:
            raw = np.asarray(mesh.colors, dtype=np.float32).reshape(-1)
            if raw.size == vertex_count * 3:
                return np.concatenate([raw.reshape(-1, 3), np.ones((vertex_count, 1), dtype=np.float32)], axis=1)
            if raw.size == vertex_count * 4:
                return raw.reshape(-1, 4)
        color = mesh.color
        rgba = np.ones(4, dtype=np.float32)
        raw = np.asarray(color, dtype=np.float32).reshape(-1)
        rgba[:min(len(raw), 4)] = raw[:4]
        if tuple(float(v) for v in rgba) == (1.0, 1.0, 1.0, 1.0):
            return None
        return np.tile(rgba, (vertex_count, 1))

    @staticmethod
    def _source_uvs(mesh: SceneDrawMesh, vertex_count: int) -> np.ndarray | None:
        if mesh.uvs is None:
            return None
        raw = np.asarray(mesh.uvs, dtype=np.float32).reshape(-1)
        return raw.reshape(-1, 2) if raw.size == vertex_count * 2 else None

    def _append_single(self, mesh: SceneDrawMesh) -> None:
        vertices = np.asarray(mesh.vertices, dtype=np.float32).reshape(-1, 3)
        batches = self._valid_batches(mesh, len(vertices))
        if not batches:
            return
        matrix = mesh.transform_matrix
        if matrix is not None:
            vertices = transform_points(vertices, matrix)
        normals = self._source_normals(mesh, len(vertices))
        if normals is not None:
            normals = _normalized_normals(transform_normals(normals, matrix)) if matrix is not None else _normalized_normals(normals)
        else:
            normals = _computed_normals(vertices, np.concatenate([indices for _, indices in batches]))
        self._append(vertices, normals, self._source_colors(mesh, len(vertices)), self._source_uvs(mesh, len(vertices)), batches, mesh.key)

    def _append_group(self, group: list[SceneDrawMesh]) -> None:
        mesh = group[0]
        source_vertices = np.asarray(mesh.vertices, dtype=np.float32).reshape(-1, 3)
        vertex_count = len(source_vertices)
        batches = self._valid_batches(mesh, vertex_count)
        if not vertex_count or not batches:
            return
        matrices = np.stack(
            [np.asarray(item.transform_matrix, dtype=np.float32) for item in group],
            axis=0,
        )
        hom = np.concatenate([source_vertices, np.ones((vertex_count, 1), dtype=np.float32)], axis=1)
        vertices = (hom[np.newaxis, :, :] @ np.swapaxes(matrices, 1, 2))[:, :, :3].reshape(-1, 3)
        offsets = (np.arange(len(group), dtype=np.uint32) * np.uint32(vertex_count))[:, np.newaxis]
        expanded = [(name, (idx[np.newaxis, :] + offsets).reshape(-1).astype(np.uint32, copy=False)) for name, idx in batches]
        normals = self._expanded_normals(mesh, vertex_count, matrices, vertices, np.concatenate([idx for _, idx in expanded]))
        source_colors = self._source_colors(mesh, vertex_count)
        colors = np.tile(source_colors, (len(group), 1)) if source_colors is not None else None
        uvs = self._source_uvs(mesh, vertex_count)
        shifted = self._append(vertices, normals, colors, np.tile(uvs, (len(group), 1)) if uvs is not None else None, expanded)
        for group_index, item in enumerate(group):
            for (material_name, indices, offset), (_, source_indices) in zip(shifted, batches):
                start = group_index * len(source_indices)
                self.triangle_chunks.append((item.key, material_name, indices[start:start + len(source_indices)]))
                self._add_key_span(item.key, offset + start, len(source_indices))

    def _expanded_normals(
        self,
        mesh: SceneDrawMesh,
        vertex_count: int,
        matrices: np.ndarray,
        vertices: np.ndarray,
        indices: np.ndarray,
    ) -> np.ndarray:
        normals = self._source_normals(mesh, vertex_count)
        if normals is None:
            return _computed_normals(vertices, indices)
        linear = matrices[:, :3, :3]
        valid = np.abs(np.linalg.det(linear)) > 1e-8
        normal_matrices = np.tile(np.identity(3, dtype=np.float32), (len(matrices), 1, 1))
        if np.any(valid):
            normal_matrices[valid] = np.linalg.inv(linear[valid]).transpose(0, 2, 1)
        return _normalized_normals((normals[np.newaxis, :, :] @ np.swapaxes(normal_matrices, 1, 2)).reshape(-1, 3))

    def _append(
        self,
        vertices: np.ndarray,
        normals: np.ndarray,
        colors: np.ndarray | None,
        uvs: np.ndarray | None,
        batches: list[tuple[str, np.ndarray]],
        key: str | None = None,
    ) -> list[tuple[str, np.ndarray, int]]:
        self.vertex_chunks.append(vertices.astype(np.float32, copy=False))
        self.normal_chunks.append(normals.astype(np.float32, copy=False))
        self.color_chunks.append(colors.astype(np.float32, copy=False) if colors is not None else None)
        self.uv_chunks.append(uvs.astype(np.float32, copy=False) if uvs is not None else np.zeros((len(vertices), 2), dtype=np.float32))
        self.any_uvs |= uvs is not None
        shifted_batches = []
        for material_name, indices in batches:
            shifted = indices.astype(np.uint32, copy=False) + np.uint32(self.base)
            offset = self.index_base
            self.index_chunks.append(shifted)
            self.draw_batches.setdefault(material_name, []).append(shifted)
            shifted_batches.append((material_name, shifted, offset))
            if key:
                self.triangle_chunks.append((key, material_name, shifted))
                self._add_key_span(key, offset, len(shifted))
            self.index_base += len(shifted)
        self.base += len(vertices)
        return shifted_batches

    def _add_key_span(self, key: str, offset: int, count: int) -> None:
        if key and count:
            self.key_spans.setdefault(str(key), []).append((int(offset), int(count)))

    def _base_colors(self) -> np.ndarray | None:
        if not any(chunk is not None for chunk in self.color_chunks):
            return None
        chunks = [
            chunk if chunk is not None else np.ones((len(vertices), 4), dtype=np.float32)
            for chunk, vertices in zip(self.color_chunks, self.vertex_chunks)
        ]
        return np.concatenate(chunks, axis=0).astype(np.float32, copy=False)
