"""See docs/internals/collision-shapes.md, Placement."""
from __future__ import annotations

import dataclasses
from collections.abc import Callable, Sequence

import numpy as np

lazy from .clsp_file import CollisionShape, ShapeKind

F = np.float32
_UNIT = np.eye(4, dtype=F)


def pairwise(rows: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    return np.array([(r[0] * matrix[0] + r[1] * matrix[1]) + (r[2] * matrix[2] + r[3] * matrix[3]) for r in rows], F)


def point(position: Sequence, matrix: np.ndarray) -> np.ndarray:
    x, y, z = position
    return ((x * matrix[0, :3] + y * matrix[1, :3]) + z * matrix[2, :3]) + matrix[3, :3]


def row_lengths(matrix: np.ndarray) -> np.ndarray:
    return np.array([np.sqrt(((a * a + b * b) + c * c) + d * d) for a, b, c, d in matrix[:3]], F)


def transform_scale(world: np.ndarray) -> np.ndarray:
    return row_lengths(np.asarray(world, F))


def _biggest(scale: np.ndarray) -> F:
    """The engines scale radii by max(x, y); z takes no part."""
    return max(scale[0], scale[1])


def hung_joints(shape: CollisionShape) -> tuple[int, ...]:
    capsule = shape.kind in (ShapeKind.CAPSULE, ShapeKind.TAPERED_CAPSULE)
    return tuple(dict.fromkeys((shape.joint, shape.end_joint))) if capsule else (shape.joint,)


def _matrix(matrix: Callable[[int], np.ndarray], joint: int) -> np.ndarray:
    return np.asarray(matrix(joint), F).reshape(4, 4)


def place(shape: CollisionShape, matrix: Callable[[int], np.ndarray], scale: Sequence[float]) -> CollisionShape:
    """``matrix`` maps a joint hash to that joint's world matrix; ``scale`` is the owning game object's
    ``transform_scale``, not a joint's."""
    v, out = shape.values, np.zeros(20, F)
    if shape.kind == ShapeKind.PLANE:
        joint, n = _matrix(matrix, shape.joint), v[0:3]
        far = point(n * -v[3], joint)
        t = (n[0] * joint[0, :3] + n[1] * joint[1, :3]) + n[2] * joint[2, :3]
        t = t * (F(1) / np.sqrt((t[0] * t[0] + t[1] * t[1]) + t[2] * t[2]))
        out[0:3], out[3] = t, -((t[0] * far[0] + t[1] * far[1]) + t[2] * far[2])
    elif shape.kind == ShapeKind.BOX:
        joint = pairwise(_UNIT, _matrix(matrix, shape.joint))
        out[:16], out[16:19] = pairwise(v[:16].reshape(4, 4), joint).ravel(), v[16:19]
    elif shape.kind in (ShapeKind.SPHERE, ShapeKind.CAPSULE, ShapeKind.TAPERED_CAPSULE):
        size = _biggest(np.asarray(scale, F))
        out[0:3] = point(v[0:3], pairwise(_UNIT, _matrix(matrix, shape.joint)))
        if shape.kind == ShapeKind.SPHERE:
            out[3] = v[3] * size
        else:
            out[4:7] = point(v[4:7], pairwise(_UNIT, _matrix(matrix, shape.end_joint)))
            if shape.kind == ShapeKind.CAPSULE:
                out[8] = v[8] * size
            else:
                out[3], out[7] = size * v[3], size * v[7]
    else:
        raise ValueError(f"collision shape kind {shape.kind} is not one of via.character.CollisionShapeType")
    return dataclasses.replace(shape, values=out)


__all__ = ["hung_joints", "pairwise", "place", "point", "row_lengths", "transform_scale"]
