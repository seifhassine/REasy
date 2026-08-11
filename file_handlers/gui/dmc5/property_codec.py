"""One proven type map for GUIR property values in both directions."""

from __future__ import annotations

import math
import struct
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

from file_handlers.clip.enums import PropertyType

from ..errors import GuiFormatError, GuiWriteError
from ..property_types import (
    FLOAT_VECTOR_COUNTS,
    SIGNED_FORMATS,
    SIGNED_VECTOR_COUNTS,
    UNSIGNED_FORMATS,
    UNSIGNED_VECTOR_COUNTS,
    WIDE_STRING_TYPES,
)


DMC5_GUI_NATIVE_POOL_ORDER = (
    "wide", "ascii", "guid", "uint2", "float2",
    "float3", "float4", "color", "special",
)
DMC5_GUI_NATIVE_VALUE_POOLS = {
    **dict.fromkeys(WIDE_STRING_TYPES, "wide"),
    PropertyType.STR8: "ascii",
    PropertyType.ENUM: "ascii",
    PropertyType.GUID: "guid",
    PropertyType.UINT2: "uint2",
    **{kind: f"float{count}" for kind, count in FLOAT_VECTOR_COUNTS.items()},
    PropertyType.COLOR: "color",
}


def dmc5_native_gui_value_pool(kind: PropertyType) -> str:
    """Return the proven DMC5 native payload pool for an offset value."""

    try:
        return DMC5_GUI_NATIVE_VALUE_POOLS[kind]
    except KeyError as exc:
        raise GuiWriteError(
            f"native DMC5 pool placement for property type {kind.name} is not proven"
        ) from exc


class ValueReader(Protocol):
    source: str
    data: bytes

    def require(self, offset: int, size: int, what: str) -> None: ...
    def unpack(self, fmt: str, offset: int, what: str): ...
    def ascii(self, offset: int, what: str) -> str: ...
    def utf16(self, offset: int, what: str) -> str: ...
    def guid(self, offset: int, what: str) -> uuid.UUID: ...


@dataclass(frozen=True, slots=True)
class EncodedGuiValue:
    inline: int | None = None
    payload: bytes | None = None
    alignment: int = 1


def decode_dmc5_gui_value(
    reader: ValueReader,
    kind: PropertyType,
    raw: int,
    what: str,
):
    raw_bytes = struct.pack("<Q", raw)
    if kind == PropertyType.BOOL:
        return bool(raw & 0xFF)
    if kind in SIGNED_FORMATS:
        return struct.unpack_from(f"<{SIGNED_FORMATS[kind]}", raw_bytes)[0]
    if kind in UNSIGNED_FORMATS:
        return struct.unpack_from(f"<{UNSIGNED_FORMATS[kind]}", raw_bytes)[0]
    if kind in (PropertyType.F32, PropertyType.F64):
        value = struct.unpack("<d", raw_bytes)[0]
        if not math.isfinite(value):
            raise GuiFormatError(f"{reader.source}: {what} is not finite")
        return value
    if raw == 0:
        return None
    if kind in (PropertyType.STR8, PropertyType.ENUM):
        return reader.ascii(raw, f"{what} string")
    if kind in WIDE_STRING_TYPES:
        return reader.utf16(raw, f"{what} string")
    if kind == PropertyType.GUID:
        return reader.guid(raw, f"{what} GUID")
    if kind == PropertyType.COLOR:
        reader.require(raw, 4, f"{what} color")
        return list(reader.data[raw : raw + 4])
    for counts, code in (
        (FLOAT_VECTOR_COUNTS, "f"),
        (SIGNED_VECTOR_COUNTS, "i"),
        (UNSIGNED_VECTOR_COUNTS, "I"),
    ):
        if kind in counts:
            count = counts[kind]
            return list(reader.unpack(f"<{count}{code}", raw, f"{what} vector"))
    raise GuiFormatError(
        f"{reader.source}: semantic support for property type {kind.name} is unavailable"
    )


def encode_dmc5_gui_value(
    kind: PropertyType,
    value: Any,
    name: str,
) -> EncodedGuiValue:
    if kind == PropertyType.BOOL:
        if type(value) is not bool:
            raise GuiWriteError(f"{name}: BOOL value must be Boolean")
        return EncodedGuiValue(inline=int(value))
    for formats, signed in ((SIGNED_FORMATS, True), (UNSIGNED_FORMATS, False)):
        if kind in formats:
            size = struct.calcsize(formats[kind]) * 8
            minimum = -(1 << (size - 1)) if signed else 0
            maximum = (1 << (size - int(signed))) - 1
            if type(value) is not int or not minimum <= value <= maximum:
                raise GuiWriteError(f"{name}: value is outside {kind.name}")
            payload = struct.pack(f"<{formats[kind]}", value).ljust(8, b"\0")
            return EncodedGuiValue(inline=int.from_bytes(payload, "little"))
    if kind in (PropertyType.F32, PropertyType.F64):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise GuiWriteError(f"{name}: floating value must be finite")
        return EncodedGuiValue(
            inline=struct.unpack("<Q", struct.pack("<d", float(value)))[0]
        )
    if value is None:
        return EncodedGuiValue()
    if kind in (PropertyType.STR8, PropertyType.ENUM):
        if not isinstance(value, str) or "\0" in value:
            raise GuiWriteError(f"{name}: expected an ASCII string")
        try:
            return EncodedGuiValue(payload=value.encode("ascii") + b"\0")
        except UnicodeEncodeError as exc:
            raise GuiWriteError(f"{name}: value is not ASCII") from exc
    if kind in WIDE_STRING_TYPES:
        if not isinstance(value, str) or "\0" in value:
            raise GuiWriteError(f"{name}: expected a string")
        return EncodedGuiValue(payload=value.encode("utf-16le") + b"\0\0", alignment=2)
    if kind == PropertyType.GUID:
        if not isinstance(value, uuid.UUID):
            raise GuiWriteError(f"{name}: expected a UUID")
        return EncodedGuiValue(payload=value.bytes_le, alignment=8)
    if kind == PropertyType.COLOR:
        values = _sequence(value, 4, name)
        if any(type(item) is not int or not 0 <= item <= 255 for item in values):
            raise GuiWriteError(f"{name}: color components must be 0..255")
        return EncodedGuiValue(payload=bytes(values), alignment=4)
    for counts, code in (
        (FLOAT_VECTOR_COUNTS, "f"),
        (SIGNED_VECTOR_COUNTS, "i"),
        (UNSIGNED_VECTOR_COUNTS, "I"),
    ):
        if kind in counts:
            count = counts[kind]
            values = _sequence(value, count, name)
            try:
                return EncodedGuiValue(
                    payload=struct.pack(f"<{count}{code}", *values),
                    alignment=4,
                )
            except (OverflowError, struct.error) as exc:
                raise GuiWriteError(f"{name}: invalid {kind.name} value") from exc
    raise GuiWriteError(f"{name}: property type {kind.name} is not serializable")


def _sequence(value: Any, count: int, name: str) -> list:
    if not isinstance(value, (list, tuple)) or len(value) != count:
        raise GuiWriteError(f"{name}: expected {count} components")
    return list(value)
