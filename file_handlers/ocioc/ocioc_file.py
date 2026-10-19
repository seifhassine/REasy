"""Layout: docs/internals/ocioc.md."""
from __future__ import annotations

import struct
from dataclasses import dataclass

lazy from file_handlers.sdf.models import SDF_MAGIC
lazy from file_handlers.tex.dxgi import get_bits_per_pixel
lazy from utils.hash_util import murmur3_hash_ascii

OCIOC_MAGIC = b"OCIO"
_HEADER = struct.Struct("<4sI2I4Q")
_MATRIX_BYTES = 36
_TEXTURES = struct.Struct("<2I5Q")
_DESCRIPTOR = struct.Struct("<8IQI")
_NO_INDEX = 0xFFFFFFFF


class OciocFormatError(ValueError):
    pass


@dataclass(frozen=True)
class OciocTexture:
    name: str
    dxgi_format: int
    width: int
    height: int
    depth: int
    texels: bytes


@dataclass(frozen=True)
class OciocFile:
    resource_version: int
    textures: tuple[OciocTexture, ...]
    sdf: bytes


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise OciocFormatError(f"OCIOC: {message}")


def _texture(data: bytes, start: int, name: str, descriptor: tuple, three_d: bool) -> OciocTexture:
    fmt, index, width, height, depth, size, row_pitch, slice_pitch, _, _ = descriptor
    _check(index == _NO_INDEX, f"texture {name!r}: descriptor word {index:#x}, not {_NO_INDEX:#x}")
    _check((depth > 1) == three_d, f"texture {name!r} of depth {depth} among the {'3D' if three_d else '1D'} ones")
    row_bytes = width * get_bits_per_pixel(fmt) // 8
    _check(row_pitch >= row_bytes and slice_pitch >= row_pitch * height and size == slice_pitch * depth,
           f"texture {name!r}: {width}x{height}x{depth} in {size} bytes of row pitch {row_pitch} and slice pitch "
           f"{slice_pitch}")
    _check(len(data) >= start + size, f"truncated texels of {name!r}")
    rows = (start + z * slice_pitch + y * row_pitch for z in range(depth) for y in range(height))
    return OciocTexture(name, fmt, width, height, depth, b"".join(data[row:row + row_bytes] for row in rows))


def parse_ocioc(data: bytes) -> OciocFile:
    _check(len(data) >= _HEADER.size and data[:4] == OCIOC_MAGIC, "not an OCIO configuration")
    _, version, first, second, *offsets = _HEADER.unpack_from(data)
    _check((first, second) == (1, 1), f"header words {first}, {second}, not 1, 1")
    expected = [_HEADER.size - 8 + _MATRIX_BYTES * index for index in range(4)]
    _check(offsets == expected, f"matrix and texture struct offsets {offsets}, not {expected}")
    base = 8 + offsets[3]
    _check(len(data) >= base + _TEXTURES.size, "truncated texture struct")
    one_d, three_d, *places = _TEXTURES.unpack_from(data, base)
    at = _TEXTURES.size + _DESCRIPTOR.size * (one_d + three_d)
    _check(len(data) >= base + at, "truncated texture descriptors")
    _check(places[:2] == [_TEXTURES.size, _TEXTURES.size + _DESCRIPTOR.size * one_d],
           f"descriptor offsets {places[:2]} do not follow the texture struct")
    descriptors = [_DESCRIPTOR.unpack_from(data, base + _TEXTURES.size + _DESCRIPTOR.size * index)
                   for index in range(one_d + three_d)]
    names = []
    for *_, name_at, name_hash in descriptors:
        _check(name_at == at, f"a texture name at {name_at}, not {at}")
        end = data.find(b"\0", base + at)
        _check(end >= 0, "unterminated texture name")
        names.append(data[base + at:end].decode("ascii"))
        _check(murmur3_hash_ascii(names[-1]) == name_hash, f"texture name {names[-1]!r} with hash {name_hash:#x}")
        at = end + 1 - base
    _check(places[2] == at, f"texels at {places[2]}, not after the names at {at}")
    textures = []
    for number, (descriptor, name) in enumerate(zip(descriptors, names)):
        if number == one_d:
            _check(places[3] == at, f"3D texels at {places[3]}, not after the 1D ones at {at}")
        textures.append(_texture(data, base + at, name, descriptor, number >= one_d))
        at += descriptor[5]
    _check(three_d or places[3] == at, f"3D texels at {places[3]}, not after the 1D ones at {at}")
    _check(places[4] == at, f"SDF document at {places[4]}, not after the texels at {at}")
    document = data[base + at:]
    _check(document[:4] == OCIOC_MAGIC, "SDF document without the OCIO magic")
    return OciocFile(version, tuple(textures), SDF_MAGIC + document[4:])
