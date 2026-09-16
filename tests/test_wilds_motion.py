"""Strict MOT 932 channels, version isolation and explicit native-rig retargeting."""
import math
import os
from pathlib import Path
import struct
import unittest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import numpy as np
from file_handlers.motion.binary import ReadContext
from file_handlers.motion.errors import MotionParseError, MotionWriteError
from file_handlers.motion.format_registry import require_motion_format
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC
from file_handlers.motion.wilds_codec import WILDS_MOTION_FORMAT_CODEC
from file_handlers.motion.wilds_tracks import decode_values
from file_handlers.motion.mhr_tracks import decode_values as decode_495
from file_handlers.motion.mot.model import Joint, Skeleton, Motion, AnimationNode, KeyTrack, TrackFamily
from file_handlers.motion.evaluation.binding import bind_motion
from file_handlers.motion.evaluation.model import Rig, RigJoint, Transform
from file_handlers.motion.evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
from file_handlers.motion.evaluation.math3d import decompose_row_srt, transform_matrix
from file_handlers.motion.evaluation.sampling import MotionEvaluator
from file_handlers.motion.evaluation.source_adapter import rig_from_motion_skeleton
from file_handlers.motion.evaluation.wilds_retarget import WILDS_TO_RISE
from file_handlers.motion.preview.controller import MotionPreviewController
from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context, find_rise_installation
from file_handlers.motion.motlist_handler import MotListHandler


CORPUS = Path(__file__).parent/'TESTFILE/Weapon'


class ChannelTests(unittest.TestCase):
    def decode(self, mode, raw, parameters, family=TrackFamily.VECTOR3):
        data = struct.pack('<'+'f'*len(parameters), *parameters).ljust(64, b'\0') + raw
        return decode_values(ReadContext.from_bytes(data), 64, 1, family, mode, 0)[0]

    def test_new_three_component_vector_endianness(self):
        # MOT 932 vector-48 is a big-endian codeword.
        value = self.decode(0x60, ((65535 << 32) | 32768).to_bytes(6, 'big'), (2, 4, 6, 10, 20, 30))
        np.testing.assert_allclose(value, (10+2*32768/65535, 20, 36))
        data = struct.pack('<6f', 1, 1, 1, 0, 0, 0).ljust(64, b'\0') + bytes(6)
        with self.assertRaises(MotionParseError):
            decode_495(ReadContext.from_bytes(data), 64, 1, TrackFamily.VECTOR3, 0x60, 0)

    def test_sparse_vectors_keep_the_inactive_component(self):
        for width in range(2, 8):
            bits = width*4
            endian = 'little' if width in (2, 4) else 'big'
            for selector, expected in ((5, (12, 20, 30)), (6, (10, 22, 30)), (7, (10, 20, 32))):
                if width == 7 and selector == 5:
                    continue
                mode = width*16+selector
                with self.subTest(mode=hex(mode)):
                    raw = ((1 << bits)-1).to_bytes(width, endian)
                    np.testing.assert_allclose(self.decode(mode, raw, (2, 3, 10, 20, 30)), expected)
                    # Exercise the second packed component independently.
                    raw = (((1 << bits)-1) << bits).to_bytes(width, endian)
                    expected_second = {5: (10, 23, 30), 6: (10, 20, 33), 7: (13, 20, 30)}[selector]
                    np.testing.assert_allclose(self.decode(mode, raw, (2, 3, 10, 20, 30)), expected_second)
        for mode, expected in ((0x85, (7, 9, 30)), (0x86, (10, 7, 9)), (0x87, (9, 20, 7))):
            np.testing.assert_allclose(self.decode(mode, struct.pack('<2f', 7, 9), (10, 20, 30)), expected)

    def test_quaternion48_changes_byte_order_at_mot932(self):
        # Non-symmetric words exercise component order AND byte order. The
        # expected XYZW comes from the integer fields, independent of the reader.
        words = (0x1234, 0xA567, 0xD8EF)
        parameters = (.2, .4, .3, 0, -.1, -.2, -.15)
        xyz = tuple(word/65535*scale+bias for word, scale, bias in zip(words, parameters[:3], parameters[4:]))
        expected = (*xyz, math.sqrt(1-sum(v*v for v in xyz)))
        rise_bytes = struct.pack('<3H', *words)
        wilds_bytes = b''.join(word.to_bytes(2, 'big') for word in reversed(words))
        data = struct.pack('<7f', *parameters).ljust(64, b'\0') + rise_bytes
        np.testing.assert_allclose(decode_495(ReadContext.from_bytes(data), 64, 1,
                                           TrackFamily.QUATERNION, 0x60, 0)[0], expected, atol=1e-7)
        np.testing.assert_allclose(self.decode(0x60, wilds_bytes, parameters, TrackFamily.QUATERNION), expected, atol=1e-7)
        self.assertFalse(np.allclose(self.decode(0x60, rise_bytes, parameters, TrackFamily.QUATERNION), expected))

    def test_single_axis_and_uniform_values(self):
        code = 0x123456
        np.testing.assert_allclose(self.decode(0x32, code.to_bytes(3, 'big'), (2, 10, 20, 30)), (10, 20+2*code/0xFFFFFF, 30))
        np.testing.assert_allclose(self.decode(0x24, struct.pack('<H', 65535), (2, .5)), (2.5, 2.5, 2.5))

    def test_unknown_and_truncated_tracks_fail_instead_of_becoming_constant(self):
        with self.assertRaises(MotionParseError):
            self.decode(0xFF, bytes(8), (1,)*7)
        with self.assertRaises(MotionParseError):
            self.decode(0x57, bytes(4), (1,)*5)

    def test_native_sampling_does_not_subtract_authored_default_pose(self):
        joint = Joint('bone', rotation=(0, math.sin(.6), 0, math.cos(.6)))
        track = KeyTrack(TrackFamily.QUATERNION, [0], [(0, math.sin(.2), 0, math.cos(.2))])
        motion = Motion('native', skeleton=Skeleton([joint]), animation_nodes=[AnimationNode(joint, rotation=track)])
        rig = Rig([RigJoint('bone', rest=Transform(rotation=(math.sin(.4), 0, 0, math.cos(.4))))])
        evaluator = MotionEvaluator(bind_motion(motion, rig, PROFILE.joint_binding), PROFILE.sampling_policy, PROFILE.pose_composition_policy)
        np.testing.assert_allclose(evaluator.sample_frame(0).local_transforms[0].rotation, track.values[0])


