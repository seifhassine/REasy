from __future__ import annotations

from typing import Protocol

import numpy as np

from ui.scene.mesh_scene import build_mesh_scene
from ui.scene.scene_model import SceneDrawMesh

from .model import MotionPreviewSnapshot
from .posed_meshes import PosedMeshes, PosedViewport
from .scene import SkeletonScene
from .skinning import SkinningError, build_skinned_mesh_deformer
from .target import RigPreviewTarget


class PreviewViewport(PosedViewport, Protocol):
    def set_scene(
        self,
        meshes: list[SceneDrawMesh],
        *,
        reset_camera: bool = True,
    ) -> None: ...

    def update_mesh_transforms(
        self,
        matrices: dict[str, np.ndarray],
        *,
        recompute_bounds: bool = True,
    ) -> None: ...


class MotionPreviewRenderer:
    TARGET_KEY = "motion-preview:target"

    def __init__(self, viewport: PreviewViewport):
        self.viewport = viewport
        self._target: RigPreviewTarget | None = None
        self._skeleton: SkeletonScene | None = None
        self._poses = PosedMeshes(viewport)

    def present(
        self,
        snapshot: MotionPreviewSnapshot,
        target: RigPreviewTarget | None,
        *,
        reset_camera: bool,
    ) -> None:
        if (
            self._skeleton is None
            or self._target is not target
            or not self._skeleton.accepts(snapshot)
        ):
            self._build(snapshot, target, reset_camera=reset_camera)
            return
        self._update(snapshot)

    def clear(self, *, reset_camera: bool = True) -> None:
        self._target = None
        self._skeleton = None
        self._poses.forget()
        self.viewport.set_scene([], reset_camera=reset_camera)

    def _build(
        self,
        snapshot: MotionPreviewSnapshot,
        target: RigPreviewTarget | None,
        *,
        reset_camera: bool,
    ) -> None:
        skeleton = SkeletonScene(snapshot)
        meshes, deformers = skeleton.meshes, {}
        if target is not None and target.mesh is not None:
            meshes = build_mesh_scene(target.mesh, key=self.TARGET_KEY)
            if len(meshes) != 1:
                raise SkinningError("target mesh has no renderable LOD 0 geometry")
            deformers[self.TARGET_KEY] = build_skinned_mesh_deformer(
                target.mesh,
                target.rig,
                meshes[0].vertices,
                meshes[0].normals,
                meshes[0].indices,
                handler=target.handler,
                skin_weight_source=target.skin_weight_source,
            )
        self._target, self._skeleton = target, skeleton
        self._poses.forget()
        self.viewport.set_scene(meshes, reset_camera=reset_camera)
        self._poses.bind(deformers)
        self._poses.show(snapshot)

    def _update(self, snapshot: MotionPreviewSnapshot) -> None:
        assert self._skeleton is not None
        pose_changed, _deformation_changed = self._poses.show(snapshot)
        if pose_changed and not self._poses.deformers:
            self.viewport.update_mesh_transforms(self._skeleton.transforms(snapshot), recompute_bounds=False)
