from dataclasses import dataclass, replace

from ..mhr_storage import MhrJoint, MhrMotion
from .profiles import DMC5_EVALUATION_PROFILE, Dmc5JointBindingStrategy


@dataclass(frozen=True, slots=True)
class MhrJointBindingStrategy(Dmc5JointBindingStrategy):
    def motion_key(self, joint):
        return joint.binding_hash if isinstance(joint, MhrJoint) else self.motion_name_key(joint.name)


def authored_frame_rate(motion: MhrMotion) -> float:
    return float(motion.frames_per_second)


MHR_EVALUATION_PROFILE = replace(
    DMC5_EVALUATION_PROFILE,
    name='Monster Hunter Rise / Sunbreak',
    joint_binding=MhrJointBindingStrategy(),
    authored_frame_rate=authored_frame_rate,
)
