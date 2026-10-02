"""XX wire decoding, skeletal retargeting and native Rise export regressions."""
from pathlib import Path
import struct
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from file_handlers.motion.binary import ReadContext
from file_handlers.motion.errors import MotionParseError, MotionWriteError
from file_handlers.motion.format_registry import require_motion_format
from file_handlers.motion.lmt_codec import (LMT_MOTION_FORMAT_CODEC as XX, decode_track,
                                          read_mod_skeleton, xx_weapon_family, XX_HUNTER_MOD)
from utils.app_paths import resource_path
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from file_handlers.motion.mhr_import import (idle_template, import_motion, source_retarget,
                                            verify_imported_motion, verify_native_contract)
from file_handlers.motion.mhr_editing import next_motion_id
from file_handlers.motion.motlist_handler import MotListHandler
from file_handlers.motion.evaluation.binding import bind_motion
from file_handlers.motion.evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
from file_handlers.motion.evaluation.sampling import MotionEvaluator, resolve_motion_frame
from file_handlers.motion.evaluation.lmt_retarget import RISE_TO_XX
from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context, find_rise_installation


CORPUS = Path(__file__).parent/'TESTFILE'
XX_DIR = CORPUS/'xx'
RISE_PATH = CORPUS/'natives/STM/player/mot/plw_GreatSword_100.motlist.528'


class LmtTrackTests(unittest.TestCase):
    def test_weapon_hold_conversion_preserves_sub_weapon(self):
        from file_handlers.motion.lmt_codec import LmtMotion
        from file_handlers.motion.lmt_events import weapon_hold_keys
        motion = LmtMotion('test', end_frame=40, events=(((3, 1, 2, 17), ((1, 10), (2, 10), (4, 10), (8, 10))),))
        props = {hand: SimpleNamespace(keys=[SimpleNamespace(value=value)]) for hand, value in (('left', 2), ('right', 3))}
        keys = weapon_hold_keys(motion, props)
        self.assertEqual(keys['_leftWp'], [(0, 2), (10, 1), (30, 2), (40, 2)])
        self.assertEqual(keys['_rightWp'], [(0, 3), (10, 4), (20, 3), (40, 3)])

    def test_sampled_hold_tracks_follow_cut_timing(self):
        from file_handlers.motion.lmt_codec import LmtMotion
        from file_handlers.motion.lmt_events import sampled_weapon_hold_keys
        motion = LmtMotion('test', end_frame=30, events=(((3, 1, 2), ((1, 10), (2, 10), (4, 10))),))
        props = {hand: SimpleNamespace(keys=[SimpleNamespace(value=value)]) for hand, value in (('left', 2), ('right', 1))}
        keys = sampled_weapon_hold_keys([(motion, frame) for frame in (5, 10, 15, 20)], props)
        self.assertEqual(keys['_leftWp'], [(0, 2), (1, 1), (3, 1)])
        self.assertEqual(keys['_rightWp'], [(0, 1), (1, 2), (3, 1)])

    def decode(self, codec, raw, *, kind=0, bounds=None, reference=(0., 0., 0., 1.)):
        b = bytearray(struct.pack('<4BfII4fI', codec, kind, 0, 0, 1., len(raw), 68,
                                  *reference, 36 if bounds else 0))
        b.extend(struct.pack('<8f', *bounds) if bounds else bytes(32))
        b.extend(raw)
        return decode_track(ReadContext.from_bytes(b), 0, 100)[3]

    def test_reference_frame_and_outgoing_deltas(self):
        raw = struct.pack('<8B', 0, 127, 255, 9, 255, 0, 0, 0)
        track = self.decode(5, raw, kind=1, bounds=(2, 4, 6, 0, 10, 20, 30, 1), reference=(7, 8, 9, 1))
        self.assertEqual(track.frames, [0, 1, 10])
        np.testing.assert_allclose(track.values, [(7, 8, 9), (10, 20+4*127/255, 36), (12, 20, 30)])

    def test_signed_quaternion_component_order(self):
        codes = (1024, 16383-2048, 512, 4096)
        raw = (sum(v << s for v, s in zip(codes, (42, 28, 14, 0))) | (9 << 56)).to_bytes(8, 'little')
        track = self.decode(6, raw)
        np.testing.assert_allclose(track.values[1], np.array((1024, -2048, 512, 4096))*4/16383)

    def test_sparse_rotation_axes_and_bounds(self):
        bounds = (2, 3, 4, 5, .1, .2, .3, .4)
        for codec, axis in ((11, 0), (12, 1)):
            track = self.decode(codec, ((16383 << 14) | 8192).to_bytes(4, 'little'), bounds=bounds)
            expected = [.1, .2, .3, 5.4]
            expected[axis] += bounds[axis]*8192/16383
            np.testing.assert_allclose(track.values[1], expected)

    def test_vector16_and_quaternion7_component_order(self):
        bounds = (2, 3, 4, 5, .1, .2, .3, .4)
        track = self.decode(4, struct.pack('<4H', 65535, 32768, 0, 0), kind=1, bounds=bounds)
        np.testing.assert_allclose(track.values[1], (2.1, .2+3*32768/65535, .3))
        packed = (127 << 21) | (64 << 14) | (32 << 7) | 16
        track = self.decode(7, packed.to_bytes(4, 'little'), bounds=bounds)
        np.testing.assert_allclose(track.values[1], (2.1, .2+3*64/127, .3+4*32/127, .4+5*16/127))
        track = self.decode(2, struct.pack('<3f', .3, .4, 0))
        np.testing.assert_allclose(track.values[1], (.3, .4, 0, np.sqrt(.75)))

    def test_constant_channels_and_invalid_buffers(self):
        self.assertEqual(self.decode(1, b'', kind=1).frames, [0])
        for codec, data in ((255, b''), (6, bytes(7)), (7, bytes(4))):
            with self.assertRaises(MotionParseError):
                self.decode(codec, data)

    def test_mt_weapon_numbering(self):
        self.assertEqual(xx_weapon_family('w07_sa'), 'longsword')
        self.assertEqual(xx_weapon_family('w03'), 'lance')
        self.assertEqual(xx_weapon_family('w13'), 'insectglaive')
        self.assertIsNone(xx_weapon_family('wp03_00'))
        from file_handlers.motion.lmt_codec import XX_WEAPONS
        for family in XX_WEAPONS.values():
            self.assertEqual(xx_weapon_family(family), family)
            self.assertEqual(xx_weapon_family(family.upper()+'_HUNTERART'), family)


