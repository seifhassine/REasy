from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from utils.hash_util import murmur3_hash_utf16le

from ..mot.model import Joint
from .binding import JointBindingStrategy
from .model import RigJoint, Vector3
from .sampling import (
    PoseCompositionPolicy,
    RotationInterpolation,
    SamplingPolicy,
    SourceDefaultTopologyPolicy,
)


@dataclass(frozen=True, slots=True)
class MotionEvaluationProfile:
    """Game-specific animation semantics consumed by evaluators and previews."""

    name: str
    sampling_policy: SamplingPolicy
    pose_composition_policy: PoseCompositionPolicy
    joint_binding: JointBindingStrategy
    source_preview_scale: Vector3
    property_name_hash: Callable[[str], int] | None = None


@dataclass(frozen=True, slots=True)
class Dmc5JointBindingStrategy:
    """The name-hash rule serialized by DMC5 MOT v65 and JMAP v10."""

    def motion_name_key(self, name: str) -> int:
        return murmur3_hash_utf16le(name)

    def motion_key(self, joint: Joint) -> int:
        return self.motion_name_key(joint.name)

    def rig_key(self, joint: RigJoint) -> int:
        key = joint.binding_key
        if key is None:
            return self.motion_name_key(joint.name)
        if isinstance(key, bool) or not isinstance(key, int) or not 0 <= key <= 0xFFFFFFFF:
            raise ValueError("DMC5 rig binding keys must be unsigned 32-bit Murmur3 hashes")
        return key


# Rotation interpolation is deliberately a replaceable evaluation policy. It
# uses shortest-path normalized linear interpolation but is not a file field.
# DMC5 MOT v65 omits joint default scale. The audited DMC5 mesh/JMAP rigs use
# unit local scale, so standalone source-skeleton preview exposes this as an
# explicit game profile choice rather than a generic evaluator fallback.
# Partial facial rigs may use synthetic or otherwise different source
# topology. Their defaults are not target-local at those boundaries.
DMC5_EVALUATION_PROFILE = MotionEvaluationProfile(
    name="Devil May Cry 5",
    sampling_policy=SamplingPolicy(
        frames_per_second=60.0,
        rotation_interpolation=RotationInterpolation.SHORTEST_NLERP,
        wrap_looping=True,
    ),
    pose_composition_policy=PoseCompositionPolicy(
        source_defaults=SourceDefaultTopologyPolicy.MATCH_TARGET_HIERARCHY,
    ),
    joint_binding=Dmc5JointBindingStrategy(),
    source_preview_scale=(1.0, 1.0, 1.0),
    property_name_hash=murmur3_hash_utf16le,
)

# Named aliases keep focused call sites concise while sharing one source of
# truth for the complete game profile.
DMC5_JOINT_BINDING = DMC5_EVALUATION_PROFILE.joint_binding
DMC5_SAMPLING_POLICY = DMC5_EVALUATION_PROFILE.sampling_policy
DMC5_POSE_COMPOSITION_POLICY = DMC5_EVALUATION_PROFILE.pose_composition_policy
DMC5_SOURCE_PREVIEW_SCALE = DMC5_EVALUATION_PROFILE.source_preview_scale
