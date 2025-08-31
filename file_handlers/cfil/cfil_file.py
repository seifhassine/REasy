import uuid
from typing import List

from utils.binary_handler import BinaryHandler


CFIL_MAGIC = 0x4C494643


class CfilFile:

    def __init__(self):
        self.version: int = 7
        self.ukn_offset: int = 0
        self.layer_guid: uuid.UUID = uuid.UUID(int=0)
        self.mask_guids: List[uuid.UUID] = []
        self.layer_index: int = 0
        self.mask_ids: List[int] = []

    @staticmethod
    def _read_uuid_le(handler: BinaryHandler) -> uuid.UUID:
        return uuid.UUID(bytes_le=handler.read_bytes(16))

    @staticmethod
    def _write_uuid_le(handler: BinaryHandler, value: uuid.UUID):
        handler.write_bytes(value.bytes_le)

    def read(self, data: bytes) -> bool:
        handler = BinaryHandler(bytearray(data))

        if len(data) < 4:
            raise ValueError("File too small for CFIL")

        magic = handler.read('<I')
        if magic != CFIL_MAGIC:
            raise ValueError("Invalid CFIL magic")

        with handler.seek_temp(4):
            try:
                mask_count = handler.read('<i')
                reserved8 = handler.read('<Q')
                layer_guid = self._read_uuid_le(handler)
                handler.skip(16)
                guid_list_off = handler.read('<Q')
                ukn_off = handler.read('<Q')

                if mask_count >= 0 and guid_list_off >= 0x40 and guid_list_off % 8 == 0:
                    data_len = len(handler.data)
                    guid_bytes = mask_count * 16
                    if guid_list_off + guid_bytes <= data_len:
                        self.version = 7
                        self.layer_guid = layer_guid
                        self.ukn_offset = ukn_off
                        self.mask_guids = []
                        handler.seek(guid_list_off)
                        for _ in range(mask_count):
                            self.mask_guids.append(self._read_uuid_le(handler))
                        return True
            except Exception:
                pass

        handler.seek(4)
        self.version = 3
        self.layer_index = handler.read('<B')
        mask_count_b = handler.read('<B')
        handler.skip(2)
        self.mask_ids = []
        if mask_count_b > 0:
            for _ in range(mask_count_b):
                self.mask_ids.append(handler.read('<B'))
            while handler.tell % 4 != 0 and handler.tell < len(handler.data):
                handler.skip(1)
        else:
            if handler.tell + 4 <= len(handler.data):
                sentinel = handler.read('<i')
                if sentinel != -1:
                    handler.seek(handler.tell - 4)
        return True

    def write(self) -> bytes:
        handler = BinaryHandler(bytearray())
        handler.write('<I', CFIL_MAGIC)

        if self.version == 3:
            mask_count = len(self.mask_ids or [])
            handler.write('<B', self.layer_index & 0xFF)
            handler.write('<B', mask_count & 0xFF)
            handler.write('<H', 0)
            if mask_count == 0:
                handler.write('<i', -1)
            else:
                for mid in self.mask_ids:
                    handler.write('<B', int(mid) & 0xFF)
                handler.align_write(4)
            handler.write('<Q', 0)
            handler.write('<Q', 0)
            return bytes(handler.data)

        mask_count = len(self.mask_guids or [])
        handler.write('<i', mask_count)
        handler.write('<Q', 0)
        self._write_uuid_le(handler, self.layer_guid or uuid.UUID(int=0))
        handler.write_bytes(b"\x00" * 16)
        guid_list_off = 64
        handler.write('<Q', guid_list_off)
        handler.write('<Q', guid_list_off + mask_count * 16)
        for g in (self.mask_guids or []):
            self._write_uuid_le(handler, g)
        return bytes(handler.data)

