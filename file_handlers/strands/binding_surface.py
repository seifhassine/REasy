"""See docs/internals/strand-editing.md, Binding surface."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

_LEAF, _CHUNK, _PAIRS = 16, 4096, 1 << 20


@dataclass(slots=True, eq=False)
class BindingSurface:
    positions: np.ndarray
    uvs: np.ndarray | None
    triangles: np.ndarray
    _tree: _Tree | None = None

    def prepare(self) -> BindingSurface:
        if self._tree is None:
            self._tree = _Tree(self.positions[self.triangles])
        return self

    @classmethod
    def of(cls, mesh, lod: int) -> BindingSurface:
        if not mesh.meshes or not 0 <= lod < len(mesh.meshes[0].lods):
            raise ValueError(f"The binding mesh has no LOD {lod}")
        submeshes = [*group.submeshes for group in mesh.meshes[0].lods[lod].mesh_groups]
        buffers = {item.buffer_index for item in submeshes}
        if len(buffers) != 1:
            raise ValueError("A binding mesh LOD draws from one vertex buffer")
        payload = mesh.mesh_buffer.buffer_payloads.get(next(iter(buffers)))
        if payload is None or not len(payload.positions):
            raise ValueError("Binding mesh vertices are unavailable; load its streaming companion")
        faces = np.asarray(payload.integer_faces if payload.integer_faces is not None else payload.faces, np.int64)
        triangles = np.concatenate([
            faces[item.faces_index_offset:item.faces_index_offset + item.indices_count].reshape(-1, 3)
            + item.verts_index_offset for item in submeshes]) if submeshes else np.zeros((0, 3), np.int64)
        positions = np.asarray(payload.positions, np.float32).reshape(-1, 3)
        if len(triangles) == 0 or triangles.max() >= len(positions) or triangles.min() < 0:
            raise ValueError(f"The binding mesh's LOD {lod} has no triangles of its vertices")
        uvs = np.asarray(payload.uv0, np.float32).reshape(-1, 2) if len(payload.uv0) else None
        return cls(positions, uvs, triangles)

    def point(self, triangles: np.ndarray, weights: np.ndarray) -> np.ndarray:
        corners = self.positions[triangles].astype(np.float64)
        v, w = weights[:, :1], weights[:, 1:]
        return corners[:, 0] * (1 - v - w) + corners[:, 1] * v + corners[:, 2] * w

    def uv(self, triangles: np.ndarray, weights: np.ndarray) -> np.ndarray:
        if self.uvs is None:
            raise ValueError("The binding mesh has no UVs")
        uv = self.uvs[triangles]
        v, w = weights[:, :1], weights[:, 1:]
        return uv[:, 0] * (1 - v - w) + uv[:, 1] * v + uv[:, 2] * w

    def nearest(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        points = np.asarray(points, np.float64).reshape(-1, 3)
        found = self.prepare()._tree.nearest(points)
        corners = self.positions[self.triangles[found]].astype(np.float64)
        closest, weights = _closest(points, corners)
        return self.triangles[found], weights, np.linalg.norm(points - closest, axis=1)


def _closest(points: np.ndarray, corners: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Ericson, RTCD 5.1.5."""
    a, b, c = corners[:, 0], corners[:, 1], corners[:, 2]
    ab, ac, ap = b - a, c - a, points - a
    d1, d2 = (ab * ap).sum(1), (ac * ap).sum(1)
    bp = points - b
    d3, d4 = (ab * bp).sum(1), (ac * bp).sum(1)
    cp = points - c
    d5, d6 = (ab * cp).sum(1), (ac * cp).sum(1)
    va, vb, vc = d3 * d6 - d5 * d4, d5 * d2 - d1 * d6, d1 * d4 - d3 * d2
    v, w = np.zeros(len(points)), np.zeros(len(points))
    done = np.zeros(len(points), bool)

    def take(where, v_value, w_value):
        nonlocal done
        where = where & ~done
        v[where], w[where] = v_value[where], w_value[where]
        done = done | where

    zero, one = np.zeros(len(points)), np.ones(len(points))
    with np.errstate(divide="ignore", invalid="ignore"):
        take((d1 <= 0) & (d2 <= 0), zero, zero)
        take((d3 >= 0) & (d4 <= d3), one, zero)
        take((d6 >= 0) & (d5 <= d6), zero, one)
        take((vc <= 0) & (d1 >= 0) & (d3 <= 0), d1 / (d1 - d3), zero)
        take((vb <= 0) & (d2 >= 0) & (d6 <= 0), zero, d2 / (d2 - d6))
        edge = (d4 - d3) + (d5 - d6)
        take((va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0), 1 - (d4 - d3) / edge, (d4 - d3) / edge)
        total = va + vb + vc
        take(np.ones(len(points), bool), vb / total, vc / total)
    bad = ~np.isfinite(v) | ~np.isfinite(w)
    v[bad], w[bad] = 0, 0
    weights = np.column_stack((v, w))
    return a + ab * v[:, None] + ac * w[:, None], weights


