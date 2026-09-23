"""60 FPS time mapping and per-segment transforms, including wp07 295/296."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

import numpy as np

from file_handlers.motion.errors import MotionWriteError
from file_handlers.motion.mhr_bake import MotionSegment
from file_handlers.motion.mhr_storage import MhrMotion
from file_handlers.motion.mot_list.model import MotList, MotionSlot, MotionSlotType, EmbeddedPayload
from file_handlers.motion.wilds_bake import segment_timeline
from file_handlers.motion.motion_root import root_transforms
from file_handlers.motion.evaluation.model import Transform
from file_handlers.motion.evaluation.math3d import transform_matrix


def matrix(transform):
    return np.asarray(transform_matrix(transform)).reshape(4, 4)


def source_document():
    return MotList('wp07', [MotionSlot(i, MotionSlotType.MOT, EmbeddedPayload(
        MhrMotion(str(i), end_frame=end, frames_per_second=60))) for i, end in ((295, 215), (296, 197))])


SEGMENTS = [MotionSegment(295, 38, 67, 1.6, 'identity'), MotionSegment(296, 30, 80, 1.6, 'relative'),
            MotionSegment(296, 80, None, 1, 'relative')]


class TimelineTests(unittest.TestCase):
    def test_constant_speed_with_integer_endpoints_and_shared_speed_boundary(self):
        source = source_document()
        fps, samples, ranges = segment_timeline(source, SEGMENTS)
        self.assertEqual(fps, 60)
        self.assertEqual(len(samples), 170)
        self.assertEqual([(r['output_start'], r['output_end']) for r in ranges], [(0, 19), (20, 52), (52, 169)])
        self.assertEqual([r['duration_rounding_frames'] for r in ranges], [.875, .75, 0])
        self.assertEqual([f for _, f in samples[:3]], [38, 39.6, 41.2])
        self.assertEqual([f for _, f in samples[17:22]], [65.2, 66.8, 67, 30, 31.6])
        self.assertEqual([f for _, f in samples[50:55]], [78, 79.6, 80, 81, 82])
        self.assertEqual([f for _, f in samples[53:]], list(range(81, 198)))
        self.assertEqual(sum(m is source.slots[1].payload.value and f == 80 for m, f in samples), 1)
        _, odd, _ = segment_timeline(source, [MotionSegment(295, 38, 61, 2)])
        self.assertEqual([f for _, f in odd], [*range(38, 61, 2), 61])

    def test_changing_root_policy_preserves_both_boundary_poses(self):
        _, samples, ranges = segment_timeline(source_document(), [MotionSegment(296, 0, 10, 1, 'relative'),
                                                                  MotionSegment(296, 10, 20, 1, 'identity')])
        self.assertEqual(len(samples), 22)
        self.assertEqual(ranges[1]['output_start'], 11)
        self.assertEqual([f for _, f in samples[10:12]], [10, 10])

    def test_invalid_segments_are_rejected(self):
        source = source_document()
        for segment in (MotionSegment(295, 0, 216), MotionSegment(295, -1, 10), MotionSegment(295, 5, 4),
                        MotionSegment(295, 0, 10, 0), MotionSegment(295, 0, 10, float('nan')),
                        MotionSegment(295, 0, 10, 1, 'unknown')):
            with self.subTest(segment=segment), self.assertRaises(MotionWriteError):
                segment_timeline(source, [segment])
        for scale in (-.1, float('nan'), float('inf'), True):
            with self.subTest(scale=scale), self.assertRaisesRegex(MotionWriteError, 'Root translation scale'):
                segment_timeline(source, [MotionSegment(295, 0, 10, root_translation_scale=scale)])


class RootTransformTests(unittest.TestCase):
    def test_translation_multiplier_preserves_rotation_scale_and_continues_at_full_rate(self):
        transforms = [Transform((f*f, 2*f, -f), (0, math.sin(f*.1), 0, math.cos(f*.1)),
                                (1+f*.1,)*3) for f in range(9)]
        ranges = [dict(motion_id=295, start=38, end=64, output_start=0, output_end=1, root_transform='identity'),
                  dict(motion_id=296, start=30, end=80, output_start=2, output_end=4, root_transform='relative'),
                  dict(motion_id=296, start=80, end=197, output_start=4, output_end=6, root_transform='relative'),
                  dict(motion_id=300, start=5, end=7, output_start=7, output_end=8, root_transform='relative')]
        baseline = root_transforms(transforms, ranges, 'relative')
        ranges[1]['root_translation_scale'] = .3
        scaled = root_transforms(transforms, ranges, 'relative')
        for frame in range(9):
            self.assertEqual(scaled[frame].rotation, baseline[frame].rotation)
            self.assertEqual(scaled[frame].scale, baseline[frame].scale)
            if frame <= 1:
                self.assertEqual(scaled[frame], Transform())
            else:
                expected = (np.array(baseline[frame].translation)*.3 if frame <= 4 else
                            np.array(baseline[frame].translation)-np.array(baseline[4].translation)*.7)
                np.testing.assert_allclose(scaled[frame].translation, expected, atol=1e-12)
        np.testing.assert_allclose(np.array(scaled[5].translation)-scaled[4].translation,
                                   np.array(baseline[5].translation)-baseline[4].translation, atol=1e-12)

    def test_identity_then_relative_translation_rotation_scale_and_speed_change(self):
        half = math.sqrt(.5)
        first = Transform((9, 8, 7), (0, half, 0, half), (2, 2, 2))
        start = Transform((10, 0, 20), (0, half, 0, half), (2, 2, 2))
        later = Transform((12, 0, 22), (0, 1, 0, 0), (3, 3, 3))
        end = Transform((14, 0, 24), (0, 1, 0, 0), (4, 4, 4))
        ranges = [dict(motion_id=295, start=38, end=61, output_start=0, output_end=1, root_transform='identity'),
                  dict(motion_id=296, start=30, end=80, output_start=2, output_end=3, root_transform='relative'),
                  dict(motion_id=296, start=80, end=197, output_start=3, output_end=4, root_transform='relative')]
        output = root_transforms([first, first, start, later, end], ranges, 'relative')
        for frame in (0, 1, 2):
            np.testing.assert_allclose(matrix(output[frame]), np.eye(4), atol=1e-12)
        np.testing.assert_allclose(matrix(output[3]), matrix(later) @ np.linalg.inv(matrix(start)), atol=1e-12)
        np.testing.assert_allclose(matrix(output[4]), matrix(end) @ np.linalg.inv(matrix(start)), atol=1e-12)
        np.testing.assert_allclose(output[4].scale, (2., 2., 2.), atol=1e-12, rtol=0)

    def test_relative_cuts_continue_previous_transform(self):
        a = Transform((10, 2, 3))
        b = Transform((12, 2, 3), (0, math.sqrt(.5), 0, math.sqrt(.5)))
        c = Transform((100, 0, -50), (0, 1, 0, 0))
        d = Transform((102, 0, -50), (0, 1, 0, 0))
        ranges = [dict(motion_id=1, start=0, end=1, output_start=0, output_end=1),
                  dict(motion_id=2, start=0, end=1, output_start=2, output_end=3)]
        output = root_transforms([a, b, c, d], ranges, 'relative')
        expected = matrix(b) @ np.linalg.inv(matrix(a))
        np.testing.assert_allclose(matrix(output[2]), expected, atol=1e-12)
        np.testing.assert_allclose(matrix(output[3]), matrix(d) @ np.linalg.inv(matrix(c)) @ expected, atol=1e-12)


CORPUS = Path(__file__).parent / 'TESTFILE'
RISE_PATH = CORPUS / 'natives/STM/player/mot/plw_GunLance_100.motlist.528'
WILDS_PATH = CORPUS / 'Weapon/Wp07/wp07_00/wp07_00.motlist.992'


@unittest.skipUnless(RISE_PATH.is_file() and WILDS_PATH.is_file(), 'wp07 native corpus unavailable')
class Wp07BakeTests(unittest.TestCase):
    def test_cli_bakes_requested_ranges_root_transforms_and_preserves_other_slots(self):
        from tools.cli.main import main
        from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
        from file_handlers.motion.wilds_codec import WILDS_MOTION_FORMAT_CODEC as WILDS
        from file_handlers.motion.mhr_editing import next_motion_id
        from file_handlers.motion.motlist_handler import MotListHandler
        from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context, find_rise_installation
        from file_handlers.motion.evaluation.wilds_retarget import wilds_to_rise
        from file_handlers.motion.evaluation.mhr import MHR_EVALUATION_PROFILE as PROFILE
        from file_handlers.motion.evaluation.binding import bind_motion
        from file_handlers.motion.evaluation.sampling import MotionEvaluator
        from tests.test_wilds_import import motion_value
        if not find_rise_installation():
            self.skipTest('Rise hunter rig unavailable')
        original, donor_bytes = RISE_PATH.read_bytes(), WILDS_PATH.read_bytes()
        target, donor = RISE.parse(original), WILDS.parse(donor_bytes)
        new_id = next_motion_id(target)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'plw_GunLance_100.motlist.528'
            args = ['motion', 'bake', str(RISE_PATH), '--donor', str(WILDS_PATH), '--hold-template', str(RISE_PATH),
                    '--target', str(new_id), '--segment', '295', '38', '67', '1.6', '--segment', '296', '30', '80', '1.6',
                    '--segment', '296', '80', 'end', '1', '--segment-root-transform', 'identity', 'relative', 'relative',
                    '-o', str(path), '--json']
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(args)
            self.assertEqual(code, 0, stderr.getvalue()+stdout.getvalue())
            details = json.loads(stdout.getvalue())['details']
            self.assertEqual((details['fps'], details['end_frame']), (60, 169))
            self.assertEqual(details['blend_frames'], 3)
            self.assertEqual(details['blend_windows'], [{'seam': 20, 'start': 17, 'end': 22}])
            self.assertLess(details['max_matrix_error'], 2e-5)
            self.assertLess(details['max_ik_goal_error'], 1e-4)
            output = RISE.parse(path.read_bytes())
            self.assertEqual(len(output.slots), len(target.slots)+1)
            after = {s.motion_id: s for s in output.slots}
            for slot in target.slots:
                actual = after[slot.motion_id]
                self.assertEqual(slot.tag_hash, actual.tag_hash)
                self.assertEqual([asdict(s) for s in slot.overrides], [asdict(s) for s in actual.overrides])
                if slot.payload:
                    self.assertEqual(motion_value(slot.payload.value), motion_value(actual.payload.value))
                else:
                    self.assertIsNone(actual.payload)
            assets = MhrPreviewAssets(preview_context(MotListHandler()))
            _, rig = assets.mesh('player/mod/m/bone/m_shadow.mesh.2109148288')
            motion = after[new_id].payload.value
            imported = MotionEvaluator(bind_motion(motion, rig, PROFILE.joint_binding),
                                       PROFILE.sampling_policy, PROFILE.pose_composition_policy)
            retarget = wilds_to_rise('gunlance')
            source_motion = next(s.payload.value for s in donor.slots if s.motion_id == 296)
            source = retarget.evaluator(retarget.bind(source_motion, rig), PROFILE.sampling_policy,
                                       PROFILE.pose_composition_policy, PROFILE.joint_binding)
            root = next(i for i, joint in enumerate(rig.joints) if joint.name == 'Root')
            for frame in range(21):
                actual = imported.sample_frame(frame, wrap_looping=False).local_transforms[root]
                np.testing.assert_allclose(matrix(actual), np.eye(4), atol=2e-6)
            origin = matrix(source.sample_frame(30, wrap_looping=False).local_transforms[root])
            for frame in range(21, 170):
                source_frame = min(30+(frame-20)*1.6, 80) if frame <= 52 else 80+frame-52
                expected = matrix(source.sample_frame(source_frame, wrap_looping=False).local_transforms[root]) @ np.linalg.inv(origin)
                actual = matrix(imported.sample_frame(frame, wrap_looping=False).local_transforms[root])
                np.testing.assert_allclose(actual, expected, atol=2e-6)
            first_motion = next(s.payload.value for s in donor.slots if s.motion_id == 295)
            first = retarget.evaluator(retarget.bind(first_motion, rig), PROFILE.sampling_policy,
                                      PROFILE.pose_composition_policy, PROFILE.joint_binding)
            waist = next(i for i, joint in enumerate(rig.joints) if joint.name == 'Waist_00')
            rotation_matrix = lambda value: matrix(Transform(rotation=value))
            reference = rotation_matrix(first.sample_frame(38, wrap_looping=False).local_transforms[waist].rotation)
            start = rotation_matrix(source.sample_frame(30, wrap_looping=False).local_transforms[waist].rotation)
            for frame in range(170):
                if frame <= 19:
                    expected = first.sample_frame(min(38+frame*1.6, 67), wrap_looping=False).local_transforms[waist]
                    expected_rotation = rotation_matrix(expected.rotation)
                else:
                    source_frame = min(30+(frame-20)*1.6, 80) if frame <= 52 else 80+frame-52
                    expected = source.sample_frame(source_frame, wrap_looping=False).local_transforms[waist]
                    expected_rotation = rotation_matrix(expected.rotation) @ np.linalg.inv(start) @ reference
                actual = imported.sample_frame(frame, wrap_looping=False).local_transforms[waist]
                np.testing.assert_allclose(actual.translation, expected.translation, atol=2e-6)
                np.testing.assert_allclose(actual.scale, expected.scale, atol=2e-6)
                if frame not in range(18, 22):
                    np.testing.assert_allclose(rotation_matrix(actual.rotation), expected_rotation, atol=2e-6)
            self.assertEqual(RISE.write(output), path.read_bytes())
            stdout = io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(io.StringIO()):
                self.assertEqual(main([*args, '--fps', '120']), 2)
            self.assertIn('unrecognized arguments', stdout.getvalue())
        self.assertEqual(RISE_PATH.read_bytes(), original)
        self.assertEqual(WILDS_PATH.read_bytes(), donor_bytes)
