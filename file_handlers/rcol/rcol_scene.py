from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from ui.scene.scene_preview import SceneDrawMesh
from .shape_types import AABB, Area, Capsule, Cylinder, OBB, Sphere, Triangle


@dataclass(slots=True)
class SceneAttachment:
    mesh: SceneDrawMesh | None = None
    joint_transforms: dict[str, np.ndarray] | None = None


def _color_for_shape(group_index: int, shape_index: int, mirror: bool) -> tuple[float, float, float]:
    regular_palette = [
        (0.22, 0.72, 1.00),  # azure
        (0.34, 0.96, 0.72),  # mint
        (0.78, 0.55, 1.00),  # violet
        (0.35, 0.86, 0.96),  # cyan
        (1.00, 0.56, 0.84),  # pink
        (0.45, 0.72, 1.00),  # steel blue
    ]
    mirror_palette = [
        (0.58, 0.60, 1.00),  # soft indigo
        (0.55, 0.86, 0.95),  # aqua
        (0.94, 0.62, 0.90),  # orchid
        (0.65, 0.78, 1.00),  # periwinkle
    ]
    palette = mirror_palette if mirror else regular_palette
    return palette[(group_index + shape_index) % len(palette)]


def _sphere(center: np.ndarray, radius: float, lat_steps: int = 10, lon_steps: int = 14) -> tuple[np.ndarray, np.ndarray]:
    vertices, indices = [], []
    for i in range(lat_steps + 1):
        theta = math.pi * i / lat_steps
        for j in range(lon_steps + 1):
            phi = 2.0 * math.pi * j / lon_steps
            p = np.array([math.cos(phi) * math.sin(theta), math.cos(theta), math.sin(phi) * math.sin(theta)], dtype=np.float32)
            vertices.append(center + p * radius)
    ring = lon_steps + 1
    for i in range(lat_steps):
        for j in range(lon_steps):
            a = i * ring + j
            b = a + ring
            indices.extend([a, b, a + 1, b, b + 1, a + 1])
    return np.asarray(vertices, dtype=np.float32), np.asarray(indices, dtype=np.uint32)


def _capsule(start: np.ndarray, end: np.ndarray, radius: float, steps: int = 12):
    axis = end - start
    length = np.linalg.norm(axis)
    if length < 1e-6:
        return _sphere(start, radius)

    axis_n = axis / length
    tangent = np.array([0.0, 1.0, 0.0], dtype=np.float32)
    if abs(np.dot(axis_n, tangent)) > 0.95:
        tangent = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    side = np.cross(axis_n, tangent)
    side /= np.linalg.norm(side)
    forward = np.cross(side, axis_n)

    vertices, indices = [], []
    for t in (0.0, 1.0):
        center = start + axis * t
        for i in range(steps):
            angle = (i / steps) * (2.0 * math.pi)
            ring = math.cos(angle) * side + math.sin(angle) * forward
            vertices.append(center + ring * radius)
    for i in range(steps):
        n = (i + 1) % steps
        indices.extend([i, i + steps, n, i + steps, n + steps, n])

    h0_v, h0_i = _sphere(start, radius, 8, steps)
    h1_v, h1_i = _sphere(end, radius, 8, steps)
    base = len(vertices)
    vertices.extend(h0_v.tolist())
    indices.extend((h0_i + base).tolist())
    base = len(vertices)
    vertices.extend(h1_v.tolist())
    indices.extend((h1_i + base).tolist())
    return np.asarray(vertices, dtype=np.float32), np.asarray(indices, dtype=np.uint32)


def _box(vmin: np.ndarray, vmax: np.ndarray):
    verts = np.array([
        [vmin[0], vmin[1], vmin[2]], [vmax[0], vmin[1], vmin[2]], [vmax[0], vmax[1], vmin[2]], [vmin[0], vmax[1], vmin[2]],
        [vmin[0], vmin[1], vmax[2]], [vmax[0], vmin[1], vmax[2]], [vmax[0], vmax[1], vmax[2]], [vmin[0], vmax[1], vmax[2]],
    ], dtype=np.float32)
    idx = np.array([
        0, 1, 2, 0, 2, 3, 4, 6, 5, 4, 7, 6,
        0, 4, 5, 0, 5, 1, 1, 5, 6, 1, 6, 2,
        2, 6, 7, 2, 7, 3, 3, 7, 4, 3, 4, 0,
    ], dtype=np.uint32)
    return verts, idx


