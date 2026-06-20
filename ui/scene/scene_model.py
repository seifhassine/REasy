from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class SceneDrawBatch:
    indices: np.ndarray
    material_name: str = ""


@dataclass(slots=True)
class SceneDrawMesh:
    key: str
    vertices: np.ndarray
    indices: np.ndarray
    color: tuple[float, float, float] | tuple[float, float, float, float]
    force_solid: bool = False
    ignore_highlight_filter: bool = False
    normals: np.ndarray | None = None
    uvs: np.ndarray | None = None
    colors: np.ndarray | None = None
    material_name: str = ""
    batches: list[SceneDrawBatch] = field(default_factory=list)
