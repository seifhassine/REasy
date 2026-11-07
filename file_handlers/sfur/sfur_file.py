"""See docs/internals/shell-fur.md."""
from __future__ import annotations

import struct
from dataclasses import dataclass, fields

_MAGIC = b"SFUR"


@dataclass(frozen=True, slots=True)
class FurParameter:
    material: str
    shell_count: int
    thin_type: int
    bottom_save: bool
    coord_type: int
    height: float
    bend_rate: float
    bend_root_rate: float
    normal_transform_rate: float
    stiffness: float
    stiffness_distribution: float
    spring_coefficient: float
    dumping: float
    gravity_scale: float
    wind_scale: float
    disable: bool
    grooming_texture: str = ""
    force_two_side: bool = False
    force_alpha_test: bool = False


PARAMETER_KINDS = {item.name: item.type for item in fields(FurParameter)}
PARAMETER_FIELDS = tuple(PARAMETER_KINDS)
_FLOATS = tuple(name for name, kind in PARAMETER_KINDS.items() if kind == "float")
_ENTRIES = {
    4: (struct.Struct("<IiIi10fI4xQQ"), ("shell_count", "thin_type", "bottom_save", "coord_type", *_FLOATS, "disable",
                                         "material", "grooming_texture")),
    5: (struct.Struct("<Iii10f4BQQ"), ("shell_count", "thin_type", "coord_type", *_FLOATS, "force_two_side",
                                       "force_alpha_test", "bottom_save", "disable", "material", "grooming_texture")),
}


def _text(data: bytes, offset: int) -> str:
    if not offset:
        return ""
    end = offset
    while data[end:end + 2] not in (b"\0\0", b""):
        end += 2
    return data[offset:end].decode("utf-16-le")


def read_shell_fur(data: bytes) -> tuple[FurParameter, ...]:
    if bytes(data[:4]) != _MAGIC:
        raise ValueError("not a shell fur file")
    version, count = struct.unpack_from("<II", data, 4)
    if version not in _ENTRIES:
        raise ValueError(f"shell fur version {version} is not one REasy reads")
    layout, names = _ENTRIES[version]
    table, = struct.unpack_from("<Q", data, 0x10)
    found = []
    for index in range(count):
        place, = struct.unpack_from("<Q", data, table + 8 * index)
        values = dict(zip(names, layout.unpack_from(data, place)))
        found.append(FurParameter(**{name: _text(data, value) if PARAMETER_KINDS[name] == "str" else
                                     bool(value) if PARAMETER_KINDS[name] == "bool" else value
                                     for name, value in values.items()}))
    return tuple(found)


__all__ = ["FurParameter", "PARAMETER_FIELDS", "PARAMETER_KINDS", "read_shell_fur"]
