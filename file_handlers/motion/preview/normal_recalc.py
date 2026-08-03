from __future__ import annotations

import numpy as np

from file_handlers.mesh.normal_recalc import mesh_normal_recalc_plan
from utils.native_build import ensure_fastmesh


_fastmesh = ensure_fastmesh()
if _fastmesh is None or not hasattr(_fastmesh, "recalculate_normals"):
    raise ImportError("fastmesh was built without normal-recalculation support")


class MeshNormalRecalculator:
    """Rebuild render normals from posed positions and semantic weld redirects."""

    def __init__(
        self,
        redirect_targets: np.ndarray,
        triangle_indices: np.ndarray,
        vertex_count: int,
    ) -> None:
        targets = np.asarray(redirect_targets, dtype=np.int64).reshape(-1)
        indices = np.asarray(triangle_indices, dtype=np.int64).reshape(-1)
        if len(targets) != vertex_count:
            raise ValueError(
                "normal-recalculation redirects do not match the LOD0 vertices"
            )
        if len(indices) % 3:
            raise ValueError(
                "normal-recalculation indices do not form complete triangles"
            )
        if np.any((targets < 0) | (targets >= vertex_count)):
            raise ValueError(
                "normal-recalculation redirect references a vertex outside LOD0"
            )
        if np.any((indices < 0) | (indices >= vertex_count)):
            raise ValueError(
                "normal-recalculation triangle references a vertex outside LOD0"
            )
        self._targets = np.ascontiguousarray(targets, dtype=np.intp)
        self._flat_triangle_vertices = np.ascontiguousarray(
            indices.reshape(-1, 3),
            dtype=np.intp,
        ).reshape(-1)
        self._vertex_count = vertex_count

    @classmethod
    def from_mesh(
        cls,
        mesh,
        triangle_indices: np.ndarray | None,
        vertex_count: int,
    ) -> "MeshNormalRecalculator | None":
        rendered = (
            None
            if triangle_indices is None
            else np.asarray(triangle_indices).reshape(-1)
        )
        plan = mesh_normal_recalc_plan(mesh, rendered)
        if plan is None:
            return None
        return cls(
            np.asarray(plan.redirect_targets),
            np.asarray(plan.triangle_vertices),
            vertex_count,
        )

    def recalculate(self, positions: np.ndarray) -> np.ndarray:
        positions = np.asarray(positions, dtype=np.float32).reshape(-1, 3)
        if len(positions) != self._vertex_count:
            raise ValueError(
                "posed positions do not match the normal-recalculation vertices"
            )

        return np.frombuffer(
            _fastmesh.recalculate_normals(
                positions,
                self._flat_triangle_vertices,
                self._targets,
            ),
            dtype=np.float32,
        ).reshape(-1, 3)