class _Tree:
    def __init__(self, corners: np.ndarray):
        count = len(corners)
        self.depth = depth = int(np.ceil(np.log2(count / _LEAF))) if count > _LEAF else 0
        centres, order = corners.mean(1), np.arange(count)
        for level in range(depth):
            starts = (np.arange(2 ** level) * count) >> level
            node = np.repeat(np.arange(2 ** level), np.diff(np.r_[starts, count]))
            at = centres[order]
            span = np.maximum.reduceat(at, starts) - np.minimum.reduceat(at, starts)
            order = order[np.lexsort((at[np.arange(count), span.argmax(1)[node]], node))]
        starts = (np.arange(2 ** depth + 1) * count) >> depth
        sizes = np.diff(starts)
        self.leaves = order[starts[:-1, None] + np.minimum(np.arange(sizes.max()), sizes[:, None] - 1)]
        self.corners = corners[self.leaves]
        self.low, self.high = np.empty((2 ** (depth + 1) - 1, 3)), np.empty((2 ** (depth + 1) - 1, 3))
        self.low[2 ** depth - 1:], self.high[2 ** depth - 1:] = self.corners.min((1, 2)), self.corners.max((1, 2))
        for level in range(depth - 1, -1, -1):
            nodes = np.arange(2 ** level - 1, 2 ** (level + 1) - 1)
            self.low[nodes] = np.minimum(self.low[2 * nodes + 1], self.low[2 * nodes + 2])
            self.high[nodes] = np.maximum(self.high[2 * nodes + 1], self.high[2 * nodes + 2])

    def nearest(self, points: np.ndarray) -> np.ndarray:
        found = np.empty(len(points), np.int64)
        for start in range(0, len(points), _CHUNK):
            found[start:start + _CHUNK] = self._search(points[start:start + _CHUNK])
        return found

    def _search(self, points: np.ndarray) -> np.ndarray:
        best, distance = np.full(len(points), -1, np.int64), np.full(len(points), np.inf)
        first = 2 ** self.depth - 1
        home = np.zeros(len(points), np.int64)
        for _level in range(self.depth):
            left = 2 * home + 1
            home = np.where(self._gap(points, left) <= self._gap(points, left + 1), left, left + 1)
        self._measure(points, np.arange(len(points)), home - first, best, distance)
        queries, nodes = np.arange(len(points)), np.zeros(len(points), np.int64)
        for level in range(self.depth + 1):
            near = self._gap(points[queries], nodes) <= distance[queries] * (1 + 1e-12)
            queries, nodes = queries[near], nodes[near]
            if level == self.depth:
                self._measure(points, queries, nodes - first, best, distance)
            else:
                queries, nodes = np.r_[queries, queries], np.r_[2 * nodes + 1, 2 * nodes + 2]
        return best

    def _gap(self, points: np.ndarray, nodes: np.ndarray) -> np.ndarray:
        gap = np.maximum(np.maximum(self.low[nodes] - points, points - self.high[nodes]), 0)
        return np.einsum("ij,ij->i", gap, gap)

    def _measure(self, points, queries, leaves, best, distance) -> None:
        width = self.leaves.shape[1]
        step = max(_PAIRS // width, 1)
        for start in range(0, len(queries), step):
            q, leaf = queries[start:start + step], leaves[start:start + step]
            at = np.repeat(points[q], width, 0)
            closest, _ = _closest(at, self.corners[leaf].reshape(-1, 3, 3).astype(np.float64))
            found = np.einsum("ij,ij->i", at - closest, at - closest).reshape(-1, width)
            near = found.min(1)
            chosen = np.where(found == near[:, None], self.leaves[leaf], np.iinfo(np.int64).max).min(1)
            order = np.lexsort((chosen, near, q))
            order = order[np.r_[True, q[order][1:] != q[order][:-1]]]
            q, near, chosen = q[order], near[order], chosen[order]
            better = (near < distance[q]) | ((near == distance[q]) & (chosen < best[q]))
            best[q[better]], distance[q[better]] = chosen[better], near[better]


__all__ = ["BindingSurface"]
