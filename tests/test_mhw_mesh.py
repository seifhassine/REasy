from dataclasses import replace
import unittest

import numpy as np

from file_handlers.motion.evaluation.mhw_mesh import MhwToMesh
from file_handlers.motion.evaluation.model import Rig
from file_handlers.motion.evaluation.source_adapter import rig_from_motion_skeleton
from file_handlers.motion.preview.controller import MotionPreviewController
from file_handlers.motion.preview.support_registry import MHW_LMT_MOTION_PREVIEW_SUPPORT
from file_handlers.motion.mhw_lmt_codec import MHW_LMT_MOTION_FORMAT_CODEC
from test_mhw_lmt import lmt, mod3


class MhwMeshTests(unittest.TestCase):
    def setUp(self):
        self.motion = MHW_LMT_MOTION_FORMAT_CODEC.parse(lmt(), skeleton_data=mod3()).slots[0].payload.value
        self.original_values = tuple(self.motion.animation_nodes[1].translation.values)
        self.source = rig_from_motion_skeleton(self.motion, scale=(1., 1., 1.))
        self.target = Rig([replace(joint, name=f'Exported_{i}', rest=replace(joint.rest,
                           translation=tuple(v*.01 for v in joint.rest.translation)))
                           for i, joint in enumerate(self.source.joints)])

    def test_renamed_mesh_preserves_original_curves_weights_and_units(self):
        profile = MHW_LMT_MOTION_PREVIEW_SUPPORT.evaluation
        original, exported = MotionPreviewController(profile), MotionPreviewController(profile)
        self.assertTrue(original.load(self.motion, self.source))
        self.assertTrue(exported.load(self.motion, self.target, retarget=MhwToMesh()))
        for frame in (0, .25, 1, 8, 30):
            original.set_frame(frame)
            exported.set_frame(frame)
            a, b = original.sample().pose, exported.sample().pose
            expected = np.array(a.world_matrices).reshape(-1, 4, 4)
            expected[:, 3, :3] *= .01
            np.testing.assert_allclose(np.array(b.world_matrices).reshape(-1, 4, 4), expected, atol=1e-8)
            self.assertEqual(a.node_weights, b.node_weights)
        np.testing.assert_allclose(self.motion.skeleton.joints[1].translation, (10, 0, 0))
        self.assertEqual(tuple(self.motion.animation_nodes[1].translation.values), self.original_values)
        exported.set_frame(1)
        np.testing.assert_allclose(exported.sample().pose.local_transforms[0].translation, (.01, .02, .03))

    def test_unrelated_mesh_cannot_bind_by_array_index(self):
        with self.assertRaisesRegex(ValueError, 'complete exported MOD3'):
            MhwToMesh().bind(self.motion, Rig(self.target.joints[:-1]))
        for changed in (replace(self.target.joints[2], parent_index=0),
                        replace(self.target.joints[2], rest=replace(self.target.joints[2].rest,
                                                                  translation=(0, .3, 0)))):
            with self.assertRaisesRegex(ValueError, 'hierarchy/rest pose'):
                MhwToMesh().bind(self.motion, Rig([*self.target.joints[:2], changed]))


if __name__ == '__main__':
    unittest.main()
