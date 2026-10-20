from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


MESH_MAGIC = 0x4853454D
MPLY_MAGIC = 0x594C504D


@dataclass(frozen=True, slots=True)
class MeshAABB:
    minimum: Tuple[float, float, float]
    maximum: Tuple[float, float, float]
    minimum_w: float = 0.0
    maximum_w: float = 0.0

    @property
    def minimum4(self) -> Tuple[float, float, float, float]:
        return (*self.minimum, self.minimum_w)

    @property
    def maximum4(self) -> Tuple[float, float, float, float]:
        return (*self.maximum, self.maximum_w)


__all__ = ["MESH_MAGIC", "MPLY_MAGIC", "MeshAABB"]
