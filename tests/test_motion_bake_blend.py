import math
import unittest

import numpy as np

from file_handlers.motion.errors import MotionWriteError
from file_handlers.motion.motion_bake_blend import blend_poses, seam_windows
from file_handlers.motion.evaluation.composition import compose_evaluated_pose
from file_handlers.motion.evaluation.math3d import transform_matrix, multiply_quaternions
from file_handlers.motion.evaluation.model import Rig, RigJoint, Transform


def yaw(angle):
    return (0., math.sin(angle/2), 0., math.cos(angle/2))


def rotation_matrix(q):
    return np.asarray(transform_matrix(Transform(rotation=q))).reshape(4, 4)


class BlendTests(unittest.TestCase):
    def setUp(self):
        self.ranges = [dict(output_start=0, output_end=5, join='adjacent'),
                       dict(output_start=6, output_end=11, join='adjacent'),
                       dict(output_start=11, output_end=14, join='continuous')]
        self.rig = Rig([RigJoint('Root'), RigJoint('Waist_00', 0), RigJoint('Arm', 1)])
        self.poses = [compose_evaluated_pose(self.rig, frame, (
            Transform((frame, 0, 0), yaw(.1*frame)),
            Transform((frame, frame*2, 1), multiply_quaternions(yaw(.4+.15*frame),
                      (math.sin(.12*frame), 0., 0., math.cos(.12*frame))), (1+frame*.01, 1, 1)),
            Transform((frame if frame < 6 else 100+frame, 0, 0), yaw(.2*frame))), (1., 1., 1.))
            for frame in range(15)]

    def test_waist_alignment_changes_only_rotation_and_keeps_speed_boundary(self):
        output, windows = blend_poses(self.poses, self.rig, self.ranges, blend_frames=0)
        self.assertEqual(windows, [])
        reference = rotation_matrix(self.poses[0].local_transforms[1].rotation)
        origin = rotation_matrix(self.poses[6].local_transforms[1].rotation)
        for frame in range(15):
            old, new = self.poses[frame].local_transforms, output[frame].local_transforms
            self.assertEqual((old[1].translation, old[1].scale), (new[1].translation, new[1].scale))
            self.assertEqual(old[0], new[0])
            self.assertEqual(old[2], new[2])
            expected = rotation_matrix(old[1].rotation)
            if frame >= 6:
                expected = expected @ np.linalg.inv(origin) @ reference
            np.testing.assert_allclose(rotation_matrix(new[1].rotation), expected, atol=1e-12)
        np.testing.assert_allclose(rotation_matrix(output[6].local_transforms[1].rotation), reference, atol=1e-12)

    def test_six_frame_seam_preserves_length_outer_frames_root_and_waist_trs(self):
        aligned, _ = blend_poses(self.poses, self.rig, self.ranges, blend_frames=0)
        output, windows = blend_poses(self.poses, self.rig, self.ranges, blend_frames=3)
        self.assertEqual(windows, [{'seam': 6, 'start': 3, 'end': 8}])
        self.assertEqual(len(output), 15)
        for frame in range(15):
            old, new = self.poses[frame].local_transforms, output[frame].local_transforms
            self.assertEqual(old[0], new[0])
            self.assertEqual(old[1].translation, new[1].translation)
            self.assertEqual(old[1].scale, new[1].scale)
            if frame not in range(4, 8):
                self.assertEqual(aligned[frame].local_transforms, output[frame].local_transforms)
        self.assertAlmostEqual(output[4].local_transforms[2].translation[0], 3+(108-3)*.104)
        self.assertNotEqual(output[5].local_transforms[2], self.poses[5].local_transforms[2])

    def test_missing_waist_and_overlapping_or_invalid_windows_fail(self):
        for frames in (-1, True, 1.5):
            with self.assertRaises(MotionWriteError):
                seam_windows(self.ranges, frames)
        ranges = [dict(output_start=i, output_end=i+1, join='adjacent') for i in (0, 2, 4)]
        with self.assertRaisesRegex(MotionWriteError, 'overlap'):
            seam_windows(ranges, 3)
        rig = Rig([RigJoint('Root'), RigJoint('Pelvis', 0), RigJoint('Arm', 1)])
        with self.assertRaisesRegex(MotionWriteError, 'Waist_00'):
            blend_poses(self.poses, rig, self.ranges)
