import re
import struct

lazy import fastdxiltext


def native_float_literal(text):
    value = text.strip()
    if re.fullmatch(r'0x[0-9a-fA-F]{8}', value):
        return struct.unpack('<f', struct.pack('<I', int(value, 16)))[0]
    if re.fullmatch(r'0x[0-9a-fA-F]{16}', value):
        return struct.unpack('<d', struct.pack('<Q', int(value, 16)))[0]
    return float(value)


def metadata_operands(text: str) -> tuple[str, ...]:
    return fastdxiltext.metadata_operands(text)


def metadata_definitions(lines):
    return fastdxiltext.metadata_definitions(lines)
