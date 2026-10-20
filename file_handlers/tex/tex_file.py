from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

lazy import numpy as np

lazy from .dds import CUBEMAP_FACES
lazy from .dxgi import (
    KNOWN_FORMATS,
    get_bits_per_pixel,
    get_block_size_bytes,
    is_block_compressed,
    is_known_format,
    top_mip_size_bytes,
)
lazy from .flags import TexFlags
lazy from .gdeflate import decompress_gdeflate, gdeflate_uncompressed_size, is_gdeflate_payload
from .profiles import (
    LAYOUT_INLINE,
    LAYOUT_LEGACY,
    LAYOUT_STORED,
    TexFormatError,
    tex_profile,
)

TEX_MAGIC = 0x00584554

_LAYOUT_NAMES = {LAYOUT_LEGACY: "legacy", LAYOUT_INLINE: "inline", LAYOUT_STORED: "stored"}


def natural_mip_layout(dxgi_format: int, width: int, height: int) -> Tuple[int, int, int]:
    if is_block_compressed(dxgi_format):
        pitch, row_step = (width + 3) // 4 * get_block_size_bytes(dxgi_format), 4
    else:
        bits_per_pixel = get_bits_per_pixel(dxgi_format)
        if bits_per_pixel % 8:
            raise TexFormatError(
                f"format {dxgi_format} is {bits_per_pixel} bpp, not a whole number of "
                "bytes per pixel"
            )
        pitch, row_step = width * (bits_per_pixel // 8), 1
    return top_mip_size_bytes(dxgi_format, width, height), pitch, row_step


def header_count_limits(layout: int) -> Tuple[int, int]:
    """One byte each in the legacy header; 12 and 4 bits of a u16 in the 40-byte header."""
    return (0xFF, 0xFF) if layout == LAYOUT_LEGACY else (0xFFF, 0xF)


def header_counts(layout: int, image_count: int, mip_count: int) -> Tuple[int, int]:
    max_images, max_mips = header_count_limits(layout)
    if not 0 < image_count <= max_images or not 0 < mip_count <= max_mips:
        raise TexFormatError(
            f"{image_count} images of {mip_count} mips; this header holds 1-{max_images} images "
            f"of 1-{max_mips} mips"
        )
    if layout == LAYOUT_LEGACY:
        return mip_count, image_count
    packed = image_count | mip_count << 12
    return packed & 0xFF, packed >> 8


@dataclass
class TexHeader:
    version: int = 0
    width: int = 0
    height: int = 0
    depth: int = 1
    image_count: int = 1
    mip_count: int = 1
    format: int = 0
    swizzle_control: int = -1
    cubemap_marker: int = 0
    flags: int = 0
    reserved: bytes = b"\x00" * 8

    @property
    def tex_flags(self) -> TexFlags:
        return TexFlags.decode(self.flags, self.version)

    @property
    def is_volume(self) -> bool:
        return self.depth > 1

    @property
    def is_cubemap(self) -> bool:
        return self.cubemap_marker != 0

    def slice_count(self, level: int) -> int:
        return max(1, self.depth >> level)


@dataclass
class MipHeader:
    offset: int
    pitch: int
    size: int  # one depth slice


@dataclass
class StoredMipHeader:
    stored_size: int
    offset: int


def write_tex(
    layout: int,
    header: TexHeader,
    mips: Sequence[MipHeader],
    stored_mips: Sequence[StoredMipHeader],
    payload: bytes,
) -> bytes:
    count_a, count_b = header_counts(layout, header.image_count, header.mip_count)
    out = bytearray(
        struct.pack(
            "<IihhhBBiiIi",
            TEX_MAGIC,
            header.version,
            header.width,
            header.height,
            header.depth,
            count_a,
            count_b,
            header.format,
            header.swizzle_control,
            header.cubemap_marker,
            header.flags,
        )
    )
    if layout != LAYOUT_LEGACY:
        out += header.reserved
    for mip in mips:
        out += struct.pack("<qii", mip.offset, mip.pitch, mip.size)
    for stored in stored_mips:
        out += struct.pack("<ii", stored.stored_size, stored.offset)
    out += payload
    return out.take_bytes()


class TexFile:
    def __init__(self) -> None:
        self.header = TexHeader()
        self.mips: List[MipHeader] = []
        self.stored_mips: List[StoredMipHeader] = []
        self.layout: int = 0
        self._data: bytes = b""
        self._payload_offset: int = 0

    @property
    def uses_stored_mips(self) -> bool:
        return bool(self.stored_mips)

    def read(self, data: bytes, file_version: int = 0) -> bool:
        self._data = data
        self.header = TexHeader()
        self.mips = []
        self.stored_mips = []
        self.layout = 0
        self._payload_offset = 0
        if len(data) < 32 or struct.unpack_from("<I", data, 0)[0] != TEX_MAGIC:
            return False

        version = struct.unpack_from("<i", data, 4)[0]
        profile = tex_profile(version)
        declared_version = int(file_version or 0)
        if declared_version and declared_version != version:
            raise TexFormatError(
                f"the resource path says version {declared_version}, but the header stores {version}"
            )
        if not self._parse_layout(profile.layout):
            raise TexFormatError(
                f"version {version} ({profile.display_name}) does not account for this payload "
                f"under its {_LAYOUT_NAMES[profile.layout]} layout"
            )
        self.layout = profile.layout
        self._assert_invariants()
        return True

    def _assert_invariants(self) -> None:
        header = self.header
        if header.swizzle_control != -1:
            raise TexFormatError(
                f"swizzle is {header.swizzle_control}; every shipped header stores -1"
            )
        if header.width < 1 or header.height < 1 or header.depth < 1:
            raise TexFormatError(
                f"header stores {header.width}x{header.height}x{header.depth}; every shipped header "
                "stores at least 1 on each axis"
            )
        if self.layout != LAYOUT_LEGACY and any(header.reserved):
            raise TexFormatError(
                f"reserved bytes are {header.reserved.hex()}; every shipped header zeroes them"
            )
        if header.cubemap_marker not in (0, 4):
            raise TexFormatError(
                f"cubemap marker is {header.cubemap_marker}; every shipped header stores 0 or 4"
            )
        if header.cubemap_marker and header.image_count != CUBEMAP_FACES:
            raise TexFormatError(
                f"a cubemap carries {header.image_count} images; every shipped cubemap carries {CUBEMAP_FACES}"
            )
        if header.cubemap_marker and header.depth != 1:
            raise TexFormatError(
                f"a cubemap stores depth {header.depth}; every shipped cubemap stores 1"
            )

        flags = header.tex_flags
        if flags.reserved_bits_set:
            raise TexFormatError(
                f"flags {flags.raw:#010x} set reserved bits: "
                f"{flags.reserved_bits_set:#010x}; every shipped header zeroes them"
            )
        if not flags.bit_12_matches_type:
            raise TexFormatError(
                f"fixedMipNum is {flags.fixed_mip_num} for {flags.type_label}; it holds exactly on the "
                "build's own ImageLinear* types"
            )

        if not is_known_format(header.format):
            raise TexFormatError(
                f"format {header.format} has no reversed size; known formats are {sorted(KNOWN_FORMATS)}"
            )

        full_chain = int(math.log2(max(header.width, header.height))) + 1
        if header.mip_count > full_chain:
            raise TexFormatError(
                f"{header.mip_count} mip levels for {header.width}x{header.height}; a full chain has {full_chain}"
            )
        expected = self.mips[0].offset
        for index, mip in enumerate(self.mips):
            level = index % header.mip_count
            width = max(1, header.width >> level)
            height = max(1, header.height >> level)
            natural_size, natural_pitch, row_step = natural_mip_layout(header.format, width, height)
            rows = (height + row_step - 1) // row_step
            if mip.offset != expected:
                raise TexFormatError(
                    f"mip {index} starts at {mip.offset}; the table is contiguous up to {expected}"
                )
            if mip.pitch < natural_pitch or mip.size < natural_size:
                raise TexFormatError(
                    f"mip {index} stores pitch {mip.pitch}/size {mip.size}, below the natural "
                    f"{natural_pitch}/{natural_size} of {width}x{height}"
                )
            if mip.size != mip.pitch * rows:
                raise TexFormatError(
                    f"mip {index} stores size {mip.size}, but pitch {mip.pitch} over {rows} rows "
                    f"is {mip.pitch * rows}"
                )
            expected += self.mip_span(index)

    def _parse_layout(self, layout: int) -> bool:
        data = self._data
        (_, version, width, height, depth, count_a, count_b, tex_format, swizzle, cube, flags) = (
            struct.unpack_from("<IihhhBBiiIi", data, 0)
        )
        if layout == LAYOUT_LEGACY:
            mips_at, mip_count, image_count = 32, count_a, count_b
        elif layout in (LAYOUT_INLINE, LAYOUT_STORED):
            packed = count_a | count_b << 8
            mips_at, mip_count, image_count = 40, packed >> 12, packed & 0xFFF
        else:
            raise TexFormatError(f"unknown header layout {layout} for version {version}")
        if mip_count <= 0 or image_count <= 0:
            return False

        total = mip_count * image_count
        if mips_at + 16 * total > len(data):
            return False
        mips = [
            MipHeader(*struct.unpack_from("<qii", data, mips_at + index * 16))
            for index in range(total)
        ]
        if mips[0].offset != mips_at + 16 * total or any(mip.size <= 0 for mip in mips):
            return False

        expected = sum(
            mip.size * max(1, depth >> (index % mip_count)) for index, mip in enumerate(mips)
        )
        stored: List[StoredMipHeader] = []
        if len(data) - mips[0].offset != expected:
            if layout != LAYOUT_STORED:
                return False
            stored = self._parse_stored_mips(mips, mip_count, depth)
            if stored is None:
                return False

        self.header = TexHeader(
            version=version,
            width=width,
            height=height,
            depth=depth,
            image_count=image_count,
            mip_count=mip_count,
            format=tex_format,
            swizzle_control=swizzle,
            cubemap_marker=cube,
            flags=flags,
            reserved=bytes(data[32:40]) if mips_at == 40 else b"\x00" * 8,
        )
        self.mips = mips
        self.stored_mips = stored
        self._payload_offset = mips[0].offset + 8 * len(stored)
        return True

    def _parse_stored_mips(
        self, mips: List[MipHeader], mip_count: int, depth: int
    ) -> Optional[List[StoredMipHeader]]:
        total = len(mips)
        table = mips[0].offset
        payload = table + 8 * total
        if payload > len(self._data):
            return None
        stored = [
            StoredMipHeader(*struct.unpack_from("<ii", self._data, table + index * 8))
            for index in range(total)
        ]
        offset = 0
        for index, entry in enumerate(stored):
            if entry.stored_size <= 0 or entry.offset != offset:
                return None
            chunk = self._data[payload + offset : payload + offset + entry.stored_size]
            if len(chunk) != entry.stored_size:
                return None
            span = mips[index].size * max(1, depth >> (index % mip_count))
            if entry.stored_size != span and not is_gdeflate_payload(chunk):
                return None
            offset += entry.stored_size
        return stored if offset == len(self._data) - payload else None

    def serialize(self) -> bytes:
        return write_tex(
            self.layout, self.header, self.mips, self.stored_mips, self._data[self._payload_offset :]
        )

    def mip_index(self, level: int, image_index: int = 0) -> int:
        return image_index * self.header.mip_count + level

    def mip_span(self, index: int) -> int:
        return self.mips[index].size * self.header.slice_count(index % self.header.mip_count)

    def _read_mip_bytes(self, index: int) -> bytes:
        mip = self.mips[index]
        span = self.mip_span(index)
        if not self.stored_mips:
            return self._data[mip.offset : mip.offset + span]

        stored = self.stored_mips[index]
        start = self._payload_offset + stored.offset
        chunk = self._data[start : start + stored.stored_size]
        if is_gdeflate_payload(chunk):
            return decompress_gdeflate(chunk, gdeflate_uncompressed_size(chunk, span))[:span]
        return chunk[:span]

    def read_mip_level(self, level: int, image_index: int = 0) -> bytes:
        index = self.mip_index(level, image_index)
        mip = self.mips[index]
        width = max(1, self.header.width >> level)
        height = max(1, self.header.height >> level)
        slices = self.header.slice_count(level)
        raw = self._read_mip_bytes(index)
        return self._repack_pitch(raw, width, height, slices, mip.pitch)

    def _repack_pitch(self, raw: bytes, width: int, height: int, slices: int, source_pitch: int) -> bytes:
        _size, expected_pitch, row_step = natural_mip_layout(self.header.format, width, height)
        row_count = (height + (row_step - 1)) // row_step
        needed = row_count * slices * source_pitch
        if source_pitch < 0 or len(raw) < needed:
            raise TexFormatError(
                f"a {width}x{height} mip needs {needed} bytes at pitch {source_pitch}, "
                f"but its payload holds {len(raw)}"
            )

        rows = row_count * slices
        if source_pitch == expected_pitch:
            return bytes(raw[: rows * expected_pitch])
        if (rows - 1) * source_pitch + expected_pitch > len(raw):
            raise TexFormatError(
                f"a {width}x{height} mip's last row at pitch {source_pitch} runs past its {len(raw)}-byte payload"
            )
        source = np.frombuffer(raw, np.uint8)
        return np.lib.stride_tricks.as_strided(source, (rows, expected_pitch), (source_pitch, 1)).tobytes()

    def read_slice(self, level: int, image_index: int = 0, slice_index: int = 0) -> Optional[bytes]:
        slices = self.header.slice_count(level)
        if not 0 <= slice_index < slices:
            return None
        data = self.read_mip_level(level, image_index)
        stride = len(data) // slices
        return data[slice_index * stride : (slice_index + 1) * stride]
