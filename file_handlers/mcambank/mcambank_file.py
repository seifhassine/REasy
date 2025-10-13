from __future__ import annotations

import struct
from dataclasses import dataclass, field
from enum import IntFlag
from typing import List

from utils.binary_handler import BinaryHandler

MCAMBANK_MAGIC = 0x6B6E6263


class ErrFlags(IntFlag):
    NONE = 0
    EMPTY = 1
    NOT_FOUND_REF_ASSET = 2
    NOT_FOUND_INCLUDE_ASSET = 4


@dataclass
class MotionCameraBankElement:
    offset: int = 0
    bank_id: int = 0
    bank_type: int = 0
    bank_type_mask_bit: int = 0
    padding: int = 0
    path: str = ""

    def read(self, handler: BinaryHandler):
        self.offset = handler.read_uint64()
        self.bank_id = handler.read_uint32()
        self.bank_type = handler.read_uint32()
        self.bank_type_mask_bit = handler.read_uint32()
        self.padding = handler.read_uint32()
        if self.offset == 0:
            self.path = ""
        else:
            with handler.seek_jump_back(self.offset):
                self.path = handler.read_wstring()

    def write(self, handler: BinaryHandler):
        handler.write_offset_wstring(self.path)
        handler.write_uint32(self.bank_id)
        handler.write_uint32(self.bank_type)
        handler.write_uint32(self.bank_type_mask_bit)
        handler.write_uint32(self.padding)


@dataclass
class McambankFile:
    version: int = 3
    magic: int = MCAMBANK_MAGIC
    err_flags: ErrFlags = ErrFlags.NONE
    master_size: int = 0
    motbank_element_offset: int = 0
    user_variables_offset: int = 0
    joint_map_offset: int = 0
    motbank_element_count: int = 0
    reserved1: int = 0
    user_variables_path: str = ""
    joint_map_path: str = ""
    items: List[MotionCameraBankElement] = field(default_factory=list)

    @staticmethod
    def can_handle(data: bytes) -> bool:
        if len(data) < 8:
            return False
        try:
            magic = struct.unpack_from('<I', data, 4)[0]
        except struct.error:
            return False
        return magic == MCAMBANK_MAGIC

    def read(self, data: bytes) -> bool:
        handler = BinaryHandler(data)
        self.version = handler.read_uint32()
        self.magic = handler.read_uint32()
        if self.magic != MCAMBANK_MAGIC:
            raise ValueError("Not a MCAMBANK file")

        self.err_flags = ErrFlags(handler.read_uint32())
        self.master_size = handler.read_uint32()
        self.motbank_element_offset = handler.read_uint64()
        self.user_variables_offset = handler.read_uint64()
        if self.version <= 1:
            self.motbank_element_count = handler.read_uint32()
            self.joint_map_offset = handler.read_uint64()
        else:
            self.joint_map_offset = handler.read_uint64()
            self.motbank_element_count = handler.read_uint32()
        self.reserved1 = handler.read_uint32()

        self.user_variables_path = ""
        if self.user_variables_offset:
            with handler.seek_jump_back(self.user_variables_offset):
                self.user_variables_path = handler.read_wstring()

        self.joint_map_path = ""
        if self.joint_map_offset:
            with handler.seek_jump_back(self.joint_map_offset):
                self.joint_map_path = handler.read_wstring()

        self.items = []
        if self.motbank_element_offset and self.motbank_element_count:
            handler.seek(self.motbank_element_offset)
            for _ in range(self.motbank_element_count):
                entry = MotionCameraBankElement()
                entry.read(handler)
                self.items.append(entry)

        return True

    def write(self) -> bytes:
        handler = BinaryHandler(bytearray())

        self.motbank_element_count = len(self.items)

        self.master_size &= 0xFFFFFFFF

        handler.write_uint32(self.version)
        handler.write_uint32(MCAMBANK_MAGIC)
        handler.write_uint32(int(self.err_flags))
        handler.write_uint32(self.master_size)

        motbank_tbl_offset_pos = handler.tell
        handler.write_uint64(0)

        user_variables_offset_pos = handler.tell
        if self.user_variables_path:
            handler.write_offset_wstring(self.user_variables_path)
        else:
            handler.write_uint64(0)

        if self.version <= 1:
            handler.write_uint32(self.motbank_element_count)
            joint_map_offset_pos = handler.tell
            if self.joint_map_path:
                handler.write_offset_wstring(self.joint_map_path)
            else:
                handler.write_uint64(0)
            handler.write_uint32(self.reserved1)
        else:
            joint_map_offset_pos = handler.tell
            if self.joint_map_path:
                handler.write_offset_wstring(self.joint_map_path)
            else:
                handler.write_uint64(0)
            handler.write_uint32(self.motbank_element_count)
            handler.write_uint32(self.reserved1)

        handler.string_table_flush()

        if self.user_variables_path:
            self.user_variables_offset = handler.read_at(user_variables_offset_pos, '<Q')
        else:
            self.user_variables_offset = 0

        if self.joint_map_path:
            self.joint_map_offset = handler.read_at(joint_map_offset_pos, '<Q')
        else:
            self.joint_map_offset = 0

        handler.align_write(16)
        motbank_table_offset = handler.tell
        for item in self.items:
            item.write(handler)

        handler.write_int64_at(motbank_tbl_offset_pos, motbank_table_offset)
        self.motbank_element_offset = motbank_table_offset

        handler.string_table_flush(dedup=False)

        return handler.get_bytes()
