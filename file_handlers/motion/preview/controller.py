from __future__ import annotations

from dataclasses import replace
import math

from ..evaluation.binding import bind_motion
from ..evaluation.deformation import (
    DeformationTarget,
    DeformationWeightEvaluator,
)
from ..evaluation.layering import LayeredPoseEvaluator, MotionLayer
from ..evaluation.model import (
    DiagnosticSeverity,
    EvaluatedPose,
    MotionRigBinding,
    Rig,
    Vector3,
)
from ..evaluation.profiles import MotionEvaluationProfile
from ..evaluation.sampling import MotionEvaluator, RotationInterpolation
from ..mot.model import Motion
from ..runtime.model import RuntimeRootMotionMode
from .model import (
    MotionPreviewError,
    MotionPreviewSnapshot,
    PreviewLoopMode,
    RootDisplayMode,
)
from .root_motion import apply_runtime_root_motion


class MotionPreviewController:
    """Qt-independent playback state over the semantic motion evaluator."""

    def __init__(self, evaluation_profile: MotionEvaluationProfile):
        self.evaluation_profile = evaluation_profile
        self.sampling_policy = evaluation_profile.sampling_policy
        self.pose_composition_policy = evaluation_profile.pose_composition_policy
        self.binding_strategy = evaluation_profile.joint_binding
        self.motion: Motion | None = None
        self.rig: Rig | None = None
        self.binding: MotionRigBinding | None = None
        self.layers: tuple[MotionLayer, ...] = ()
        self._evaluator: MotionEvaluator | LayeredPoseEvaluator | None = None
        self._deformation_evaluator: DeformationWeightEvaluator | None = None
        self._deformation_targets: tuple[DeformationTarget, ...] = ()
        self._root_by_joint: tuple[int, ...] = ()
        self._base_root_positions: dict[int, Vector3] = {}
        self._root_motion_mode: RuntimeRootMotionMode | None = None
        self._root_cycle: tuple[EvaluatedPose, EvaluatedPose] | None = None
        self._static_snapshot: tuple[RootDisplayMode, MotionPreviewSnapshot] | None = None
        self.current_frame = 0.0
        self._layer_clock_frame = 0.0
        self.speed = 1.0
        self.playing = False
        self.deformation_enabled = True
        self.loop_mode = PreviewLoopMode.SOURCE
        self.root_display_mode = RootDisplayMode.AUTHORED

    @property
    def ready(self) -> bool:
        return self._evaluator is not None

    @property
    def frames_per_second(self) -> float:
        return self.sampling_policy.frames_per_second

    @property
    def end_frame(self) -> float:
        return self.motion.end_frame if self.motion is not None else 0.0

    @property
    def error_message(self) -> str:
        if self.binding is None:
            return "no motion and rig are loaded"
        errors = [
            item.message for item in self.binding.diagnostics
            if item.severity is DiagnosticSeverity.ERROR
        ]
        return "; ".join(errors)

    def clear(self) -> None:
        self.motion = None
        self.rig = None
        self.binding = None
        self.layers = ()
        self._evaluator = None
        self._deformation_evaluator = None
        self._deformation_targets = ()
        self._root_by_joint = ()
        self._base_root_positions.clear()
        self._root_motion_mode = None
        self._root_cycle = None
        self._static_snapshot = None
        self.current_frame = 0.0
        self._layer_clock_frame = 0.0
        self.playing = False

    def load(
        self,
        motion: Motion,
        rig: Rig,
        *,
        layers: tuple[MotionLayer, ...] | list[MotionLayer] = (),
        deformation_targets: tuple[DeformationTarget, ...] = (),
        root_motion_mode: RuntimeRootMotionMode | None = None,
    ) -> bool:
        self.clear()
        if (
            root_motion_mode is not None
            and (
                not isinstance(root_motion_mode, RuntimeRootMotionMode)
                or root_motion_mode not in (
                    RuntimeRootMotionMode.NONE,
                    RuntimeRootMotionMode.FIXED,
                    RuntimeRootMotionMode.CONTINUANCE,
                    RuntimeRootMotionMode.JOINT,
                )
            )
        ):
            raise ValueError(
                f"{getattr(root_motion_mode, 'label', root_motion_mode)!s} "
                "root-motion evaluation "
                "is not established"
            )
        self.motion = motion
        self.rig = rig
        self.layers = tuple(layers)
        self._deformation_targets = tuple(deformation_targets)
        self._root_motion_mode = root_motion_mode
        self.binding = bind_motion(motion, rig, self.binding_strategy)
        self._root_by_joint = rig.root_indices
        if self.binding.has_errors:
            return False
        self._rebuild_evaluator()
        return True

    def _rebuild_evaluator(self) -> None:
        self._static_snapshot = None
        if self.binding is None or self.binding.has_errors:
            self._evaluator = None
            return
        base = MotionEvaluator(
            self.binding,
            self.sampling_policy,
            self.pose_composition_policy,
        )
        self._evaluator = (
            LayeredPoseEvaluator(base, self.layers, self.binding_strategy)
            if self.layers else base
        )
        self._deformation_evaluator = (
            DeformationWeightEvaluator(
                self.binding.motion,
                self.layers,
                self._deformation_targets,
                self.binding_strategy,
                self.sampling_policy,
            )
            if self._deformation_targets
            else None
        )
        self._root_cycle = None
        reference_pose = self._sample_pose(0.0, 0.0)
        if self._root_motion_mode is RuntimeRootMotionMode.CONTINUANCE:
            self._root_cycle = (
                reference_pose,
                self._sample_pose(self.end_frame, self.end_frame),
            )
        reference_pose = apply_runtime_root_motion(
            reference_pose,
            self.binding.rig,
            self._root_motion_mode,
        )
        self._base_root_positions = {
            index: self._matrix_translation(reference_pose.world_matrices[index])
            for index, joint in enumerate(self.binding.rig.joints)
            if joint.parent_index is None
        }

    def set_rotation_interpolation(self, interpolation: RotationInterpolation) -> None:
        if interpolation is self.sampling_policy.rotation_interpolation:
            return
        self.sampling_policy = replace(
            self.sampling_policy,
            rotation_interpolation=interpolation,
        )
        self._rebuild_evaluator()

    def set_deformation_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        if enabled == self.deformation_enabled:
            return
        self.deformation_enabled = enabled
        self._static_snapshot = None

    @property
    def _snapshot_is_time_invariant(self) -> bool:
        if self._evaluator is None or not self._evaluator.time_invariant:
            return False
        if (
            self.deformation_enabled
            and self._deformation_evaluator is not None
            and not self._deformation_evaluator.time_invariant
        ):
            return False
        return (
            self._root_motion_mode is not RuntimeRootMotionMode.CONTINUANCE
            or self._root_cycle is not None
            and self._root_cycle[0].local_transforms
            == self._root_cycle[1].local_transforms
        )

    def set_frame(self, frame: float) -> float:
        if not math.isfinite(frame):
            raise ValueError("preview frame must be finite")
        self.current_frame = min(max(frame, 0.0), self.end_frame)
        self._layer_clock_frame = self.current_frame
        return self.current_frame

    def set_speed(self, speed: float) -> None:
        if not math.isfinite(speed) or speed <= 0.0:
            raise ValueError("preview speed must be positive and finite")
        self.speed = speed

    def restart(self) -> None:
        self.current_frame = 0.0
        self._layer_clock_frame = 0.0
        self.playing = False

    def advance(self, elapsed_seconds: float) -> float:
        if not math.isfinite(elapsed_seconds) or elapsed_seconds < 0.0:
            raise ValueError("elapsed preview time must be nonnegative and finite")
        return self.advance_frames(
            elapsed_seconds * self.frames_per_second * self.speed
        )

    def advance_frames(self, frame_delta: float) -> float:
        if not math.isfinite(frame_delta) or frame_delta < 0.0:
            raise ValueError("preview frame delta must be nonnegative and finite")
        if not self.playing or not self.ready:
            return self.current_frame
        end = self.end_frame
        if end <= 0.0:
            self.current_frame = 0.0
            self._layer_clock_frame = 0.0
            self.playing = False
            return self.current_frame
        next_frame = self.current_frame + frame_delta
        if self._loops():
            self.current_frame = next_frame % end
            self._layer_clock_frame += frame_delta
        elif next_frame >= end:
            self.current_frame = end
            self._layer_clock_frame = end
            self.playing = False
        else:
            self.current_frame = next_frame
            self._layer_clock_frame = next_frame
        return self.current_frame

    def _loops(self) -> bool:
        if self.loop_mode is PreviewLoopMode.LOOP:
            return True
        if self.loop_mode is PreviewLoopMode.ONCE:
            return False
        return bool(self.motion and self.motion.looping)

    @staticmethod
    def _matrix_translation(matrix) -> Vector3:
        return float(matrix[12]), float(matrix[13]), float(matrix[14])

    def sample(self) -> MotionPreviewSnapshot:
        if self._evaluator is None or self.binding is None or self.rig is None:
            raise MotionPreviewError(self.error_message)
        if (
            self._static_snapshot is not None
            and self._static_snapshot[0] is self.root_display_mode
        ):
            cached = self._static_snapshot[1]
            return replace(
                cached,
                frame=self.current_frame,
                pose=replace(cached.pose, frame=self.current_frame),
            )
        pose = self._sample_pose(
            self.current_frame,
            self._layer_clock_frame,
        )
        completed_cycles = (
            int(round(
                (self._layer_clock_frame - self.current_frame)
                / self.end_frame
            ))
            if self._loops()
            and self.end_frame > 0.0
            and self._layer_clock_frame >= self.current_frame
            else 0
        )
        pose = apply_runtime_root_motion(
            pose,
            self.rig,
            self._root_motion_mode,
            completed_cycles=completed_cycles,
            cycle_start=self._root_cycle[0] if self._root_cycle else None,
            cycle_end=self._root_cycle[1] if self._root_cycle else None,
        )
        positions = [self._matrix_translation(matrix) for matrix in pose.world_matrices]
        root_deltas: dict[int, Vector3] = {}
        if self.root_display_mode is RootDisplayMode.LOCK_TRANSLATION:
            for root, base in self._base_root_positions.items():
                current = positions[root]
                root_deltas[root] = (
                    current[0] - base[0],
                    current[1] - base[1],
                    current[2] - base[2],
                )
            positions = [
                (
                    position[0] - root_deltas.get(self._root_by_joint[index], (0.0, 0.0, 0.0))[0],
                    position[1] - root_deltas.get(self._root_by_joint[index], (0.0, 0.0, 0.0))[1],
                    position[2] - root_deltas.get(self._root_by_joint[index], (0.0, 0.0, 0.0))[2],
                )
                for index, position in enumerate(positions)
            ]
        pairs = tuple(
            (joint.parent_index, index)
            for index, joint in enumerate(self.rig.joints)
            if joint.parent_index is not None
        )
        snapshot = MotionPreviewSnapshot(
            frame=self.current_frame,
            end_frame=self.end_frame,
            pose=pose,
            joint_names=tuple(joint.name for joint in self.rig.joints),
            joint_positions=tuple(positions),
            bone_pairs=pairs,
            node_weights=pose.node_weights,
            root_deltas=tuple(sorted(root_deltas.items())),
            deformation_weights=(
                self._deformation_evaluator.sample(
                    self.current_frame,
                    layer_clock_frame=self._layer_clock_frame,
                )
                if self.deformation_enabled
                and self._deformation_evaluator is not None
                else ()
            ),
            diagnostics=(
                self._evaluator.diagnostics
                if isinstance(self._evaluator, LayeredPoseEvaluator)
                else self.binding.diagnostics
            ),
        )
        if self._snapshot_is_time_invariant:
            self._static_snapshot = self.root_display_mode, snapshot
        return snapshot

    def _sample_pose(
        self,
        frame: float,
        layer_clock_frame: float,
    ) -> EvaluatedPose:
        assert self._evaluator is not None
        return (
            self._evaluator.sample_frame(
                frame,
                wrap_looping=False,
                layer_clock_frame=layer_clock_frame,
            )
            if isinstance(self._evaluator, LayeredPoseEvaluator)
            else self._evaluator.sample_frame(frame, wrap_looping=False)
        )
