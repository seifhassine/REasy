from dataclasses import asdict
from pathlib import Path
import sys
import unittest

import numpy as np

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC
from file_handlers.motion.mot_clip.model import ClipKey, ClipProperty, ClipPropertyType
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.preview.clip_timeline import ClipLane, ClipTimelineView, move_timeline_item, sequence_lanes
from file_handlers.motion.preview.mhr_assets import (MhrPreviewAssets, WEAPON_PRESETS,
    find_rise_installation, preview_context, weapon_family)
from file_handlers.motion.preview.mhr_attachments import weapon_attachment, weapon_hold_properties
from file_handlers.motion.evaluation.mhr import MHR_EVALUATION_PROFILE
from file_handlers.motion.preview.controller import MotionPreviewController
from file_handlers.motion.preview.skinning import build_shared_rig_deformer
from file_handlers.motion.preview.weapon_motion import select_weapon_motion, WeaponMotionPlayer
from file_handlers.mesh.material_session import MeshMaterialSession
from ui.scene.mesh_scene import build_mesh_scene


CORPUS = Path(__file__).parent/'TESTFILE/natives/STM/player/mot'


class TimelineTests(unittest.TestCase):
    def test_key_drag_keeps_native_key_order_and_expands_parent_window(self):
        parent = ClipProperty('parent', ClipPropertyType.STRUCT, 0, 5)
        prop = ClipProperty('sound', ClipPropertyType.S32, 0, 5,
                            keys=[ClipKey(0, value=7), ClipKey(5, value=8), ClipKey(15, value=9)])
        parent.children.append(prop)
        lane = ClipLane('SOUND', 'Sound / sound', prop, (parent,))
        saved = move_timeline_item(lane, prop.keys[1], 'key', 100, 40)
        self.assertEqual([k.frame for k in prop.keys], [0, 15, 15])
        self.assertEqual(prop.end_frame, 15)
        self.assertEqual(parent.end_frame, 15)
        for item, attr, value in saved: setattr(item, attr, value)
        self.assertEqual([k.frame for k in prop.keys], [0, 5, 15])
        self.assertEqual(parent.end_frame, 5)

    def test_range_move_preserves_relative_key_times_and_values(self):
        child = ClipProperty('ID', ClipPropertyType.S32, 10, 30, keys=[ClipKey(12, value=123), ClipKey(24, value=456)])
        prop = ClipProperty('event', ClipPropertyType.STRUCT, 10, 30, children=[child])
        lane = ClipLane('VFX', 'VFX / event', prop, ())
        move_timeline_item(lane, None, 'move', 15, 100)
        self.assertEqual((prop.start_frame, prop.end_frame, child.start_frame, child.end_frame), (25, 45, 25, 45))
        self.assertEqual([(key.frame, key.value) for key in child.keys], [(27, 123), (39, 456)])
        move_timeline_item(lane, None, 'move', -100, 100)
        self.assertEqual((prop.start_frame, prop.end_frame), (0, 20))
        self.assertEqual([key.frame for key in child.keys], [2, 14])

    def test_weapon_type_comes_from_list_family_not_embedded_shared_motion_name(self):
        self.assertEqual(weapon_family('plw_GunLance_100'), 'gunlance')
        self.assertEqual(weapon_family('wpg_GreatSword_Main_02_Common_000'), 'greatsword')
        self.assertIsNone(weapon_family('plc_Emotion_003'))

    @unittest.skipUnless(CORPUS.is_dir(), 'local motion corpus is absent')
    def test_real_clip_key_edit_roundtrips_with_events_and_animation_preserved(self):
        source = (CORPUS/'plw_DualBlades_100.motlist.528').read_bytes()
        model = MHR_MOTION_FORMAT_CODEC.parse(source, label='timeline test')
        motion = model.slots[0].payload.value
        lane = next(lane for lane in sequence_lanes(motion.sequences) if lane.category == 'SOUND' and len(lane.prop.keys) == 1)
        key = lane.prop.keys[0]
        original_value = key.value
        move_timeline_item(lane, key, 'key', 5, motion.end_frame)
        output = MHR_MOTION_FORMAT_CODEC.write(model)
        reopened = MHR_MOTION_FORMAT_CODEC.parse(output, label='edited timeline')
        self.assertEqual([asdict(s) for s in motion.sequences],
                         [asdict(s) for s in reopened.slots[0].payload.value.sequences])
        self.assertEqual(key.value, original_value)
        self.assertEqual(key.frame, 5)
        for before, after in zip(model.tracks, reopened.tracks):
            self.assertEqual(before.track.frames, after.track.frames)
            self.assertEqual(before.track.values, after.track.values)
        self.assertEqual(MHR_MOTION_FORMAT_CODEC.write(reopened), output)


