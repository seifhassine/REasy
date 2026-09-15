from __future__ import annotations

import math

from ..mot.model import Motion
from .model import Rig, RigJoint, Transform, Vector3


def rig_from_motion_skeleton(motion: Motion, *, scale: Vector3) -> Rig:
    """Create an explicit standalone preview rig from MOT joint defaults.

    MOT v65 has no joint default scale, so callers must choose a scale rather
    than receiving an implicit fallback. Non-finite defaults remain
    unevaluable without an external target rig and fail clearly.
    """
    if motion.skeleton is None or not motion.skeleton.joints:
        raise ValueError("motion has no source skeleton")
    if len(scale) != 3 or any(
        not math.isfinite(value) or not value for value in scale
    ):
        raise ValueError("source-rig scale must contain three finite nonzero values")

    joints = motion.skeleton.joints
    indices = {id(joint): index for index, joint in enumerate(joints)}
    result: list[RigJoint] = []
    for joint in joints:
        if not all(math.isfinite(value) for value in joint.translation):
            raise ValueError(
                f"motion joint {joint.name!r} has no finite default translation; load a target mesh"
            )
        if not all(math.isfinite(value) for value in joint.rotation):
            raise ValueError(
                f"motion joint {joint.name!r} has no finite default rotation; load a target mesh"
            )
        parent_index = None
        if joint.parent is not None:
            parent_index = indices.get(id(joint.parent))
            if parent_index is None:
                raise ValueError(f"motion joint {joint.name!r} has a parent outside its skeleton")
        result.append(RigJoint(
            name=joint.name,
            parent_index=parent_index,
            rest=Transform(joint.translation, joint.rotation, scale),
        ))
    return Rig(result)
