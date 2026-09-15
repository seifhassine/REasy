# Misc raw file operations

from typing import Tuple
import uuid


def align(offset, alignment=16):
    r = offset % alignment
    return offset if r == 0 else offset + (alignment - r)


def available(data: bytes, offset: int, size: int) -> bool:
    """Return True if there are at least 'size' bytes remaining in data from offset."""
    return offset + size <= len(data)


def read_wstring(data: bytes, offset: int, max_wchars: int) -> Tuple[str, int]:
    """
    Reads a UTF-16LE string from data starting at offset.
    Stops when two consecutive null bytes are found.
    Uses memoryview and direct decoding for better performance.
    """
    view = memoryview(data)
    pos = offset
    
    # Skip BOM if present
    if pos + 1 < len(data) and view[pos:pos+2].tobytes() == b"\xff\xfe":
        pos += 2
        
    # Find null terminator
    end = pos
    while end + 1 < len(data) and not (view[end] == 0 and view[end+1] == 0):
        end += 2
        if (end - pos) // 2 >= max_wchars:
            break
        
    string = view[pos:end].tobytes().decode('utf-16le')
    return string, end + 2  



def guid_le_to_str(guid_bytes: bytes) -> str:
    """Convert little-endian GUID bytes to string format"""
    if len(guid_bytes) != 16:
        return "00000000-0000-0000-0000-000000000000"
        
    try:
        guid = uuid.UUID(bytes_le=bytes(guid_bytes))  # Ensure bytes conversion
        return str(guid)
    except Exception as e:
        print(f"Error converting GUID bytes {guid_bytes.hex()}: {e}")
        return "00000000-0000-0000-0000-000000000000"

@staticmethod
def is_null_guid(guid_bytes, guid_str=None):
    NULL_GUID = bytes(16)
    NULL_GUID_STR = "00000000-0000-0000-0000-000000000000"
    
    if guid_bytes == NULL_GUID:
        return True
    if guid_str and guid_str == NULL_GUID_STR:
        return True
    return False
