from __future__ import annotations

import struct


_U16 = struct.Struct("<H")
_I16 = struct.Struct("<h")
_U32 = struct.Struct("<I")
_I32 = struct.Struct("<i")
_U64 = struct.Struct("<Q")
_F32 = struct.Struct("<f")


class ClipParserError(ValueError):
    pass


class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.size = len(data)

    def _check(self, offset: int, size: int):
        if offset < 0 or offset + size > self.size:
            raise ClipParserError(f"Read out of bounds at 0x{offset:X} size={size}")

    def _unpack(self, o: int, fmt: struct.Struct):
        self._check(o, fmt.size)
        return fmt.unpack_from(self.data, o)[0]

    def u8(self, o: int) -> int:
        self._check(o, 1)
        return self.data[o]

    def u16(self, o: int) -> int:
        return self._unpack(o, _U16)

    def i16(self, o: int) -> int:
        return self._unpack(o, _I16)

    def u32(self, o: int) -> int:
        return self._unpack(o, _U32)

    def i32(self, o: int) -> int:
        return self._unpack(o, _I32)

    def u64(self, o: int) -> int:
        return self._unpack(o, _U64)

    def f32(self, o: int) -> float:
        return self._unpack(o, _F32)

    def bytes(self, o: int, n: int) -> bytes:
        self._check(o, n)
        return self.data[o:o + n]

    def read_cstr(self, o: int) -> str:
        if o < 0 or o >= self.size:
            raise ClipParserError(f"Invalid c8 string offset: 0x{o:X}")
        end = self.data.find(b"\x00", o)
        if end < 0:
            raise ClipParserError(f"Unterminated c8 string at offset: 0x{o:X}")
        return self.data[o:end].decode("utf-8", errors="replace")

    def read_wstr(self, o: int) -> str:
        if o < 0 or o >= self.size:
            raise ClipParserError(f"Invalid c16 string offset: 0x{o:X}")
        end = o
        while end + 1 < self.size:
            if self.data[end] == 0 and self.data[end + 1] == 0:
                break
            end += 2
        else:
            raise ClipParserError(f"Unterminated c16 string at offset: 0x{o:X}")
        return self.data[o:end].decode("utf-16le", errors="replace")
