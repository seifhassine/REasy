"""
Common binary file handling utilities.

This module provides a unified BinaryHandler class that can be used
by different file format handlers to avoid code duplication.
"""

import struct
import uuid
from typing import Any, Union, List, Tuple
from contextlib import contextmanager


class BinaryHandler:
    
    def __init__(self, data: Union[bytes, bytearray], offset: int = 0, file_version: int = 0, file_path: str = ""):
        self.data = bytearray(data) if isinstance(data, bytes) else data
        self.position = 0
        self.offset = offset
        self.file_version = file_version
        self.file_path = file_path
        self.string_table_offsets = []
        self.offset_content_table = []
        
    @property
    def tell(self) -> int:
        """Get current position in file."""
        return self.offset + self.position
        
    def seek(self, pos: int):
        """Seek to absolute position."""
        if pos < 0:
            raise ValueError(f"Cannot seek to negative position: {pos}")
        self.position = pos - self.offset
        
    def seek_relative(self, pos: int):
        """Seek to position relative to offset."""
        self.position = pos
        
    @contextmanager
    def seek_temp(self, pos: int):
        """Context manager for temporary seek operations."""
        saved = self.position
        try:
            self.seek(pos)
            yield
        finally:
            self.position = saved
            
    @contextmanager
    def seek_jump_back(self, pos: int):
        """Context manager for temporary seek operations (alias for seek_temp)."""
        saved = self.position
        try:
            self.seek(pos)
            yield
        finally:
            self.position = saved
    
    def skip(self, count: int):
        self.position += count
        
    def align(self, alignment: int):
        padding = (alignment - (self.tell % alignment)) % alignment
        if padding > 0:
            self.skip(padding)
            
    def align_write(self, alignment: int):
        padding = (alignment - (self.tell % alignment)) % alignment
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
        
    def _ensure_capacity(self, required: int):
        if required > len(self.data):
            self.data.extend(b'\x00' * (required - len(self.data)))
    
    def read_uint8(self) -> int:
        return self.read('<B')
    
    def read_int8(self) -> int:
        return self.read('<b')
    
    def read_uint16(self) -> int:
        return self.read('<H')
    
    def read_int16(self) -> int:
        return self.read('<h')
    
    def read_int32(self) -> int:
        return self.read('<i')
    
    def read_uint32(self) -> int:
        return self.read('<I')
    
    def read_int64(self) -> int:
        return self.read('<q')
    
    def read_uint64(self) -> int:
        return self.read('<Q')
    
    def read_float(self) -> float:
        return self.read('<f')
    
    def read_double(self) -> float:
        return self.read('<d')
    
    def write_uint8(self, value: int):
        self.write('<B', value)
    
    def write_int8(self, value: int):
        self.write('<b', value)
    
    def write_uint16(self, value: int):
        self.write('<H', value)
    
    def write_int16(self, value: int):
        self.write('<h', value)
    
    def write_int32(self, value: int):
        self.write('<i', value)
    
    def write_uint32(self, value: int):
        self.write('<I', value)
    
    def write_uint64(self, value: int):
        self.write('<Q', value)
    
    def write_int64(self, value: int):
        self.write('<q', value)
    
    def write_float(self, value: float):
        self.write('<f', value)
    
    def write_double(self, value: float):
        self.write('<d', value)
    
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
    
    def read_offset_wstring(self) -> str:
        offset = self.read_uint64()
        if offset == 0:
            return ""
        
        current_pos = self.tell
        
        if offset >= len(self.data):
            print(f"Warning: Invalid string offset {offset} (file size: {len(self.data)})")
            return ""
        
        self.seek(offset)
        result = self.read_wstring()
        self.seek(current_pos)
        return result
    
    def write_offset_wstring(self, value: str, context: str = None):
        offset_pos = self.tell
        self.write_uint64(0) 
        self.string_table_offsets.append((offset_pos, value))
    
    def read_guid(self) -> bytes:
        return self.read_bytes(16)
    
    def write_guid(self, guid: bytes):
        if len(guid) != 16:
            raise ValueError(f"GUID must be exactly 16 bytes, got {len(guid)}")
        self.write_bytes(guid)
    
    def read_vec3(self) -> Tuple[float, float, float]:
        return self.read('<fff')
    
    def write_vec3(self, x: float, y: float, z: float):
        self.write('<fff', x, y, z)
    
    def read_vec4(self) -> Tuple[float, float, float, float]:
        return self.read('<ffff')
    
    def write_vec4(self, x: float, y: float, z: float, w: float):
        self.write('<ffff', x, y, z, w)
    
    def read_matrix4x4(self) -> List[float]:
        return list(self.read('<16f'))
    
    def write_matrix4x4(self, matrix: List[float]):
        if len(matrix) != 16:
            raise ValueError(f"Matrix must have 16 elements, got {len(matrix)}")
        self.write('<16f', *matrix)
    
    # Additional methods for compatibility
    def read_byte(self) -> int:
        return self.read_uint8()
    
    def read_bool(self) -> bool:
        return self.read('<?')
    
    def read_short(self) -> int:
        return self.read_int16()
    
    def read_ushort(self) -> int:
        return self.read_uint16()
    
    def write_byte(self, value: int):
        self.write_uint8(value)
    
    def write_bool(self, value: bool):
        self.write('<?', value)
    
    def write_short(self, value: int):
        self.write_int16(value)
    
    def write_ushort(self, value: int):
        self.write_uint16(value)
    
    def read_at(self, offset: int, fmt: str) -> Any:
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
    
    def write_int64_at(self, offset: int, value: int):
        self.write_at(offset, '<q', value)
    
    def read_list(self, lst: List, count: int):
        lst.clear()
        for _ in range(count):
            if hasattr(lst, '_item_type'):
                item = lst._item_type()
                item.read(self)
                lst.append(item)
            else:
                lst.append(self.read_guid())
    
    def write_list(self, lst: List):
        for item in lst:
            if isinstance(item, uuid.UUID):
                self.write_guid(item.bytes_le)
            elif hasattr(item, 'write'):
                item.write(self)
            else:
                self.write_int32(item)
    
    def offset_content_table_add(self, writer_func):
        self.offset_content_table.append((self.tell, writer_func))
    
    def string_table_flush(self):
        sorted_entries = sorted(self.string_table_offsets, key=lambda x: x[0])
        
        first_offset_by_string = {}
        for offset_pos, string in sorted_entries:
            if string not in first_offset_by_string:
                first_offset_by_string[string] = self.tell
            self.write_wstring(string)
            self.write_at(offset_pos, '<q', first_offset_by_string[string])
        
        self.string_table_offsets.clear()
    
    def offset_content_table_flush(self):
        for offset, writer_func in self.offset_content_table:
            content_offset = self.tell
            writer_func(self)
            self.write_at(offset, '<q', content_offset)
        self.offset_content_table.clear()
    
    def clear(self):
        self.data = bytearray()
        self.position = 0
        self.string_table_offsets.clear()
        self.offset_content_table.clear()
    
    def get_bytes(self) -> bytes:
        return bytes(self.data[:self.tell])
    
    def get_all_bytes(self) -> bytes:
        end = len(self.data)
        while end > 0 and self.data[end-1] == 0:
            end -= 1
        end = max(end, self.tell)
        return bytes(self.data[:end])