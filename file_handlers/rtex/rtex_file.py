"""See docs/internals/rtex-format.md."""
from __future__ import annotations

import functools
import struct
from collections.abc import Mapping
from dataclasses import dataclass

lazy from file_handlers.layout_codec import RecordCodec, game_enums, load_table
lazy from utils.enum_manager import EnumDefinitionError

DATA_DIRECTORY = "resources/data/rtex"
MAGIC = b"RTEX"
# Dimension values, via.render.RtvDimension's.
TEXTURE_2D = 4


class RtexFormatError(ValueError):
    pass


@functools.cache
def _layouts() -> dict:
    return load_table(DATA_DIRECTORY, "layouts", RtexFormatError)


@functools.cache
def _record(version: int) -> RecordCodec:
    fields = _layouts()["versions"].get(str(version))
    if fields is None:
        raise RtexFormatError(f"REasy does not read render textures of version {version}")
    return RecordCodec(f"RTEX v{version}", _layouts()["size"], fields, RtexFormatError)


@dataclass(frozen=True, slots=True)
class RtexFile:
    version: int
    fields: Mapping[str, int | float]

    @property
    def form(self) -> int:
        return int(self.fields["Format"])

    @property
    def size(self) -> tuple[int, int]:
        return int(self.fields["Width"]), int(self.fields["Height"])

    def set(self, name: str) -> bool:
        return bool(self.fields.get(name, 0))


def is_rtex(data: bytes) -> bool:
    return len(data) >= 8 and data[:4] == MAGIC and str(struct.unpack_from("<I", data, 4)[0]) in _layouts()["versions"]


def read_rtex(data: bytes) -> RtexFile:
    if len(data) < 8 or data[:4] != MAGIC:
        raise RtexFormatError("not a render texture")
    version = struct.unpack_from("<I", data, 4)[0]
    record = _record(version)
    if len(data) != record.size:
        raise RtexFormatError(f"a version {version} render texture holds {record.size} bytes, not {len(data)}")
    return RtexFile(version, record.decode(data, 0))


def field_kinds() -> tuple[Mapping[str, str], frozenset[str]]:
    return _layouts()["enums"], frozenset(_layouts()["flags"])


def value_names(game: str) -> dict[str, dict[int, str]]:
    try:
        return game_enums(_layouts()["enums"], game)
    except EnumDefinitionError:
        return {}


__all__ = ["DATA_DIRECTORY", "MAGIC", "TEXTURE_2D", "RtexFile", "RtexFormatError", "field_kinds", "is_rtex",
           "read_rtex", "value_names"]
