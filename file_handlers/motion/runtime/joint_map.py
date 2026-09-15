from __future__ import annotations

from dataclasses import dataclass
from enum import IntFlag

from ..transform_channels import TransformChannels


class JointMapAttributes(IntFlag):
    NONE = 0
    DEFORM = 1


class ExtraJointFlags(IntFlag):
    NONE = 0
    DISABLE_EMPTY_INTERPOLATION = 1


@dataclass(frozen=True, slots=True)
class JointMapJoint:
    translation: tuple[float, float, float]
    rotation: tuple[float, float, float, float]
    parent_index: int | None
    name_hash: int


@dataclass(frozen=True, slots=True)
class JointMaskEntry:
    joint_hash: int
    channels: TransformChannels


@dataclass(frozen=True, slots=True)
class JointMaskGroup:
    group_id: int
    entries: tuple[JointMaskEntry, ...]


@dataclass(frozen=True, slots=True)
class JointMapExtraJoint:
    parent_name: str
    joint_name: str
    parent_hash: int
    joint_hash: int
    symmetry_hash: int
    flags: ExtraJointFlags


@dataclass(frozen=True, slots=True)
class JointMapDefinition:
    version: int
    joints: tuple[JointMapJoint, ...]
    mask_groups: tuple[JointMaskGroup, ...]
    attributes: JointMapAttributes
    extra_joints: tuple[JointMapExtraJoint, ...]

    @property
    def bone_count(self) -> int:
        return len(self.joints)

    @property
    def extra_joint_count(self) -> int:
        return len(self.extra_joints)

    def mask_group(self, group_id: int) -> JointMaskGroup | None:
        return next(
            (item for item in self.mask_groups if item.group_id == group_id),
            None,
        )
