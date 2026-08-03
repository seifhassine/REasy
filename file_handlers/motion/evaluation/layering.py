from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math
from typing import TYPE_CHECKING, Hashable

from ..mot.model import AnimationNode, Motion
from ..transform_channels import TransformChannels
from .composition import compose_evaluated_pose
from .binding import JointBindingStrategy, bind_motion
from .math3d import multiply_quaternions
from .model import EvaluationDiagnostic, EvaluatedPose, Transform
from .sampling import (
    MotionEvaluator,
    RotationInterpolation,
    interpolate_quaternion,
    resolve_motion_frame,
)

if TYPE_CHECKING:
    from collections.abc import Sequence


class LayerBlendMode(Enum):
    OVERWRITE = "overwrite"
    ADDITIVE = "additive"


class LayerTimePolicy(Enum):
    """How a layer obtains its animation frame."""

    INDEPENDENT = "independent"
    SYNCHRONIZED_NORMALIZED_TIME = "synchronized_normalized_time"


class LayerInterpolationCurve(Enum):
    LINEAR = "linear"
    SMOOTH = "smooth"
    EASE_IN = "ease_in"
    EASE_OUT = "ease_out"


@dataclass(frozen=True, slots=True)
class LayerTiming:
    """Activation timing on the base motion's frame clock."""

    start_frame: float = 0.0
    fade_in_frames: float = 0.0
    curve: LayerInterpolationCurve = LayerInterpolationCurve.LINEAR

    def __post_init__(self) -> None:
        if (
            not math.isfinite(self.start_frame)
            or not math.isfinite(self.fade_in_frames)
            or self.fade_in_frames < 0.0
        ):
            raise ValueError("layer timing values must be finite and fade-in nonnegative")

    def weight_at(self, frame: float) -> float:
        if frame < self.start_frame:
            return 0.0
        if self.fade_in_frames == 0.0:
            return 1.0
        amount = min(1.0, (frame - self.start_frame) / self.fade_in_frames)
        if self.curve is LayerInterpolationCurve.LINEAR:
            return amount
        if self.curve is LayerInterpolationCurve.SMOOTH:
            return amount * amount * (3.0 - 2.0 * amount)
        if self.curve is LayerInterpolationCurve.EASE_IN:
            return amount * amount
        if self.curve is LayerInterpolationCurve.EASE_OUT:
            return 1.0 - (1.0 - amount) * (1.0 - amount)
        raise ValueError(f"unsupported layer interpolation curve {self.curve!r}")

    @property
    def constant_for_nonnegative_frames(self) -> bool:
        return self.start_frame + self.fade_in_frames <= 0.0


@dataclass(frozen=True, slots=True)
class MotionLayer:
    """A semantic pose layer with no serialized offsets or runtime IDs."""

    motion: Motion
    blend_mode: LayerBlendMode = LayerBlendMode.OVERWRITE
    weight: float = 1.0
    timing: LayerTiming = LayerTiming()
    speed: float = 1.0
    wrap_looping: bool | None = None
    layer_key: Hashable | None = None
    time_policy: LayerTimePolicy = LayerTimePolicy.INDEPENDENT
    time_source_key: Hashable | None = None
    joint_channels: (
        tuple[tuple[Hashable, TransformChannels], ...] | None
    ) = None

    def __post_init__(self) -> None:
        if not isinstance(self.blend_mode, LayerBlendMode):
            raise ValueError(f"unsupported layer blend mode {self.blend_mode!r}")
        if not math.isfinite(self.weight) or not 0.0 <= self.weight <= 1.0:
            raise ValueError("layer weight must be finite and between zero and one")
        if not math.isfinite(self.speed) or self.speed < 0.0:
            raise ValueError("layer speed must be nonnegative and finite")
        if not isinstance(self.time_policy, LayerTimePolicy):
            raise ValueError(f"unsupported layer time policy {self.time_policy!r}")
        for label, key in (
            ("layer", self.layer_key),
            ("time source", self.time_source_key),
        ):
            if key is not None:
                try:
                    hash(key)
                except TypeError as exc:
                    raise ValueError(f"{label} key must be hashable") from exc
        if (
            self.time_policy
            is LayerTimePolicy.SYNCHRONIZED_NORMALIZED_TIME
            and self.time_source_key is None
        ):
            raise ValueError(
                "normalized-time synchronization requires a source layer"
            )
        if (
            self.time_policy is LayerTimePolicy.INDEPENDENT
            and self.time_source_key is not None
        ):
            raise ValueError("an independent layer cannot define a time source")
        if self.joint_channels is not None:
            normalized = tuple(
                (key, TransformChannels(channels))
                for key, channels in self.joint_channels
            )
            if len({key for key, _channels in normalized}) != len(normalized):
                raise ValueError("layer joint mask contains duplicate binding keys")
            if any(
                int(channels) & ~int(TransformChannels.ALL)
                for _key, channels in normalized
            ):
                raise ValueError("layer joint mask contains unknown transform channels")
            object.__setattr__(self, "joint_channels", normalized)


