from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, IntFlag

from ..mot.model import Motion
from ..mot_tree.model import MotTree
from ..sequence.model import SequenceData


class MotionSlotType(IntEnum):
    MOT = 1
    MOT_TREE = 2


class MotionSlotFlags(IntFlag):
    NONE = 0
    MIRROR = 1
    LOCAL_TREE = 2
    SEQUENCE_ONLY = 0x80


@dataclass(slots=True, eq=False)
class EmbeddedPayload:
    """Identity-bearing wrapper for intentional slot payload aliasing."""

    value: Motion | MotTree


@dataclass(slots=True)
class MotionSlot:
    motion_id: int
    slot_type: MotionSlotType
    payload: EmbeddedPayload | None
    flags: MotionSlotFlags = MotionSlotFlags.NONE
    tag_hash: int = 0
    physics_group_flags: int = 0
    joint_mask_id: int = 0
    overrides: list[SequenceData] = field(default_factory=list)


@dataclass(slots=True)
class MotList:
    name: str
    slots: list[MotionSlot] = field(default_factory=list)
    base_motion_list_path: str | None = None
    error_flags: int = 0
