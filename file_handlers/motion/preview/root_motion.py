from __future__ import annotations

from collections.abc import Sequence

from ..evaluation.composition import compose_evaluated_pose
from ..evaluation.math3d import (
    inverse_quaternion,
    multiply_quaternions,
)
from ..evaluation.model import (
    EvaluatedPose,
    Quaternion,
    Rig,
    Transform,
)
from ..runtime.model import RuntimeRootMotionMode


def apply_runtime_root_motion(
    pose: EvaluatedPose,
    rig: Rig,
    mode: RuntimeRootMotionMode | None,
    *,
    completed_cycles: int = 0,
    cycle_start: EvaluatedPose | None = None,
    cycle_end: EvaluatedPose | None = None,
) -> EvaluatedPose:
    """Project RE Engine root extraction back into one combined preview pose."""
    if mode in (
        None,
        RuntimeRootMotionMode.FIXED,
        RuntimeRootMotionMode.JOINT,
    ):
        return pose
    if (
        mode is RuntimeRootMotionMode.CONTINUANCE
        and completed_cycles == 0
    ):
        return pose
    roots = tuple(
        index
        for index, joint in enumerate(rig.joints)
        if joint.parent_index is None
    )
    local = list(pose.local_transforms)
    if mode is RuntimeRootMotionMode.NONE:
        for index in roots:
            local[index] = Transform(
                scale=local[index].scale,
            )
    elif mode is RuntimeRootMotionMode.CONTINUANCE:
        if completed_cycles:
            if cycle_start is None or cycle_end is None:
                raise ValueError(
                    "continuance root motion requires cycle endpoints"
                )
            for index in roots:
                current = local[index]
                start = cycle_start.local_transforms[index]
                end = cycle_end.local_transforms[index]
                cycle_rotation = multiply_quaternions(
                    inverse_quaternion(start.rotation),
                    end.rotation,
                )
                partial_rotation = multiply_quaternions(
                    inverse_quaternion(start.rotation),
                    current.rotation,
                )
                local[index] = Transform(
                    tuple(
                        value + completed_cycles * (stop - begin)
                        for value, begin, stop in zip(
                            current.translation,
                            start.translation,
                            end.translation,
                        )
                    ),
                    multiply_quaternions(
                        multiply_quaternions(
                            start.rotation,
                            _quaternion_power(
                                cycle_rotation,
                                completed_cycles,
                            ),
                        ),
                        partial_rotation,
                    ),
                    current.scale,
                )
    else:
        raise ValueError(
            f"{mode.label} root-motion evaluation is not established"
        )
    return compose_evaluated_pose(
        rig,
        pose.frame,
        local,
        pose.node_weights,
    )


def _quaternion_power(
    value: Sequence[float],
    exponent: int,
) -> Quaternion:
    if exponent < 0:
        raise ValueError("quaternion exponent must be nonnegative")
    result = (0.0, 0.0, 0.0, 1.0)
    factor = value
    while exponent:
        if exponent & 1:
            result = multiply_quaternions(result, factor)
        factor = multiply_quaternions(factor, factor)
        exponent >>= 1
    return result
