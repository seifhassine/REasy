"""XX hunter -> Rise, retaining a native fixed grip and default weapon carriers."""
from dataclasses import replace
import numpy as np

from .binding import bind_motion
from .composition import compose_evaluated_pose
from .math3d import decompose_row_srt, transform_matrix
from .model import BoundJoint, MotionRigBinding, SampledLocalPose, Transform
from .sampling import MotionEvaluator, RotationInterpolation
from .source_adapter import rig_from_motion_skeleton


RISE_TO_XX = {'Root': 'XX_Root', 'Waist_00': 'XX_0', 'Spine_00': 'XX_1',
              'Spine_01': 'XX_2', 'Neck_00': 'XX_3', 'Head_00': 'XX_4'}
for side, arm, leg in (('L', 5, 14), ('R', 9, 17)):
    RISE_TO_XX.update({f'{side}_Arm_{i:02}': f'XX_{arm+i}' for i in range(4)})
    RISE_TO_XX.update({f'{side}_Leg_{i:02}': f'XX_{leg+i}' for i in range(3)})


class LmtToRise:
    name = 'XX → Rise'

    def __init__(self, hold_motion):
        self.hold_motion = hold_motion

    def bind(self, motion, rig):
        source = {j.name: j for j in motion.skeleton.joints}
        target = {j.name for j in rig.joints}
        if not set(RISE_TO_XX.values()).issubset(source) or not set(RISE_TO_XX).issubset(target) or 'Cog' not in target:
            raise ValueError('XX → Rise requires the XX hunter MOD and Rise hunter rig')
        nodes = {id(n.joint): n for n in motion.animation_nodes}
        return MotionRigBinding(rig, motion, tuple(
            BoundJoint(i, source.get(RISE_TO_XX.get(j.name)), nodes.get(id(source.get(RISE_TO_XX.get(j.name)))))
            for i, j in enumerate(rig.joints)), ())

    def evaluator(self, binding, sampling, composition, strategy):
        return LmtRetargetEvaluator(binding, self.hold_motion, sampling, composition, strategy)


class LmtRetargetEvaluator:
    def __init__(self, binding, hold_motion, sampling, composition, strategy):
        self.binding, self.motion = binding, binding.motion
        source_rig = rig_from_motion_skeleton(self.motion, scale=(1, 1, 1), joint_binding=strategy)
        self.source = MotionEvaluator(bind_motion(self.motion, source_rig, strategy),
                                      replace(sampling, rotation_interpolation=RotationInterpolation.SHORTEST_SLERP), composition)
        self.time_invariant = self.source.time_invariant
        native = MotionEvaluator(bind_motion(hold_motion, binding.rig, strategy), sampling, composition).sample_frame(0)
        self.defaults = [native.local_transforms[i] if ('Finger_' in j.name or 'Grip_' in j.name or 'Weapon_' in j.name)
                         else j.rest for i, j in enumerate(binding.rig.joints)]
        source_indices = {id(j): i for i, j in enumerate(self.motion.skeleton.joints)}
        self.indices = [source_indices.get(id(j.motion_joint)) for j in binding.joints]
        self.pelvis = next(i for i, j in enumerate(source_rig.joints) if j.name == 'XX_0')
        def leg_length(rig, names):
            return sum(float(np.linalg.norm(j.rest.translation)) for j in rig.joints if j.name in names)
        self.scale = leg_length(binding.rig, ('L_Leg_00', 'L_Leg_01', 'L_Leg_02')) / leg_length(source_rig, ('XX_14', 'XX_15', 'XX_16'))
        self.order, seen = [], set()
        def visit(i):
            if i in seen:
                return
            parent = binding.rig.joints[i].parent_index
            if parent is not None:
                visit(parent)
            seen.add(i)
            self.order.append(i)
        for i in range(len(binding.joints)):
            visit(i)

    def resolve_frame(self, frame, *, wrap_looping=None):
        return self.source.resolve_frame(frame, wrap_looping=wrap_looping)

    def sample_local_frame(self, frame, *, wrap_looping=None, apply_node_weights=True):
        pose = self.source.sample_frame(frame, wrap_looping=wrap_looping)
        local, world = list(self.defaults), [None]*len(self.defaults)
        for i in self.order:
            joint = self.binding.rig.joints[i]
            parent = np.eye(4) if joint.parent_index is None else world[joint.parent_index]
            source_index = self.indices[i]
            if joint.name == 'Cog':
                local[i] = Transform(tuple(v*self.scale for v in pose.local_transforms[self.pelvis].translation))
            elif source_index is not None:
                source_world = np.asarray(pose.world_matrices[source_index]).reshape(4, 4)
                rotation = np.eye(4)
                rotation[:3, :3] = source_world[:3, :3] @ parent[:3, :3].T
                translation = (tuple(v*self.scale for v in pose.local_transforms[source_index].translation)
                               if joint.name == 'Root' else joint.rest.translation)
                local[i] = Transform(translation, decompose_row_srt(rotation.ravel()).rotation, joint.rest.scale)
            world[i] = np.asarray(transform_matrix(local[i])).reshape(4, 4) @ parent
        return SampledLocalPose(pose.frame, tuple(local), (1.,)*len(local))

    def sample_frame(self, frame, *, wrap_looping=None):
        sampled = self.sample_local_frame(frame, wrap_looping=wrap_looping)
        return compose_evaluated_pose(self.binding.rig, sampled.frame, sampled.local_transforms, sampled.node_weights)
