"""See docs/internals/clip-format.md."""

from __future__ import annotations

import math
import struct
from collections.abc import Callable

from .layout import ClipFormatError
from .rules import ValueCodec

_DOUBLE = struct.Struct("<d")
_QWORD = struct.Struct("<Q")
_FLOAT = struct.Struct("<f")


def decode_value(codec: ValueCodec, raw: int, string: Callable[[str, int], str],
                 row: Callable[[str, int], object]) -> object:
    match codec.kind:
        case "float":
            return _DOUBLE.unpack(_QWORD.pack(raw))[0]
        case "int":
            value = raw - (1 << 64) if codec.signed and raw >> 63 else raw
            if problem := _int_problem(codec, value):
                raise ClipFormatError(problem)
            return value
        case "bool":
            if raw > 1:
                raise ClipFormatError(f"Bool key value is {raw}")
            return bool(raw)
        case "string":
            return None if raw == 0 else string(codec.pool, raw)
        case "row":
            return row(codec.table, raw)
    if raw != codec.raw:
        raise ClipFormatError(f"{codec.name} key value is {raw}, not {codec.raw}")
    return None


def encode_value(codec: ValueCodec, value: object, intern: Callable[[str, str], int],
                 add_row: Callable[[str, object], int]) -> int:
    match codec.kind:
        case "float":
            return _QWORD.unpack(_DOUBLE.pack(value))[0]
        case "int":
            return value & 0xFFFFFFFFFFFFFFFF
        case "bool":
            return int(value)
        case "string":
            return 0 if value is None else intern(codec.pool, value)
        case "row":
            return add_row(codec.table, value)
    return codec.raw


def default_value(codec: ValueCodec) -> object:
    match codec.kind:
        case "float":
            return 0.0
        case "int":
            return 0
        case "bool":
            return False
        case "string":
            return ""
        case "row":
            return (0.0, 0.0, 0.0) if codec.table == "path_points" else ("", "")
    return None


def value_problem(codec: ValueCodec, value: object) -> str:
    match codec.kind:
        case "float":
            return "" if type(value) is float else "needs a float"
        case "int":
            return "needs an integer" if type(value) is not int else _int_problem(codec, value)
        case "bool":
            return "" if type(value) is bool else "needs True or False"
        case "string":
            if value is None:
                return ""
            return "needs text" if not isinstance(value, str) else "holds a NUL character" if "\0" in value else ""
        case "row" if codec.table == "path_points":
            ok = (isinstance(value, tuple) and len(value) == 3 and all(type(item) is float for item in value)
                  and all(math.isfinite(item) and _fits_f32(item) for item in value))
            return "" if ok else "needs three finite 32-bit floats"
        case "row":
            ok = (isinstance(value, tuple) and len(value) == 2 and all(isinstance(item, str) for item in value)
                  and not any("\0" in item for item in value))
            return "" if ok else "needs a type name and a path"
    return "" if value is None else "holds no value"


def coerce(codec: ValueCodec, number: int | float) -> object:
    if codec.kind == "float":
        return float(number)
    if codec.kind != "int":
        return default_value(codec)
    low, high = _int_range(codec)
    if isinstance(number, float):
        if math.isnan(number):
            return 0
        if math.isinf(number):
            return high if number > 0 else low
        number = round(number)
    return max(low, min(high, number))


def _int_range(codec: ValueCodec) -> tuple[int, int]:
    if codec.signed:
        return -(1 << (codec.bits - 1)), (1 << (codec.bits - 1)) - 1
    return 0, (1 << codec.bits) - 1


def _int_problem(codec: ValueCodec, value: int) -> str:
    low, high = _int_range(codec)
    return "" if low <= value <= high else f"{value} is outside {codec.name}"


def _fits_f32(value: float) -> bool:
    try:
        _FLOAT.pack(value)
    except OverflowError:
        return False
    return True
