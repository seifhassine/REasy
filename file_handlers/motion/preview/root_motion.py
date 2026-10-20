from __future__ import annotations

from collections.abc import Sequence

lazy import numpy as np

from ..evaluation.composition import compose_evaluated_pose
from ..evaluation.math3d import (
    inverse_quaternion,
    multiply_quaternions,
)
from ..evaluation.model import (
    ROTATION,
    TRANSLATION,
    EvaluatedPose,
    Quaternion,
    Rig,
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
    roots = np.flatnonzero(rig.parents < 0)
    local = pose.local.copy()
    if mode is RuntimeRootMotionMode.NONE:
        local[roots, TRANSLATION] = 0.0
        local[roots, ROTATION] = (0.0, 0.0, 0.0, 1.0)
    elif mode is RuntimeRootMotionMode.CONTINUANCE:
        if cycle_start is None or cycle_end is None:
            raise ValueError(
                "continuance root motion requires cycle endpoints"
            )
        for index in roots:
            current = local[index]
            start = cycle_start.local[index]
            end = cycle_end.local[index]
            undone = inverse_quaternion(start[ROTATION])
            cycle_rotation = multiply_quaternions(undone, end[ROTATION])
            partial_rotation = multiply_quaternions(undone, current[ROTATION])
            current[ROTATION] = multiply_quaternions(
                multiply_quaternions(
                    start[ROTATION],
                    _quaternion_power(cycle_rotation, completed_cycles),
                ),
                partial_rotation,
            )
            current[TRANSLATION] += completed_cycles * (end[TRANSLATION] - start[TRANSLATION])
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
