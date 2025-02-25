# Misc raw file operations

import struct
from typing import Tuple
import uuid


def align(offset, alignment=16):
    r = offset % alignment
    return offset if r == 0 else offset + (alignment - r)


def available(data: bytes, offset: int, size: int) -> bool:
    """Return True if there are at least 'size' bytes remaining in data from offset."""
    return offset + size <= len(data)


def read_null_terminated_wstring(data: bytes, offset: int, max_chars=65535):
    """Read a null-terminated UTF-16LE string from data starting at offset."""
    chars = []
    pos = offset
    count = 0
    while count < max_chars and available(data, pos, 2):
        val = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        count += 1
        if val == 0:
            break
        chars.append(val)
    return "".join(chr(c) for c in chars), pos, count


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

def sanitize_guid_str(guid_str: str) -> str:
    """Clean and validate GUID string"""
    try:
        # Try parsing as UUID first
        guid = uuid.UUID(guid_str)
        return str(guid)
    except ValueError:
        # If that fails, try cleaning the string
        clean = ''.join(c for c in guid_str if c in '0123456789abcdefABCDEF-')
        try:
            return str(uuid.UUID(clean))
        except ValueError:
            print(f"Invalid GUID string: {guid_str}")
            return "00000000-0000-0000-0000-000000000000"
