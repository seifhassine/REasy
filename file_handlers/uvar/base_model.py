from abc import ABC, abstractmethod
import struct
import uuid
from typing import Any, Union, Tuple
from contextlib import contextmanager

class FileHandler:
    def __init__(self, data: Union[bytes, bytearray], offset: int = 0):
        self.data = bytearray(data) if isinstance(data, bytes) else data
        self.position = 0
        self.offset = offset
        
    @property
    def tell(self) -> int:
        return self.offset + self.position
        
    def seek(self, pos: int):
        self.position = pos - self.offset
        
    def seek_relative(self, pos: int):
        self.position = pos
        
    @contextmanager
    def seek_temp(self, pos: int):
        saved = self.position
        try:
            self.seek_relative(pos)
            yield
        finally:
            self.position = saved
    
    def skip(self, count: int):
        self.position += count
        
    def align(self, alignment: int):
        padding = (alignment - (self.position % alignment)) % alignment
        if padding > 0:
            self.skip(padding)
            
    def align_write(self, alignment: int):
        padding = (alignment - (self.position % alignment)) % alignment
        if padding > 0:
            self.write_bytes(b'\x00' * padding)
    
    def read(self, fmt: str) -> Any:
        size = struct.calcsize(fmt)
        data = self.read_bytes(size)
        result = struct.unpack_from(fmt, data, 0)
        return result[0] if len(result) == 1 else result
    
    def write(self, fmt: str, *values):
        size = struct.calcsize(fmt)
        self._ensure_capacity(self.tell + size)
        struct.pack_into(fmt, self.data, self.tell, *values)
        self.position += size
        
    def read_bytes(self, count: int) -> bytes:
        available = len(self.data) - self.tell
        if available < count:
            raise ValueError(f"Attempted to read {count} bytes but only {available} bytes available at position {self.tell}")
        result = self.data[self.tell:self.tell + count]
        self.position += count
        return bytes(result)
        
    def write_bytes(self, data: bytes):
        self._ensure_capacity(self.tell + len(data))
        self.data[self.tell:self.tell + len(data)] = data
        self.position += len(data)
        
    def read_string(self, encoding: str = 'utf-8', null_terminated: bool = True) -> str:
        if null_terminated:
            end = self.data.find(0, self.tell)
            if end == -1:
                end = len(self.data)
            result = self.data[self.tell:end].decode(encoding)
            self.position = end - self.offset + 1  
            return result
        else:
            length = self.read('<I')
            return self.read_bytes(length).decode(encoding)
    
    def write_string(self, value: str, encoding: str = 'utf-8', null_terminated: bool = True):
        encoded = value.encode(encoding)
        if null_terminated:
            self.write_bytes(encoded + b'\x00')
        else:
            self.write('<I', len(encoded))
            self.write_bytes(encoded)
    
    def read_wstring(self, null_terminated: bool = True) -> str:
        if null_terminated:
            end = self.tell
            while end < len(self.data) - 1:
                if self.data[end] == 0 and self.data[end + 1] == 0:
                    break
                end += 2
            result = self.data[self.tell:end].decode('utf-16le')
            self.position = end - self.offset + 2 
            return result
        else:
            length = self.read('<I')
            return self.read_bytes(length * 2).decode('utf-16le')
    
    def write_wstring(self, value: str, null_terminated: bool = True):
        encoded = value.encode('utf-16le')
        if null_terminated:
            self.write_bytes(encoded + b'\x00\x00')
        else:
            self.write('<I', len(value))
            self.write_bytes(encoded)
    
    def read_guid(self) -> uuid.UUID:
        return uuid.UUID(bytes=self.read_bytes(16))
    
    def write_guid(self, guid: uuid.UUID):
        self.write_bytes(guid.bytes)
    
    def read_at(self, offset: int, fmt: str) -> tuple:
        saved_pos = self.position
        self.seek(offset)
        result = self.read(fmt)
        self.position = saved_pos
        return result
    
    def write_at(self, offset: int, fmt: str, *values):
        saved_pos = self.position
        self.seek(offset)
        self.write(fmt, *values)
        self.position = saved_pos
    
    def _ensure_capacity(self, required_size: int):
        if required_size > len(self.data):
            new_size = max(required_size, len(self.data) * 2)
            self.data.extend(b'\x00' * (new_size - len(self.data)))
    
    def get_bytes(self) -> bytes:
        return bytes(self.data[:self.tell])

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