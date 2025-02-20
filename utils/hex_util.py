# Misc raw file operations

import struct
from typing import Tuple


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
    if len(guid_bytes) != 16:
        return f"INVALID_GUID_{guid_bytes.hex()}"
    try:
        return str(uuid.UUID(bytes_le=guid_bytes))
    except Exception:
        return f"INVALID_GUID_{guid_bytes.hex()}"
