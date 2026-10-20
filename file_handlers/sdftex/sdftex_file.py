from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import math
import struct

lazy import numpy as np

from file_handlers.tex.dxgi import (DXGI_FORMAT_BC4_UNORM, DXGI_FORMAT_R8_UNORM, DXGI_FORMAT_R16_FLOAT,
                                    get_bits_per_pixel, get_block_size_bytes, is_block_compressed)


class StorageFormat(IntEnum):
    R16_FLOAT = DXGI_FORMAT_R16_FLOAT
    R8_UNORM = DXGI_FORMAT_R8_UNORM
    BC4_UNORM = DXGI_FORMAT_BC4_UNORM


@dataclass(frozen=True)
class _Profile:
    level_size: int
    formats: frozenset[StorageFormat]
    table: bool = True
    shadow_offset_scale: bool = False


_PROFILES = {
    1628766211: _Profile(44, frozenset({StorageFormat.R16_FLOAT}), table=False),
    481229356: _Profile(56, frozenset({StorageFormat.BC4_UNORM})),
    501229472: _Profile(64, frozenset({StorageFormat.BC4_UNORM})),
    501527429: _Profile(64, frozenset({StorageFormat.BC4_UNORM, StorageFormat.R8_UNORM})),
    502231882: _Profile(64, frozenset(StorageFormat), shadow_offset_scale=True),
}


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"SDFTEX: {message}")


def _profile(version: int) -> _Profile:
    try:
        return _PROFILES[version]
    except KeyError:
        raise ValueError(f"SDFTEX: unsupported resource version {version}") from None


def supported_formats(version: int) -> tuple[StorageFormat, ...]:
    return tuple(sorted(_profile(version).formats))


def _footprint(dimensions: tuple[int, int, int], fmt: StorageFormat):
    width, height, depth = dimensions
    _check(all(isinstance(n, int) and 0 < n <= 0xFFFFFFFF for n in dimensions),
           f"invalid volume dimensions {dimensions}")
    blocks = is_block_compressed(fmt)
    if blocks:
        # Each quality level is its own Texture3D, not a mip, so each needs BC4 width/height in multiples of four.
        _check(width % 4 == height % 4 == 0, "BC4 texture width/height must be multiples of four")
    block_width = (width + 3) // 4 if blocks else width
    rows = (height + 3) // 4 if blocks else height
    stride = get_block_size_bytes(fmt) if blocks else get_bits_per_pixel(fmt) // 8
    row_bytes = block_width * stride
    row_pitch = (row_bytes + 255) & ~255
    slice_pitch = rows * row_pitch
    size = depth * slice_pitch
    _check(size <= 0xFFFFFFFF, "volume upload footprint exceeds its u32 size field")
    return block_width, rows, row_bytes, row_pitch, slice_pitch, size


def bc4_palette(endpoints: np.ndarray) -> np.ndarray:
    first = endpoints[..., 0].astype(np.float32) / 255.0
    second = endpoints[..., 1].astype(np.float32) / 255.0
    palette = np.empty((*first.shape, 8), dtype=np.float32)
    palette[..., 0], palette[..., 1] = first, second
    eight_values = first > second
    for index in range(2, 8):
        interpolated = ((8 - index) * first + (index - 1) * second) / 7.0
        short = (((6 - index) * first + (index - 1) * second) / 5.0
                 if index < 6 else np.full_like(first, index - 6))
        palette[..., index] = np.where(eight_values, interpolated, short)
    return palette


@dataclass
class Bc4Blocks:
    endpoints: np.ndarray
    selectors: np.ndarray

    def validate(self, shape: tuple[int, int, int]) -> None:
        for name, array, lanes, maximum in (
            ("endpoints", self.endpoints, 2, 255),
            ("selectors", self.selectors, 16, 7),
        ):
            _check(isinstance(array, np.ndarray) and array.shape == (*shape, lanes),
                   f"BC4 {name} must have shape {(*shape, lanes)}")
            _check(array.dtype.kind in "ui" and np.all(array <= maximum),
                   f"BC4 {name} must contain unsigned values <= {maximum}")
            _check(np.all(array >= 0), f"BC4 {name} contains negative values")

    @classmethod
    def from_words(cls, words: np.ndarray) -> Bc4Blocks:
        endpoints = np.stack((words & 255, (words >> 8) & 255), axis=-1).astype(np.uint8)
        shifts = np.arange(16, dtype=np.uint64) * 3 + 16
        selectors = ((words[..., None] >> shifts) & 7).astype(np.uint8)
        return cls(endpoints, selectors)

    def words(self) -> np.ndarray:
        words = (self.endpoints[..., 0].astype(np.uint64)
                 | (self.endpoints[..., 1].astype(np.uint64) << 8))
        for lane in range(16):
            words |= self.selectors[..., lane].astype(np.uint64) << (16 + lane * 3)
        return words.astype("<u8", copy=False)

    def normalized_values(self) -> np.ndarray:
        values = np.take_along_axis(bc4_palette(self.endpoints), self.selectors.astype(np.intp), axis=-1)
        depth, rows, columns, _ = values.shape
        return values.reshape(depth, rows, columns, 4, 4).transpose(0, 1, 3, 2, 4).reshape(
            depth, rows * 4, columns * 4,
        )


