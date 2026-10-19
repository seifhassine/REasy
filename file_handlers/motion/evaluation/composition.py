from __future__ import annotations

import numpy as np

from .math3d import compose_world_matrices
from .model import EvaluatedPose, Rig


def compose_evaluated_pose(
    rig: Rig,
    frame: float,
    local: np.ndarray,
    node_weights: np.ndarray,
) -> EvaluatedPose:
    """``local`` rows as ``Transform.row``."""
    if len(local) != len(rig.joints) or len(node_weights) != len(rig.joints):
        raise ValueError("pose component counts must match the target rig")
    return EvaluatedPose(frame, local, compose_world_matrices(local, rig.parents, rig.compensating), node_weights)
