import uuid
from typing import List

from utils.binary_handler import BinaryHandler


CFIL_MAGIC = 0x4C494643


class CfilFile:

    def __init__(self):
        self.version: int = 7
        self.status: int = 0
        self.layerGuid: uuid.UUID = uuid.UUID(int=0)
        self.materialIdGuid: uuid.UUID = uuid.UUID(int=0)
        self.materialAttributeGuids: List[uuid.UUID] = []
        self.mask_guids: List[uuid.UUID] = []
        self.layer_index: int = 0
        self.mask_ids: List[int] = []

    @staticmethod
    def _read_uuid_le(handler: BinaryHandler) -> uuid.UUID:
        return uuid.UUID(bytes_le=handler.read_bytes(16))

    @staticmethod
    def _write_uuid_le(handler: BinaryHandler, value: uuid.UUID):
        handler.write_bytes(value.bytes_le)

    def read(self, data: bytes, version: int = 0) -> bool:
        handler = BinaryHandler(bytearray(data))

        if len(data) < 4:
            raise ValueError("File too small for CFIL")

        magic = handler.read('<I')
        if magic != CFIL_MAGIC:
            raise ValueError("Invalid CFIL magic")

        if version == 0:
            version = 7
        
        self.version = version
        
        if version == 7:
            numMaskGuids = handler.read('<i')
            numMaterialAttributeGuids = handler.read('<i')
            status = handler.read('<I')
            layerGuid = self._read_uuid_le(handler)
            materialIdGuid = self._read_uuid_le(handler)
            maskGuidsTblOffset = handler.read('<Q')
            materialAttributeGuidsOffset = handler.read('<Q')
            
            self.status = status
            self.layerGuid = layerGuid
            self.materialIdGuid = materialIdGuid
            
            handler.seek(maskGuidsTblOffset)
            self.mask_guids = []
            for _ in range(numMaskGuids):
                self.mask_guids.append(self._read_uuid_le(handler))
            
            handler.seek(materialAttributeGuidsOffset)
            self.materialAttributeGuids = []
            for _ in range(numMaterialAttributeGuids):
                self.materialAttributeGuids.append(self._read_uuid_le(handler))
            
            return True

        handler.seek(4)
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
        mat_attr_count = len(self.materialAttributeGuids or [])
        maskGuidsTblOffset = 64
        materialAttributeGuidsOffset = maskGuidsTblOffset + mask_count * 16
        
        handler.write('<i', mask_count)
        handler.write('<i', mat_attr_count)
        handler.write('<I', self.status)
        self._write_uuid_le(handler, self.layerGuid or uuid.UUID(int=0))
        self._write_uuid_le(handler, self.materialIdGuid or uuid.UUID(int=0))
        handler.write('<Q', maskGuidsTblOffset)
        handler.write('<Q', materialAttributeGuidsOffset)
        
        for g in (self.mask_guids or []):
            self._write_uuid_le(handler, g)
        for g in (self.materialAttributeGuids or []):
            self._write_uuid_le(handler, g)
        
        return bytes(handler.data)

