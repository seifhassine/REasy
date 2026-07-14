from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(slots=True)
class PrbData:
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
    bsp_data_size: int
    bsp_padding_size: int


@dataclass(slots=True)
class LprbData:
    probe_count: int
    terms_rgb: np.ndarray


@dataclass(slots=True)
class LightProbeData:
    prb: PrbData
    lprb: LprbData

    def validate(self) -> None:
        if self.prb.probe_count != self.lprb.probe_count:
            raise ValueError(
                f"Probe count mismatch: PRB has {self.prb.probe_count}, "
                f"LPRB has {self.lprb.probe_count}"
            )
        terms_shape = self.lprb.terms_rgb.shape
        if (
            self.lprb.terms_rgb.ndim != 3
            or terms_shape[0:1] != (self.lprb.probe_count,)
            or terms_shape[2:] != (3,)
        ):
            raise ValueError(
                "LPRB lighting terms must have shape "
                f"({self.lprb.probe_count}, term_count, 3)"
            )
        if self.lprb.terms_rgb.shape[1] not in (6, 12):
            raise ValueError(
                f"Unsupported LPRB lighting term count: {self.lprb.terms_rgb.shape[1]}"
            )
