"""See docs/internals/gcp-format.md."""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass, field
from itertools import chain

MAGIC = b"GCPR"
PATTERNS = {1: 1, 2: 4}
_HEADER = struct.Struct("<I4sII")
_ENTRIES = {1: struct.Struct("<4sI16s"), 2: struct.Struct("<16sI?3x16s")}


@dataclass(slots=True)
class ColorPreset:
    guid: uuid.UUID
    name_hash: int
    colors: list[tuple[int, int, int, int]]
    enable_alpha: bool = False


@dataclass(slots=True)
class GcpFile:
    version: int = 2
    presets: list[ColorPreset] = field(default_factory=list)


def read_gcp(data: bytes) -> GcpFile:
    version, magic, count, _reserved = _HEADER.unpack_from(data)
    if magic != MAGIC or version not in _ENTRIES:
        raise ValueError(f"not a GCPR colour preset file of version {', '.join(map(str, _ENTRIES))}")
    entry = _ENTRIES[version]
    if len(data) < (end := _HEADER.size + count * entry.size):
        raise ValueError(f"the file lists {count} presets but ends at byte {len(data)} of {end}")
    return GcpFile(version, [ColorPreset(uuid.UUID(bytes_le=values[-1]), values[1],
                                         [tuple(values[0][at:at + 4]) for at in range(0, len(values[0]), 4)],
                                         version == 2 and values[2])
                             for values in entry.iter_unpack(data[_HEADER.size:end])])


def write_gcp(gcp: GcpFile) -> bytes:
    def fields(preset: ColorPreset) -> tuple:
        if len(colors := bytes(chain.from_iterable(preset.colors))) != 4 * PATTERNS[gcp.version]:
            raise ValueError(f"a version {gcp.version} preset holds {PATTERNS[gcp.version]} RGBA colours")
        guid = preset.guid.bytes_le
        return (colors, preset.name_hash, preset.enable_alpha, guid) if gcp.version == 2 else \
            (colors, preset.name_hash, guid)
    return _HEADER.pack(gcp.version, MAGIC, len(gcp.presets), 0) + b"".join(
        _ENTRIES[gcp.version].pack(*fields(preset)) for preset in gcp.presets)


def preset_lookup(data: bytes) -> dict[uuid.UUID, ColorPreset]:
    return {preset.guid: preset for preset in read_gcp(data).presets}