@dataclass(frozen=True, slots=True)
class _BoundLayer:
    source: MotionLayer
    evaluator: MotionEvaluator
    joint_channels: tuple[TransformChannels, ...] | None


class LayerFrameResolver:
    """Resolve independent or source-normalized clocks in composition order."""

    def __init__(
        self,
        base_motion: Motion,
        layers: "Sequence[MotionLayer]",
        *,
        default_wrap_looping: bool,
        base_layer_key: Hashable = 0,
    ):
        self.base_motion = base_motion
        self.layers = tuple(layers)
        self.default_wrap_looping = default_wrap_looping
        positions: dict[Hashable, int] = {base_layer_key: -1}
        sources: list[int | None] = []
        for index, layer in enumerate(self.layers):
            if layer.layer_key is not None and layer.layer_key in positions:
                raise ValueError(f"duplicate layer key {layer.layer_key!r}")
            source_position = None
            if (
                layer.time_policy
                is LayerTimePolicy.SYNCHRONIZED_NORMALIZED_TIME
            ):
                source_position = positions.get(layer.time_source_key)
                if source_position is None:
                    raise ValueError(
                        f"normalized-time source {layer.time_source_key!r} "
                        "must identify the base or an earlier layer"
                    )
            sources.append(source_position)
            if layer.layer_key is not None:
                positions[layer.layer_key] = index
        self._source_positions = tuple(sources)

    def resolve(
        self,
        base_frame: float,
        layer_clock_frame: float,
    ) -> tuple[float, ...]:
        if not math.isfinite(base_frame) or not math.isfinite(layer_clock_frame):
            raise ValueError("layer clocks must be finite")
        resolved: list[float] = []
        for layer, source_position in zip(
            self.layers,
            self._source_positions,
        ):
            if source_position is None:
                requested = max(
                    0.0,
                    (layer_clock_frame - layer.timing.start_frame)
                    * layer.speed,
                )
                frame = resolve_motion_frame(
                    layer.motion,
                    requested,
                    wrap_looping=layer.wrap_looping,
                    default_wrap_looping=self.default_wrap_looping,
                )
            else:
                # Synchronization replaces the destination clock, so speed is
                # not applied; start/fade timing still gates the layer weight.
                source_motion = (
                    self.base_motion
                    if source_position < 0
                    else self.layers[source_position].motion
                )
                source_frame = (
                    base_frame
                    if source_position < 0
                    else resolved[source_position]
                )
                normalized = (
                    min(1.0, max(0.0, source_frame / source_motion.end_frame))
                    if source_motion.end_frame > 0.0
                    else 0.0
                )
                frame = resolve_motion_frame(
                    layer.motion,
                    normalized * layer.motion.end_frame,
                    wrap_looping=False,
                    default_wrap_looping=self.default_wrap_looping,
                )
            resolved.append(frame)
        return tuple(resolved)


