import re
import struct
import sys

from file_handlers.bounded_cache import BoundedCache
lazy from .shader_graph import ShaderGraphError

_METADATA_TUPLE = re.compile(r"(![\w.]+) = (!\{.*\})")
_NESTED_OPERAND = re.compile(r'["{}()\[\]]')


_METADATA_OPERANDS_CACHE = BoundedCache()
_MAX_CACHED_METADATA_TEXT = 4096


def native_float_literal(text):
    value = text.strip()
    if re.fullmatch(r'0x[0-9a-fA-F]{8}', value):
        return struct.unpack('<f', struct.pack('<I', int(value, 16)))[0]
    if re.fullmatch(r'0x[0-9a-fA-F]{16}', value):
        return struct.unpack('<d', struct.pack('<Q', int(value, 16)))[0]
    return float(value)


def _parse_metadata_operands(text: str) -> tuple[str, ...]:
    if not text.startswith("!{") or not text.endswith("}"):
        raise ShaderGraphError("DXIL entry metadata is not a tuple.")
    body = text[2:-1]
    if not _NESTED_OPERAND.search(body):
        parts = body.split(",")
        if not parts[-1].strip():
            parts.pop()
        return tuple(part.strip() for part in parts)
    parts, start, depth, quoted, escaped = [], 0, 0, False, False
    for index, char in enumerate(body):
        if escaped:
            escaped = False
        elif char == "\\" and quoted:
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif not quoted:
            if char in "{([":
                depth += 1
            elif char in "})]":
                depth -= 1
            elif char == "," and depth == 0:
                parts.append(body[start:index].strip())
                start = index + 1
    if quoted or depth:
        raise ShaderGraphError("DXIL entry metadata is unbalanced.")
    if body[start:].strip():
        parts.append(body[start:].strip())
    return tuple(parts)


def metadata_operands(text: str) -> tuple[str, ...]:
    if type(text) is not str or len(text) > _MAX_CACHED_METADATA_TEXT:
        return _parse_metadata_operands(text)
    key = _parse_metadata_operands, _NESTED_OPERAND, text
    cached = _METADATA_OPERANDS_CACHE.get(key)
    if cached is not None:
        return cached
    result = _parse_metadata_operands(text)
    cost = (sys.getsizeof(key) + sys.getsizeof(text) + sys.getsizeof(result)
            + sum(sys.getsizeof(item) for item in result) + 256)
    _METADATA_OPERANDS_CACHE.put(key, result, cost, 4096, 4 * 1024 * 1024)
    return result


def metadata_definitions(lines):
    result = {}
    for line in lines:
        if type(line) is str and "!" not in line:
            continue
        if line.lstrip().startswith("!"):
            match = _METADATA_TUPLE.fullmatch(line.strip())
            if match:
                result[match[1]] = metadata_operands(match[2])
    return result