@dataclass
class DistanceLevel:
    voxel_size: float
    # Stored texture axes; rotation maps these back to mesh-local axes.
    dimensions: tuple[int, int, int]
    format: StorageFormat
    values: np.ndarray | Bc4Blocks
    minimum_distance: float = 0.0
    maximum_distance: float = 0.0
    rotation: int = 0

    @property
    def logical_dimensions(self) -> tuple[int, int, int]:
        width, height, depth = self.dimensions
        # Enum names: XYZ=0, XZY=1, YZX=2.
        return ((width, height, depth), (width, depth, height), (depth, width, height))[
            self.rotation
        ]

    def normalized_values(self) -> np.ndarray:
        width, height, _ = self.dimensions
        if self.format == StorageFormat.BC4_UNORM:
            return self.values.normalized_values()[:, :height, :width]
        result = self.values.astype(np.float32)
        return result / 255.0 if self.format == StorageFormat.R8_UNORM else result

    def distances(self, *, mesh_axes: bool = True) -> np.ndarray:
        result = self.normalized_values()
        if self.format != StorageFormat.R16_FLOAT:
            result = result * (self.maximum_distance - self.minimum_distance) + self.minimum_distance
        if mesh_axes:
            result = result.transpose(((0, 1, 2), (1, 0, 2), (1, 2, 0))[self.rotation])
        return result

    def validate(self, profile: _Profile) -> None:
        _check(self.format in profile.formats, f"unverified format {self.format} for this version")
        _check(math.isfinite(self.voxel_size) and self.voxel_size > 0, "invalid voxel size")
        _check(self.rotation in (0, 1, 2), f"unverified rotation {self.rotation}")
        _check(math.isfinite(self.minimum_distance) and math.isfinite(self.maximum_distance),
               "distance bounds must be finite")
        _check(self.minimum_distance <= self.maximum_distance, "reversed distance range")
        if not profile.table:
            _check(self.rotation == 0 and self.minimum_distance == self.maximum_distance == 0,
                   "RE4 does not store rotation or normalized distance bounds")
        width, height, depth = self.dimensions
        columns, rows, *_ = _footprint(self.dimensions, self.format)
        if self.format == StorageFormat.BC4_UNORM:
            _check(isinstance(self.values, Bc4Blocks), "BC4 requires semantic blocks")
            self.values.validate((depth, rows, columns))
        else:
            _check(isinstance(self.values, np.ndarray) and self.values.shape == (depth, height, width),
                   f"sample array must have shape {(depth, height, width)}")
            _check(np.all(np.isfinite(self.values)), "non-finite distance samples")
            if self.format == StorageFormat.R8_UNORM:
                _check(self.values.dtype.kind in "ui" and np.all(self.values >= 0)
                       and np.all(self.values <= 255), "R8 samples must be integers in [0,255]")
            else:
                _check(self.values.dtype.kind in "fiu", "R16 samples must be real numbers")
                limit = np.finfo(np.float16).max
                _check(np.all(self.values >= -limit) and np.all(self.values <= limit),
                       "R16 samples exceed half-float range")


