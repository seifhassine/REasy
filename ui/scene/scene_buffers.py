from __future__ import annotations

from typing import Iterable

import numpy as np

lazy from .scene_model import SceneDrawMesh

# Towards the "software" light, in the world.
SOFTWARE_LIGHT_DIRECTION = (0.4, 0.8, 0.4)
# Towards the viewports' fixed light, in the view.
VIEW_LIGHT_DIRECTION = (0.5, 1.0, 1.0)


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
            mins, maxs = mesh.bounds_minimum, mesh.bounds_maximum
            if mins is not None and maxs is not None:
                mins = np.asarray(mins, dtype=np.float32).reshape(3)
                maxs = np.asarray(maxs, dtype=np.float32).reshape(3)
                if not (np.isfinite(mins).all() and np.isfinite(maxs).all()) or np.any(maxs < mins):
                    raise ValueError(f"mesh {mesh.key!r} has invalid declared bounds")
            if mins is None or maxs is None or np.array_equal(mins, maxs):
                vertices = np.asarray(mesh.vertices, dtype=np.float32).reshape(-1, 3)
                vertices = vertices[np.isfinite(vertices).all(axis=1)]
                if not len(vertices):
                    continue
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


def computed_normals(vertices: np.ndarray, indices: np.ndarray) -> np.ndarray:
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
