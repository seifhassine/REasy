from __future__ import annotations

import struct

from ..binary import pad_to_alignment
from ..mot.writer import MotV65Writer
from ..mot_tree.writer import MotTreeV4Writer
from ..profiles import MotionFormatProfile
from ..sequence.writer import SequenceV65Writer
from .model import MotList, MotionSlotType
from .validator import MotListV85Validator


class MotListV85Writer:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(motlist=85, mot=65, mot_clip=27, mot_tree=4)
        self.profile = profile
        self.validator = MotListV85Validator(profile)
        self.mot_writer = MotV65Writer(profile)
        self.tree_writer = MotTreeV4Writer(profile)
        self.sequence_writer = SequenceV65Writer(profile)

    def build(self, motlist: MotList) -> bytes:
        self.validator.validate(motlist)
        slots = sorted(motlist.slots, key=lambda slot: slot.motion_id)
        layout = self.profile.motlist
        out = bytearray(layout.header_size)
        name_offset = len(out)
        out.extend(motlist.name.encode("utf-16le") + b"\0\0")
        base_path_offset = 0
        if motlist.base_motion_list_path is not None:
            base_path_offset = len(out)
            out.extend(motlist.base_motion_list_path.encode("utf-16le") + b"\0\0")
        pad_to_alignment(out, 16)
        pointer_table = len(out)
        out.extend(bytes(len(slots) * 8))

        payload_offsets: dict[int, int] = {}
        for slot in slots:
            if slot.payload is None or id(slot.payload) in payload_offsets:
                continue
            pad_to_alignment(out, layout.main_alignment)
            payload_offsets[id(slot.payload)] = len(out)
            if slot.slot_type == MotionSlotType.MOT:
                out.extend(self.mot_writer.build(slot.payload.value))
            else:
                out.extend(self.tree_writer.build(slot.payload.value))
        pad_to_alignment(out, 16)
        element_table = len(out)
        out.extend(bytes(len(slots) * layout.row_size))

        for index, slot in enumerate(slots):
            data_offset = 0 if slot.payload is None else payload_offsets[id(slot.payload)]
            struct.pack_into("<Q", out, pointer_table + index * 8, data_offset)
            row = element_table + index * layout.row_size
            struct.pack_into(
                "<QHBBIBBHI",
                out,
                row,
                0,
                slot.motion_id,
                int(slot.slot_type),
                slot.flags,
                slot.tag_hash,
                slot.physics_group_flags,
                len(slot.overrides),
                0,
                slot.joint_mask_id,
            )

        for index, slot in enumerate(slots):
            if not slot.overrides:
                continue
            pad_to_alignment(out, layout.override_table_alignment)
            table = len(out)
            struct.pack_into("<Q", out, element_table + index * layout.row_size, table)
            out.extend(bytes(len(slot.overrides) * 8))
            for sequence_index, sequence in enumerate(slot.overrides):
                pad_to_alignment(out, layout.sequence_alignment)
                sequence_offset = len(out)
                struct.pack_into("<Q", out, table + sequence_index * 8, sequence_offset)
                out.extend(
                    self.sequence_writer.build(
                        sequence,
                        sequence_offset=sequence_offset,
                        pointer_base=0,
                        allowed_categories=self.profile.override_categories,
                    )
                )

        struct.pack_into(
            "<I4sIIQQQQI",
            out,
            0,
            layout.version,
            b"mlst",
            motlist.error_flags,
            0,
            pointer_table,
            element_table,
            name_offset,
            base_path_offset,
            len(slots),
        )
        return bytes(out)