class TimelineQtInteractionTests(unittest.TestCase):
    def test_seek_and_key_drag_repaint_without_mutating_unselected_events(self):
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest
        app = QApplication.instance() or QApplication(['timeline-test', '-platform', 'offscreen'])
        errors, commits, frames = [], [], []
        original_hook = sys.excepthook
        sys.excepthook = lambda kind, value, trace: errors.append(value)
        prop = ClipProperty('event', ClipPropertyType.S32, 0, 20, keys=[ClipKey(10, value=123)])
        view = ClipTimelineView(lambda: commits.append(True))
        view.set_lanes([ClipLane('SOUND', 'Sound / event', prop, ())], 100)
        view.frame_requested.connect(frames.append)
        view.resize(800, 220);view.show();app.processEvents()
        try:
            for y in (10, view.RULER_HEIGHT+13):
                start, end = QPoint(round(view.x_at(50)), y), QPoint(round(view.x_at(65)), y)
                QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
                view.viewport().grab()
                QTest.mouseMove(view.viewport(), end)
                view.viewport().grab()
                QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
                self.assertEqual(prop.keys[0].frame, 10)
                self.assertFalse(commits)
            self.assertTrue(frames)
            y = view.RULER_HEIGHT+13
            start, end = QPoint(round(view.x_at(10)), y), QPoint(round(view.x_at(15)), y)
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(view.viewport(), end)
            view.viewport().grab()
            QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertEqual(prop.keys[0].frame, 15)
            self.assertEqual(commits, [True])
            start = QPoint(round(view.x_at(15)), y)
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=start)
            QTest.mouseMove(view.viewport(), QPoint(round(view.x_at(20)), y))
            view.set_lanes([], 100)
            view.viewport().grab()
            QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=end)
            self.assertEqual(prop.keys[0].frame, 15)
            self.assertEqual(commits, [True])
            self.assertFalse(errors, errors)
        finally:
            view.close()
            sys.excepthook = original_hook


