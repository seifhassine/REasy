from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from ..evaluation.model import (
    DiagnosticSeverity,
    EvaluatedPose,
    EvaluationDiagnostic,
    Vector3,
)
from ..evaluation.sampling import track_is_time_invariant
from ..mot.model import Motion


class PreviewLoopMode(Enum):
    SOURCE = "source"
    LOOP = "loop"
    ONCE = "once"


class RootDisplayMode(Enum):
    AUTHORED = "authored"
    LOCK_TRANSLATION = "lock_translation"


@dataclass(frozen=True, slots=True)
class MotionPreviewSnapshot:
    frame: float
    end_frame: float
    pose: EvaluatedPose
    joint_names: tuple[str, ...]
    joint_positions: tuple[Vector3, ...]
    bone_pairs: tuple[tuple[int, int], ...]
    node_weights: tuple[float, ...]
    root_deltas: tuple[tuple[int, Vector3], ...]
    deformation_weights: tuple[tuple[str, float], ...]
    diagnostics: tuple[EvaluationDiagnostic, ...]


class MotionPreviewError(ValueError):
    pass


def is_static_skeletal_pose(motion: Motion) -> bool:
    """Return whether skeletal tracks contain no time-varying values."""
    tracks = [
        track
        for node in motion.animation_nodes
        for track in (node.translation, node.rotation, node.scale)
        if track is not None and track.values
    ]
    return bool(tracks) and all(track_is_time_invariant(track) for track in tracks)


def snapshot_status_messages(
    snapshot: MotionPreviewSnapshot,
    frames_per_second: float,
    translate: Callable[[str], str],
) -> list[str]:
    messages = [
        translate("{joints} joints · {fps:g} fps").format(
            joints=len(snapshot.joint_names),
            fps=frames_per_second,
        )
    ]
    if snapshot.deformation_weights:
        active = sum(
            abs(weight) > 1e-6
            for _name, weight in snapshot.deformation_weights
        )
        messages.append(
            translate("Blend shapes: {active}/{total} active").format(
                active=active,
                total=len(snapshot.deformation_weights),
            )
        )
    return messages


def snapshot_diagnostic_messages(
    snapshot: MotionPreviewSnapshot,
) -> list[str]:
    return [
        f"{diagnostic.severity.value}: {diagnostic.message}"
        for diagnostic in snapshot.diagnostics
        if diagnostic.severity is not DiagnosticSeverity.INFO
    ]
