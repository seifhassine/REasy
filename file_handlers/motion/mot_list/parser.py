from __future__ import annotations

from dataclasses import dataclass

from ..binary import ReadContext, align_up
from ..errors import MotionParseError
from ..mot.parser import MotV65Parser
from ..mot_tree.parser import MotTreeV4Parser
from ..profiles import MotionFormatProfile
from ..sequence.parser import SequenceV65Parser
from .model import (
    EmbeddedPayload,
    MotList,
    MotionSlot,
    MotionSlotFlags,
    MotionSlotType,
)
from .validator import MotListV85Validator


@dataclass(frozen=True, slots=True)
class MotListV85RowLayout:
    pointer: int
    override_table: int
    motion_id: int
    slot_type: MotionSlotType
    flags: MotionSlotFlags
    tag_hash: int
    physics: int
    override_count: int
    joint_mask: int


@dataclass(frozen=True, slots=True)
class MotListV85PayloadLayout:
    offset: int
    physical_end: int
    slot_type: MotionSlotType


@dataclass(frozen=True, slots=True)
class MotListV85Layout:
    context: ReadContext
    name: str
    base_path: str | None
    error_flags: int
    element_table: int
    rows: tuple[MotListV85RowLayout, ...]
    payloads: tuple[MotListV85PayloadLayout, ...]


