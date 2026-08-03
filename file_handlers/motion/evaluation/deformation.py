from __future__ import annotations

from dataclasses import dataclass
from typing import Hashable
import math

from ..mot.model import AnimationNode, KeyTrack, Motion, TrackFamily
from ..transform_channels import TransformChannels
from .binding import JointBindingStrategy
from .layering import LayerBlendMode, LayerFrameResolver, MotionLayer
from .sampling import (
    SamplingPolicy,
    resolve_motion_frame,
    sample_track,
    track_is_time_invariant,
)


@dataclass(frozen=True, slots=True)
class DeformationTarget:
    binding_key: Hashable | None
    name: str
    property_hash: int | None = None

    def __post_init__(self) -> None:
        if self.binding_key is None and self.property_hash is None:
            raise ValueError("deformation target has no animation or property key")
        if (
            self.property_hash is not None
            and (
                isinstance(self.property_hash, bool)
                or not isinstance(self.property_hash, int)
                or not 0 <= self.property_hash <= 0xFFFFFFFF
            )
        ):
            raise ValueError("deformation property hash must be unsigned 32-bit")


class DeformationWeightEvaluator:
    """Sample scalar MOT nodes and properties mapped by runtime resources."""

    def __init__(
        self,
        motion: Motion,
        layers: tuple[MotionLayer, ...],
        targets: tuple[DeformationTarget, ...],
        binding: JointBindingStrategy,
        policy: SamplingPolicy,
        *,
        base_layer_key: Hashable = 0,
    ):
        unique: dict[str, DeformationTarget] = {}
        for target in targets:
            previous = unique.get(target.name)
            if previous is None:
                unique[target.name] = target
                continue
            binding_key = self._merge_key(
                target.name,
                "animation",
                previous.binding_key,
                target.binding_key,
            )
            property_hash = self._merge_key(
                target.name,
                "property",
                previous.property_hash,
                target.property_hash,
            )
            unique[target.name] = DeformationTarget(
                binding_key,
                target.name,
                property_hash,
            )
        self.targets = tuple(unique.values())
        self.policy = policy
        self.motion = motion
        self.base = self._bind(motion, binding)
        self.layers = tuple(
            (
                layer,
                self._bind(
                    layer.motion,
                    binding,
                    layer.joint_channels,
                ),
            )
            for layer in layers
        )
        self._frame_resolver = LayerFrameResolver(
            motion,
            layers,
            default_wrap_looping=policy.wrap_looping,
            base_layer_key=base_layer_key,
        )
        self.time_invariant = all(
            self._source_is_time_invariant(source)
            for source in self.base.values()
        ) and all(
            not sources
            or not layer.weight
            or (
                layer.timing.constant_for_nonnegative_frames
                and all(
                    self._source_is_time_invariant(source)
                    for source in sources.values()
                )
            )
            for layer, sources in self.layers
        )

    def _bind(
        self,
        motion: Motion,
        binding: JointBindingStrategy,
        joint_channels: (
            tuple[tuple[Hashable, TransformChannels], ...] | None
        ) = None,
    ) -> dict[str, AnimationNode | KeyTrack]:
        allowed = dict(joint_channels) if joint_channels is not None else None
        nodes: dict[Hashable, list[AnimationNode]] = {}
        for node in motion.animation_nodes:
            nodes.setdefault(binding.motion_key(node.joint), []).append(node)
        properties: dict[int, list[KeyTrack]] = {}
        for item in motion.property_tracks:
            properties.setdefault(item.target_name_hash, []).append(item.track)
        result = {}
        for target in self.targets:
            node_matches = (
                nodes.get(target.binding_key, ())
                if target.binding_key is not None
                and (
                    allowed is None
                    or (
                        allowed.get(
                            target.binding_key,
                            TransformChannels.NONE,
                        )
                        & TransformChannels.TRANSLATION
                    )
                )
                else ()
            )
            property_matches = (
                properties.get(target.property_hash, ())
                if target.property_hash is not None
                else ()
            )
            if len(node_matches) > 1:
                raise ValueError(
                    f"motion {motion.name!r} has multiple deformation nodes "
                    f"for {target.name!r}"
                )
            if len(property_matches) > 1:
                raise ValueError(
                    f"motion {motion.name!r} has multiple deformation properties "
                    f"for {target.name!r}"
                )
            if node_matches and property_matches:
                raise ValueError(
                    f"motion {motion.name!r} has both node and property "
                    f"deformation sources for {target.name!r}"
                )
            if property_matches:
                self._validate_property(property_matches[0], target.name)
                result[target.name] = property_matches[0]
            elif node_matches:
                self._validate_node(node_matches[0], target.name)
                if node_matches[0].weight > 0.0:
                    result[target.name] = node_matches[0]
        return result

    @staticmethod
    def _merge_key(name: str, kind: str, left, right):
        if left is None:
            return right
        if right is None or right == left:
            return left
        raise ValueError(
            f"deformation target {name!r} has conflicting {kind} keys"
        )

    @staticmethod
    def _validate_node(node: AnimationNode, name: str) -> None:
        if node.rotation is not None or node.scale is not None:
            raise ValueError(
                f"deformation node {name!r} has unsupported rotation/scale tracks"
            )
        values = (
            node.translation.values
            if node.translation is not None
            else (node.joint.translation,)
        )
        for value in values:
            if (
                len(value) != 3
                or not all(math.isfinite(component) for component in value)
                or abs(value[1]) > 1e-6
                or abs(value[2]) > 1e-6
            ):
                raise ValueError(
                    f"deformation node {name!r} is not an X-only scalar channel"
                )

    @staticmethod
    def _validate_property(track: KeyTrack, name: str) -> None:
        if track.family is not TrackFamily.FLOAT:
            raise ValueError(
                f"deformation property {name!r} is not a scalar track"
            )
        if not all(
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            for value in track.values
        ):
            raise ValueError(
                f"deformation property {name!r} contains a nonfinite value"
            )

    def sample(
        self,
        frame: float,
        *,
        layer_clock_frame: float,
    ) -> tuple[tuple[str, float], ...]:
        values = {target.name: 0.0 for target in self.targets}
        base_frame = self._motion_frame(
            self.motion,
            frame,
            False,
        )
        # Base playback time is already wrapped/clamped by the controller.
        for name, source in self.base.items():
            target, rate = self._sample_source(source, base_frame)
            values[name] = target * rate

        layer_frames = self._frame_resolver.resolve(
            base_frame,
            layer_clock_frame,
        )
        for (layer, sources), resolved in zip(self.layers, layer_frames):
            layer_amount = (
                layer.weight * layer.timing.weight_at(layer_clock_frame)
            )
            if layer_amount <= 0.0:
                continue
            for name, source in sources.items():
                target, rate = self._sample_source(source, resolved)
                amount = layer_amount * rate
                if layer.blend_mode is LayerBlendMode.ADDITIVE:
                    values[name] += target * amount
                else:
                    values[name] += (target - values[name]) * amount
        return tuple(values.items())

    def _sample_source(
        self,
        source: AnimationNode | KeyTrack,
        frame: float,
    ) -> tuple[float, float]:
        if isinstance(source, KeyTrack):
            return (
                float(
                    sample_track(
                        source,
                        frame,
                        self.policy.rotation_interpolation,
                    )
                ),
                1.0,
            )
        node = source
        value = (
            sample_track(
                node.translation,
                frame,
                self.policy.rotation_interpolation,
            )
            if node.translation is not None
            else node.joint.translation
        )
        return float(value[0]), node.weight

    @staticmethod
    def _source_is_time_invariant(source: AnimationNode | KeyTrack) -> bool:
        track = source if isinstance(source, KeyTrack) else source.translation
        return track is None or track_is_time_invariant(track)

    def _motion_frame(
        self,
        motion: Motion,
        frame: float,
        wrap_looping: bool | None,
    ) -> float:
        return resolve_motion_frame(
            motion,
            frame,
            wrap_looping=wrap_looping,
            default_wrap_looping=self.policy.wrap_looping,
        )
