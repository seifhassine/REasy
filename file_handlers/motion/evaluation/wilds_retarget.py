"""Explicit Wilds hunter -> Rise hunter skeletal preview.

The two native hunter rigs share world and limb-local axes. MOT joint defaults
are an authored pose, not a mesh bind pose: subtracting them would turn the first
animation pose into the target T pose. Transfer anatomical world rotations and
solve target-local rotations, composing Wilds' extra Spine/Knee/Palm joints.
"""
import numpy as np

from .binding import bind_motion
from .composition import compose_evaluated_pose
from .math3d import decompose_row_srt, transform_matrix
from .model import BoundJoint, MotionRigBinding, SampledLocalPose, Transform
from .sampling import MotionEvaluator
from .source_adapter import rig_from_motion_skeleton


RISE_TO_WILDS = {
    'Root': 'root', 'Cog': 'COG', 'Waist_00': 'Hip',
    'Spine_00': 'Spine_0', 'Spine_01': 'Spine_2', 'Neck_00': 'Neck_1', 'Head_00': 'Head',
}
for _side in ('L', 'R'):
    for _target, _source in (
        ('Arm_00', 'Shoulder'), ('Arm_01', 'UpperArm'), ('Arm_02', 'Forearm'), ('Arm_03', 'Hand'),
        ('Leg_00', 'Thigh'), ('Leg_01', 'Shin'), ('Leg_02', 'Foot'), ('Leg_03', 'Toe'),
        ('Weapon_00', 'Wep'),
    ):
        RISE_TO_WILDS[f'{_side}_{_target}'] = f'{_side}_{_source}'
    for _start, _name in ((0, 'Thumb'), (3, 'IndexF'), (6, 'MiddleF')):
        for _i in range(3):
            RISE_TO_WILDS[f'{_side}_Finger_{_start+_i:02}'] = f'{_side}_{_name}{_i+1}'
# Rise's left palm is Finger_09; the right palm is Grip_00.
RISE_TO_WILDS.update({'L_Finger_09': 'L_Palm', 'R_Grip_00': 'R_Palm'})
for _side, _ring, _pinky in (('L', 10, 13), ('R', 9, 12)):
    for _start, _name in ((_ring, 'RingF'), (_pinky, 'PinkyF')):
        for _i in range(3):
            RISE_TO_WILDS[f'{_side}_Finger_{_start+_i:02}'] = f'{_side}_{_name}{_i+1}'


class WildsToRise:
    name = 'Wilds → Rise'

    def bind(self, motion, rig):
        source = {j.name: j for j in motion.skeleton.joints}
        target = {j.name for j in rig.joints}
        required = ('Root', 'Cog', 'Waist_00', 'Spine_00', 'Spine_01', 'Head_00',
                    'L_Arm_01', 'R_Arm_01', 'L_Leg_00', 'R_Leg_00')
        if not all(name in target and RISE_TO_WILDS[name] in source for name in required):
            raise ValueError('Wilds → Rise requires the native Wilds hunter and Rise hunter rigs')
        missing = {source_name for target_name, source_name in RISE_TO_WILDS.items()
                   if target_name in target and source_name not in source}
        if missing:
            raise ValueError('Missing Wilds retarget joints: ' + ', '.join(sorted(missing)))
        nodes = {id(node.joint): node for node in motion.animation_nodes}
        joints = []
        for index, joint in enumerate(rig.joints):
            name = RISE_TO_WILDS.get(joint.name)
            source_joint = source[name] if name is not None else None
            joints.append(BoundJoint(index, source_joint, nodes.get(id(source_joint))))
        return MotionRigBinding(rig, motion, tuple(joints), ())

    def evaluator(self, binding, sampling, composition, strategy):
        return RetargetEvaluator(binding, sampling, composition, strategy)


class RetargetEvaluator:
    def __init__(self, binding, sampling, composition, strategy):
        self.binding, self.motion = binding, binding.motion
        self.source_rig = rig_from_motion_skeleton(self.motion, scale=(1, 1, 1), joint_binding=strategy)
        self.source = MotionEvaluator(bind_motion(self.motion, self.source_rig, strategy), sampling, composition)
        self.time_invariant = self.source.time_invariant
        source_indices = {id(j): i for i, j in enumerate(self.motion.skeleton.joints)}
        self.source_indices = [source_indices.get(id(j.motion_joint)) for j in binding.joints]
        self.order = []
        visited = set()
        def visit(i):
            if i in visited:
                return
            parent = binding.rig.joints[i].parent_index
            if parent is not None:
                visit(parent)
            visited.add(i)
            self.order.append(i)
        for i in range(len(binding.joints)):
            visit(i)
        self.translation_scale = self._leg_length(binding.rig, 'L_Leg_03', 'Waist_00') / self._leg_length(self.source_rig, 'L_Toe', 'Hip')

    @staticmethod
    def _leg_length(rig, end_name, root_name):
        index = next(i for i, joint in enumerate(rig.joints) if joint.name == end_name)
        length = 0.0
        while rig.joints[index].name != root_name:
            joint = rig.joints[index]
            length += float(np.linalg.norm(joint.rest.translation))
            if joint.parent_index is None:
                raise ValueError('Retarget leg is not descended from the pelvis')
            index = joint.parent_index
        if length <= 0:
            raise ValueError('Retarget leg has zero length')
        return length

    def resolve_frame(self, frame, *, wrap_looping=None):
        return self.source.resolve_frame(frame, wrap_looping=wrap_looping)

    def sample_local_frame(self, frame, *, wrap_looping=None, apply_node_weights=True):
        pose = self.source.sample_frame(frame, wrap_looping=wrap_looping)
        rotations = {}
        for index in set(self.source_indices) - {None}:
            matrix = pose.world_matrices[index]
            rotation = decompose_row_srt(matrix).rotation
            rotations[index] = np.asarray(transform_matrix(Transform(rotation=rotation))).reshape(4, 4)[:3, :3]
        local = [j.rest for j in self.binding.rig.joints]
        world = [None] * len(local)
        for i in self.order:
            joint = self.binding.rig.joints[i]
            parent_rotation = np.eye(3) if joint.parent_index is None else world[joint.parent_index]
            source_index = self.source_indices[i]
            if source_index is not None:
                matrix = np.eye(4)
                matrix[:3, :3] = rotations[source_index] @ parent_rotation.T
                rotation = decompose_row_srt(matrix.ravel()).rotation
                translation, scale = joint.rest.translation, joint.rest.scale
                source_local = pose.local_transforms[source_index]
                weapon_joint = joint.name in ('L_Weapon_00', 'R_Weapon_00')
                if joint.name in ('Root', 'Cog') or weapon_joint:
                    translation = tuple(v*self.translation_scale for v in source_local.translation)
                if weapon_joint:
                    scale = source_local.scale
                local[i] = Transform(translation, rotation, scale)
            local_rotation = np.asarray(transform_matrix(Transform(rotation=local[i].rotation))).reshape(4, 4)[:3, :3]
            world[i] = local_rotation @ parent_rotation
        return SampledLocalPose(pose.frame, tuple(local), (1.0,) * len(local))

    def sample_frame(self, frame, *, wrap_looping=None):
        sampled = self.sample_local_frame(frame, wrap_looping=wrap_looping)
        return compose_evaluated_pose(self.binding.rig, sampled.frame, sampled.local_transforms, sampled.node_weights)


WILDS_TO_RISE = WildsToRise()