@unittest.skipUnless((XX_DIR/'greatsword.lmt').is_file(), 'XX corpus absent')
class LmtCorpusTests(unittest.TestCase):
    def test_slashaxe_sheath_event_boundary_and_backward_seeking(self):
        from file_handlers.motion.preview.lmt_attachments import lmt_attachment_state, lmt_weapon_attachment
        path = XX_DIR/'slashaxe.lmt'
        doc = XX.parse(path.read_bytes(), label=str(path))
        motion = next(s.payload.value for s in doc.slots if s.motion_id == 195)
        hand, body = object(), object()
        part = SimpleNamespace(weapon_role='main', parent_joint='L_Weapon_00', local_transform=hand,
                               attachment_options=(('left', 'L_Weapon_00', hand), ('body', 'Spine_01', body)))
        for frame, state in ((0, 'left'), (71.99, 'left'), (72, 'body'), (76, 'body'),
                             (81, 'body'), (143, 'body'), (71, 'left'), (80, 'body')):
            self.assertEqual(lmt_attachment_state(motion, frame), state)
            self.assertEqual(lmt_weapon_attachment(part, motion, frame),
                             ('Spine_01', body) if state == 'body' else ('L_Weapon_00', hand))

    def test_unconfirmed_weapon_events_keep_default_attachment(self):
        from file_handlers.motion.preview.lmt_attachments import lmt_attachment_state
        from dataclasses import replace
        path = XX_DIR/'slashaxe.lmt'
        doc = XX.parse(path.read_bytes(), label=str(path))
        motion = next(s.payload.value for s in doc.slots if s.motion_id == 195)
        self.assertEqual(lmt_attachment_state(replace(motion, name='dualblades_195'), 80), 'body')
        remap, records = motion.events[0]
        unknown = replace(motion, events=(((17, *remap[1:]), records), *motion.events[1:]))
        self.assertIsNone(lmt_attachment_state(unknown, 0))

    def test_slashaxe_hand_transfer_then_sheath(self):
        from dataclasses import replace
        from file_handlers.motion.preview.lmt_attachments import lmt_attachment_state, lmt_weapon_attachment
        path = XX_DIR/'slashaxe.lmt'
        doc = XX.parse(path.read_bytes(), label=str(path))
        motion = next(s.payload.value for s in doc.slots if s.motion_id == 194)
        transforms = {name: object() for name in ('left', 'right', 'body')}
        part = SimpleNamespace(weapon_role='main', parent_joint='left', local_transform=transforms['left'],
                               attachment_options=tuple((name, name, matrix) for name, matrix in transforms.items()))
        for frame, expected in ((0, 'left'), (10.99, 'left'), (11, 'right'), (20, 'right'),
                                (66.99, 'right'), (67, 'body'), (77, 'body'), (20, 'right'), (0, 'left')):
            self.assertEqual(lmt_weapon_attachment(part, motion, frame), (expected, transforms[expected]))
        self.assertEqual(lmt_attachment_state(replace(motion, name='greatsword_194'), 20), 'right')
        self.assertEqual(lmt_attachment_state(replace(motion, name='w08_194'), 20), 'right')

    def test_all_weapon_sheath_events_include_combined_values(self):
        from file_handlers.motion.preview.lmt_attachments import lmt_attachment_state, lmt_weapon_attachment
        from file_handlers.motion.lmt_codec import XX_WEAPONS
        hand, body, right = object(), object(), object()
        for family in XX_WEAPONS.values():
            path = XX_DIR/(family+'.lmt')
            doc = XX.parse(path.read_bytes(), label=str(path))
            motion = next(s.payload.value for s in doc.slots if s.motion_id == 195)
            remap, records = motion.events[0]
            frame = 0
            for mask, duration in records:
                if mask:
                    values = {remap[bit] for bit in range(32) if mask & (1 << bit)}
                    contains_sheath = 2 in values
                    with self.subTest(family=family, frame=frame):
                        hands = values & {1, 3, 5, 29}
                        expected = 'body' if contains_sheath else ('left' if hands == {3} else 'right'
                                   if hands and 3 not in hands else None)
                        self.assertEqual(lmt_attachment_state(motion, frame), expected)
                        for role in ('main', 'sub'):
                            part = SimpleNamespace(weapon_role=role, parent_joint='Hand', local_transform=hand,
                                                   attachment_options=(('body', 'Back', body), ('left', 'Hand', hand), ('right', 'RightHand', right)))
                            attachment = (('Back', body) if contains_sheath else ('RightHand', right)
                                          if expected == 'right' else ('Hand', hand)) if role == 'main' else ('Hand', hand)
                            self.assertEqual(lmt_weapon_attachment(part, motion, frame), attachment)
                frame += duration

    def test_longsword_sheath_stays_on_native_body_attachment(self):
        from file_handlers.motion.preview.lmt_attachments import lmt_weapon_attachment
        from file_handlers.motion.preview.mhr_attachments import weapon_hold_properties
        path = XX_DIR/'longsword_hunterart.lmt'
        doc = XX.parse(path.read_bytes(), label=str(path))
        motion = next(s.payload.value for s in doc.slots if s.motion_id == 3)
        rise = RISE.parse((CORPUS/'natives/STM/player/mot/plw_LongSword_100.motlist.528').read_bytes())
        hold = weapon_hold_properties(rise.slots[idle_template(rise)].payload.value)
        left, right, body = object(), object(), object()
        part = SimpleNamespace(weapon_role='sub', parent_joint='R_Weapon_00', local_transform=right,
                               attachment_options=(('left', 'L_Weapon_00', left), ('right', 'R_Weapon_00', right),
                                                   ('body', 'Spine_01', body)))
        for frame in (0, 40, 100, 109, 139, 40, 0):
            self.assertEqual(lmt_weapon_attachment(part, motion, frame, default_properties=hold.items()), ('Spine_01', body))

    def test_longsword_hunterart_hand_events_with_unknown_flags(self):
        from file_handlers.motion.preview.lmt_attachments import lmt_attachment_state
        from dataclasses import replace
        path = XX_DIR/'longsword_hunterart.lmt'
        doc = XX.parse(path.read_bytes(), label=str(path))
        motion = next(s.payload.value for s in doc.slots if s.motion_id == 3)
        for frame, expected in ((0, 'left'), (39.99, 'left'), (40, 'right'), (100, 'right'),
                                (108.99, 'right'), (109, 'body'), (139, 'body'), (100, 'right'), (0, 'left')):
            self.assertEqual(lmt_attachment_state(motion, frame), expected)
        conflict = replace(motion, events=(((1, 3, 10), ((7, 10),)),))
        self.assertIsNone(lmt_attachment_state(conflict, 0))

    def test_all_files_slots_channels_and_preserved_bytes(self):
        paths = sorted(XX_DIR.glob('*.lmt'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                raw = path.read_bytes()
                self.assertIs(require_motion_format(raw), XX)
                document = XX.parse(raw, label=str(path))
                count = struct.unpack_from('<H', raw, 6)[0]
                self.assertEqual(document.slot_count, count)
                occupied = [i for i in range(count) if struct.unpack_from('<I', raw, 8+i*4)[0]]
                self.assertEqual([slot.motion_id for slot in document.slots], occupied)
                for slot in document.slots:
                    self.assertIsNotNone(slot.payload)
                    motion = slot.payload.value
                    self.assertEqual(len(motion.events), 4)
                    for node in motion.animation_nodes:
                        for track in (node.translation, node.rotation, node.scale):
                            if track is None:
                                continue
                            self.assertTrue(np.isfinite(track.values).all())
                            self.assertEqual(track.frames[0], 0)
                            self.assertEqual(track.frames, sorted(track.frames))
                            if len(track.frames) > 1:
                                self.assertEqual(track.frames[-1], motion.end_frame)
                self.assertEqual(document.source, raw)
                with self.assertRaises(MotionWriteError):
                    XX.write(document)

    def test_standalone_lmt_uses_bundled_rig_and_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'player/mot/w00/w00.lmt'
            path.parent.mkdir(parents=True)
            path.write_bytes((XX_DIR/'greatsword.lmt').read_bytes())
            handler = MotListHandler()
            handler.filepath = str(path)
            handler.read(path.read_bytes())
            self.assertFalse(handler.supports_editing())
            self.assertEqual(Path(handler.model.skeleton_path), resource_path(XX_HUNTER_MOD, required=True))
            # An unrelated or damaged neighboring MOD must not override the bundle.
            (path.parent/'m_body001.mod').write_bytes(b'not a MOD')
            handler.read(path.read_bytes())
            self.assertEqual(Path(handler.model.skeleton_path), resource_path(XX_HUNTER_MOD, required=True))

    def test_frozen_resource_layout_and_explicit_override(self):
        raw = (XX_DIR/'greatsword.lmt').read_bytes()
        mod = resource_path(XX_HUNTER_MOD, required=True).read_bytes()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundled = root/XX_HUNTER_MOD
            bundled.parent.mkdir(parents=True)
            bundled.write_bytes(mod)
            with patch('sys.frozen', True, create=True), patch('sys.executable', str(root/'REasy.exe')):
                document = XX.parse(raw)
                self.assertEqual(Path(document.skeleton_path), bundled.resolve())
                explicit = XX.parse(raw, skeleton_data=mod)
                def joints(doc):
                    return [(j.name, j.parent.name if j.parent else None, j.translation, j.rotation)
                            for j in doc.slots[0].payload.value.skeleton.joints]
                self.assertEqual(joints(document), joints(explicit))
                self.assertIsNot(document.slots[0].payload.value.skeleton, explicit.slots[0].payload.value.skeleton)
                with self.assertRaises(MotionParseError):
                    XX.parse(raw, skeleton_data=b'bad explicit MOD')

    def test_bad_pointer_and_cyclic_skeleton_rejected(self):
        path = XX_DIR/'greatsword.lmt'
        b = bytearray(path.read_bytes())
        struct.pack_into('<I', b, 12, len(b)-10)
        with self.assertRaises(MotionParseError):
            XX.parse(b, label=str(path))
        b = bytearray(resource_path(XX_HUNTER_MOD, required=True).read_bytes())
        base = struct.unpack_from('<I', b, 40)[0]
        b[base+1] = 0
        with self.assertRaisesRegex(MotionParseError, 'cyclic'):
            read_mod_skeleton(b)

    def test_intro_is_not_replayed_on_partial_loop(self):
        path = XX_DIR/'greatsword.lmt'
        doc = XX.parse(path.read_bytes(), label=str(path))
        motion = next(s.payload.value for s in doc.slots if s.payload and s.payload.value.loop_start_frame > 0)
        def resolve(frame):
            return resolve_motion_frame(motion, frame, wrap_looping=True, default_wrap_looping=True)
        self.assertEqual(resolve(0), 0)
        self.assertEqual(resolve(motion.end_frame), motion.loop_start_frame)
        self.assertEqual(resolve(motion.end_frame+3), motion.loop_start_frame+3)


@unittest.skipUnless((XX_DIR/'greatsword.lmt').is_file() and RISE_PATH.is_file() and find_rise_installation(), 'XX/Rise assets absent')
class LmtRetargetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        p = XX_DIR/'greatsword.lmt'
        cls.doc = XX.parse(p.read_bytes(), label=str(p))
        cls.hold = RISE.parse(RISE_PATH.read_bytes())
        cls.assets = MhrPreviewAssets(preview_context(MotListHandler()))
        _, cls.rig = cls.assets.mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
        cls.motion = min((s.payload.value for s in cls.doc.slots if s.payload and not s.payload.value.looping and s.payload.value.end_frame > 1),
                         key=lambda m: m.end_frame)

    def evaluator(self, motion=None):
        motion = self.motion if motion is None else motion
        retarget = source_retarget(motion, self.hold)
        return retarget.evaluator(retarget.bind(motion, self.rig), PROFILE.sampling_policy,
                                  PROFILE.pose_composition_policy, PROFILE.joint_binding)

    def test_body_world_rotations_fixed_grip_and_weapon_defaults(self):
        hold = self.hold.slots[idle_template(self.hold)].payload.value
        native = MotionEvaluator(bind_motion(hold, self.rig, PROFILE.joint_binding), PROFILE.sampling_policy,
                                 PROFILE.pose_composition_policy).sample_frame(0)
        evaluator = self.evaluator()
        self.assertAlmostEqual(evaluator.scale, .01, places=8)
        for frame in (0, self.motion.end_frame*.37, self.motion.end_frame):
            source = evaluator.source.sample_frame(frame, wrap_looping=False)
            pose = evaluator.sample_frame(frame, wrap_looping=False)
            self.assertTrue(np.isfinite(pose.world_matrices).all())
            for i, joint in enumerate(self.rig.joints):
                if 'Finger_' in joint.name or 'Grip_' in joint.name or 'Weapon_' in joint.name:
                    self.assertEqual(pose.local_transforms[i], native.local_transforms[i])
                if joint.name in RISE_TO_XX:
                    si = evaluator.indices[i]
                    np.testing.assert_allclose(np.array(pose.world_matrices[i]).reshape(4, 4)[:3, :3],
                                               np.array(source.world_matrices[si]).reshape(4, 4)[:3, :3], atol=1e-6)
        self.assertNotEqual(evaluator.sample_frame(0).world_matrices, evaluator.sample_frame(self.motion.end_frame*.5).world_matrices)

    def test_export_reopen_matches_preview_and_native_ik(self):
        original = RISE.write(self.hold)
        motion_id = next_motion_id(self.hold)
        output = import_motion(self.hold, self.motion, self.rig, self.hold, motion_id)
        raw = RISE.write(output)
        reopened = RISE.parse(raw)
        self.assertEqual(RISE.write(reopened), raw)
        motion = next(s.payload.value for s in reopened.slots if s.motion_id == motion_id)
        self.assertLess(verify_imported_motion(self.motion, motion, self.rig, hold_document=self.hold), 2e-5)
        self.assertLess(verify_native_contract(reopened, motion_id, motion, self.rig), 4e-3)
        for node in motion.animation_nodes:
            if 'Finger_' in node.joint.name or 'Grip_' in node.joint.name:
                self.assertEqual(len(node.rotation.frames), 1)
        self.assertEqual(RISE.write(self.hold), original)

    def test_lmt_replace_preserves_other_clips_and_overrides(self):
        from dataclasses import replace, asdict
        from copy import deepcopy
        from file_handlers.motion.mhr_structure import copy_sequences
        from file_handlers.motion.lmt_import import verify_weapon_hold
        from file_handlers.motion.preview.mhr_attachments import weapon_hold_properties
        path = XX_DIR/'slashaxe.lmt'
        source = XX.parse(path.read_bytes(), label=str(path))
        motion = replace(next(s.payload.value for s in source.slots if s.motion_id == 122),
                         end_frame=40, raw_end_frame=40, looping=False)
        original = RISE.parse((CORPUS/'natives/STM/player/mot/plw_SlashAxe_100.motlist.528').read_bytes())
        before_motion = next(s.payload.value for s in original.slots if s.motion_id == 116)
        hold_sequence = next(i for i, seq in enumerate(before_motion.sequences)
                             if any(n.name.endswith('WeaponHold') for n in seq.clip.root.children))
        target = copy_sequences(original, 116, 116, positions=[hold_sequence], target_scope='override')
        before_bytes = RISE.write(target)
        result = import_motion(target, motion, self.rig, original, 116, replace_existing=True)
        raw = RISE.write(result)
        reopened = RISE.parse(raw)
        self.assertEqual(RISE.write(reopened), raw)
        before = next(s for s in target.slots if s.motion_id == 116)
        after = next(s for s in reopened.slots if s.motion_id == 116)
        verify_weapon_hold(motion, after.payload.value, original)
        override_motion = replace(after.payload.value, sequences=after.overrides)
        verify_weapon_hold(motion, override_motion, original)
        holds = weapon_hold_properties(after.payload.value)
        self.assertEqual([(k.frame, k.value) for k in holds['right'].keys], [(0, 1), (33, 2), (40, 2)])
        def without_hold(sequences):
            sequences = deepcopy(sequences)
            def prune(node):
                node.children = [n for n in node.children if not n.name.endswith('WeaponHold')]
                for child in node.children:
                    prune(child)
            for seq in sequences:
                prune(seq.clip.root)
            return repr([asdict(seq) for seq in sequences])
        self.assertEqual(without_hold(before.payload.value.sequences), without_hold(after.payload.value.sequences))
        self.assertEqual(without_hold(before.overrides), without_hold(after.overrides))
        self.assertEqual(RISE.write(target), before_bytes)
        self.assertLess(verify_imported_motion(motion, after.payload.value, self.rig, hold_document=original), 2e-5)
        self.assertLess(verify_native_contract(reopened, 116, after.payload.value, self.rig), 4e-3)

    def test_partial_loop_requires_explicit_range_for_rise_export(self):
        from file_handlers.motion.mhr_bake import MotionSegment
        from file_handlers.motion.wilds_bake import bake_wilds_segments
        motion = next(s.payload.value for s in self.doc.slots if s.payload.value.loop_start_frame > 0)
        slot_id = next(s.motion_id for s in self.doc.slots if s.payload.value is motion)
        motion_id = next_motion_id(self.hold)
        from file_handlers.motion.mhr_import import bake_evaluated_motion
        with self.assertRaisesRegex(MotionWriteError, 'loop starting after frame zero'):
            bake_evaluated_motion(motion, [], self.rig, self.hold)
        start = motion.loop_start_frame
        output, report = bake_wilds_segments(self.hold, self.doc, [MotionSegment(slot_id, start, start+2, 1)],
                                             self.rig, self.hold, motion_id, blend_frames=0)
        result = next(s.payload.value for s in output.slots if s.motion_id == motion_id)
        self.assertEqual(result.end_frame, 2)
        self.assertFalse(result.looping)
        self.assertLess(report['max_matrix_error'], 2e-5)


if __name__ == '__main__':
    unittest.main()
