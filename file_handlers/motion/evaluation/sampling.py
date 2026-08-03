from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum
import math
from typing import Sequence

from ..mot.model import AnimationNode, Joint, KeyTrack, Motion, TrackFamily
from .composition import compose_evaluated_pose
from .math3d import normalize_quaternion
from .model import (
    DiagnosticSeverity,
    EvaluatedPose,
    MotionRigBinding,
    Quaternion,
    SampledLocalPose,
    Transform,
)


class RotationInterpolation(Enum):
    SHORTEST_NLERP = "shortest_nlerp"
    SHORTEST_SLERP = "shortest_slerp"


class SourceDefaultTopologyPolicy(Enum):
    ALWAYS = "always"
    MATCH_TARGET_HIERARCHY = "match_target_hierarchy"


@dataclass(frozen=True, slots=True)
class PoseCompositionPolicy:
    """How source defaults are layered onto a target rig."""

    source_defaults: SourceDefaultTopologyPolicy


@dataclass(frozen=True, slots=True)
class SamplingPolicy:
    frames_per_second: float
    rotation_interpolation: RotationInterpolation
    wrap_looping: bool = True

    def __post_init__(self) -> None:
        if not math.isfinite(self.frames_per_second) or self.frames_per_second <= 0.0:
            raise ValueError("frames_per_second must be positive and finite")


def resolve_motion_frame(
    motion: Motion,
    frame: float,
    *,
    wrap_looping: bool | None,
    default_wrap_looping: bool,
) -> float:
    if not math.isfinite(frame):
        raise ValueError("sample frame must be finite")
    end = motion.end_frame
    should_wrap = (
        default_wrap_looping
        if wrap_looping is None
        else wrap_looping
    )
    if motion.looping and should_wrap and end > 0.0:
        return frame % end
    return min(max(frame, 0.0), end)


def interpolate_quaternion(
    left: Sequence[float],
    right: Sequence[float],
    amount: float,
    strategy: RotationInterpolation,
) -> Quaternion:
    first = normalize_quaternion(left)
    second = normalize_quaternion(right)
    dot = sum(a * b for a, b in zip(first, second))
    if dot < 0.0:
        second = tuple(-value for value in second)  # type: ignore[assignment]
        dot = -dot
    amount = min(1.0, max(0.0, amount))
    if strategy is RotationInterpolation.SHORTEST_NLERP:
        return normalize_quaternion(tuple(a + (b - a) * amount for a, b in zip(first, second)))
    if strategy is not RotationInterpolation.SHORTEST_SLERP:
        raise ValueError(f"unsupported quaternion interpolation {strategy!r}")
    dot = min(1.0, max(-1.0, dot))
    if dot > 0.999999:
        return normalize_quaternion(tuple(a + (b - a) * amount for a, b in zip(first, second)))
    angle = math.acos(dot)
    denominator = math.sin(angle)
    left_weight = math.sin((1.0 - amount) * angle) / denominator
    right_weight = math.sin(amount * angle) / denominator
    return normalize_quaternion(tuple(
        a * left_weight + b * right_weight for a, b in zip(first, second)
    ))


def sample_track(
    track: KeyTrack,
    frame: float,
    rotation_interpolation: RotationInterpolation = RotationInterpolation.SHORTEST_NLERP,
):
    if not track.frames or len(track.frames) != len(track.values):
        raise ValueError("track must have matching nonempty frame and value arrays")
    if frame < track.frames[0]:
        return track.values[0]
    if frame >= track.frames[-1]:
        return track.values[-1]
    right_index = bisect_right(track.frames, frame)
    left_index = right_index - 1
    left_frame = track.frames[left_index]
    right_frame = track.frames[right_index]
    if right_frame == left_frame:
        return track.values[right_index]
    amount = (frame - left_frame) / (right_frame - left_frame)
    left = track.values[left_index]
    right = track.values[right_index]
    if track.family is TrackFamily.QUATERNION:
        return interpolate_quaternion(left, right, amount, rotation_interpolation)
    if track.family is TrackFamily.FLOAT:
        return float(left) + (float(right) - float(left)) * amount
    return tuple(a + (b - a) * amount for a, b in zip(left, right))


def track_is_time_invariant(track: KeyTrack) -> bool:
    """Return whether sampling the track can ever change its value."""

    return (
        bool(track.values)
        and len(track.frames) == len(track.values)
        and all(value == track.values[0] for value in track.values[1:])
    )


