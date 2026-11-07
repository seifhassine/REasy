"""See docs/internals/jcns-evaluation.md."""

from __future__ import annotations

from dataclasses import dataclass, field

lazy import numpy as np

ROW_WIDTHS = {"positions": 3, "rotations": 4, "scales": 3, "base_positions": 3, "base_rotations": 4, "base_scales": 3}


@dataclass(eq=False)
class Rig:
    hashes: np.ndarray
    parents: np.ndarray
    positions: np.ndarray
    rotations: np.ndarray
    scales: np.ndarray
    base_positions: np.ndarray
    base_rotations: np.ndarray
    base_scales: np.ndarray
    owner_rotation: np.ndarray = (0.0, 0.0, 0.0, 1.0)
    owner_matrix: np.ndarray = field(default_factory=lambda: np.eye(4, dtype=np.float32))
    host: Rig | None = None
    follows: np.ndarray | None = None
    symmetry: np.ndarray | None = None

    def __post_init__(self):
        count = len(self.hashes)
        self.hashes, self.parents = np.array(self.hashes, np.uint32), np.array(self.parents, np.int32)
        for name, width in ROW_WIDTHS.items():
            setattr(self, name, np.array(getattr(self, name), np.float32, order="C").reshape(count, width))
        for name in ("follows", "symmetry"):
            value = getattr(self, name)
            setattr(self, name, np.full(count, -1, np.int32) if value is None else np.array(value, np.int32))
        self.owner_rotation = np.array(self.owner_rotation, np.float32).reshape(4)
        self.owner_matrix = np.array(self.owner_matrix, np.float32, order="C").reshape(4, 4)
