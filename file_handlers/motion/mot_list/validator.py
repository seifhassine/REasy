from __future__ import annotations

from ..errors import MotionValidationError
from ..mot.model import Motion
from ..mot.validator import MotV65Validator
from ..mot_tree.model import MotTree
from ..mot_tree.validator import MotTreeV4Validator
from ..profiles import MotionFormatProfile
from ..sequence.validator import SequenceV65Validator
from .model import MotList, MotionSlotFlags, MotionSlotType


class MotListV85Validator:
    _ACTIVE_FLAGS = frozenset(
        (
            MotionSlotFlags.NONE,
            MotionSlotFlags.MIRROR,
            MotionSlotFlags.LOCAL_TREE,
            MotionSlotFlags.SEQUENCE_ONLY,
        )
    )

    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(motlist=85, mot=65, mot_clip=27, mot_tree=4)
        self.profile = profile
        self.mot_validator = MotV65Validator(profile)
        self.tree_validator = MotTreeV4Validator(profile)
        self.sequence_validator = SequenceV65Validator(profile)

    def validate(self, motlist: MotList, *, validate_nested: bool = True) -> None:
        self._utf16(motlist.name, "MOTLIST name")
        if motlist.base_motion_list_path is not None:
            self._utf16(motlist.base_motion_list_path, "base MOTLIST path")
        if motlist.error_flags not in (0, 4):
            self._fail("v85 error flags must be zero or the observed value 4")
        if len(motlist.slots) > 0xFFFFFFFF:
            self._fail("MOTLIST slot count exceeds u32")
        ids = [slot.motion_id for slot in motlist.slots]
        if len(ids) != len(set(ids)):
            self._fail("MOTLIST motion IDs must be unique")
        if any(not 0 <= value <= 0xFFFF for value in ids):
            self._fail("MOTLIST motion ID exceeds u16")

        validated_payloads: set[int] = set()
        payload_types: dict[int, MotionSlotType] = {}
        for slot in motlist.slots:
            try:
                slot_type = MotionSlotType(slot.slot_type)
            except ValueError as exc:
                raise MotionValidationError(f"unsupported v85 slot type {slot.slot_type}") from exc
            if len(slot.overrides) > 0xFF:
                self._fail("MOTLIST sequence override count exceeds u8")
            if slot.flags not in self._ACTIVE_FLAGS:
                self._fail(f"unsupported v85 slot flags 0x{slot.flags:X}")
            if slot.physics_group_flags != 0 or slot.joint_mask_id != 0:
                self._fail("DMC5 v85 physics flags and joint-mask ID must be zero")
            if not 0 <= slot.tag_hash <= 0xFFFFFFFF:
                self._fail("MOTLIST tag hash exceeds u32")
            if slot_type == MotionSlotType.MOT_TREE and slot.overrides:
                self._fail("v85 MotTree slots cannot own sequence overrides")
            if (
                slot.flags & MotionSlotFlags.LOCAL_TREE
                and slot_type != MotionSlotType.MOT_TREE
            ):
                self._fail("LocalTree flag requires a MotTree slot")

            if slot.payload is None:
                if motlist.base_motion_list_path is None:
                    self._fail("null v85 slot requires a base MOTLIST path")
                if slot_type != MotionSlotType.MOT:
                    self._fail("v85 inherited null slots must be MOT slots")
                if slot.flags not in (
                    MotionSlotFlags.NONE,
                    MotionSlotFlags.SEQUENCE_ONLY,
                ):
                    self._fail("v85 inherited null slot flags must be zero or SeqOnly")
            else:
                if slot.flags & MotionSlotFlags.SEQUENCE_ONLY:
                    self._fail("SeqOnly slot cannot own an embedded payload")
                payload_id = id(slot.payload)
                previous_type = payload_types.setdefault(payload_id, slot_type)
                if previous_type != slot_type:
                    self._fail("aliased payload is referenced with conflicting slot types")
                value = slot.payload.value
                if slot_type == MotionSlotType.MOT and not isinstance(value, Motion):
                    self._fail("MOT slot payload is not a Motion")
                if slot_type == MotionSlotType.MOT_TREE and not isinstance(value, MotTree):
                    self._fail("MotTree slot payload is not a MotTree")
                if validate_nested and payload_id not in validated_payloads:
                    if isinstance(value, Motion):
                        self.mot_validator.validate(value)
                    else:
                        self.tree_validator.validate(value)
                    validated_payloads.add(payload_id)

            if validate_nested:
                for sequence in slot.overrides:
                    self.sequence_validator.validate(
                        sequence,
                        allowed_categories=self.profile.override_categories,
                    )

    @staticmethod
    def _utf16(value: str, what: str) -> None:
        if "\0" in value:
            raise MotionValidationError(f"{what} contains NUL")
        value.encode("utf-16le")

    @staticmethod
    def _fail(message: str) -> None:
        raise MotionValidationError(message)
