"""Explicit Wilds hunter -> Rise hunter skeletal preview.

The two native hunter rigs share world and limb-local axes. MOT joint defaults
are an authored pose, not a mesh bind pose: subtracting them would turn the first
animation pose into the target T pose. Transfer anatomical world rotations and
solve target-local rotations, composing Wilds' extra Spine/Knee/Palm joints.

Weapon carriers are family specific (:mod:`file_handlers.motion.wilds_weapons`).
A shielded model's Rise part hangs off ``R_Weapon_00`` while its Wilds driver is
``R_Shield``, a child of the forearm: that carrier reproduces the source *world*
transform instead of the source local translation, so the shield stays on the arm.
"""
from functools import lru_cache

import numpy as np

from ..wilds_weapons import weapon_attach
from .binding import bind_motion
from .composition import compose_evaluated_pose
from .math3d import decompose_row_srt, transform_matrix
from .model import BoundJoint, MotionRigBinding, SampledLocalPose, Transform
from .sampling import MotionEvaluator
from .source_adapter import rig_from_motion_skeleton


# Rise hunter joints that carry a weapon part; the Wilds driver is per family.
WEAPON_CARRIERS = ('L_Weapon_00', 'R_Weapon_00')
_IDENTITY_ROTATION = (0.0, 0.0, 0.0, 1.0)
_MINIMUM_SCALE = 1e-6


def decompose_carried_srt(matrix):
    """Decompose a carried row-vector SRT, tolerating a hidden (zero-scale) part.

    Wilds hides a stowed weapon part by animating its scale to zero, and the
    strict preview helper rejects those matrices. A hidden axis keeps the rig axis
    as its rotation and the authored zero as its scale instead of failing the
    whole motion.
    """
    values = np.asarray(matrix, dtype=np.float64).reshape(4, 4)
    rows = values[:3, :3].copy()
    scale = np.linalg.norm(rows, axis=1)
    hidden = scale <= _MINIMUM_SCALE
    rows[hidden] = np.eye(3)[hidden]
    safe = np.eye(4)
    safe[:3, :3] = rows
    safe[3, :3] = values[3, :3]
    try:
        rotation = decompose_row_srt(safe.ravel()).rotation
    except ValueError:
        # A reflected carry has no native counterpart; keep the pose evaluable.
        rotation = _IDENTITY_ROTATION
    return Transform(tuple(float(value) for value in values[3, :3]), rotation,
                     tuple(float(value) for value in scale))


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

    def __init__(self, family=None):
        self.family = family
        self.attach = weapon_attach(family)
        self.mapping = {**RISE_TO_WILDS, **self.attach}

    def bind(self, motion, rig):
        source = {j.name: j for j in motion.skeleton.joints}
        target = {j.name for j in rig.joints}
        required = ('Root', 'Cog', 'Waist_00', 'Spine_00', 'Spine_01', 'Head_00',
                    'L_Arm_01', 'R_Arm_01', 'L_Leg_00', 'R_Leg_00')
        if not all(name in target and self.mapping.get(name) in source for name in required):
            raise ValueError('Wilds → Rise requires the native Wilds hunter and Rise hunter rigs')
        missing = {source_name for target_name, source_name in self.mapping.items()
                   if target_name in target and source_name not in source}
        if missing:
            raise ValueError('Missing Wilds retarget joints: ' + ', '.join(sorted(missing)))
        nodes = {id(node.joint): node for node in motion.animation_nodes}
        joints = []
        for index, joint in enumerate(rig.joints):
            name = self.mapping.get(joint.name)
            source_joint = source[name] if name is not None else None
            joints.append(BoundJoint(index, source_joint, nodes.get(id(source_joint))))
        return MotionRigBinding(rig, motion, tuple(joints), ())

    def evaluator(self, binding, sampling, composition, strategy):
        return RetargetEvaluator(binding, sampling, composition, strategy, mapping=self.mapping)


