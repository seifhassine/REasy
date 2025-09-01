import struct
from dataclasses import dataclass, field
from typing import List

from utils.binary_handler import BinaryHandler


MOTBANK_MAGIC = 0x6B6E626D


@dataclass
class MotlistItem:
	offset: int = 0
	bank_id: int = 0
	bank_type: int = 0
	bank_type_mask_bits: int = 0
	path: str = ""

	def read(self, handler: BinaryHandler, version: int):
		self.offset = handler.read_int64()
		if version in (3,4):
			self.bank_id = handler.read_int32()
			self.bank_type = handler.read_uint32()
		else:
			self.bank_id = handler.read_int64()
		self.bank_type_mask_bits = handler.read_int64()
		if self.offset == 0:
			self.path = ""
		else:
			with handler.seek_jump_back(self.offset):
				self.path = handler.read_wstring()

	def write(self, handler: BinaryHandler, version: int):
		handler.write_offset_wstring(self.path)
		if version in (3,4):
			handler.write_int32(int(self.bank_id))
			handler.write_uint32(self.bank_type)
		else:
			handler.write_int64(self.bank_id)
		handler.write_int64(self.bank_type_mask_bits)


class MotbankFile:
	EXTENSION = ".motbank"

	def __init__(self):
		self.version: int = 0
		self.magic: int = MOTBANK_MAGIC
		self.motlists_offset: int = 0
		self.uvar_offset: int = 0
		self.jmap_offset: int = 0
		self.motlist_count: int = 0

		self.uvar_path: str = ""
		self.jmap_path: str = ""
		self.items: List[MotlistItem] = []

	@staticmethod
	def can_handle(data: bytes) -> bool:
		if len(data) < 12:
			return False
		try:
			magic = struct.unpack_from('<I', data, 4)[0]
			return magic == MOTBANK_MAGIC
		except Exception:
			return False

	def read(self, data: bytes) -> bool:
		handler = BinaryHandler(data)
		self.version = handler.read_int32()
		self.magic = handler.read_uint32()
		if self.magic != MOTBANK_MAGIC:
			raise ValueError("Not a motbank file")
		handler.skip(8)
		self.motlists_offset = handler.read_int64()
		self.uvar_offset = handler.read_int64()
		if self.version in (3,4):
			self.jmap_offset = handler.read_int64()
		self.motlist_count = handler.read_int32()

		if self.uvar_offset == 0:
			self.uvar_path = ""
		else:
			with handler.seek_jump_back(self.uvar_offset):
				self.uvar_path = handler.read_wstring()

		if self.version in (3,4):
			if self.jmap_offset == 0:
				self.jmap_path = ""
			else:
				with handler.seek_jump_back(self.jmap_offset):
					self.jmap_path = handler.read_wstring()

		handler.seek(self.motlists_offset)
		self.items = []
		for _ in range(self.motlist_count):
			item = MotlistItem()
			item.read(handler, self.version)
			self.items.append(item)

		return True

	def write(self) -> bytes:
		handler = BinaryHandler(bytearray())

		self.motlist_count = len(self.items)
		handler.write_int32(self.version)
		handler.write_uint32(MOTBANK_MAGIC)
		handler.skip(8)

		motlists_offset_placeholder_pos = handler.tell
		handler.write_int64(0)
		if self.uvar_path:
			handler.write_offset_wstring(self.uvar_path)
		else:
			handler.write_int64(0)
		if self.version in (3,4):
			if self.jmap_path:
				handler.write_offset_wstring(self.jmap_path)
			else:
				handler.write_int64(0)
		handler.write_int32(self.motlist_count)

		handler.string_table_flush()

		handler.align_write(16)
		motlists_offset = handler.tell
		for item in self.items:
			item.write(handler, self.version)

		handler.write_int64_at(motlists_offset_placeholder_pos, motlists_offset)

		handler.string_table_flush()

		return handler.get_bytes()

