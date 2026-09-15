"""Pure helpers for constructing and matching binary search patterns."""

import re
import struct
import uuid


_INTEGER_FORMATS = {
    "int32": "i",
    "uint32": "I",
    "int64": "q",
    "uint64": "Q",
}


def normalize_hex_string(value: str) -> str:
    """Return *value* without whitespace after validating its hex syntax."""
    normalized = re.sub(r"\s+", "", value)
    if not re.fullmatch(r"[0-9a-fA-F]+", normalized):
        raise ValueError("Invalid hexadecimal string. Use only 0-9, A-F characters.")
    if len(normalized) % 2:
        raise ValueError("Hexadecimal string must contain an even number of characters.")
    return normalized


def hex_string_to_bytes(value: str, reverse_bytes: bool = False) -> bytes:
    """Convert a hexadecimal string to bytes, optionally reversing byte order."""
    data = bytes.fromhex(normalize_hex_string(value))
    return data[::-1] if reverse_bytes and len(data) > 1 else data


def create_search_patterns(search_type: str, value) -> list[bytes]:
    """Build the byte sequences used by a directory or PAK search."""
    if search_type == "number":
        try:
            integer_type, number = value
            format_char = _INTEGER_FORMATS[integer_type]
            little_endian = struct.pack("<" + format_char, number)
            big_endian = struct.pack(">" + format_char, number)
        except (KeyError, TypeError, ValueError, struct.error) as exc:
            raise ValueError(f"Could not convert number: {exc}") from exc
        return list(dict.fromkeys((little_endian, big_endian)))

    if search_type == "text":
        utf16 = value.encode("utf-16le")
        utf8 = value.encode("utf-8")
        return list(dict.fromkeys((utf16, utf16 + b"\x00\x00", utf8, utf8 + b"\x00")))

    if search_type == "guid":
        try:
            guid = uuid.UUID(value.strip())
        except (AttributeError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid GUID: {value}\n{exc}") from exc
        return [guid.bytes_le, guid.bytes, guid.bytes_le.hex().encode("utf-8")]

    if search_type == "hex":
        hex_text, reverse_bytes = value if isinstance(value, tuple) else (value, False)
        try:
            return [hex_string_to_bytes(hex_text, reverse_bytes)]
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid hex value: {exc}") from exc

    raise ValueError(f"Unsupported search type: {search_type}")


def create_binary_matcher(patterns: list[bytes], case_insensitive: bool = False):
    """Return a predicate that finds any pattern in a bytes-like object."""
    if not case_insensitive:
        return lambda data: any(data.find(pattern) != -1 for pattern in patterns)

    folded_patterns = [pattern.lower() for pattern in patterns]

    def contains_folded(data: bytes, pattern: bytes, chunk_size: int = 4 * 1024 * 1024) -> bool:
        if not pattern:
            return True
        overlap = len(pattern) - 1
        for start in range(0, len(data), chunk_size):
            chunk = data[start : start + chunk_size + overlap]
            if chunk.lower().find(pattern) != -1:
                return True
        return False

    return lambda data: any(contains_folded(data, pattern) for pattern in folded_patterns)