class RetargetEvaluator:
    def __init__(self, binding, sampling, composition, strategy, mapping=None):
        self.binding, self.motion = binding, binding.motion
        self.mapping = dict(RISE_TO_WILDS if mapping is None else mapping)
        self.source_rig = rig_from_motion_skeleton(self.motion, scale=(1, 1, 1), joint_binding=strategy)
        self.source = MotionEvaluator(bind_motion(self.motion, self.source_rig, strategy), sampling, composition)
        self.time_invariant = self.source.time_invariant
        source_indices = {id(j): i for i, j in enumerate(self.motion.skeleton.joints)}
        self.source_indices = [source_indices.get(id(j.motion_joint)) for j in binding.joints]
        self.world_carried = frozenset(self._world_carried_carriers())
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

    def _world_carried_carriers(self):
        """Weapon carriers whose Wilds driver hangs off a different parent.

        ``R_Weapon_00`` is a hand child while the shield it must carry for a
        shielded family (``R_Shield``) is a forearm child, so composing the source
        local transform would push the shield past the wrist. Those carriers
        reproduce the source world transform instead; every other joint keeps the
        measured local-transfer rule (Rise's palms are finger children while the
        Wilds palms hang off the hand, for instance).
        """
        carried = set()
        for index, bound in enumerate(self.binding.joints):
            if self.binding.rig.joints[index].name not in WEAPON_CARRIERS:
                continue
            source_joint = bound.motion_joint
            if source_joint is None or source_joint.parent is None:
                continue
            parent_index = self.binding.rig.joints[index].parent_index
            if parent_index is None:
                continue
            parent_source = self.mapping.get(self.binding.rig.joints[parent_index].name)
            if parent_source is not None and source_joint.parent.name != parent_source:
                carried.add(index)
        return carried

    def _carried_local(self, pose, source_index, parent_world):
        """Local transform reproducing a source joint carried by another parent."""
        source_world = np.asarray(pose.world_matrices[source_index], dtype=np.float64).reshape(4, 4).copy()
        source_world[3, :3] *= self.translation_scale
        parent = np.eye(4) if parent_world is None else parent_world
        return decompose_carried_srt(source_world @ np.linalg.inv(parent))

    def _weapon_carrier_local(self, pose, source_index, joint, *, carried, parent_world):
        """Local transform for a weapon carrier (``L_Weapon_00`` / ``R_Weapon_00``).

        Carriers keep the source translation (scaled) and rotation, but **never the
        source scale**: a Wilds rig stretches its weapon/shield joints for its own
        geometry (``wp07`` stores ``L_Wep``/``R_Shield`` = ``(1, 2, 1)``), and
        writing that value onto Rise's carrier renders the weapon fat. The Rise
        joint's rest scale is the correct local scale.
        """
        scale = tuple(float(value) for value in joint.rest.scale)
        if carried:
            # the carrier hangs off another parent in the source rig, so reproduce
            # the world transform relative to the Rise parent and keep its rotation
            source_world = np.asarray(pose.world_matrices[source_index], dtype=np.float64).reshape(4, 4).copy()
            source_world[3, :3] *= self.translation_scale
            parent = np.eye(4) if parent_world is None else parent_world
            carried_srt = decompose_carried_srt(source_world @ np.linalg.inv(parent))
            return Transform(carried_srt.translation, carried_srt.rotation, scale)
        parent_rotation = np.eye(3) if parent_world is None else parent_world[:3, :3]
        world_rotation = decompose_carried_srt(pose.world_matrices[source_index]).rotation
        matrix = np.eye(4)
        matrix[:3, :3] = np.asarray(transform_matrix(Transform(rotation=world_rotation))).reshape(4, 4)[:3, :3] @ parent_rotation.T
        rotation = decompose_row_srt(matrix.ravel()).rotation
        source_local = pose.local_transforms[source_index]
        translation = tuple(v*self.translation_scale for v in source_local.translation)
        return Transform(translation, rotation, scale)

    def resolve_frame(self, frame, *, wrap_looping=None):
        return self.source.resolve_frame(frame, wrap_looping=wrap_looping)

    def sample_local_frame(self, frame, *, wrap_looping=None, apply_node_weights=True):
        pose = self.source.sample_frame(frame, wrap_looping=wrap_looping)
        rotations = {}
        for index in set(self.source_indices) - {None}:
            rotation = decompose_carried_srt(pose.world_matrices[index]).rotation
            rotations[index] = np.asarray(transform_matrix(Transform(rotation=rotation))).reshape(4, 4)[:3, :3]
        local = [j.rest for j in self.binding.rig.joints]
        world = [None] * len(local)
        for i in self.order:
            joint = self.binding.rig.joints[i]
            parent_world = None if joint.parent_index is None else world[joint.parent_index]
            parent_rotation = np.eye(3) if parent_world is None else parent_world[:3, :3]
            source_index = self.source_indices[i]
            if source_index is not None:
                if joint.name in WEAPON_CARRIERS:
                    local[i] = self._weapon_carrier_local(pose, source_index, joint,
                                                          carried=i in self.world_carried,
                                                          parent_world=parent_world)
                elif i in self.world_carried:
                    local[i] = self._carried_local(pose, source_index, parent_world)
                else:
                    matrix = np.eye(4)
                    matrix[:3, :3] = rotations[source_index] @ parent_rotation.T
                    rotation = decompose_row_srt(matrix.ravel()).rotation
                    translation, scale = joint.rest.translation, joint.rest.scale
                    source_local = pose.local_transforms[source_index]
                    if joint.name in ('Root', 'Cog'):
                        translation = tuple(v*self.translation_scale for v in source_local.translation)
                    local[i] = Transform(translation, rotation, scale)
            local_matrix = np.asarray(transform_matrix(local[i]), dtype=np.float64).reshape(4, 4)
            world[i] = local_matrix if parent_world is None else local_matrix @ parent_world
        return SampledLocalPose(pose.frame, tuple(local), (1.0,) * len(local))

    def sample_frame(self, frame, *, wrap_looping=None):
        sampled = self.sample_local_frame(frame, wrap_looping=wrap_looping)
        return compose_evaluated_pose(self.binding.rig, sampled.frame, sampled.local_transforms, sampled.node_weights)


WILDS_TO_RISE = WildsToRise()


@lru_cache(maxsize=None)
def wilds_to_rise(family=None):
    """Return the retarget for one Wilds/Rise weapon family.

    ``family`` selects the weapon carriers (:data:`WILDS_WEAPON_ATTACH`): shield
    models drive ``R_Weapon_00`` from ``R_Shield``, everything else from the hand.
    """
    return WildsToRise(family)
