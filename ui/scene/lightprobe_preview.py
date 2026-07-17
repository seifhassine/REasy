from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from file_handlers.lightprobe.data import LightProbeData


# Captured probe shaders through PRB.11 test at most 199 tetrahedra per lookup.
_RUNTIME_MAX_TETRA_STEPS = 199
_TETRA_CORNER_TAGS = np.arange(4, dtype=np.uint32)

_LEGACY_LPRB8_CUBE_TERM_INDICES = np.asarray(
    (
        (0, 2, 8),    # +X
        (9, 4, 8),    # +Z
        (0, 4, 6),    # +Y
        (1, 3, 9),    # -X
        (11, 6, 10),  # -Z
        (2, 5, 7),    # -Y
    ),
    dtype=np.int64,
)
_LEGACY_LPRB8_CUBE_TERM_WEIGHTS = np.asarray(
    (
        (0.4707382, 0.5292618, 0.0),
        (0.4707382, 0.0, 0.5292618),
        (0.0, 0.5292618, 0.5292618),
        (0.4707382, 0.5292618, 0.0),
        (0.4707382, 0.0, 0.5292618),
        (0.0, 0.5292618, 0.5292618),
    ),
    dtype=np.float32,
)


class ProbeShadingCancelled(RuntimeError):
    """Raised when a in-progress probe shading request is superceded"""


