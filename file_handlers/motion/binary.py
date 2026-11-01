from __future__ import annotations

import struct
from dataclasses import dataclass

lazy from .errors import MotionParseError


def align_up(value: int, alignment: int) -> int:
    if alignment <= 0 or alignment & (alignment - 1):
        raise ValueError("alignment must be a positive power of two")
    return (value + alignment - 1) & -alignment


def pad_to_alignment(out: bytearray, alignment: int) -> None:
    out.extend(bytes(align_up(len(out), alignment) - len(out)))


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid span [{self.start}, {self.end})")

    def contains(self, offset: int, size: int = 0) -> bool:
        return size >= 0 and self.start <= offset and offset + size <= self.end


_STRUCTS = {
    "u8": struct.Struct("<B"),
    "u16": struct.Struct("<H"),
    "i16": struct.Struct("<h"),
    "u32": struct.Struct("<I"),
    "i32": struct.Struct("<i"),
    "u64": struct.Struct("<Q"),
    "f32": struct.Struct("<f"),
}


@dataclass(frozen=True, slots=True)
class ReadContext:
    data: memoryview
    span: Span
    label: str = "file"

    @classmethod
    def from_bytes(cls, data: bytes | bytearray | memoryview, label: str = "file") -> "ReadContext":
        view = memoryview(data)
        return cls(view, Span(0, len(view)), label)

    def require(self, offset: int, size: int, what: str) -> None:
        if not self.span.contains(offset, size):
            raise MotionParseError(
                f"{self.label}: {what} [0x{offset:X}, 0x{offset + size:X}) "
                f"outside [0x{self.span.start:X}, 0x{self.span.end:X})"
            )

    def unpack(self, kind: str, offset: int, what: str = "value"):
        fmt = _STRUCTS[kind]
        self.require(offset, fmt.size, what)
        return fmt.unpack_from(self.data, offset)[0]

    def u8(self, offset: int, what: str = "u8") -> int:
        return self.unpack("u8", offset, what)

    def u16(self, offset: int, what: str = "u16") -> int:
        return self.unpack("u16", offset, what)

    def i16(self, offset: int, what: str = "i16") -> int:
        return self.unpack("i16", offset, what)

    def u32(self, offset: int, what: str = "u32") -> int:
        return self.unpack("u32", offset, what)

    def i32(self, offset: int, what: str = "i32") -> int:
        return self.unpack("i32", offset, what)

    def u64(self, offset: int, what: str = "u64") -> int:
        return self.unpack("u64", offset, what)

    def f32(self, offset: int, what: str = "f32") -> float:
        return self.unpack("f32", offset, what)

    def bytes(self, offset: int, size: int, what: str = "bytes") -> bytes:
        self.require(offset, size, what)
        return bytes(self.data[offset : offset + size])

    def ascii_z(self, offset: int, what: str = "ASCII string") -> tuple[str, int]:
        self.require(offset, 1, what)
        cursor = offset
        while cursor < self.span.end and self.data[cursor] != 0:
            cursor += 1
        if cursor == self.span.end:
            raise MotionParseError(f"{self.label}: unterminated {what} at 0x{offset:X}")
        try:
            value = bytes(self.data[offset:cursor]).decode("ascii")
        except UnicodeDecodeError as exc:
            raise MotionParseError(f"{self.label}: non-ASCII {what} at 0x{offset:X}") from exc
        return value, cursor + 1

    def utf16_z(self, offset: int, what: str = "UTF-16 string") -> tuple[str, int]:
        if offset & 1:
            raise MotionParseError(f"{self.label}: odd {what} offset 0x{offset:X}")
        self.require(offset, 2, what)
        cursor = offset
        while cursor + 2 <= self.span.end and self.u16(cursor, what) != 0:
            cursor += 2
        if cursor + 2 > self.span.end:
            raise MotionParseError(f"{self.label}: unterminated {what} at 0x{offset:X}")
        try:
            value = bytes(self.data[offset:cursor]).decode("utf-16le")
        except UnicodeDecodeError as exc:
            raise MotionParseError(f"{self.label}: invalid {what} at 0x{offset:X}") from exc
        return value, cursor + 2

    def require_zero(self, start: int, end: int, what: str = "padding") -> None:
        self.require(start, end - start, what)
        if any(self.data[start:end]):
            raise MotionParseError(f"{self.label}: nonzero {what} at [0x{start:X}, 0x{end:X})")

