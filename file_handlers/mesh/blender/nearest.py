"""Written here to avoid a spatial-index dependency."""

from __future__ import annotations

lazy import numpy as np

LEAF = 32
CHUNK = 8192
BRUTE_FORCE_PAIRS = 1 << 20


def nearest(points, candidates):
    """Among coincident candidates the first answers."""
    return NearestIndex(candidates).query(points)


class NearestIndex:
    def __init__(self, candidates):
        candidates = np.asarray(candidates, np.float64).reshape(-1, 3)
        if not len(candidates):
            raise ValueError("A nearest-point index needs at least one candidate")
        candidates, self.first = np.unique(candidates, axis=0, return_index=True)
        # Centred coordinates keep distances precise far from the origin.
        self.center = candidates.mean(axis=0)
        self.candidates = candidates - self.center
        nodes = {"left": [], "right": [], "leaf": [], "axis": [], "split": []}
        leaves = []
        self._split(np.arange(len(candidates)), nodes, leaves)
        self.left, self.right = np.array(nodes["left"]), np.array(nodes["right"])
        self.leaf_of, self.axis = np.array(nodes["leaf"]), np.array(nodes["axis"])
        self.split = np.array(nodes["split"])
        # Edge padding is harmless: the repeated candidate only ties with itself.
        self.leaves = np.array([np.pad(leaf, (0, LEAF - len(leaf)), mode="edge") for leaf in leaves])
        self.packed = np.ascontiguousarray(self.candidates[self.leaves])
        self.low, self.high = self._boxes()

    def _split(self, indices, nodes, leaves):
        """Children are numbered after parents, as ``_boxes`` needs."""
        node = len(nodes["left"])
        for key, value in (("left", -1), ("right", -1), ("leaf", -1), ("axis", 0), ("split", 0.0)):
            nodes[key].append(value)
        points = self.candidates[indices]
        if len(indices) <= LEAF:
            nodes["leaf"][node] = len(leaves)
            leaves.append(indices)
            return node
        axis = int(np.argmax(np.ptp(points, axis=0)))
        half = len(indices) // 2
        order = np.argpartition(points[:, axis], half)
        nodes["axis"][node], nodes["split"][node] = axis, float(points[order[half], axis])
        nodes["left"][node] = self._split(indices[order[:half]], nodes, leaves)
        nodes["right"][node] = self._split(indices[order[half:]], nodes, leaves)
        return node

    def _boxes(self):
        count = len(self.left)
        low, high = np.zeros((count, 3)), np.zeros((count, 3))
        for node in range(count - 1, -1, -1):
            leaf = self.leaf_of[node]
            if leaf >= 0:
                points = self.candidates[self.leaves[leaf]]
                low[node], high[node] = points.min(axis=0), points.max(axis=0)
            else:
                children = [self.left[node], self.right[node]]
                low[node], high[node] = low[children].min(axis=0), high[children].max(axis=0)
        return low, high

    def query(self, points):
        points = np.asarray(points, np.float64).reshape(-1, 3) - self.center
        if len(points) * len(self.candidates) <= BRUTE_FORCE_PAIRS:
            distance = (self.candidates ** 2).sum(axis=1)[None, :] - 2.0 * (points @ self.candidates.T)
            return self.first[np.argmin(distance, axis=1)]
        result = np.empty(len(points), np.int64)
        for start in range(0, len(points), CHUNK):
            result[start:start + CHUNK] = self._traverse(points[start:start + CHUNK])
        return self.first[result]

    def _home(self, points):
        node = np.zeros(len(points), np.int64)
        inner = np.flatnonzero(self.leaf_of[node] < 0)
        while len(inner):
            at = node[inner]
            upper = points[inner, self.axis[at]] >= self.split[at]
            node[inner] = np.where(upper, self.right[at], self.left[at])
            inner = inner[self.leaf_of[node[inner]] < 0]
        return self.leaf_of[node]

    def _measure(self, points, query, leaves):
        offset = self.packed[leaves] - points[query, None, :]
        distance = np.einsum("klj,klj->kl", offset, offset)
        column = distance.argmin(axis=1)
        rows = np.arange(len(query))
        return distance[rows, column], self.leaves[leaves, column]

    def _traverse(self, points):
        best, found = self._measure(points, np.arange(len(points)), self._home(points))
        query, node = np.arange(len(points)), np.zeros(len(points), np.int64)
        while len(query):
            gap = np.maximum(self.low[node] - points[query], 0) + np.maximum(points[query] - self.high[node], 0)
            keep = (gap * gap).sum(axis=1) < best[query]
            query, node = query[keep], node[keep]
            leaf = self.leaf_of[node] >= 0
            if leaf.any():
                rows = query[leaf]
                distance, member = self._measure(points, rows, self.leaf_of[node[leaf]])
                np.minimum.at(best, rows, distance)
                won = distance == best[rows]
                found[rows[won]] = member[won]
            query, node = query[~leaf], node[~leaf]
            query = np.concatenate((query, query))
            node = np.concatenate((self.left[node], self.right[node]))
        return found


__all__ = ["NearestIndex", "nearest"]