@dataclass(slots=True)
class SceneLightProbeSet:
    probe_count: int
    probe_positions: np.ndarray
    tetrahedron_count: int
    tetra_probe_indices: np.ndarray
    tetra_neighbors: np.ndarray
    tetra_transforms: np.ndarray | None
    grid_bias: tuple[float, float, float]
    inv_cell_size: tuple[float, float, float]
    linear_x_stride: int
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
            linear_x_stride=prb.linear_x_stride,
            linear_y_stride=prb.linear_y_stride,
            grid_dimensions=prb.grid_dimensions,
            grid_indices=prb.grid_indices,
            terms_rgb=_scene_directional_terms(data),
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
        rgb = _sample_directional_rgb(
            self.terms_rgb[indices],
            np.asarray(normal, dtype=np.float32).reshape(1, 3),
        )
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
        max_neighbor_steps: int = _RUNTIME_MAX_TETRA_STEPS,
        exact_vertex_limit: int = 4096,
        progress_callback: Callable[[int, int], None] | None = None,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> np.ndarray:
        points = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
        normals_array = _normal_array(points, normals)
        if len(points) > exact_vertex_limit:
            return self._shade_vertices_chunked(
                points,
                normals_array,
                exposure=exposure,
                max_neighbor_steps=max_neighbor_steps,
                progress_callback=progress_callback,
                cancel_requested=cancel_requested,
            )
        return self._shade_vertices_exact(
            points,
            normals_array,
            exposure=exposure,
            max_neighbor_steps=max_neighbor_steps,
            progress_callback=progress_callback,
            cancel_requested=cancel_requested,
        )

    def _shade_vertices_exact(
        self,
        points: np.ndarray,
        normals_array: np.ndarray,
        *,
        exposure: float,
        max_neighbor_steps: int,
        progress_callback: Callable[[int, int], None] | None,
        cancel_requested: Callable[[], bool] | None,
    ) -> np.ndarray:
        colors = np.ones((len(points), 4), dtype=np.float32)
        for index, (point, normal) in enumerate(zip(points, normals_array)):
            if cancel_requested is not None and cancel_requested():
                raise ProbeShadingCancelled()
            rgb = self.sample_surface_rgb(point, normal, max_neighbor_steps=max_neighbor_steps)
            if rgb is None:
                colors[index, :3] = (0.05, 0.05, 0.06)
            else:
                colors[index, :3] = _tonemap_rgb(rgb, exposure)
            completed = index + 1
            if progress_callback is not None and (completed == len(points) or completed % 64 == 0):
                progress_callback(completed, len(points))
        return colors

    def _shade_vertices_chunked(
        self,
        points: np.ndarray,
        normals_array: np.ndarray,
        *,
        exposure: float,
        max_neighbor_steps: int,
        progress_callback: Callable[[int, int], None] | None,
        cancel_requested: Callable[[], bool] | None,
        chunk_size: int = 8192,
    ) -> np.ndarray:
        colors = np.ones((len(points), 4), dtype=np.float32)
        probe_indices_by_tetra = self.tetra_probe_indices.reshape(-1, 4)
        for start in range(0, len(points), chunk_size):
            if cancel_requested is not None and cancel_requested():
                raise ProbeShadingCancelled()
            end = min(start + chunk_size, len(points))
            chunk_points = points[start:end]
            chunk_normals = normals_array[start:end]
            initial_tetra = self._initial_tetra_indices(chunk_points)
            valid_rows = np.flatnonzero(initial_tetra >= 0)
            invalid_rows = np.flatnonzero(initial_tetra < 0)
            if len(invalid_rows):
                colors[start + invalid_rows, :3] = (0.05, 0.05, 0.06)
            if len(valid_rows):
                tetra, weights = self._walk_tetra_rows(
                    chunk_points[valid_rows],
                    initial_tetra[valid_rows],
                    max_neighbor_steps=max_neighbor_steps,
                    cancel_requested=cancel_requested,
                )
                resolved = tetra >= 0
                if np.any(~resolved):
                    colors[start + valid_rows[~resolved], :3] = (0.05, 0.05, 0.06)
                if np.any(resolved):
                    resolved_rows = valid_rows[resolved]
                    probe_indices = probe_indices_by_tetra[tetra[resolved]]
                    blended_terms = (
                        self.terms_rgb[probe_indices]
                        * weights[resolved, :, np.newaxis, np.newaxis]
                    ).sum(axis=1)
                    rgb = _sample_directional_rgb(
                        blended_terms,
                        chunk_normals[resolved_rows],
                    )
                    colors[start + resolved_rows, :3] = _tonemap_rgb_array(rgb, exposure)
            if progress_callback is not None:
                progress_callback(end, len(points))
        return colors

    def _walk_tetra_rows(
        self,
        points: np.ndarray,
        tetra_indices: np.ndarray,
        *,
        max_neighbor_steps: int,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        points = np.asarray(points, dtype=np.float32).reshape(-1, 3)
        current = np.asarray(tetra_indices, dtype=np.int64).reshape(-1).copy()
        if len(current) != len(points):
            raise ValueError("Point and tetrahedron row counts must match")
        valid = (current >= 0) & (current < self.tetrahedron_count)
        current[~valid] = -1
        weights = np.zeros((len(points), 4), dtype=np.float32)
        if not np.any(valid):
            return current, weights

        transforms = (
            None
            if self.tetra_transforms is None
            else self.tetra_transforms.reshape(-1, 12)
        )
        probe_indices = self.tetra_probe_indices.reshape(-1, 4)
        neighbors = self.tetra_neighbors.reshape(-1, 4)
        active = valid.copy()
        for _step in range(max(1, int(max_neighbor_steps))):
            if cancel_requested is not None and cancel_requested():
                raise ProbeShadingCancelled()
            rows = np.flatnonzero(active)
            if not len(rows):
                break
            tetra = current[rows]
            row_weights = (
                _tetra_weights_from_positions(
                    self.probe_positions[probe_indices[tetra]],
                    points[rows],
                )
                if transforms is None
                else _tetra_weights_batch(transforms[tetra], points[rows])
            )
            weights[rows] = row_weights
            min_corner, outside = _tetra_exit_rows(row_weights)
            inside = ~outside
            if np.any(inside):
                active[rows[inside]] = False
            outside_rows = rows[~inside]
            if not len(outside_rows):
                continue
            outside_tetra = tetra[~inside]
            outside_corner = min_corner[~inside]
            next_tetra = neighbors[outside_tetra, outside_corner].astype(np.int64, copy=False)
            can_step = (next_tetra >= 0) & (next_tetra < self.tetrahedron_count)
            if np.any(can_step):
                current[outside_rows[can_step]] = next_tetra[can_step]
            if np.any(~can_step):
                current[outside_rows[~can_step]] = -1
                active[outside_rows[~can_step]] = False
        return current, weights

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
            cells[valid_rows, 0] * self.linear_x_stride
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
        max_neighbor_steps: int = _RUNTIME_MAX_TETRA_STEPS,
    ) -> tuple[float, float, float] | None:
        result = self._find_tetra(point, max_neighbor_steps=max_neighbor_steps)
        if result is None:
            return None
        tetra_index, weights = result
        base = int(tetra_index) * 4
        probe_indices = self.tetra_probe_indices[base:base + 4]
        blended_terms = np.tensordot(weights, self.terms_rgb[probe_indices], axes=(0, 0))
        rgb = _sample_directional_rgb(
            blended_terms[np.newaxis, ...],
            np.asarray(normal, dtype=np.float32).reshape(1, 3),
        )[0]
        return float(max(0.0, rgb[0])), float(max(0.0, rgb[1])), float(max(0.0, rgb[2]))

    def _find_tetra(
        self,
        point: np.ndarray,
        *,
        max_neighbor_steps: int,
    ) -> tuple[int, np.ndarray] | None:
        points = np.asarray(point, dtype=np.float32).reshape(1, 3)
        initial_tetra = self._initial_tetra_indices(points)
        if initial_tetra[0] < 0:
            return None
        tetra, weights = self._walk_tetra_rows(
            points,
            initial_tetra,
            max_neighbor_steps=max_neighbor_steps,
        )
        if tetra[0] < 0:
            return None
        return int(tetra[0]), weights[0]


@dataclass(slots=True)
class SceneLightProbeInstance:
    key: str
    probe_set: SceneLightProbeSet | None
    obbs: list[object]
    priority: int = 0
    intensity: float = 1.0


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


