"""See docs/internals/gui-rendering.md, Colour presets."""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass

_HEADER = struct.Struct("<I4sII")
_ENTRY = struct.Struct("<16sII16s")


@dataclass(frozen=True, slots=True)
class ColorPreset:
    colors: tuple[tuple[int, int, int, int], ...]
    name_hash: int
    enable_alpha: bool


def parse_gcp(data: bytes) -> dict[uuid.UUID, ColorPreset]:
    version, magic, count, _reserved = _HEADER.unpack_from(data)
    if magic != b"GCPR" or version != 2 or len(data) < _HEADER.size + count * _ENTRY.size:
        raise ValueError("not a version 2 GCPR colour preset file")
    return {uuid.UUID(bytes_le=guid): ColorPreset(tuple(tuple(colors[at:at + 4]) for at in range(0, 16, 4)), name_hash,
                                                  bool(enable_alpha & 0xFF))
            for colors, name_hash, enable_alpha, guid in _ENTRY.iter_unpack(
                data[_HEADER.size:_HEADER.size + count * _ENTRY.size])}