class MotionEvaluator:
    def __init__(
        self,
        binding: MotionRigBinding,
        policy: SamplingPolicy,
        pose_policy: PoseCompositionPolicy,
    ):
        self.binding = binding
        self.policy = policy
        self.pose_policy = pose_policy
        if any(
            not math.isfinite(node.weight)
            or not 0.0 <= node.weight <= 1.0
            for node in binding.motion.animation_nodes
        ):
            raise ValueError("motion node weights must be finite and within [0, 1]")
        self._target_by_source = {
            id(bound.motion_joint): bound.rig_index
            for bound in binding.joints
            if bound.motion_joint is not None
        }
        tracks = tuple(
            track
            for node in binding.motion.animation_nodes
            for track in (node.translation, node.rotation, node.scale)
            if track is not None
        )
        self._constant_tracks = {
            id(track): track.values[0]
            for track in tracks
            if track_is_time_invariant(track)
        }
        self.time_invariant = len(self._constant_tracks) == len(tracks)
        if binding.has_errors:
            messages = "; ".join(
                item.message for item in binding.diagnostics
                if item.severity is DiagnosticSeverity.ERROR
            )
            raise ValueError(f"motion binding is not evaluable: {messages}")

    @property
    def motion(self) -> Motion:
        return self.binding.motion

    def resolve_frame(self, frame: float, *, wrap_looping: bool | None = None) -> float:
        return resolve_motion_frame(
            self.motion,
            frame,
            wrap_looping=wrap_looping,
            default_wrap_looping=self.policy.wrap_looping,
        )

    def sample_seconds(self, seconds: float, *, wrap_looping: bool | None = None) -> EvaluatedPose:
        return self.sample_frame(seconds * self.policy.frames_per_second, wrap_looping=wrap_looping)

    def sample_local_frame(
        self,
        frame: float,
        *,
        wrap_looping: bool | None = None,
        apply_node_weights: bool = True,
    ) -> SampledLocalPose:
        resolved = self.resolve_frame(frame, wrap_looping=wrap_looping)
        local: list[Transform] = []
        weights: list[float] = []
        for bound, rig_joint in zip(self.binding.joints, self.binding.rig.joints):
            current = rig_joint.rest
            target = current
            motion_joint = bound.motion_joint
            node = bound.animation_node
            use_source_default = (
                isinstance(node, AnimationNode)
                and node.weight > 0.0
                and isinstance(motion_joint, Joint)
                and self._source_default_matches_target(
                    motion_joint,
                    rig_joint.parent_index,
                )
            )
            if use_source_default:
                assert isinstance(motion_joint, Joint)
                target = Transform(
                    motion_joint.translation
                    if node.translation is None
                    and all(math.isfinite(value) for value in motion_joint.translation)
                    else target.translation,
                    normalize_quaternion(motion_joint.rotation)
                    if node.rotation is None
                    and all(math.isfinite(value) for value in motion_joint.rotation)
                    else target.rotation,
                    target.scale,
                )
            if isinstance(node, AnimationNode) and node.weight > 0.0:
                target = Transform(
                    self._sample_track(node.translation, resolved)
                    if node.translation else target.translation,
                    self._sample_track(node.rotation, resolved)
                    if node.rotation else target.rotation,
                    self._sample_track(node.scale, resolved)
                    if node.scale else target.scale,
                )
            transform = target
            if apply_node_weights and isinstance(node, AnimationNode):
                if node.weight <= 0.0:
                    transform = current
                elif node.weight < 1.0:
                    transform = _blend_transform(
                        current,
                        target,
                        node.weight,
                        self.policy.rotation_interpolation,
                    )
            weights.append(
                node.weight if isinstance(node, AnimationNode) else 1.0
            )
            local.append(transform)

        return SampledLocalPose(resolved, tuple(local), tuple(weights))

    def sample_frame(self, frame: float, *, wrap_looping: bool | None = None) -> EvaluatedPose:
        sampled = self.sample_local_frame(frame, wrap_looping=wrap_looping)
        return compose_evaluated_pose(
            self.binding.rig,
            sampled.frame,
            sampled.local_transforms,
            sampled.node_weights,
        )

    def _sample_track(self, track: KeyTrack, frame: float):
        constant = self._constant_tracks.get(id(track))
        return (
            constant
            if constant is not None
            else sample_track(track, frame, self.policy.rotation_interpolation)
        )

    def _source_default_matches_target(
        self,
        motion_joint: Joint,
        target_parent: int | None,
    ) -> bool:
        if (
            self.pose_policy.source_defaults
            is SourceDefaultTopologyPolicy.ALWAYS
        ):
            return True
        source_parent = motion_joint.parent
        if source_parent is None:
            return target_parent is None
        return self._target_by_source.get(id(source_parent), -1) == target_parent


def _blend_transform(
    current: Transform,
    target: Transform,
    amount: float,
    rotation_interpolation: RotationInterpolation,
) -> Transform:
    amount = min(1.0, max(0.0, amount))
    return Transform(
        tuple(
            left + (right - left) * amount
            for left, right in zip(current.translation, target.translation)
        ),
        interpolate_quaternion(
            current.rotation,
            target.rotation,
            amount,
            rotation_interpolation,
        ),
        tuple(
            left + (right - left) * amount
            for left, right in zip(current.scale, target.scale)
        ),
    )