def _obb(shape: OBB):
    local, idx = _box(-np.asarray(shape.extent, dtype=np.float32), np.asarray(shape.extent, dtype=np.float32))
    mat = np.asarray(shape.matrix, dtype=np.float32)
    if mat.shape == (4, 4):
        hom = np.concatenate([local, np.ones((len(local), 1), dtype=np.float32)], axis=1)
        return (hom @ mat.T)[:, :3].astype(np.float32), idx
    return local, idx


def _area(shape: Area):
    p = np.asarray(shape.points, dtype=np.float32)
    if p.shape != (4, 2):
        p = np.zeros((4, 2), dtype=np.float32)
    bottom, top = float(shape.bottom), float(shape.bottom + shape.height)
    return _box(np.array([p[:, 0].min(), bottom, p[:, 1].min()], dtype=np.float32), np.array([p[:, 0].max(), top, p[:, 1].max()], dtype=np.float32))


def _triangle(shape: Triangle):
    verts = np.asarray(shape.vertices, dtype=np.float32)
    if verts.shape != (3, 3):
        verts = np.zeros((3, 3), dtype=np.float32)
    return verts, np.array([0, 1, 2], dtype=np.uint32)


def _mesh_for_shape(shape_payload):
    if isinstance(shape_payload, Sphere):
        return _sphere(np.asarray(shape_payload.center, dtype=np.float32), float(shape_payload.radius))
    if isinstance(shape_payload, (Capsule, Cylinder)):
        return _capsule(np.asarray(shape_payload.start, dtype=np.float32), np.asarray(shape_payload.end, dtype=np.float32), float(shape_payload.radius))
    if isinstance(shape_payload, AABB):
        return _box(np.asarray(shape_payload.min, dtype=np.float32), np.asarray(shape_payload.max, dtype=np.float32))
    if isinstance(shape_payload, OBB):
        return _obb(shape_payload)
    if isinstance(shape_payload, Area):
        return _area(shape_payload)
    if isinstance(shape_payload, Triangle):
        return _triangle(shape_payload)
    return None


def _as_transform_matrix(raw: np.ndarray | None) -> np.ndarray | None:
    if raw is None:
        return None
    mat = np.asarray(raw, dtype=np.float32)
    if mat.shape != (4, 4):
        return None
    return mat


def _transform_vertices(vertices: np.ndarray, transform: np.ndarray | None) -> np.ndarray:
    if transform is None or vertices.size == 0:
        return vertices
    hom = np.concatenate([vertices, np.ones((len(vertices), 1), dtype=np.float32)], axis=1)
    return (hom @ transform.T)[:, :3].astype(np.float32)


def build_scene_meshes(rcol, attachments: list[SceneAttachment] | None = None) -> list[SceneDrawMesh]:
    meshes: list[SceneDrawMesh] = []
    active_attachments = attachments or []
    for attachment in active_attachments:
        if attachment.mesh is not None:
            meshes.append(attachment.mesh)

    joint_transforms: dict[str, np.ndarray] = {}
    for attachment in active_attachments:
        for joint_name, matrix in (attachment.joint_transforms or {}).items():
            joint_transforms.setdefault(joint_name, matrix)

    for g_idx, group in enumerate(getattr(rcol, "groups", [])):
        for mirror, shapes in ((False, group.shapes), (True, group.extra_shapes or [])):
            for s_idx, shape in enumerate(shapes):
                mesh = _mesh_for_shape(shape.shape)
                if mesh is None:
                    continue
                vertices, indices = mesh

                joint_name = str(getattr(shape.info, "primary_joint_name_str", "") or "")
                joint_transform = _as_transform_matrix(joint_transforms.get(joint_name)) if joint_name else None
                if joint_transform is not None:
                    vertices = _transform_vertices(vertices, joint_transform)

                meshes.append(
                    SceneDrawMesh(
                        key=f"g{g_idx}:{'m' if mirror else 'r'}:{s_idx}",
                        vertices=vertices,
                        indices=indices,
                        color=_color_for_shape(g_idx, s_idx, mirror),
                    )
                )
    return meshes
