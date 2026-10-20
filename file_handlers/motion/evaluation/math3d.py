from __future__ import annotations

import math
from typing import Sequence

lazy import numpy as np

from utils.native_build import ensure_fastmotion

from .model import TRANSFORM_WIDTH, Quaternion, Transform

fastmotion = ensure_fastmotion()


def normalize_quaternion(value: Sequence[float]) -> Quaternion:
    length = math.sqrt(sum(float(part) * float(part) for part in value))
    if length <= 1e-12:
        raise ValueError("cannot normalize a zero-length quaternion")
    return tuple(float(part) / length for part in value)  # type: ignore[return-value]


def multiply_quaternions(
    left: Sequence[float],
    right: Sequence[float],
) -> Quaternion:
    """Return the normalized Hamilton product ``left * right``."""
    x1, y1, z1, w1 = normalize_quaternion(left)
    x2, y2, z2, w2 = normalize_quaternion(right)
    return normalize_quaternion((
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
    ))


def inverse_quaternion(value: Sequence[float]) -> Quaternion:
    x, y, z, w = normalize_quaternion(value)
    return -x, -y, -z, w


def transform_matrices(local: np.ndarray) -> np.ndarray:
    """``local`` rows as ``Transform.row``; row-vector 4x4 matrices."""
    local = _rows(local)
    return np.frombuffer(fastmotion.matrices(local), dtype=np.float64).reshape(-1, 4, 4)


def compose_world_matrices(
    local: np.ndarray,
    parents: np.ndarray,
    compensating: np.ndarray | None = None,
) -> np.ndarray:
    """A ``compensating`` joint (``via::Joint::getPartSegmentScaleMatrix``) leaves its parent's scale out of its basis
    but not its translation."""
    local = _rows(local)
    count = len(local)
    parents = np.ascontiguousarray(parents, dtype=np.int32)
    compensating = (np.zeros(count, dtype=np.uint8) if compensating is None
                    else np.ascontiguousarray(compensating, dtype=np.uint8))
    if len(parents) != count or len(compensating) != count:
        raise ValueError("local transforms and hierarchy lengths differ")
    return np.frombuffer(fastmotion.compose(local, parents, compensating), dtype=np.float64).reshape(-1, 4, 4)


def _rows(local: np.ndarray) -> np.ndarray:
    local = np.ascontiguousarray(local, dtype=np.float64)
    if local.ndim != 2 or local.shape[1] != TRANSFORM_WIDTH:
        raise ValueError("local transforms must be rows of translation, rotation and scale")
    return local


def decompose_row_srt(matrix: Sequence[float], *, tolerance: float = 1e-5) -> Transform:
    """Decompose a DMC5/System.Numerics row-vector S*R*T matrix."""
    if len(matrix) != 16 or not all(math.isfinite(float(value)) for value in matrix):
        raise ValueError("expected a finite 4x4 matrix")
    if any(abs(float(matrix[index])) > tolerance for index in (3, 7, 11)) or abs(float(matrix[15]) - 1.0) > tolerance:
        raise ValueError("matrix is not affine row-vector SRT")

    rows = [[float(matrix[row * 4 + column]) for column in range(3)] for row in range(3)]
    scale = tuple(math.sqrt(sum(value * value for value in row)) for row in rows)
    if any(value <= tolerance for value in scale):
        raise ValueError("matrix has a degenerate scale")
    rotation_rows = [[rows[row][column] / scale[row] for column in range(3)] for row in range(3)]
    determinant = (
        rotation_rows[0][0] * (rotation_rows[1][1] * rotation_rows[2][2] - rotation_rows[1][2] * rotation_rows[2][1])
        - rotation_rows[0][1] * (rotation_rows[1][0] * rotation_rows[2][2] - rotation_rows[1][2] * rotation_rows[2][0])
        + rotation_rows[0][2] * (rotation_rows[1][0] * rotation_rows[2][1] - rotation_rows[1][1] * rotation_rows[2][0])
    )
    if determinant < 0.0:
        raise ValueError("reflected matrices require an explicit signed-scale policy")

    # Standard matrix-to-quaternion formulas use column-vector matrices.  The
    # row-vector rotation above is their transpose.
    column = [[rotation_rows[column][row] for column in range(3)] for row in range(3)]
    trace = column[0][0] + column[1][1] + column[2][2]
    if trace > 0.0:
        size = math.sqrt(trace + 1.0) * 2.0
        quaternion = (
            (column[2][1] - column[1][2]) / size,
            (column[0][2] - column[2][0]) / size,
            (column[1][0] - column[0][1]) / size,
            0.25 * size,
        )
    elif column[0][0] > column[1][1] and column[0][0] > column[2][2]:
        size = math.sqrt(1.0 + column[0][0] - column[1][1] - column[2][2]) * 2.0
        quaternion = (
            0.25 * size,
            (column[0][1] + column[1][0]) / size,
            (column[0][2] + column[2][0]) / size,
            (column[2][1] - column[1][2]) / size,
        )
    elif column[1][1] > column[2][2]:
        size = math.sqrt(1.0 + column[1][1] - column[0][0] - column[2][2]) * 2.0
        quaternion = (
            (column[0][1] + column[1][0]) / size,
            0.25 * size,
            (column[1][2] + column[2][1]) / size,
            (column[0][2] - column[2][0]) / size,
        )
    else:
        size = math.sqrt(1.0 + column[2][2] - column[0][0] - column[1][1]) * 2.0
        quaternion = (
            (column[0][2] + column[2][0]) / size,
            (column[1][2] + column[2][1]) / size,
            0.25 * size,
            (column[1][0] - column[0][1]) / size,
        )

    return Transform(
        (float(matrix[12]), float(matrix[13]), float(matrix[14])),
        normalize_quaternion(quaternion),
        scale,  # type: ignore[arg-type]
    )
