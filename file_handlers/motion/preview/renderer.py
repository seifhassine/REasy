from __future__ import annotations

from typing import Protocol

import numpy as np

from ui.scene.mesh_scene import build_mesh_scene
from ui.scene.scene_model import SceneDrawMesh

from .model import MotionPreviewSnapshot
from .scene import SkeletonScene
from .skinning import (
    SkinnedMeshDeformer,
    SkinningError,
    build_skinned_mesh_deformer,
)
from .target import RigPreviewTarget


class PreviewViewport(Protocol):
    def set_scene(
        self,
        meshes: list[SceneDrawMesh],
        *,
        reset_camera: bool = True,
    ) -> None: ...

    def set_mesh_skinning(self, key: str, binding: object) -> None: ...

    def update_mesh_skinning(
        self,
        key: str,
        matrices: np.ndarray,
    ) -> None: ...

    def update_mesh_skinning_source(
        self,
        key: str,
        positions: np.ndarray,
        normals: np.ndarray | None,
    ) -> None: ...

    def clear_mesh_skinning(self, keys: set[str] | None = None) -> None: ...

    def update_mesh_geometry(
        self,
        key: str,
        vertices: np.ndarray,
        normals: np.ndarray | None,
        *,
        recompute_bounds: bool = True,
    ) -> None: ...

    def update_mesh_transforms(
        self,
        matrices: dict[str, np.ndarray],
        *,
        recompute_bounds: bool = True,
    ) -> None: ...


class MotionRenderState:
    """Detect semantic pose/deformation changes between rendered snapshots."""

    def __init__(self):
        self.clear()

    def clear(self) -> None:
        self.world_matrices = None
        self.root_deltas = None
        self.deformation_weights = None

    def changes(self, snapshot: MotionPreviewSnapshot) -> tuple[bool, bool]:
        return (
            snapshot.pose.world_matrices is not self.world_matrices
            or snapshot.root_deltas != self.root_deltas,
            snapshot.deformation_weights != self.deformation_weights,
        )

    def accept(self, snapshot: MotionPreviewSnapshot) -> None:
        self.world_matrices = snapshot.pose.world_matrices
        self.root_deltas = snapshot.root_deltas
        self.deformation_weights = snapshot.deformation_weights


class MotionPreviewRenderer:
    """Own the stable preview scene and apply sampled poses incrementally."""

    TARGET_KEY = "motion-preview:target"

    def __init__(self, viewport: PreviewViewport):
        self.viewport = viewport
        self._target: RigPreviewTarget | None = None
        self._target_mesh: SceneDrawMesh | None = None
        self._deformer: SkinnedMeshDeformer | None = None
        self._gpu_skinning = False
        self._skeleton: SkeletonScene | None = None
        self._render_state = MotionRenderState()

    def present(
        self,
        snapshot: MotionPreviewSnapshot,
        target: RigPreviewTarget | None,
        *,
        reset_camera: bool,
    ) -> None:
        if (
            reset_camera
            or self._skeleton is None
            or self._target is not target
            or not self._skeleton.accepts(snapshot)
        ):
            self._build(snapshot, target, reset_camera=reset_camera)
            return
        self._update(snapshot)

    def clear(self, *, reset_camera: bool = True) -> None:
        self._target = None
        self._target_mesh = None
        self._deformer = None
        self._gpu_skinning = False
        self._skeleton = None
        self._render_state.clear()
        self.viewport.set_scene([], reset_camera=reset_camera)

    def _build(
        self,
        snapshot: MotionPreviewSnapshot,
        target: RigPreviewTarget | None,
        *,
        reset_camera: bool,
    ) -> None:
        self._target = target
        self._target_mesh = None
        self._deformer = None
        self._gpu_skinning = False
        self._render_state.clear()
        self._skeleton = SkeletonScene(snapshot)
        meshes: list[SceneDrawMesh] = []

        if target is not None and target.mesh is not None:
            target_scene = build_mesh_scene(target.mesh, key=self.TARGET_KEY)
            if len(target_scene) != 1:
                raise SkinningError("target mesh has no renderable LOD 0 geometry")
            self._target_mesh = target_scene[0]
            self._deformer = build_skinned_mesh_deformer(
                target.mesh,
                target.rig,
                self._target_mesh.vertices,
                self._target_mesh.normals,
                self._target_mesh.indices,
                handler=target.handler,
            )
            self._gpu_skinning = not self._deformer.requires_post_skin_normals
            if not self._gpu_skinning:
                vertices, normals = self._deformer.deform(snapshot)
                self._target_mesh.vertices = vertices
                if normals is not None:
                    self._target_mesh.normals = normals
            meshes.append(self._target_mesh)

        if self._target_mesh is None:
            meshes.extend(self._skeleton.meshes)
        self.viewport.set_scene(meshes, reset_camera=reset_camera)
        if self._gpu_skinning and self._deformer is not None:
            self.viewport.set_mesh_skinning(
                self.TARGET_KEY,
                self._deformer.binding,
            )
            if snapshot.deformation_weights:
                positions, normals = self._deformer.source_geometry(
                    snapshot.deformation_weights
                )
                self.viewport.update_mesh_skinning_source(
                    self.TARGET_KEY,
                    positions,
                    normals,
                )
            self.viewport.update_mesh_skinning(
                self.TARGET_KEY,
                self._deformer.skin_matrices(snapshot),
            )
        self._render_state.accept(snapshot)

    def _update(self, snapshot: MotionPreviewSnapshot) -> None:
        assert self._skeleton is not None
        pose_changed, deformation_changed = self._render_state.changes(snapshot)
        if self._deformer is not None and self._target_mesh is not None:
            if self._gpu_skinning:
                if deformation_changed:
                    positions, normals = self._deformer.source_geometry(
                        snapshot.deformation_weights
                    )
                    self.viewport.update_mesh_skinning_source(
                        self.TARGET_KEY,
                        positions,
                        normals,
                    )
                if pose_changed:
                    self.viewport.update_mesh_skinning(
                        self.TARGET_KEY,
                        self._deformer.skin_matrices(snapshot),
                    )
            elif pose_changed or deformation_changed:
                vertices, normals = self._deformer.deform(snapshot)
                self.viewport.update_mesh_geometry(
                    self.TARGET_KEY,
                    vertices,
                    normals,
                    recompute_bounds=False,
                )

        if self._target_mesh is not None:
            self._render_state.accept(snapshot)
            return

        if pose_changed:
            transforms = self._skeleton.transforms(snapshot)
            self.viewport.update_mesh_transforms(transforms, recompute_bounds=False)
        self._render_state.accept(snapshot)
