from __future__ import annotations

import math
from typing import Sequence

import numpy as np

from .model import Matrix4, Quaternion, Transform


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


def multiply_matrices(left: Sequence[float], right: Sequence[float]) -> Matrix4:
    if len(left) != 16 or len(right) != 16:
        raise ValueError("matrix multiplication requires two 4x4 matrices")
    return tuple(
        (
            np.asarray(left, dtype=np.float64).reshape(4, 4)
            @ np.asarray(right, dtype=np.float64).reshape(4, 4)
        ).ravel().tolist()
    )


def transform_matrix(transform: Transform) -> Matrix4:
    x, y, z, w = normalize_quaternion(transform.rotation)
    sx, sy, sz = transform.scale
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    xw, yw, zw = x * w, y * w, z * w
    tx, ty, tz = transform.translation
    return (
        (1.0 - 2.0 * (yy + zz)) * sx,
        2.0 * (xy + zw) * sx,
        2.0 * (xz - yw) * sx,
        0.0,
        2.0 * (xy - zw) * sy,
        (1.0 - 2.0 * (xx + zz)) * sy,
        2.0 * (yz + xw) * sy,
        0.0,
        2.0 * (xz + yw) * sz,
        2.0 * (yz - xw) * sz,
        (1.0 - 2.0 * (xx + yy)) * sz,
        0.0,
        tx,
        ty,
        tz,
        1.0,
    )


def compose_world_matrices(
    local_transforms: Sequence[Transform], parent_indices: Sequence[int | None]
) -> tuple[Matrix4, ...]:
    if len(local_transforms) != len(parent_indices):
        raise ValueError("local transforms and hierarchy lengths differ")
    count = len(local_transforms)
    if count == 0:
        return ()
    parents = np.fromiter(
        (-1 if index is None else index for index in parent_indices),
        dtype=np.int64,
        count=count,
    )
    if np.any(parents < -1) or np.any(parents >= count):
        raise ValueError("hierarchy contains an invalid parent index")

    translation = np.asarray(
        [value.translation for value in local_transforms], dtype=np.float64
    )
    rotation = np.asarray(
        [value.rotation for value in local_transforms], dtype=np.float64
    )
    scale = np.asarray(
        [value.scale for value in local_transforms], dtype=np.float64
    )
    lengths = np.linalg.norm(rotation, axis=1)
    if np.any(~np.isfinite(lengths)) or np.any(lengths <= 1e-12):
        raise ValueError("cannot normalize a zero-length or nonfinite quaternion")
    x, y, z, w = (rotation / lengths[:, None]).T
    sx, sy, sz = scale.T
    xx, yy, zz = x * x, y * y, z * z
    xy, xz, yz = x * y, x * z, y * z
    xw, yw, zw = x * w, y * w, z * w

    local = np.zeros((count, 4, 4), dtype=np.float64)
    local[:, 0, 0] = (1.0 - 2.0 * (yy + zz)) * sx
    local[:, 0, 1] = 2.0 * (xy + zw) * sx
    local[:, 0, 2] = 2.0 * (xz - yw) * sx
    local[:, 1, 0] = 2.0 * (xy - zw) * sy
    local[:, 1, 1] = (1.0 - 2.0 * (xx + zz)) * sy
    local[:, 1, 2] = 2.0 * (yz + xw) * sy
    local[:, 2, 0] = 2.0 * (xz + yw) * sz
    local[:, 2, 1] = 2.0 * (yz - xw) * sz
    local[:, 2, 2] = (1.0 - 2.0 * (xx + yy)) * sz
    local[:, 3, :3] = translation
    local[:, 3, 3] = 1.0

    world = np.empty_like(local)
    pending = np.ones(count, dtype=bool)
    while np.any(pending):
        indices = np.flatnonzero(pending)
        parent = parents[indices]
        ready = parent < 0
        children = ~ready
        ready[children] = ~pending[parent[children]]
        if not np.any(ready):
            raise ValueError("hierarchy contains a cycle")
        indices = indices[ready]
        parent = parents[indices]
        roots = parent < 0
        world[indices[roots]] = local[indices[roots]]
        world[indices[~roots]] = (
            local[indices[~roots]] @ world[parent[~roots]]
        )
        pending[indices] = False
    return tuple(map(tuple, world.reshape(count, 16).tolist()))


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
