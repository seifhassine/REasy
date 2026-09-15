from __future__ import annotations

import numpy as np

from file_handlers.mesh.material_session import scoped_material_key
from ui.scene.mesh_scene import build_mesh_scene
from .renderer import MotionPreviewRenderer
from .scene import SkeletonScene
from .skinning import build_shared_rig_deformer
from .mhr_attachments import weapon_hold_properties, weapon_attachment
from .weapon_motion import WeaponMotionPlayer, select_weapon_motion


class MotionAssemblyRenderer(MotionPreviewRenderer):
    """Multiple skinned armor parts and joint-attached weapon meshes."""

    def __init__(self, viewport):
        super().__init__(viewport)
        self._parts = []
        self._hold_properties = {}
        self._last_frame = None
        self._motion_id = None
        self._source_fps = 60
        self.weapon_selections = {}

    def set_motion(self, motion, motion_id=None):
        self._hold_properties = weapon_hold_properties(motion)
        self._motion_id = motion_id
        self._source_fps = motion.frames_per_second
        self._skeleton = None
        self._last_frame = None

    def set_weapon_selection(self, part_key, selection):
        self.weapon_selections[part_key] = selection
        self._skeleton = None

    def clear(self, *, reset_camera=True):
        self._parts.clear()
        super().clear(reset_camera=reset_camera)

    def _build(self, snapshot, target, *, reset_camera):
        self._parts.clear()
        if target is None or not target.parts:
            return super()._build(snapshot, target, reset_camera=reset_camera)
        self._target = target
        self._target_mesh = None
        self._deformer = None
        self._gpu_skinning = False
        self._skeleton = SkeletonScene(snapshot)
        self._render_state.clear()
        meshes = []
        indices = {joint.name: i for i, joint in enumerate(target.rig.joints)}
        for part in target.parts:
            scene = build_mesh_scene(part.mesh, key=part.key,
                                     material_key=lambda name, scope=part.key: scoped_material_key(scope, name))
            if len(scene) != 1:
                raise ValueError(f'{part.key}: no renderable LOD 0 mesh')
            draw = scene[0]
            deformer = None
            joint_index = None
            player = None
            part_snapshot = snapshot
            if part.parent_joint:
                joint, local = weapon_attachment(part, self._hold_properties, snapshot.frame)
                joint_index = indices[joint]
                draw.transform_matrix = self._attachment_matrix(snapshot, joint_index, local)
                choice = select_weapon_motion(part.weapon_motions, self._motion_id,
                                              self.weapon_selections.get(part.key, -1))
                if choice is not None:
                    player = WeaponMotionPlayer(choice, part.rig)
                    part_snapshot = player.sample(snapshot.frame, self._source_fps)
            if not part.parent_joint or player is not None:
                deformer = build_shared_rig_deformer(
                    part.mesh, part.rig, part.rig if player is not None else target.rig, draw.vertices, draw.normals, draw.indices,
                    pose_to_constrained_matrix=np.eye(4), handler=part.handler,
                    explicit_mdf_path=part.mdf_path)
                if deformer.requires_post_skin_normals:
                    draw.vertices, draw.normals = deformer.deform(part_snapshot)
            self._parts.append((part, draw, deformer, joint_index, player))
            meshes.append(draw)
        self.viewport.set_scene(meshes, reset_camera=reset_camera)
        for part, draw, deformer, joint_index, player in self._parts:
            if deformer is not None and not deformer.requires_post_skin_normals:
                self.viewport.set_mesh_skinning(part.key, deformer.binding)
                part_snapshot = player.sample(snapshot.frame, self._source_fps) if player is not None else snapshot
                self.viewport.update_mesh_skinning(part.key, deformer.skin_matrices(part_snapshot))
        self._render_state.accept(snapshot)
        self._last_frame = snapshot.frame

    def _attachment_matrix(self, snapshot, joint_index, local):
        world = np.asarray(snapshot.pose.world_matrices[joint_index], dtype=np.float32).reshape(4, 4).copy()
        root = self._target.rig.root_indices[joint_index]
        world[3, :3] -= dict(snapshot.root_deltas).get(root, (0, 0, 0))
        return world.T @ (local if local is not None else np.eye(4, dtype=np.float32))

    def _update(self, snapshot):
        if not self._parts:
            return super()._update(snapshot)
        changed, _ = self._render_state.changes(snapshot)
        if not changed and self._last_frame == snapshot.frame:
            return
        transforms = {}
        for part, draw, deformer, joint_index, player in self._parts:
            if joint_index is not None:
                joint, local = weapon_attachment(part, self._hold_properties, snapshot.frame)
                index = next(i for i, value in enumerate(self._target.rig.joints) if value.name == joint)
                transforms[part.key] = self._attachment_matrix(snapshot, index, local)
            if deformer is None:
                continue
            part_snapshot = player.sample(snapshot.frame, self._source_fps) if player is not None else snapshot
            if deformer.requires_post_skin_normals:
                vertices, normals = deformer.deform(part_snapshot)
                self.viewport.update_mesh_geometry(part.key, vertices, normals, recompute_bounds=False)
            else:
                self.viewport.update_mesh_skinning(part.key, deformer.skin_matrices(part_snapshot))
        if transforms:
            self.viewport.set_mesh_draw_transforms(transforms)
        self._render_state.accept(snapshot)
        self._last_frame = snapshot.frame