class MotListV85Parser:
    def __init__(self, profile: MotionFormatProfile):
        profile.require_versions(motlist=85, mot=65, mot_clip=27, mot_tree=4)
        self.profile = profile
        self.validator = MotListV85Validator(profile)
        self.mot_parser = MotV65Parser(profile)
        self.tree_parser = MotTreeV4Parser(profile)
        self.sequence_parser = SequenceV65Parser(profile)

    def parse(self, data: bytes | bytearray | memoryview, *, label: str = "MOTLIST") -> MotList:
        scanned = self.scan_layout(data, label=label)
        c = scanned.context
        layout = self.profile.motlist
        payloads: dict[int, EmbeddedPayload] = {}
        for payload in scanned.payloads:
            parser = (
                self.mot_parser
                if payload.slot_type == MotionSlotType.MOT
                else self.tree_parser
            )
            payloads[payload.offset] = EmbeddedPayload(
                parser.parse(c, payload.offset, payload.physical_end)
            )

        slots = [
            MotionSlot(
                row.motion_id,
                row.slot_type,
                payloads.get(row.pointer),
                row.flags,
                row.tag_hash,
                row.physics,
                row.joint_mask,
                [],
            )
            for row in scanned.rows
        ]

        cursor = scanned.element_table + len(scanned.rows) * layout.row_size
        for slot, row in zip(slots, scanned.rows):
            override_count = row.override_count
            override_table = row.override_table
            if not override_count:
                if override_table:
                    raise MotionParseError(f"{label}: empty override table has a pointer")
                continue
            expected_table = align_up(cursor, layout.override_table_alignment)
            c.require_zero(cursor, expected_table, "override pointer-table alignment")
            if override_table != expected_table:
                raise MotionParseError(f"{label}: override pointer table violates writer cursor")
            sequence_pointers = c.read_array(
                override_table,
                override_count,
                8,
                c.u64,
                "override sequence pointer table",
            )
            cursor = override_table + override_count * 8
            for sequence_pointer in sequence_pointers:
                expected_sequence = align_up(cursor, layout.sequence_alignment)
                c.require_zero(cursor, expected_sequence, "override sequence alignment")
                if sequence_pointer != expected_sequence:
                    raise MotionParseError(f"{label}: override SequenceData violates writer cursor")
                sequence = self.sequence_parser.parse(
                    c,
                    sequence_pointer,
                    pointer_base=0,
                    allowed_categories=self.profile.override_categories,
                )
                slot.overrides.append(sequence)
                cursor = self.sequence_parser.physical_end(c, sequence_pointer, pointer_base=0)
        if cursor != len(c.data):
            raise MotionParseError(
                f"{label}: trailing or unmodeled bytes [0x{cursor:X}, 0x{len(c.data):X})"
            )
        result = MotList(
            scanned.name,
            slots,
            scanned.base_path,
            scanned.error_flags,
        )
        # Embedded parsers already validated their semantic graphs.
        self.validator.validate(result, validate_nested=False)
        return result

    def scan_layout(
        self,
        data: bytes | bytearray | memoryview,
        *,
        label: str = "MOTLIST",
    ) -> MotListV85Layout:
        c = ReadContext.from_bytes(data, label)
        layout = self.profile.motlist
        c.require(0, layout.header_size, "MOTLIST header")
        if c.u32(0) != layout.version or c.bytes(4, 4) != b"mlst":
            raise MotionParseError(f"{label}: expected MOTLIST v{layout.version}")
        error_flags = c.u32(8)
        if error_flags not in (0, 4) or c.u32(0xC):
            raise MotionParseError(f"{label}: unsupported MOTLIST error/master state")
        pointer_table = c.u64(0x10)
        element_table = c.u64(0x18)
        name_offset = c.u64(0x20)
        base_path_offset = c.u64(0x28)
        count = c.u32(0x30)
        if name_offset != layout.header_size:
            raise MotionParseError(f"{label}: v85 name must follow the packed header")
        name, cursor = c.utf16_z(name_offset, "MOTLIST name")
        base_path = None
        if base_path_offset:
            if base_path_offset != cursor:
                raise MotionParseError(f"{label}: base MOTLIST path violates writer cursor")
            base_path, cursor = c.utf16_z(base_path_offset, "base MOTLIST path")
        expected_pointer_table = align_up(cursor, 16)
        c.require_zero(cursor, expected_pointer_table, "MOTLIST pointer-table alignment")
        if pointer_table != expected_pointer_table:
            raise MotionParseError(f"{label}: main pointer table violates writer cursor")
        pointers = c.read_array(pointer_table, count, 8, c.u64, "MOTLIST main pointer table")
        c.require(element_table, count * layout.row_size, "MOTLIST element table")

        rows = []
        for index in range(count):
            row = element_table + index * layout.row_size
            try:
                slot_type = MotionSlotType(c.u8(row + 0xA))
            except ValueError as exc:
                raise MotionParseError(f"{label}: unsupported v85 slot type") from exc
            if c.u16(row + 0x12):
                raise MotionParseError(f"{label}: nonzero v85 element reserved field")
            rows.append(
                MotListV85RowLayout(
                    pointers[index],
                    c.u64(row),
                    c.u16(row + 8),
                    slot_type,
                    MotionSlotFlags(c.u8(row + 0xB)),
                    c.u32(row + 0xC),
                    c.u8(row + 0x10),
                    c.u8(row + 0x11),
                    c.u32(row + 0x14),
                )
            )
        ids = [row.motion_id for row in rows]
        if ids != sorted(ids):
            raise MotionParseError(f"{label}: v85 element rows are not sorted by motion ID")

        first_occurrence_offsets = []
        first_occurrence_types = {}
        seen = set()
        for row in rows:
            pointer = row.pointer
            if pointer and pointer not in seen:
                seen.add(pointer)
                first_occurrence_offsets.append(pointer)
                first_occurrence_types[pointer] = row.slot_type
            elif pointer and first_occurrence_types[pointer] != row.slot_type:
                raise MotionParseError(f"{label}: aliased payload has conflicting slot types")
        if first_occurrence_offsets != sorted(first_occurrence_offsets):
            raise MotionParseError(f"{label}: main payloads violate first-occurrence physical order")

        main_cursor = align_up(pointer_table + count * 8, layout.main_alignment)
        c.require_zero(pointer_table + count * 8, main_cursor, "MOTLIST main-data alignment")
        payloads = []
        for index, offset in enumerate(first_occurrence_offsets):
            if offset != main_cursor:
                raise MotionParseError(f"{label}: main payload violates writer cursor")
            physical_end = (
                first_occurrence_offsets[index + 1]
                if index + 1 < len(first_occurrence_offsets)
                else element_table
            )
            c.require(offset, physical_end - offset, "MOTLIST main payload")
            payloads.append(MotListV85PayloadLayout(
                offset,
                physical_end,
                first_occurrence_types[offset],
            ))
            main_cursor = physical_end
        expected_elements = align_up(main_cursor, 16)
        c.require_zero(main_cursor, expected_elements, "MOTLIST element-table alignment")
        if element_table != expected_elements:
            raise MotionParseError(f"{label}: element table violates writer cursor")

        return MotListV85Layout(
            c,
            name,
            base_path,
            error_flags,
            element_table,
            tuple(rows),
            tuple(payloads),
        )
