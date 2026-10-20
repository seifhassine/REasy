"""See docs/internals/collision-shapes.md."""
from __future__ import annotations

import dataclasses

lazy import numpy as np

lazy from .clsp_file import CollisionShape, ShapeKind

_STEPS, _PLANE = 24, 0.5


def _basis(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    axis = axis / max(float(np.linalg.norm(axis)), 1e-12)
    other = np.array((1.0, 0, 0)) if abs(axis[0]) < 0.9 else np.array((0, 1.0, 0))
    first = np.cross(axis, other)
    first /= np.linalg.norm(first)
    return first, np.cross(axis, first)


def _circle(centre, radius, u, v) -> np.ndarray:
    angles = np.linspace(0, 2 * np.pi, _STEPS + 1)
    points = centre + radius * (np.cos(angles)[:, None] * u + np.sin(angles)[:, None] * v)
    return np.stack((points[:-1], points[1:]), axis=1)


def capsule_ends(shape: CollisionShape) -> tuple[np.ndarray, float, np.ndarray, float]:
    v = np.asarray(shape.values, np.float64)
    if shape.kind == ShapeKind.SPHERE:
        return v[0:3], v[3], v[0:3], v[3]
    if shape.kind == ShapeKind.CAPSULE:
        return v[0:3], v[8], v[4:7], v[8]
    return v[0:3], v[3], v[4:7], v[7]


def box_axes(shape: CollisionShape) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """The extents are half extents."""
    v = np.asarray(shape.values, np.float64)
    matrix = v[:16].reshape(4, 4)
    lengths = np.maximum(np.linalg.norm(matrix[:3, :3], axis=1), 1e-12)
    return matrix[3, :3], matrix[:3, :3] / lengths[:, None], v[16:19] * lengths


def outline(shape: CollisionShape) -> np.ndarray:
    """n x 2 x 3."""
    if shape.kind == ShapeKind.BOX:
        centre, axes, extents = box_axes(shape)
        signs = np.array([(x, y, z) for x in (-1, 1) for y in (-1, 1) for z in (-1, 1)], np.float64)
        corners = centre + (signs * extents) @ axes
        edges = [(a, b) for a in range(8) for b in range(a + 1, 8) if np.abs(signs[a] - signs[b]).sum() == 2]
        return corners[np.array(edges)]
    if shape.kind == ShapeKind.PLANE:
        v = np.asarray(shape.values, np.float64)
        normal = v[0:3] / max(float(np.linalg.norm(v[0:3])), 1e-12)
        centre, (u, w) = -v[3] * normal, _basis(normal)
        square = centre + _PLANE * np.array([u + w, u - w, -u - w, -u + w])
        return np.concatenate((np.stack((square, np.roll(square, -1, 0)), 1), [[centre, centre + normal * 0.1]]))
    start, start_radius, end, end_radius = capsule_ends(shape)
    u, w = _basis(end - start if np.any(end != start) else np.array((0, 1.0, 0)))
    axis = np.cross(u, w)
    lines = [_circle(start, start_radius, u, w), _circle(end, end_radius, u, w)]
    for radius, centre in ((start_radius, start), (end_radius, end)):
        lines += [_circle(centre, radius, u, axis), _circle(centre, radius, w, axis)]
    if shape.kind != ShapeKind.SPHERE:
        lines.append(np.array([[start + side * start_radius, end + side * end_radius]
                               for side in (u, -u, w, -w)]))
    return np.concatenate(lines)


def bounds(shape: CollisionShape) -> tuple[np.ndarray, np.ndarray] | None:
    if shape.kind == ShapeKind.PLANE:
        return None
    if shape.kind == ShapeKind.BOX:
        centre, axes, extents = box_axes(shape)
        half = (np.abs(axes) * extents[:, None]).sum(0)
        return centre - half, centre + half
    start, start_radius, end, end_radius = capsule_ends(shape)
    return np.minimum(start - start_radius, end - end_radius), np.maximum(start + start_radius, end + end_radius)


def depth(points: np.ndarray, shape: CollisionShape) -> tuple[np.ndarray, np.ndarray]:
    """Depth is negative outside."""
    points = np.asarray(points, np.float64)
    if shape.kind == ShapeKind.PLANE:
        v = np.asarray(shape.values, np.float64)
        length = max(float(np.linalg.norm(v[0:3])), 1e-12)
        normal = v[0:3] / length
        return -(points @ normal + v[3] / length), np.broadcast_to(normal, points.shape)
    if shape.kind == ShapeKind.BOX:
        centre, axes, extents = box_axes(shape)
        local = (points - centre) @ axes.T
        room = extents - np.abs(local)
        axis = np.argmin(room, axis=1)
        rows = np.arange(len(points))
        way = axes[axis] * np.where(local[rows, axis] < 0, -1.0, 1.0)[:, None]
        return room[rows, axis], way
    start, start_radius, end, end_radius = capsule_ends(shape)
    span = end - start
    length = float(span @ span)
    share = np.clip((points - start) @ span / length, 0, 1) if length > 0 else np.zeros(len(points))
    away = points - (start + share[:, None] * span)
    distance = np.linalg.norm(away, axis=1)
    way = np.divide(away, distance[:, None], out=np.tile(_basis(span if length else np.ones(3))[0], (len(points), 1)),
                    where=distance[:, None] > 1e-12)
    return start_radius + (end_radius - start_radius) * share - distance, way


def made(kind: int, joint: int, end_joint: int, tag: int, size: float) -> CollisionShape:
    """Values are in the joints' space."""
    values = np.zeros(20, np.float32)
    capsule = kind in (ShapeKind.CAPSULE, ShapeKind.TAPERED_CAPSULE)
    end_joint = end_joint if capsule else joint
    if kind == ShapeKind.SPHERE:
        values[3] = size
    elif capsule:
        values[4] = 2 * size if end_joint == joint else 0
        values[[8] if kind == ShapeKind.CAPSULE else [3, 7]] = size
    elif kind == ShapeKind.BOX:
        values[:16], values[16:19] = np.eye(4).ravel(), size
    elif kind == ShapeKind.PLANE:
        values[1] = 1
    else:
        raise ValueError(f"collision shape kind {kind} is not one of via.character.CollisionShapeType")
    return CollisionShape(int(kind), joint, end_joint, tag, values)


def moved(shape: CollisionShape, first: np.ndarray, second: np.ndarray | None = None) -> CollisionShape:
    """``first`` is in the joint's space."""
    first = np.asarray(first, np.float64)
    second = first if second is None else np.asarray(second, np.float64)
    v = np.asarray(shape.values, np.float64).copy()

    def point(p, matrix):
        return p @ matrix[:3, :3] + matrix[3, :3]
    if shape.kind == ShapeKind.BOX:
        matrix = v[:16].reshape(4, 4) @ first
        lengths = np.linalg.norm(matrix[:3, :3], axis=1)
        v[16:19] *= lengths / np.maximum(np.linalg.norm(v[:12].reshape(3, 4)[:, :3], axis=1), 1e-12)
        axes, _ = np.linalg.qr(matrix[:3, :3].T)
        signs = np.sign(np.diag(axes.T @ matrix[:3, :3].T))
        matrix[:3, :3] = (axes * np.where(signs == 0, 1, signs)).T
        v[:16] = matrix.ravel()
    elif shape.kind == ShapeKind.PLANE:
        normal = v[0:3]
        at = point(-v[3] * normal / max(float(normal @ normal), 1e-12), first)
        normal = normal @ np.linalg.inv(first[:3, :3]).T
        normal /= max(float(np.linalg.norm(normal)), 1e-12)
        v[0:3], v[3] = normal, -(normal @ at)
    else:
        v[0:3] = point(v[0:3], first)
        if shape.kind != ShapeKind.SPHERE:
            v[4:7] = point(v[4:7], second)
    return dataclasses.replace(shape, values=v.astype(np.float32))


__all__ = ["bounds", "depth", "made", "moved", "outline"]