@dataclass
class SdfTexFile:
    resource_version: int
    levels: list[DistanceLevel] = field(default_factory=list)
    shadow_offset_scale: float = 1.0

    @classmethod
    def from_bytes(cls, data: bytes, *, resource_version: int) -> SdfTexFile:
        profile = _profile(resource_version)
        spans = []

        def read(fmt: str, offset: int, label: str):
            size = struct.calcsize(fmt)
            _check(0 <= offset <= len(data) - size, f"{label} is outside the file")
            spans.append((offset, offset + size, label))
            return struct.unpack_from(fmt, data, offset)

        shadow_scale = 1.0
        if profile.table:
            magic, count = read("<4sI", 0, "header")
            table_offset = 8
            if profile.shadow_offset_scale:
                shadow_scale, = read("<f", 8, "shadow offset scale")
                table_offset = 12
            _check(table_offset + count * 8 <= len(data), "truncated level pointer table")
            offsets = read(f"<{count}Q", table_offset, "level pointer table")
        else:
            magic, = read("<4s", 0, "header")
            offsets = (4,)
        _check(magic == b"SDFT", f"wrong magic {magic!r}")
        levels = []
        for index, offset in enumerate(offsets):
            label = f"level {index}"
            if profile.table:
                voxel, width, height, depth, high, low, fmt, size, row_pitch, slice_pitch, tile, rotation = read(
                    "<fIIIffIIIIII", offset, label + " description",
                )
                if profile.level_size == 64:
                    reserved = read("<II", offset + 48, label + " reserved words")
                    _check(reserved == (0, 0), f"{label} reserved words must be zero; got {reserved}")
                payload_offset, = read("<Q", offset + profile.level_size - 8, label + " payload pointer")
            else:
                voxel, width, height, depth, fmt, size, row_pitch, slice_pitch, tile, payload_offset = read(
                    "<fIIIIIIIIQ", offset, label + " description",
                )
                high = low = 0.0
                rotation = 0
            _check(tile == 0xFFFFFFFF, f"{label} has unverified tile mode {tile:#x}")
            _check(fmt in profile.formats, f"{label} has unverified texture format {fmt}")
            fmt = StorageFormat(fmt)
            dimensions = (width, height, depth)
            columns, rows, row_bytes, expected_row, expected_slice, expected_size = _footprint(dimensions, fmt)
            _check((row_pitch, slice_pitch, size) == (expected_row, expected_slice, expected_size),
                   f"{label} upload pitches/size disagree with dimensions and format: "
                   f"{(row_pitch, slice_pitch, size)} != {(expected_row, expected_slice, expected_size)}")
            _check(0 <= payload_offset <= len(data) - size, f"{label} payload is outside the file")
            spans.append((payload_offset, payload_offset + size, label + " payload"))
            upload = np.frombuffer(data, dtype=np.uint8, count=size, offset=payload_offset).reshape(
                depth, rows, row_pitch,
            )
            _check(not np.any(upload[:, :, row_bytes:]), f"{label} has nonzero upload row padding")
            packed = np.ascontiguousarray(upload[:, :, :row_bytes])
            if fmt == StorageFormat.BC4_UNORM:
                values = Bc4Blocks.from_words(packed.view("<u8").reshape(depth, rows, columns))
            else:
                dtype = "<f2" if fmt == StorageFormat.R16_FLOAT else "u1"
                values = packed.view(dtype).reshape(depth, height, width).copy()
            levels.append(DistanceLevel(voxel, dimensions, fmt, values, low, high, rotation))
        cursor = 0
        for start, end, label in sorted(spans):
            _check(start >= cursor, f"overlapping {label}")
            _check(not any(data[cursor:start]), f"unowned nonzero bytes before {label}")
            cursor = end
        _check(not any(data[cursor:]), "unowned nonzero trailing data")
        result = cls(resource_version, levels, shadow_scale)
        result.validate()
        return result

    def validate(self) -> None:
        profile = _profile(self.resource_version)
        _check(profile.table or len(self.levels) == 1, "RE4 requires exactly one level")
        _check(math.isfinite(self.shadow_offset_scale) and self.shadow_offset_scale > 0,
               "invalid shadow offset scale")
        _check(profile.shadow_offset_scale or self.shadow_offset_scale == 1.0,
               "this version has no shadow offset scale field")
        for level in self.levels:
            level.validate(profile)

    def to_bytes(self) -> bytes:
        self.validate()
        profile = _profile(self.resource_version)
        if profile.table:
            output = bytearray(struct.pack("<4sI", b"SDFT", len(self.levels)))
            if profile.shadow_offset_scale:
                output.extend(struct.pack("<f", self.shadow_offset_scale))
            table_offset = len(output)
            output.extend(bytes(len(self.levels) * 8))
        else:
            output = bytearray(b"SDFT")
        for index, level in enumerate(self.levels):
            offset = len(output)
            if profile.table:
                struct.pack_into("<Q", output, table_offset + index * 8, offset)
            _columns, rows, row_bytes, row_pitch, slice_pitch, size = _footprint(level.dimensions, level.format)
            payload_offset = offset + profile.level_size
            if profile.table:
                output.extend(struct.pack("<fIIIffIIIIII", level.voxel_size, *level.dimensions,
                                          level.maximum_distance, level.minimum_distance,
                                          level.format, size, row_pitch, slice_pitch, 0xFFFFFFFF,
                                          level.rotation))
                if profile.level_size == 64:
                    output.extend(bytes(8))
                output.extend(struct.pack("<Q", payload_offset))
            else:
                output.extend(struct.pack("<fIIIIIIIIQ", level.voxel_size, *level.dimensions,
                                          level.format, size, row_pitch, slice_pitch, 0xFFFFFFFF,
                                          payload_offset))
            depth = level.dimensions[2]
            if level.format == StorageFormat.BC4_UNORM:
                elements = level.values.words()
            else:
                dtype = "<f2" if level.format == StorageFormat.R16_FLOAT else "u1"
                elements = np.ascontiguousarray(level.values, dtype=dtype)
            upload = np.zeros((depth, rows, row_pitch), dtype=np.uint8)
            upload[:, :, :row_bytes] = elements.view(np.uint8).reshape(depth, rows, row_bytes)
            output.extend(upload.tobytes())
        return output.take_bytes()