@unittest.skipUnless(CORPUS.is_dir(), 'Wilds corpus absent')
class WildsCorpusTests(unittest.TestCase):
    def test_shared_root_motion_matches_native_full_precision_rise_track(self):
        wilds_path = CORPUS/'Wp03/wp03_00/wp03_00.motlist.992'
        rise_path = CORPUS.parent/'natives/STM/player/mot/plw_LongSword_100.motlist.528'
        if not wilds_path.exists() or not rise_path.exists():
            self.skipTest('Shared LongSword animation corpus absent')
        wilds = WILDS_MOTION_FORMAT_CODEC.parse(wilds_path.read_bytes())
        rise = MHR_MOTION_FORMAT_CODEC.parse(rise_path.read_bytes())
        def root_track(document, name, bone):
            motion = next(s.payload.value for s in document.slots if s.payload and s.payload.value.name == name)
            return next(n.translation for n in motion.animation_nodes if n.joint.name == bone)
        compressed = root_track(wilds, 'wp03_00_001', 'root')
        native = root_track(rise, 'plw_LongSword_100_002', 'Root')
        self.assertEqual(compressed.frames, native.frames)
        # These are the same authored root trajectory stored with different
        # precision. This oracle catches swapping forward motion into height.
        np.testing.assert_allclose(compressed.values, native.values, atol=1e-5, rtol=0)
        np.testing.assert_array_equal(np.asarray(compressed.values)[:, 1], 0)

    def test_all_native_weapon_motions_decode_without_degraded_tracks(self):
        paths = sorted(CORPUS.rglob('*_00.motlist.992'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(file=path.name):
                source = path.read_bytes()
                codec = require_motion_format(source)
                self.assertIs(codec, WILDS_MOTION_FORMAT_CODEC)
                self.assertFalse(MHR_MOTION_FORMAT_CODEC.matches(source))
                document = codec.parse(source, label=path.name)
                self.assertEqual(document.source, source)
                self.assertTrue(document.tracks)
                self.assertTrue(document.deferred_sections)
                for track in document.tracks:
                    self.assertTrue(np.isfinite(track.track.values).all())
                motion = next(s.payload.value for s in document.slots if s.payload)
                rig = rig_from_motion_skeleton(motion, scale=(1, 1, 1), joint_binding=PROFILE.joint_binding)
                binding = bind_motion(motion, rig, PROFILE.joint_binding)
                self.assertEqual(sum(j.animation_node is not None for j in binding.joints), len(motion.animation_nodes))
                evaluator = MotionEvaluator(binding, PROFILE.sampling_policy, PROFILE.pose_composition_policy)
                self.assertNotEqual(evaluator.sample_frame(0).world_matrices, evaluator.sample_frame(motion.end_frame/2).world_matrices)
                with self.assertRaisesRegex(MotionWriteError, 'preview-only'):
                    codec.write(document)
                with self.assertRaisesRegex(MotionParseError, 'expected MOTLIST 528'):
                    MHR_MOTION_FORMAT_CODEC.parse(source)
                self.assertEqual(path.read_bytes(), source)

    def test_motion_trees_stay_explicitly_unsupported(self):
        paths = sorted(CORPUS.rglob('*_tree.motlist.992'))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(file=path.name), self.assertRaisesRegex(MotionParseError, 'Motion Tree'):
                WILDS_MOTION_FORMAT_CODEC.parse(path.read_bytes(), label=path.name)

    @unittest.skipUnless(find_rise_installation(), 'Rise assets absent')
    def test_retarget_preserves_body_lengths_and_transfers_weapon_transforms(self):
        _, rig = MhrPreviewAssets(preview_context(MotListHandler())).mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
        # Exercise the shared hunter rig from each discovered weapon class.
        for path in sorted(CORPUS.rglob('*_00.motlist.992')):
            doc = WILDS_MOTION_FORMAT_CODEC.parse(path.read_bytes())
            motions = {id(s.payload.value): s.payload.value for s in doc.slots if s.payload}
            moving = [m for m in motions.values() if m.end_frame > 1]
            for motion in (moving[0], moving[len(moving)//2], moving[-1]):
                with self.subTest(file=path.name, motion=motion.name):
                    controller = MotionPreviewController(PROFILE)
                    self.assertTrue(controller.load(motion, rig, retarget=WILDS_TO_RISE))
                    evaluator = controller._evaluator
                    for frame in (0, motion.end_frame*.37, motion.end_frame):
                        source = evaluator.source.sample_frame(frame, wrap_looping=False)
                        result = evaluator.sample_frame(frame, wrap_looping=False)
                        for i, joint in enumerate(rig.joints):
                            si = evaluator.source_indices[i]
                            actual_local = result.local_transforms[i]
                            if joint.name in ('Root', 'Cog', 'L_Weapon_00', 'R_Weapon_00'):
                                np.testing.assert_allclose(actual_local.translation,
                                    np.asarray(source.local_transforms[si].translation)*evaluator.translation_scale)
                            else:
                                self.assertEqual(actual_local.translation, joint.rest.translation)
                            if joint.name in ('L_Weapon_00', 'R_Weapon_00'):
                                self.assertEqual(actual_local.scale, source.local_transforms[si].scale)
                                source_parent = evaluator.source_rig.joints[si].parent_index
                                target_parent = joint.parent_index
                                # The hand-relative attachment keeps the full authored
                                # SRT, with translation adapted to the hunter's size.
                                relative = np.asarray(result.world_matrices[i]).reshape(4, 4) @ np.linalg.inv(
                                    np.asarray(result.world_matrices[target_parent]).reshape(4, 4))
                                expected = np.asarray(source.world_matrices[si]).reshape(4, 4) @ np.linalg.inv(
                                    np.asarray(source.world_matrices[source_parent]).reshape(4, 4))
                                expected[3, :3] *= evaluator.translation_scale
                                np.testing.assert_allclose(relative, expected, atol=2e-6)
                            else:
                                self.assertEqual(actual_local.scale, joint.rest.scale)
                            if si is not None:
                                expected = decompose_row_srt(source.world_matrices[si]).rotation
                                actual = decompose_row_srt(result.world_matrices[i]).rotation
                                np.testing.assert_allclose(transform_matrix(Transform(rotation=actual)),
                                                           transform_matrix(Transform(rotation=expected)), atol=2e-6)
                    self.assertNotEqual(evaluator.sample_frame(0).world_matrices, evaluator.sample_frame(motion.end_frame/2).world_matrices)
                    # Switching back to the source rig must discard the retarget path.
                    self.assertTrue(controller.load(motion, evaluator.source_rig))
                    self.assertIsNone(controller._retarget)
                    self.assertEqual(sum(j.animation_node is not None for j in controller.binding.joints), len(motion.animation_nodes))


if __name__ == '__main__':
    unittest.main()
