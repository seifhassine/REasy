"""See docs/internals/collision-shapes.md, Wrappers."""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .clsp_file import ShapeKind
from .placement import pairwise, point, row_lengths

F = np.float32


def _scale(matrix: np.ndarray) -> F:
    a, b, c = (((r[0] * r[0] + r[1] * r[1]) + r[2] * r[2]) + r[3] * r[3] for r in matrix[:3])
    inner = a if a > b else b
    return np.sqrt(c if c > inner else inner)


def placed(kind: ShapeKind, world: Sequence, local: Sequence, matrix: Sequence | None) -> np.ndarray:
    out = np.zeros(20, F)
    m = None if matrix is None else np.asarray(matrix, F).reshape(4, 4)
    if kind == ShapeKind.BOX:
        if m is None:
            out[:19] = np.asarray(world, F)[:19]
        else:
            coord, extent = local
            out[:16] = pairwise(np.asarray(coord, F).reshape(4, 4), m).ravel()
            out[16:19] = np.asarray(extent, F)[:3] * row_lengths(m)
        return out
    if kind not in (ShapeKind.SPHERE, ShapeKind.CAPSULE):
        raise ValueError(f"collision shape kind {kind} has no collision wrapper")
    if m is None:
        *ends, radius = world
    else:
        *ends, radius = [point(np.asarray(end, F)[:3], m) for end in local[:-1]] + [_scale(m) * F(local[-1])]
    out[0:3] = ends[0]
    if kind == ShapeKind.SPHERE:
        out[3] = radius
    else:
        out[4:7], out[8] = ends[1], radius
    return out


__all__ = ["placed"]
