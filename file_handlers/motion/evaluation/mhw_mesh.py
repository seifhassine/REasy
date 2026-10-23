"""Preview original MHW curves on a mesh exported with the same MOD3 rig."""
from dataclasses import replace

import numpy as np

from .binding import bind_motion
from .composition import compose_evaluated_pose
from .math3d import transform_matrix
from .model import BoundJoint, MotionRigBinding, SampledLocalPose
from .sampling import MotionEvaluator
from .source_adapter import rig_from_motion_skeleton


MHW_TO_RE_UNITS = 0.01


class MhwToMesh:
    def bind(self, motion, rig):
        source = rig_from_motion_skeleton(motion, scale=(1., 1., 1.))
        if len(source.joints) != len(rig.joints):
            raise ValueError('MHW mesh preview requires the complete exported MOD3 skeleton')
        for index, (original, target) in enumerate(zip(source.joints, rig.joints)):
            matrix = np.asarray(transform_matrix(original.rest)).reshape(4, 4).copy()
            matrix[3, :3] *= MHW_TO_RE_UNITS
            if (original.parent_index != target.parent_index or not np.allclose(
                    matrix, np.asarray(transform_matrix(target.rest)).reshape(4, 4),
                    rtol=1e-5, atol=2e-5)):
                raise ValueError(f'Mesh joint {index} ({target.name}) does not preserve '
                                 f'the MOD3 hierarchy/rest pose ({original.name}, cm to m)')
        nodes = {id(node.joint): node for node in motion.animation_nodes}
        return MotionRigBinding(rig, motion, tuple(
            BoundJoint(index, joint, nodes.get(id(joint)))
            for index, joint in enumerate(motion.skeleton.joints)))

    def evaluator(self, binding, sampling, composition, strategy):
        return MhwMeshEvaluator(binding, sampling, composition, strategy)


class MhwMeshEvaluator:
    def __init__(self, binding, sampling, composition, strategy):
        self.binding, self.motion = binding, binding.motion
        source = rig_from_motion_skeleton(self.motion, scale=(1., 1., 1.), joint_binding=strategy)
        self.source = MotionEvaluator(bind_motion(self.motion, source, strategy), sampling, composition)
        self.time_invariant = self.source.time_invariant

    def resolve_frame(self, frame, *, wrap_looping=None):
        return self.source.resolve_frame(frame, wrap_looping=wrap_looping)

    def sample_local_frame(self, frame, *, wrap_looping=None, apply_node_weights=True):
        pose = self.source.sample_local_frame(frame, wrap_looping=wrap_looping,
                                             apply_node_weights=apply_node_weights)
        local = tuple(replace(transform, translation=tuple(
            value * MHW_TO_RE_UNITS for value in transform.translation))
            for transform in pose.local_transforms)
        return SampledLocalPose(pose.frame, local, pose.node_weights)

    def sample_frame(self, frame, *, wrap_looping=None):
        pose = self.sample_local_frame(frame, wrap_looping=wrap_looping)
        return compose_evaluated_pose(self.binding.rig, pose.frame, pose.local_transforms, pose.node_weights)
