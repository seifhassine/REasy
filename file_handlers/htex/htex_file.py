"""See docs/internals/htex-format.md."""
from __future__ import annotations

import functools
import struct
from dataclasses import dataclass, field

lazy from file_handlers.layout_codec import load_table

DATA_DIRECTORY = "resources/data/htex"
MAGIC = b"HTEX"
_HEADER = struct.Struct("<4siQ")
_ENTRY = struct.Struct("<IIQ")


@dataclass(slots=True)
class HeightImage:
    width: int
    height: int
    texels: bytes


@dataclass(slots=True)
class HtexFile:
    images: list[HeightImage] = field(default_factory=list)


@functools.cache
def htex_versions() -> dict[str, int]:
    return load_table(DATA_DIRECTORY, "versions", ValueError)["games"]


def read_htex(data: bytes) -> HtexFile:
    if data[:4] != MAGIC or len(data) < _HEADER.size:
        raise ValueError("a height texture starts with HTEX, its image count and its table offset")
    _magic, count, table = _HEADER.unpack_from(data)
    if count < 0 or count and table + count * _ENTRY.size > len(data):
        raise ValueError(f"the table of {count} images lies outside the file")
    images = []
    for at in range(count):
        width, height, start = _ENTRY.unpack_from(data, table + at * _ENTRY.size)
        if width * height and (not start or start + width * height > len(data)):
            raise ValueError(f"image {at} ({width} x {height}) lies outside the file")
        images.append(HeightImage(width, height, data[start:start + width * height]))
    return HtexFile(images)


def write_htex(file: HtexFile) -> bytes:
    if any(len(image.texels) != image.width * image.height for image in file.images):
        raise ValueError("every image holds one byte per texel")
    at, table = _HEADER.size + _ENTRY.size * len(file.images), []
    for image in file.images:
        table.append(_ENTRY.pack(image.width, image.height, at))
        at += len(image.texels)
    return b"".join((_HEADER.pack(MAGIC, len(file.images), _HEADER.size), *table,
                     *(image.texels for image in file.images)))
