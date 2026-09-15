from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from .math3d import compose_world_matrices
from .model import EvaluatedPose, Matrix4, Rig, Transform


def compose_evaluated_pose(
    rig: Rig,
    frame: float,
    local_transforms: Sequence[Transform],
    node_weights: Sequence[float],
) -> EvaluatedPose:
    """Build hierarchy and skinning outputs from target-rig local transforms."""

    local = tuple(local_transforms)
    weights = tuple(node_weights)
    if len(local) != len(rig.joints) or len(weights) != len(rig.joints):
        raise ValueError("pose component counts must match the target rig")
    parents = tuple(joint.parent_index for joint in rig.joints)
    world = compose_world_matrices(local, parents)
    skin: list[Matrix4 | None] = [None] * len(rig.joints)
    skinned = tuple(
        index
        for index, joint in enumerate(rig.joints)
        if joint.inverse_bind_matrix is not None
    )
    if skinned:
        inverse = np.asarray(
            [rig.joints[index].inverse_bind_matrix for index in skinned],
            dtype=np.float64,
        ).reshape(-1, 4, 4)
        result = inverse @ np.asarray(
            [world[index] for index in skinned], dtype=np.float64
        ).reshape(-1, 4, 4)
        for index, matrix in zip(skinned, result.reshape(-1, 16).tolist()):
            skin[index] = tuple(matrix)
    roots = tuple(
        (index, local[index])
        for index, joint in enumerate(rig.joints)
        if joint.parent_index is None
    )
    return EvaluatedPose(frame, local, world, tuple(skin), roots, weights)
