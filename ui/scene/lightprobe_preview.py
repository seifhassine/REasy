from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from file_handlers.lightprobe.data import LightProbeData


@dataclass(slots=True)
class SceneLightProbeSet:
    probe_count: int
    probe_positions: np.ndarray
    tetrahedron_count: int
    tetra_probe_indices: np.ndarray
    tetra_neighbors: np.ndarray
    tetra_transforms: np.ndarray
    grid_bias: tuple[float, float, float]
    inv_cell_size: tuple[float, float, float]
    linear_z_stride: int
    linear_y_stride: int
    grid_dimensions: tuple[int, int, int]
    grid_indices: np.ndarray
    terms_rgb: np.ndarray

    @classmethod
    def from_data(cls, data: LightProbeData) -> "SceneLightProbeSet":
        prb = data.prb
        return cls(
            probe_count=prb.probe_count,
            probe_positions=prb.probe_positions,
            tetrahedron_count=prb.tetrahedron_count,
            tetra_probe_indices=prb.tetra_probe_indices,
            tetra_neighbors=prb.tetra_neighbors,
            tetra_transforms=prb.tetra_transforms,
            grid_bias=prb.grid_bias,
            inv_cell_size=prb.inv_cell_size,
            linear_z_stride=prb.linear_z_stride,
            linear_y_stride=prb.linear_y_stride,
            grid_dimensions=prb.grid_dimensions,
            grid_indices=prb.grid_indices,
            terms_rgb=data.lprb.terms_rgb,
        )

    def probe_point_cloud(
        self,
        *,
        max_points: int = 12000,
        normal: tuple[float, float, float] = (0.0, 1.0, 0.0),
        exposure: float = 0.12,
        candidate_indices: np.ndarray | None = None,
        normalize_display: bool = True,
    ) -> tuple[np.ndarray, np.ndarray]:
        if self.probe_count <= 0 or not len(self.probe_positions):
            return np.zeros((0, 3), dtype=np.float32), np.zeros((0, 4), dtype=np.float32)
        source_indices = np.asarray(candidate_indices, dtype=np.int64).reshape(-1) if candidate_indices is not None else np.arange(self.probe_count, dtype=np.int64)
        source_indices = source_indices[(source_indices >= 0) & (source_indices < self.probe_count)]
        if not len(source_indices):
            return np.zeros((0, 3), dtype=np.float32), np.zeros((0, 4), dtype=np.float32)
        count = max(1, min(int(max_points), len(source_indices)))
        indices = source_indices[np.linspace(0, len(source_indices) - 1, count, dtype=np.int64)] if count < len(source_indices) else source_indices
        positions = self.probe_positions[indices].astype(np.float32, copy=False)
        term_indices, term_weights = _directional_term_rows(np.asarray(normal, dtype=np.float32).reshape(1, 3))
        terms = self.terms_rgb[indices][:, term_indices[0]]
        rgb = (terms * term_weights[0, :, np.newaxis]).sum(axis=1)
        colors = np.ones((len(positions), 4), dtype=np.float32)
        colors[:, :3] = _tonemap_rgb_array(rgb, exposure)
        if normalize_display and len(colors):
            luminance = (colors[:, 0] * 0.2126) + (colors[:, 1] * 0.7152) + (colors[:, 2] * 0.0722)
            lit = luminance[luminance > 1e-5]
            if len(lit):
                reference = float(np.percentile(lit, 95.0))
                if 1e-5 < reference < 0.7:
                    colors[:, :3] = np.clip(colors[:, :3] * (0.7 / reference), 0.0, 1.0)
        return positions, colors

    def shade_vertices(
        self,
        vertices: np.ndarray,
        normals: np.ndarray | None,
        *,
        exposure: float = 0.035,
        max_neighbor_steps: int = 96,
        exact_vertex_limit: int = 4096,
        preview_vertex_budget: int = 80000,
    ) -> np.ndarray:
        points = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
        normals_array = _normal_array(points, normals)
        if len(points) > exact_vertex_limit:
            if len(points) > preview_vertex_budget:
                return self._shade_vertices_budgeted(
                    points,
                    normals_array,
                    exposure=exposure,
                    budget=preview_vertex_budget,
                    max_neighbor_steps=max_neighbor_steps,
                )
            return self._shade_vertices_approx(
                points,
                normals_array,
                exposure=exposure,
                max_neighbor_steps=max_neighbor_steps,
            )
        return self._shade_vertices_exact(
            points,
            normals_array,
            exposure=exposure,
            max_neighbor_steps=max_neighbor_steps,
        )

    def _shade_vertices_exact(
        self,
        points: np.ndarray,
        normals_array: np.ndarray,
        *,
        exposure: float,
        max_neighbor_steps: int,
    ) -> np.ndarray:
        colors = np.ones((len(points), 4), dtype=np.float32)
        for index, (point, normal) in enumerate(zip(points, normals_array)):
            rgb = self.sample_surface_rgb(point, normal, max_neighbor_steps=max_neighbor_steps)
            if rgb is None:
                colors[index, :3] = (0.05, 0.05, 0.06)
            else:
                colors[index, :3] = _tonemap_rgb(rgb, exposure)
        return colors

    def _shade_vertices_budgeted(
        self,
        points: np.ndarray,
        normals_array: np.ndarray,
        *,
        exposure: float,
        budget: int,
        max_neighbor_steps: int,
    ) -> np.ndarray:
        sample_count = max(2, min(int(budget), len(points)))
        sample_indices = np.linspace(0, len(points) - 1, sample_count, dtype=np.int64)
        sample_colors = self._shade_vertices_approx(
            points[sample_indices],
            normals_array[sample_indices],
            exposure=exposure,
            max_neighbor_steps=max_neighbor_steps,
        )
        boundaries = np.empty(sample_count + 1, dtype=np.int64)
        boundaries[0] = 0
        boundaries[-1] = len(points)
        boundaries[1:-1] = ((sample_indices[:-1] + sample_indices[1:]) // 2) + 1
        return np.repeat(sample_colors, np.diff(boundaries), axis=0).astype(np.float32, copy=False)

    def _shade_vertices_approx(
        self,
        points: np.ndarray,
        normals_array: np.ndarray,
        *,
        exposure: float,
        max_neighbor_steps: int,
        chunk_size: int = 16384,
    ) -> np.ndarray:
        colors = np.ones((len(points), 4), dtype=np.float32)
        tetra_indices = self._initial_tetra_indices(points)
        invalid = tetra_indices < 0
        if np.any(invalid):
            colors[invalid, :3] = (0.05, 0.05, 0.06)
        valid_indices = np.flatnonzero(~invalid)
        probe_indices_by_tetra = self.tetra_probe_indices.reshape(-1, 4)
        for start in range(0, len(valid_indices), chunk_size):
            rows = valid_indices[start:start + chunk_size]
            chunk_points = points[rows]
            chunk_normals = normals_array[rows]
            chunk_tetra = tetra_indices[rows].astype(np.int64, copy=False)
            chunk_tetra, weights = self._walk_tetra_rows(
                chunk_points,
                chunk_tetra,
                max_neighbor_steps=max_neighbor_steps,
            )
            probe_indices = probe_indices_by_tetra[chunk_tetra]
            blended_terms = (self.terms_rgb[probe_indices] * weights[:, :, np.newaxis, np.newaxis]).sum(axis=1)
            term_indices, term_weights = _directional_term_rows(chunk_normals)
            selected = blended_terms[np.arange(len(rows))[:, np.newaxis], term_indices]
            rgb = (selected * term_weights[:, :, np.newaxis]).sum(axis=1)
            colors[rows, :3] = _tonemap_rgb_array(rgb, exposure)
        return colors

    def _walk_tetra_rows(
        self,
        points: np.ndarray,
        tetra_indices: np.ndarray,
        *,
        max_neighbor_steps: int,
    ) -> tuple[np.ndarray, np.ndarray]:
        points = np.asarray(points, dtype=np.float32).reshape(-1, 3)
        current = np.asarray(tetra_indices, dtype=np.int64).reshape(-1).copy()
        valid = (current >= 0) & (current < self.tetrahedron_count)
        best_tetra = np.where(valid, current, 0).astype(np.int64, copy=False)
        best_weights = np.full((len(points), 4), 0.25, dtype=np.float32)
        best_min_weight = np.full(len(points), -np.inf, dtype=np.float32)
        if not np.any(valid):
            return best_tetra, best_weights

        transforms = self.tetra_transforms.reshape(-1, 12)
        neighbors = self.tetra_neighbors.reshape(-1, 4)
        active = valid.copy()
        for _step in range(max(1, int(max_neighbor_steps))):
            rows = np.flatnonzero(active)
            if not len(rows):
                break
            tetra = current[rows]
            weights = _tetra_weights_batch(transforms[tetra], points[rows])
            min_corner = np.argmin(weights, axis=1).astype(np.int64, copy=False)
            min_weight = weights[np.arange(len(rows)), min_corner]
            improved = min_weight > best_min_weight[rows]
            if np.any(improved):
                improved_rows = rows[improved]
                best_tetra[improved_rows] = tetra[improved]
                best_weights[improved_rows] = weights[improved]
                best_min_weight[improved_rows] = min_weight[improved]

            inside = min_weight >= -0.0001
            if np.any(inside):
                active[rows[inside]] = False
            outside_rows = rows[~inside]
            if not len(outside_rows):
                continue
            outside_tetra = tetra[~inside]
            outside_corner = min_corner[~inside]
            next_tetra = neighbors[outside_tetra, outside_corner].astype(np.int64, copy=False)
            can_step = (next_tetra >= 0) & (next_tetra < self.tetrahedron_count) & (next_tetra != outside_tetra)
            if np.any(can_step):
                current[outside_rows[can_step]] = next_tetra[can_step]
            if np.any(~can_step):
                active[outside_rows[~can_step]] = False
        return best_tetra, _normalized_barycentric_rows(best_weights)

    def _initial_tetra_indices(self, points: np.ndarray) -> np.ndarray:
        dim_x, dim_y, dim_z = self.grid_dimensions
        cells = np.floor(
            points * np.asarray(self.inv_cell_size, dtype=np.float32)
            + np.asarray(self.grid_bias, dtype=np.float32)
        ).astype(np.int64, copy=False)
        valid = (
            (cells[:, 0] >= 0)
            & (cells[:, 1] >= 0)
            & (cells[:, 2] >= 0)
            & (cells[:, 0] < dim_x)
            & (cells[:, 1] < dim_y)
            & (cells[:, 2] < dim_z)
        )
        tetra_indices = np.full(len(points), -1, dtype=np.int64)
        if not np.any(valid):
            return tetra_indices
        valid_rows = np.flatnonzero(valid)
        linear_indices = (
            cells[valid_rows, 0] * self.linear_z_stride
            + cells[valid_rows, 1] * self.linear_y_stride
            + cells[valid_rows, 2]
        )
        in_grid = (linear_indices >= 0) & (linear_indices < len(self.grid_indices))
        if not np.any(in_grid):
            return tetra_indices
        rows = valid_rows[in_grid]
        values = self.grid_indices[linear_indices[in_grid]].astype(np.int64, copy=False)
        valid_values = (values != 0xFFFFFFFF) & (values >= 0) & (values < self.tetrahedron_count)
        tetra_indices[rows[valid_values]] = values[valid_values]
        return tetra_indices

    def sample_surface_rgb(
        self,
        point: np.ndarray,
        normal: np.ndarray,
        *,
        max_neighbor_steps: int = 96,
    ) -> tuple[float, float, float] | None:
        result = self._find_tetra(point, max_neighbor_steps=max_neighbor_steps)
        if result is None:
            return None
        tetra_index, weights = result
        base = int(tetra_index) * 4
        probe_indices = self.tetra_probe_indices[base:base + 4]
        blended_terms = np.tensordot(weights, self.terms_rgb[probe_indices], axes=(0, 0))
        term_indices, term_weights = _directional_term_rows(np.asarray(normal, dtype=np.float32).reshape(1, 3))
        rgb = (blended_terms[term_indices[0]] * term_weights[0, :, np.newaxis]).sum(axis=0)
        return float(max(0.0, rgb[0])), float(max(0.0, rgb[1])), float(max(0.0, rgb[2]))

    def _find_tetra(
        self,
        point: np.ndarray,
        *,
        max_neighbor_steps: int,
    ) -> tuple[int, np.ndarray] | None:
        tetra_index = self._initial_tetra(point)
        if tetra_index is None:
            return None
        best_tetra = tetra_index
        best_weights = np.array((0.25, 0.25, 0.25, 0.25), dtype=np.float32)
        best_min_weight = -float("inf")
        for _step in range(max_neighbor_steps):
            weights = self._tetra_weights(tetra_index, point)
            min_corner = int(np.argmin(weights))
            min_weight = float(weights[min_corner])
            if min_weight > best_min_weight:
                best_tetra = tetra_index
                best_weights = weights
                best_min_weight = min_weight
            if min_weight >= -0.0001:
                return tetra_index, _normalized_barycentric(weights)
            neighbor = int(self.tetra_neighbors[(tetra_index * 4) + min_corner])
            if neighbor < 0 or neighbor >= self.tetrahedron_count or neighbor == tetra_index:
                return best_tetra, _normalized_barycentric(best_weights)
            tetra_index = neighbor
        return best_tetra, _normalized_barycentric(best_weights)

    def _initial_tetra(self, point: np.ndarray) -> int | None:
        dim_x, dim_y, dim_z = self.grid_dimensions
        cell_x = math.floor((float(point[0]) * self.inv_cell_size[0]) + self.grid_bias[0])
        cell_y = math.floor((float(point[1]) * self.inv_cell_size[1]) + self.grid_bias[1])
        cell_z = math.floor((float(point[2]) * self.inv_cell_size[2]) + self.grid_bias[2])
        if cell_x < 0 or cell_y < 0 or cell_z < 0 or cell_x >= dim_x or cell_y >= dim_y or cell_z >= dim_z:
            return None
        linear_index = (cell_x * self.linear_z_stride) + (cell_y * self.linear_y_stride) + cell_z
        if linear_index < 0 or linear_index >= len(self.grid_indices):
            return None
        tetra_index = int(self.grid_indices[linear_index])
        if tetra_index == 0xFFFFFFFF or tetra_index >= self.tetrahedron_count:
            return None
        return tetra_index

    def _tetra_weights(self, tetra_index: int, point: np.ndarray) -> np.ndarray:
        base = tetra_index * 12
        transform = self.tetra_transforms
        dx = float(point[0]) - float(transform[base + 3])
        dy = float(point[1]) - float(transform[base + 7])
        dz = float(point[2]) - float(transform[base + 11])
        b0 = (transform[base + 0] * dx) + (transform[base + 1] * dy) + (transform[base + 2] * dz)
        b1 = (transform[base + 4] * dx) + (transform[base + 5] * dy) + (transform[base + 6] * dz)
        b2 = (transform[base + 8] * dx) + (transform[base + 9] * dy) + (transform[base + 10] * dz)
        return np.asarray((b0, b1, b2, 1.0 - b0 - b1 - b2), dtype=np.float32)


def _normal_array(points: np.ndarray, normals: np.ndarray | None) -> np.ndarray:
    fallback = np.array((0.0, 1.0, 0.0), dtype=np.float32)
    if normals is None:
        return np.tile(fallback, (len(points), 1))
    normals_array = np.asarray(normals, dtype=np.float32).reshape(-1, 3)
    if len(normals_array) != len(points):
        return np.tile(fallback, (len(points), 1))
    return normals_array


def _normalize3_rows(values: np.ndarray) -> np.ndarray:
    rows = np.asarray(values, dtype=np.float32).reshape(-1, 3)
    lengths = np.linalg.norm(rows, axis=1)
    normalized = np.zeros_like(rows, dtype=np.float32)
    np.divide(rows, lengths[:, np.newaxis], out=normalized, where=lengths[:, np.newaxis] > 1e-6)
    invalid = ~np.isfinite(normalized).all(axis=1) | (lengths <= 1e-6)
    if np.any(invalid):
        normalized[invalid] = np.array((0.0, 1.0, 0.0), dtype=np.float32)
    return normalized


def _directional_term_rows(normals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    normal = _normalize3_rows(normals)
    x = normal[:, 0]
    y = normal[:, 1]
    z = normal[:, 2]

    sx = np.where(x < 0.0, -1.0, 1.0).astype(np.float32)
    sy = np.where(y < 0.0, -1.0, 1.0).astype(np.float32)
    sz = np.where(z < 0.0, -1.0, 1.0).astype(np.float32)
    px = (sx > 0.0).astype(np.float32)
    py = (sy > 0.0).astype(np.float32)
    pz = (sz > 0.0).astype(np.float32)

    sx_mid = sx * 0.525731086730957
    sy_mid = sy * 0.525731086730957
    sz_mid = sz * 0.525731086730957
    sx_edge = sx * 0.850651
    sy_edge = sy * 0.850651
    sz_edge = sz * 0.850651

    abs_normal = np.abs(normal)
    use_x_y = ((abs_normal * np.array((1.0, 0.381966, -0.618034), dtype=np.float32)).sum(axis=1) > 0.0)
    use_y_z = ((abs_normal * np.array((-0.618034, 1.0, 0.381966), dtype=np.float32)).sum(axis=1) > 0.0)
    use_z_x = ((abs_normal * np.array((0.381966, -0.618034, 1.0), dtype=np.float32)).sum(axis=1) > 0.0)

    v0 = np.stack(
        (
            np.where(use_x_y, sx_edge, -sx_mid),
            np.where(use_x_y, sy_mid, 0.0),
            np.where(use_x_y, 0.0, sz_edge),
        ),
        axis=1,
    ).astype(np.float32)
    v1 = np.stack(
        (
            np.where(use_y_z, 0.0, sx_edge),
            np.where(use_y_z, sy_edge, -sy_mid),
            np.where(use_y_z, sz_mid, 0.0),
        ),
        axis=1,
    ).astype(np.float32)
    v2 = np.stack(
        (
            np.where(use_z_x, sx_mid, 0.0),
            np.where(use_z_x, 0.0, sy_edge),
            np.where(use_z_x, sz_edge, -sz_mid),
        ),
        axis=1,
    ).astype(np.float32)

    term0 = np.where(use_x_y, ((1.0 - py) * 2.0) + (1.0 - px), px + 8.0 + ((1.0 - pz) * 2.0))
    term1 = np.where(use_y_z, 5.0 - py + ((1.0 - pz) * 2.0), (py * 2.0) + (1.0 - px))
    term2 = np.where(use_z_x, 9.0 - px + ((1.0 - pz) * 2.0), 5.0 - py + (pz * 2.0))
    term_indices = np.stack((term0, term1, term2), axis=1).astype(np.int64, copy=False)
    term_indices = np.clip(term_indices, 0, 11)

    axis1 = v1 - ((v2 + v0) * 0.5)
    axis2 = v2 - ((v1 + v0) * 0.5)
    numerator1 = ((normal - v0) * axis1).sum(axis=1)
    denominator1 = ((v1 - v0) * axis1).sum(axis=1)
    numerator2 = ((normal - v0) * axis2).sum(axis=1)
    denominator2 = ((v2 - v0) * axis2).sum(axis=1)
    weight1 = np.divide(
        numerator1,
        denominator1,
        out=np.zeros_like(numerator1, dtype=np.float32),
        where=np.abs(denominator1) > 1e-8,
    )
    weight2 = np.divide(
        numerator2,
        denominator2,
        out=np.zeros_like(numerator2, dtype=np.float32),
        where=np.abs(denominator2) > 1e-8,
    )
    weight1 = np.clip(weight1, 0.0, 1.0).astype(np.float32, copy=False)
    weight2 = np.clip(weight2, 0.0, 1.0).astype(np.float32, copy=False)
    weight0 = np.clip(1.0 - weight1 - weight2, 0.0, 1.0).astype(np.float32, copy=False)
    weights = np.stack((weight0, weight1, weight2), axis=1).astype(np.float32, copy=False)
    fallback = weights.sum(axis=1) <= 1e-6
    if np.any(fallback):
        weights[fallback] = np.array((1.0, 0.0, 0.0), dtype=np.float32)
    return term_indices, weights.astype(np.float32, copy=False)


def _normalized_barycentric(weights: np.ndarray) -> np.ndarray:
    clamped = np.maximum(weights, 0.0)
    total = float(clamped.sum())
    if total > 1e-6:
        return (clamped / total).astype(np.float32)
    total = float(weights.sum())
    if abs(total) > 1e-6:
        return (weights / total).astype(np.float32)
    return np.array((0.25, 0.25, 0.25, 0.25), dtype=np.float32)


def _normalized_barycentric_rows(weights: np.ndarray) -> np.ndarray:
    clamped = np.maximum(weights, 0.0)
    totals = clamped.sum(axis=1)
    normalized = np.divide(
        clamped,
        totals[:, np.newaxis],
        out=np.zeros_like(clamped, dtype=np.float32),
        where=totals[:, np.newaxis] > 1e-6,
    )
    fallback = totals <= 1e-6
    if np.any(fallback):
        raw_totals = weights[fallback].sum(axis=1)
        normalized[fallback] = np.divide(
            weights[fallback],
            raw_totals[:, np.newaxis],
            out=np.full((int(np.count_nonzero(fallback)), 4), 0.25, dtype=np.float32),
            where=np.abs(raw_totals[:, np.newaxis]) > 1e-6,
        )
    return normalized.astype(np.float32, copy=False)


def _tetra_weights_batch(transforms: np.ndarray, points: np.ndarray) -> np.ndarray:
    delta = points - transforms[:, (3, 7, 11)]
    b0 = (transforms[:, 0:3] * delta).sum(axis=1)
    b1 = (transforms[:, 4:7] * delta).sum(axis=1)
    b2 = (transforms[:, 8:11] * delta).sum(axis=1)
    return np.stack((b0, b1, b2, 1.0 - b0 - b1 - b2), axis=1).astype(np.float32, copy=False)


def _tonemap_rgb(rgb: tuple[float, float, float], exposure: float) -> np.ndarray:
    values = np.maximum(np.asarray(rgb, dtype=np.float32), 0.0)
    mapped = 1.0 - np.exp(-values * float(exposure))
    mapped = np.clip(mapped, 0.0, 1.0) ** (1.0 / 2.2)
    return mapped.astype(np.float32)


def _tonemap_rgb_array(rgb: np.ndarray, exposure: float) -> np.ndarray:
    values = np.maximum(np.asarray(rgb, dtype=np.float32), 0.0)
    mapped = 1.0 - np.exp(-values * float(exposure))
    mapped = np.clip(mapped, 0.0, 1.0) ** (1.0 / 2.2)
    return mapped.astype(np.float32, copy=False)