class LayeredPoseEvaluator:
    """Compose partial MOT channels over a fully evaluated base pose.

    Only authored overlay channels participate. A partial facial MOT therefore
    cannot reset an animated parent merely because that parent exists in its
    source skeleton.
    """

    def __init__(
        self,
        base: MotionEvaluator,
        layers: "Sequence[MotionLayer]",
        binding_strategy: JointBindingStrategy,
        *,
        base_layer_key: Hashable = 0,
    ):
        self.base = base
        self.layers = tuple(
            self._bind_layer(layer, binding_strategy)
            for layer in layers
        )
        self._frame_resolver = LayerFrameResolver(
            base.motion,
            tuple(layer.source for layer in self.layers),
            default_wrap_looping=base.policy.wrap_looping,
            base_layer_key=base_layer_key,
        )
        self.time_invariant = base.time_invariant and all(
            layer.source.weight == 0.0
            or (
                layer.evaluator.time_invariant
                and layer.source.timing.constant_for_nonnegative_frames
            )
            for layer in self.layers
        )

    @property
    def diagnostics(self) -> tuple[EvaluationDiagnostic, ...]:
        return tuple(
            diagnostic
            for evaluator in (self.base, *(layer.evaluator for layer in self.layers))
            for diagnostic in evaluator.binding.diagnostics
        )

    def _bind_layer(
        self,
        layer: MotionLayer,
        binding_strategy: JointBindingStrategy,
    ) -> _BoundLayer:
        binding = bind_motion(layer.motion, self.base.binding.rig, binding_strategy)
        evaluator = MotionEvaluator(binding, self.base.policy, self.base.pose_policy)
        joint_channels = None
        if layer.joint_channels is not None:
            by_key = dict(layer.joint_channels)
            rig_keys = tuple(
                binding_strategy.rig_key(joint)
                for joint in self.base.binding.rig.joints
            )
            if len(set(rig_keys)) != len(rig_keys):
                raise ValueError("layer joint mask is ambiguous on the target rig")
            joint_channels = tuple(
                by_key.get(key, TransformChannels.NONE)
                for key in rig_keys
            )
        return _BoundLayer(layer, evaluator, joint_channels)

    def sample_frame(
        self,
        frame: float,
        *,
        wrap_looping: bool | None = None,
        layer_clock_frame: float | None = None,
    ) -> EvaluatedPose:
        base_pose = self.base.sample_local_frame(
            frame,
            wrap_looping=wrap_looping,
        )
        layer_clock = base_pose.frame if layer_clock_frame is None else layer_clock_frame
        if not math.isfinite(layer_clock):
            raise ValueError("layer clock frame must be finite")
        layer_frames = self._frame_resolver.resolve(
            base_pose.frame,
            layer_clock,
        )
        local = list(base_pose.local_transforms)
        for layer, layer_frame in zip(self.layers, layer_frames):
            weight = layer.source.weight * layer.source.timing.weight_at(layer_clock)
            if weight <= 0.0:
                continue
            overlay = layer.evaluator.sample_local_frame(
                layer_frame,
                wrap_looping=False,
                apply_node_weights=False,
            )
            for index, bound in enumerate(layer.evaluator.binding.joints):
                node = bound.animation_node
                if (
                    not isinstance(node, AnimationNode)
                    or node.weight <= 0.0
                ):
                    continue
                amount = weight * node.weight
                channels = (
                    TransformChannels.ALL
                    if layer.joint_channels is None
                    else layer.joint_channels[index]
                )
                current = local[index]
                target = overlay.local_transforms[index]
                local[index] = (
                    _additive_transform(
                        current,
                        target,
                        amount,
                        node,
                        channels,
                        self.base.policy.rotation_interpolation,
                    )
                    if layer.source.blend_mode is LayerBlendMode.ADDITIVE
                    else _overwrite_transform(
                        current,
                        target,
                        amount,
                        node,
                        channels,
                        self.base.policy.rotation_interpolation,
                    )
                )
        return compose_evaluated_pose(
            self.base.binding.rig,
            base_pose.frame,
            local,
            base_pose.node_weights,
        )


def _lerp3(
    left: tuple[float, float, float],
    right: tuple[float, float, float],
    amount: float,
) -> tuple[float, float, float]:
    return (
        left[0] + (right[0] - left[0]) * amount,
        left[1] + (right[1] - left[1]) * amount,
        left[2] + (right[2] - left[2]) * amount,
    )


def _overwrite_transform(
    current: Transform,
    target: Transform,
    amount: float,
    node: AnimationNode,
    channels: TransformChannels,
    rotation_interpolation: RotationInterpolation,
) -> Transform:
    return Transform(
        _lerp3(current.translation, target.translation, amount)
        if node.translation is not None
        and channels & TransformChannels.TRANSLATION
        else current.translation,
        interpolate_quaternion(
            current.rotation,
            target.rotation,
            amount,
            rotation_interpolation,
        )
        if node.rotation is not None
        and channels & TransformChannels.ROTATION
        else current.rotation,
        _lerp3(current.scale, target.scale, amount)
        if node.scale is not None
        and channels & TransformChannels.SCALE
        else current.scale,
    )


def _additive_transform(
    current: Transform,
    target: Transform,
    amount: float,
    node: AnimationNode,
    channels: TransformChannels,
    rotation_interpolation: RotationInterpolation,
) -> Transform:
    return Transform(
        tuple(
            value + delta * amount
            for value, delta in zip(current.translation, target.translation)
        )
        if node.translation is not None
        and channels & TransformChannels.TRANSLATION
        else current.translation,
        multiply_quaternions(
            current.rotation,
            interpolate_quaternion(
                (0.0, 0.0, 0.0, 1.0),
                target.rotation,
                amount,
                rotation_interpolation,
            ),
        )
        if node.rotation is not None
        and channels & TransformChannels.ROTATION
        else current.rotation,
        tuple(
            value + (delta - 1.0) * amount
            for value, delta in zip(current.scale, target.scale)
        )
        if node.scale is not None
        and channels & TransformChannels.SCALE
        else current.scale,
    )
