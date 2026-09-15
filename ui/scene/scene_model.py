from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass(slots=True)
class SceneDrawBatch:
    indices: np.ndarray
    material_name: str = ""
    part_index: int | None = None


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
    transform_matrix: np.ndarray | None = None
    geometry_key: str = ""


@dataclass(frozen=True, slots=True)
class SceneSkinningBinding:
    """Immutable bind-pose vertex data for a renderer skinning backend."""

    positions: np.ndarray
    normals: np.ndarray | None
    joint_indices: np.ndarray
    weights: np.ndarray

    def __post_init__(self) -> None:
        positions = np.ascontiguousarray(
            self.positions,
            dtype=np.float32,
        ).reshape(-1, 3)
        normals = (
            np.ascontiguousarray(self.normals, dtype=np.float32).reshape(-1, 3)
            if self.normals is not None
            else None
        )
        joints = np.ascontiguousarray(self.joint_indices, dtype=np.uint16)
        weights = np.ascontiguousarray(self.weights, dtype=np.float32)
        if joints.ndim != 2 or weights.shape != joints.shape:
            raise ValueError(
                "skin joint indices and weights must have the same 2D layout"
            )
        if not len(positions):
            raise ValueError("skinning data has no vertices")
        if len(positions) != len(joints):
            raise ValueError(
                "skin influences must match the bind-pose vertex count"
            )
        if normals is not None and len(normals) != len(positions):
            raise ValueError(
                "bind-pose normals must match the vertex count"
            )
        if joints.shape[1] < 1:
            raise ValueError("skinned vertices need at least one influence")
        if not np.isfinite(positions).all() or not np.isfinite(weights).all():
            raise ValueError("skinning data contains a non-finite value")
        if normals is not None and not np.isfinite(normals).all():
            raise ValueError("skinning normals contain a non-finite value")
        if np.any(weights < 0.0) or np.any(np.sum(weights, axis=1) <= 0.0):
            raise ValueError(
                "every skinned vertex must have positive influence weight"
            )
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "normals", normals)
        object.__setattr__(self, "joint_indices", joints)
        object.__setattr__(self, "weights", weights)

    @property
    def group_count(self) -> int:
        return (self.joint_indices.shape[1] + 3) // 4