def _sample_directional_rgb(terms_rgb: np.ndarray, normals: np.ndarray) -> np.ndarray:
    terms = np.asarray(terms_rgb, dtype=np.float32)
    if terms.ndim != 3 or terms.shape[2] != 3:
        raise ValueError("Directional lighting terms must have shape (row_count, term_count, 3)")

    normal_rows = np.asarray(normals, dtype=np.float32).reshape(-1, 3)
    if len(normal_rows) == 1 and len(terms) != 1:
        normal_rows = np.broadcast_to(normal_rows, (len(terms), 3))
    elif len(normal_rows) != len(terms):
        raise ValueError("Directional lighting terms and normals must have the same row count")

    term_indices, term_weights = _directional_term_rows(normal_rows, terms.shape[1])
    selected = terms[np.arange(len(terms))[:, np.newaxis], term_indices]
    return (selected * term_weights[:, :, np.newaxis]).sum(axis=1)


def _directional_term_rows(normals: np.ndarray, term_count: int) -> tuple[np.ndarray, np.ndarray]:
    normal = _normalize3_rows(normals)
    if term_count == 6:
        return _ambient_cube_term_rows(normal)
    if term_count == 12:
        return _icosahedral_term_rows(normal)
    raise ValueError(f"Unsupported directional lighting term count: {term_count}")


def _scene_directional_terms(data: LightProbeData) -> np.ndarray:
    terms = data.lprb.terms_rgb
    if data.prb.version == 9 and data.lprb.version == 8:
        return _legacy_lprb8_ambient_cube_terms(terms)
    return terms


def _legacy_lprb8_ambient_cube_terms(terms: np.ndarray) -> np.ndarray:
    """Apply the PRB.9/LPRB.8 runtime's 12-to-6-term injection pass."""
    terms = np.asarray(terms, dtype=np.float32)
    if terms.ndim != 3 or terms.shape[1:] != (12, 3):
        raise ValueError("LPRB.8 lighting terms must have shape (probe_count, 12, 3)")
    selected = terms[:, _LEGACY_LPRB8_CUBE_TERM_INDICES]
    return (
        selected * _LEGACY_LPRB8_CUBE_TERM_WEIGHTS[np.newaxis, :, :, np.newaxis]
    ).sum(axis=2, dtype=np.float32)


def _ambient_cube_term_rows(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    # LPRB.3 stores the positive X/Z/Y terms first, followed by negative X/Z/Y.
    axis_values = normal[:, (0, 2, 1)]
    positive_terms = np.array((0, 1, 2), dtype=np.int64)
    negative_terms = np.array((3, 4, 5), dtype=np.int64)
    term_indices = np.where(axis_values < 0.0, negative_terms, positive_terms)
    return term_indices, np.square(axis_values).astype(np.float32, copy=False)


def _icosahedral_term_rows(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
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


def _tetra_weights_batch(transforms: np.ndarray, points: np.ndarray) -> np.ndarray:
    delta = points - transforms[:, (3, 7, 11)]
    b0 = (transforms[:, 0:3] * delta).sum(axis=1)
    b1 = (transforms[:, 4:7] * delta).sum(axis=1)
    b2 = (transforms[:, 8:11] * delta).sum(axis=1)
    return np.stack((b0, b1, b2, 1.0 - b0 - b1 - b2), axis=1).astype(np.float32, copy=False)


def _tetra_weights_from_positions(
    tetra_positions: np.ndarray,
    points: np.ndarray,
) -> np.ndarray:
    """Rebuild PRB.11 barycentric coordinates as its captured shader does."""
    positions = np.asarray(tetra_positions, dtype=np.float32).reshape(-1, 4, 3)
    points = np.asarray(points, dtype=np.float32).reshape(-1, 3)
    edge0 = positions[:, 0] - positions[:, 3]
    edge1 = positions[:, 1] - positions[:, 3]
    edge2 = positions[:, 2] - positions[:, 3]
    delta = points - positions[:, 3]
    cross12 = np.cross(edge1, edge2)
    denominator = (edge0 * cross12).sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        b0 = (delta * cross12).sum(axis=1) / denominator
        b1 = (delta * np.cross(edge2, edge0)).sum(axis=1) / denominator
        b2 = (delta * np.cross(edge0, edge1)).sum(axis=1) / denominator
    return np.stack((b0, b1, b2, 1.0 - b0 - b1 - b2), axis=1).astype(
        np.float32,
        copy=False,
    )


def _tetra_exit_rows(weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Recover the runtime's low-bit-tagged exit corner and outside mask."""
    tagged_bits = (
        np.asarray(weights, dtype=np.float32).view(np.uint32)
        & np.uint32(0xFFFFFFFC)
    ) | _TETRA_CORNER_TAGS
    tagged_weights = tagged_bits.view(np.float32)
    corners = np.argmin(tagged_weights, axis=1).astype(np.int64, copy=False)
    minima = tagged_weights[np.arange(len(tagged_weights)), corners]
    outside = np.isnan(minima) | (minima < np.float32(-0.0))
    return corners, outside


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
