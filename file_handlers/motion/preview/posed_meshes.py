from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

lazy import numpy as np

from ui.scene.scene_model import SceneSkinningBinding
lazy from ui.scene.scene_view import ScenePose

from .model import MotionPreviewSnapshot


class PosedViewport(Protocol):
    def set_mesh_skinning(self, key: str, binding: object) -> None: ...

    def update_mesh_skinning(self, key: str, matrices: np.ndarray) -> None: ...

    def update_mesh_skinning_source(self, key: str, positions: np.ndarray, normals: np.ndarray | None) -> None: ...

    def clear_mesh_skinning(self, keys: set[str] | None = None) -> None: ...

    def set_mesh_pose(self, key: str, pose: ScenePose | None) -> None: ...

    def update_mesh_geometry(
        self,
        key: str,
        vertices: np.ndarray,
        normals: np.ndarray | None,
        *,
        recompute_bounds: bool = True,
    ) -> None: ...


class PoseDeformer(Protocol):
    binding: SceneSkinningBinding

    @property
    def requires_post_skin_normals(self) -> bool: ...

    def skin_matrices(self, snapshot: MotionPreviewSnapshot) -> np.ndarray: ...

    def mesh_bones(self, matrices: np.ndarray) -> np.ndarray: ...

    def joint_locals(self, snapshot: MotionPreviewSnapshot) -> np.ndarray | None: ...

    def source_geometry(self, deformation_weights) -> tuple[np.ndarray, np.ndarray | None]: ...

    def deform(self, snapshot: MotionPreviewSnapshot) -> tuple[np.ndarray, np.ndarray | None]: ...


class MotionRenderState:
    def __init__(self):
        self.clear()

    def clear(self) -> None:
        self.world = None
        self.root_deltas = None
        self.deformation_weights = None

    def changes(self, snapshot: MotionPreviewSnapshot) -> tuple[bool, bool]:
        return (
            snapshot.pose.world is not self.world
            or snapshot.root_deltas != self.root_deltas,
            snapshot.deformation_weights != self.deformation_weights,
        )

    def accept(self, snapshot: MotionPreviewSnapshot) -> None:
        self.world = snapshot.pose.world
        self.root_deltas = snapshot.root_deltas
        self.deformation_weights = snapshot.deformation_weights


class PosedMeshes:
    def __init__(self, viewport: PosedViewport, restore_geometry: Callable[[str], None] | None = None):
        self.viewport = viewport
        self.restore_geometry = restore_geometry
        self.deformers: dict[str, PoseDeformer] = {}
        self._on_device: set[str] = set()
        self._state = MotionRenderState()

    def bind(self, deformers: Mapping[str, PoseDeformer]) -> None:
        deformers = dict(deformers)
        before, self.deformers = self.deformers, deformers
        on_device = {key for key, deformer in deformers.items() if not deformer.requires_post_skin_normals}
        kept = {key for key in on_device & self._on_device if before.get(key) is deformers[key]}
        self._let_go({key: deformer for key, deformer in before.items() if key not in deformers},
                     self._on_device - kept)
        for key in on_device - kept:
            self.viewport.set_mesh_skinning(key, deformers[key].binding)
        self._on_device = on_device
        self._state.clear()

    def forget(self) -> None:
        """For after the viewport's meshes were replaced: unlike ``release``, it does not tell the viewport."""
        self.deformers, self._on_device = {}, set()
        self._state.clear()

    def release(self) -> None:
        self.bind({})

    def _let_go(self, dropped: Mapping[str, PoseDeformer], unskinned: set[str]) -> None:
        if unskinned:
            self.viewport.clear_mesh_skinning(set(unskinned))
        for key in dropped:
            if key not in self._on_device and self.restore_geometry is not None:
                self.restore_geometry(key)
            self.viewport.set_mesh_pose(key, None)

    def show(self, snapshot: MotionPreviewSnapshot) -> tuple[bool, bool]:
        pose_changed, deformation_changed = self._state.changes(snapshot)
        if pose_changed or deformation_changed:
            for key, deformer in self.deformers.items():
                matrices = deformer.skin_matrices(snapshot)
                self.viewport.set_mesh_pose(key, ScenePose(
                    deformer.mesh_bones(matrices), snapshot.deformation_weights, deformer.joint_locals(snapshot)))
                if key not in self._on_device:
                    vertices, normals = deformer.deform(snapshot)
                    self.viewport.update_mesh_geometry(key, vertices, normals, recompute_bounds=False)
                    continue
                if deformation_changed:
                    self.viewport.update_mesh_skinning_source(
                        key, *deformer.source_geometry(snapshot.deformation_weights))
                if pose_changed:
                    self.viewport.update_mesh_skinning(key, matrices)
        self._state.accept(snapshot)
        return pose_changed, deformation_changed


__all__ = ["PoseDeformer", "PosedMeshes", "PosedViewport"]
