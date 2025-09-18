from abc import ABC, abstractmethod
from contextlib import contextmanager
from typing import Union
import uuid

from utils.binary_handler import BinaryHandler


class FileHandler(BinaryHandler):

    def __init__(self, data: Union[bytes, bytearray], offset: int = 0):
        super().__init__(data, offset)

    def align(self, alignment: int):
        padding = (alignment - (self.position % alignment)) % alignment
        if padding > 0:
            self.skip(padding)

    def align_write(self, alignment: int):
        padding = (alignment - (self.position % alignment)) % alignment
        if padding > 0:
            self.write_bytes(b'\x00' * padding)

    @contextmanager
    def seek_temp(self, pos: int):
        saved = self.position
        try:
            self.seek_relative(pos)
            yield
        finally:
            self.position = saved

    def read_guid(self) -> uuid.UUID:
        return uuid.UUID(bytes=super().read_guid())

    def write_guid(self, guid: uuid.UUID):
        if isinstance(guid, uuid.UUID):
            data = guid.bytes
        elif isinstance(guid, bytes):
            if len(guid) != 16:
                raise ValueError(f"GUID byte length must be 16, got {len(guid)}")
            data = guid
        elif isinstance(guid, str):
            data = uuid.UUID(guid).bytes
        else:
            raise TypeError("GUID must be uuid.UUID, bytes, or str")
        self.write_bytes(data)

class BaseModel(ABC):
    def __init__(self):
        self.start_offset = 0
        
    def read(self, handler: FileHandler) -> bool:
        self.start_offset = handler.tell
        return self.do_read(handler)
        
    def write(self, handler: FileHandler) -> bool:
        self.start_offset = handler.tell
        return self.do_write(handler)
        
    @abstractmethod
    def do_read(self, handler: FileHandler) -> bool:
        pass
        
    @abstractmethod  
    def do_write(self, handler: FileHandler) -> bool:
        pass