@unittest.skipUnless(find_rise_installation() is not None and CORPUS.is_dir(), 'local Rise installation/corpus is absent')
class NativePreviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = MhrPreviewAssets(preview_context(MotListHandler()))

    def test_all_weapon_presets_resolve_native_meshes_materials_and_mounts(self):
        for family in WEAPON_PRESETS:
            with self.subTest(weapon=family):
                target = self.assets.load(family)
                joints = {joint.name for joint in target.rig.joints}
                for part in target.parts:
                    if part.parent_joint:
                        self.assertIn(part.parent_joint, joints)
                        self.assertTrue(all(joint in joints for _, joint, _ in part.attachment_options))
                    elif family != 'greatsword':
                        continue
                    materials = MeshMaterialSession(part.handler, explicit_mdf_path=part.mdf_path,
                                                    material_scope=part.key, parse_in_subprocess=False)
                    materials.prepare_all()
                    self.assertTrue(materials.images)
                    self.assertFalse(materials.errors)
                    if part.key == 'face':
                        face = next(binding for binding in materials.bindings if binding.mesh_material_name == 'Face')
                        self.assertEqual(face.texture_type, 'FaceBaseMap')
                        self.assertEqual(face.status, 'Resolved')

    def test_body_deformation_and_clip_driven_sheathing_use_native_rigs(self):
        target = self.assets.load('gunlance')
        source = MHR_MOTION_FORMAT_CODEC.parse((CORPUS/'plw_GunLance_100.motlist.528').read_bytes(), label='draw weapon')
        motion = source.slots[1].payload.value
        controller = MotionPreviewController(MHR_EVALUATION_PROFILE)
        self.assertTrue(controller.load(motion, target.rig), controller.error_message)
        body = target.parts[0]
        scene = build_mesh_scene(body.mesh)[0]
        deformer = build_shared_rig_deformer(body.mesh, body.rig, target.rig, scene.vertices, scene.normals,
            scene.indices, pose_to_constrained_matrix=np.eye(4), handler=body.handler, explicit_mdf_path=body.mdf_path)
        controller.set_frame(0);before, _ = deformer.deform(controller.sample())
        controller.set_frame(20);after, _ = deformer.deform(controller.sample())
        self.assertTrue(np.isfinite(after).all())
        self.assertGreater(float(np.linalg.norm(after-before)), .01)
        weapon = next(part for part in target.parts if part.weapon_role == 'main')
        properties = weapon_hold_properties(motion)
        self.assertEqual(weapon_attachment(weapon, properties, 0)[0], 'Spine_01')
        self.assertEqual(weapon_attachment(weapon, properties, 20)[0], 'L_Weapon_00')

    def test_prefab_weapon_motion_deforms_locally_and_special_bank_is_explicit(self):
        part = self.assets.weapon('gunlance', 'gl_lan001', 'left')
        definition = self.assets.weapon_definition('gunlance', 'gl_lan001')
        self.assertEqual(definition.motion_bank_path, 'player/mot/wpg_GunLance_Main_00_bank.motbank')
        choice = select_weapon_motion(part.weapon_motions, 2)
        self.assertEqual(choice.bank_id, 0)
        player = WeaponMotionPlayer(choice, part.rig)
        self.assertGreater(sum(j.animation_node is not None for j in player.controller.binding.joints), 0)
        scene = build_mesh_scene(part.mesh)[0]
        deformer = build_shared_rig_deformer(part.mesh, part.rig, part.rig, scene.vertices, scene.normals,
            scene.indices, pose_to_constrained_matrix=np.eye(4), handler=part.handler, explicit_mdf_path=part.mdf_path)
        before, _ = deformer.deform(player.sample(0, 60))
        after, _ = deformer.deform(player.sample(75, 60))
        self.assertTrue(np.isfinite(after).all())
        self.assertGreater(float(np.linalg.norm(after-before)), .01)
        np.testing.assert_allclose(player.sample(10, 30).pose.world_matrices,
                                   player.sample(20, 60).pose.world_matrices)
        self.assertIsNone(select_weapon_motion(part.weapon_motions, 65535))
        self.assertIsNone(select_weapon_motion(part.weapon_motions, 2, -2))
        bowgun = self.assets.weapon('lightbowgun', 'l_bg003', 'left')
        normal = select_weapon_motion(bowgun.weapon_motions, 116)
        self.assertFalse(normal.special)
        special_index = next(i for i, entry in enumerate(bowgun.weapon_motions) if entry.special and entry.motion_id == 116)
        self.assertTrue(select_weapon_motion(bowgun.weapon_motions, 116, special_index).special)

    def test_weapon_only_list_uses_weapon_rig(self):
        source = MHR_MOTION_FORMAT_CODEC.parse(
            (CORPUS/'wpg_GreatSword_Main_02_Common_000.motlist.528').read_bytes(), label='weapon motion')
        target = self.assets.load('greatsword', weapon_only=True, motion_list_name=source.name,
                                 required_bones={node.joint.binding_hash for node in source.slots[0].payload.value.animation_nodes})
        self.assertEqual(len(target.parts), 1)
        self.assertFalse(target.parts[0].parent_joint)
        controller = MotionPreviewController(MHR_EVALUATION_PROFILE)
        self.assertTrue(controller.load(source.slots[0].payload.value, target.rig))
        bound = [joint for joint in controller.binding.joints if joint.animation_node is not None]
        self.assertGreater(len(bound), 1)


if __name__ == '__main__':
    unittest.main()
