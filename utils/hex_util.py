# Misc raw file operations

import struct


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
