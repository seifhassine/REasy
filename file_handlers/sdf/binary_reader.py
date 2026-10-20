from __future__ import annotations

import struct


class BoundedReader:
    def __init__(self, data: bytes | bytearray | memoryview, label: str, error: type[ValueError]):
        self.source = data if isinstance(data, (bytes, bytearray)) else bytes(data)
        self.data = memoryview(self.source)
        self.label = label
        self.error = error
        self.string_cache: dict[tuple[int, int], str] = {}

    def require(self, offset: int, size: int, field: str, *, end: int | None = None) -> None:
        limit = len(self.data) if end is None else end
        stop = offset + size
        if offset < 0 or size < 0 or stop < offset or stop > limit:
            raise self.error(
                f"{self.label}: {field} range [0x{offset:X}, 0x{stop:X}) "
                f"exceeds 0x{limit:X}"
            )

    def u8(self, offset: int, field: str, *, end: int | None = None) -> int:
        self.require(offset, 1, field, end=end)
        return self.data[offset]

    def u16(self, offset: int, field: str, *, end: int | None = None) -> int:
        self.require(offset, 2, field, end=end)
        return struct.unpack_from("<H", self.data, offset)[0]

    def u32(self, offset: int, field: str, *, end: int | None = None) -> int:
        self.require(offset, 4, field, end=end)
        return struct.unpack_from("<I", self.data, offset)[0]

    def i32(self, offset: int, field: str, *, end: int | None = None) -> int:
        self.require(offset, 4, field, end=end)
        return struct.unpack_from("<i", self.data, offset)[0]

    def u64(self, offset: int, field: str, *, end: int | None = None) -> int:
        self.require(offset, 8, field, end=end)
        return struct.unpack_from("<Q", self.data, offset)[0]

    def f32(self, offset: int, field: str, *, end: int | None = None) -> float:
        self.require(offset, 4, field, end=end)
        return struct.unpack_from("<f", self.data, offset)[0]

    def bytes(self, offset: int, size: int, field: str, *, end: int | None = None) -> bytes:
        self.require(offset, size, field, end=end)
        return self.data[offset : offset + size].tobytes()

    def string(self, offset: int, field: str, *, start: int, end: int) -> str:
        if offset < start:
            raise self.error(
                f"{self.label}: {field} offset 0x{offset:X} precedes 0x{start:X}"
            )
        self.require(offset, 1, field, end=end)
        key = (offset, end)
        cached = self.string_cache.get(key)
        if cached is not None:
            return cached
        terminator = self.source.find(b"\x00", offset, end)
        if terminator < 0:
            raise self.error(f"{self.label}: {field} is not null terminated")
        value = self.source[offset:terminator].decode("utf-8", "replace")
        self.string_cache[key] = value
        return value
