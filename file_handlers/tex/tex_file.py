from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import List, Tuple

from .dxgi import get_bits_per_pixel, is_block_compressed, get_block_size_bytes
from .gdeflate import decompress_gdeflate, gdeflate_uncompressed_size, is_gdeflate_payload

SERIALIZER_RE7 = 1
SERIALIZER_MHRISE = 2
SERIALIZER_MHWILDS = 3
SERIALIZER_UNKNOWN = 0

_VERSION_SERIALIZER_LOOKUP = {
    8: SERIALIZER_RE7,
    10: SERIALIZER_RE7,
    11: SERIALIZER_RE7,
    190820018: SERIALIZER_RE7,
    28: SERIALIZER_MHRISE,
    30: SERIALIZER_MHRISE,
    34: SERIALIZER_MHRISE,
    35: SERIALIZER_MHRISE,
    143221013: SERIALIZER_MHRISE,
    760230703: SERIALIZER_MHRISE,
    240606151: SERIALIZER_MHRISE,
    240701001: SERIALIZER_MHRISE,
    241106027: SERIALIZER_MHWILDS,
    250813143: SERIALIZER_MHWILDS,
    251111100: SERIALIZER_MHWILDS,
}


def _lookup_serializer_version(version: int, file_version: int = 0) -> int | None:
    mapped = _VERSION_SERIALIZER_LOOKUP.get(version)
    if mapped is not None:
        return mapped

    if file_version:
        mapped = _VERSION_SERIALIZER_LOOKUP.get(file_version)
        if mapped is not None:
            return mapped

    return None


def get_known_serializer_version(version: int, file_version: int = 0) -> int:
    mapped = _lookup_serializer_version(version, file_version)
    if mapped is not None:
        return mapped
    return SERIALIZER_UNKNOWN


def get_serializer_version(version: int, file_version: int = 0) -> int:
    mapped = _lookup_serializer_version(version, file_version)
    if mapped is not None:
        return mapped
    return SERIALIZER_MHWILDS

TEX_MAGIC = 0x00584554
@dataclass
class TexHeader:
    magic: int = TEX_MAGIC
    version: int = 0
    width: int = 0
    height: int = 0
    depth: int = 0
    image_count: int = 1
    mip_header_size: int = 0
    mip_count: int = 1
    format: int = 0
    swizzle_control: int = 0
    cubemap_marker: int = 0
    flags: int = 0
    swizzle_height_depth: int = 0
    swizzle_width: int = 0
    null1: int = 0
    seven: int = 0
    one: int = 0

    def get_serializer_version(self) -> int:
        return get_serializer_version(self.version)

    @property
    def bits_per_pixel(self) -> int:
        return get_bits_per_pixel(self.format)

    @property
    def is_power_of_two(self) -> bool:
        def is_pow2(x: int) -> bool:
            return x > 0 and (x & (x - 1)) == 0
        return is_pow2(self.width) and is_pow2(self.height)

    def format_is_block_compressed(self) -> bool:
        return is_block_compressed(self.format)


@dataclass
class MipHeader:
    offset: int
    pitch: int
    size: int


@dataclass
class PackedMipHeader:
    size: int
    offset: int


class TexFile:
    def __init__(self) -> None:
        self.header = TexHeader()
        self.mips: List[MipHeader] = []
        self.packed_mips: List[PackedMipHeader] = []
        self._data: bytes = b""
        self._file_version_hint: int = 0

    @property
    def uses_packed_mips(self) -> bool:
        return get_known_serializer_version(self.header.version, self._file_version_hint) >= SERIALIZER_MHWILDS

    @property
    def _packed_payload_offset(self) -> int:
        if not self.mips:
            return 0
        return self.mips[0].offset + (len(self.packed_mips) * 8)

    def read(self, data: bytes, file_version: int = 0) -> bool:
        self._data = data
        self._file_version_hint = int(file_version or 0)
        unpack_from = struct.unpack_from
        (
            magic,
            version,
            width,
            height,
            depth,
            count_a,
            count_b,
            tex_format,
            swizzle_control,
            cubemap_marker,
            flags,
        ) = unpack_from("<IihhhBBiiIi", data, 0)
        if magic != TEX_MAGIC:
            return False

        header = self.header
        header.magic = magic
        header.version = version
        header.width = width
        header.height = height
        header.depth = depth
        header.format = tex_format
        header.swizzle_control = swizzle_control
        header.cubemap_marker = cubemap_marker
        header.flags = flags
        serializer_version = get_serializer_version(self.header.version, self._file_version_hint)
        if serializer_version >= SERIALIZER_MHRISE:
            header.image_count = count_a
            header.mip_header_size = count_b
            header.mip_count = header.mip_header_size // 16
            if header.image_count == 0 and header.mip_count > 0:
                header.image_count = 1
            (
                header.swizzle_height_depth,
                header.swizzle_width,
                header.null1,
                header.seven,
                header.one,
            ) = unpack_from("<BBHHH", data, 32)
            pos = 40
        else:
            header.mip_count = count_a
            header.image_count = count_b
            pos = 32

        total = header.mip_count * header.image_count
        self.mips = [MipHeader(*unpack_from("<qii", data, pos + index * 16)) for index in range(total)]
        self.packed_mips.clear()

        if self.uses_packed_mips and self.mips and not self._has_unpacked_mip_payload():
            self._read_packed_mip_headers(total)

        return True

    def _has_unpacked_mip_payload(self) -> bool:
        """Return whether the declared mip ranges form the complete file payload."""
        if not self.mips:
            return False

        expected_offset = self.mips[0].offset
        for mip in self.mips:
            if mip.size <= 0 or mip.offset != expected_offset:
                return False
            expected_offset += mip.size
        return expected_offset == len(self._data)

    def _read_packed_mip_headers(self, total: int) -> bool:
        table_offset = self.mips[0].offset
        table_size = total * 8
        if table_offset < 0 or table_offset + table_size > len(self._data):
            return False

        unpack_from = struct.unpack_from
        candidate = [PackedMipHeader(*unpack_from("<ii", self._data, table_offset + index * 8)) for index in range(total)]
        packed_payload_offset = table_offset + table_size
        max_payload = len(self._data) - packed_payload_offset
        if max_payload < 0:
            return False

        expected_offset = 0
        for cmip, mip in zip(candidate, self.mips):
            if (
                cmip.size <= 0
                or cmip.offset != expected_offset
                or cmip.offset + cmip.size > max_payload
            ):
                return False
            start = packed_payload_offset + cmip.offset
            chunk = self._data[start:start + cmip.size]
            if not is_gdeflate_payload(chunk) and cmip.size != mip.size:
                return False
            expected_offset += cmip.size

        # Packed mip chunks fill the payload contiguously.
        if expected_offset != max_payload:
            return False

        self.packed_mips = candidate
        return True

    def _read_mip_bytes(self, idx: int, mh: MipHeader) -> bytes:
        if not self.packed_mips:
            start = mh.offset
            end = start + mh.size
            return self._data[start:end]

        cmip = self.packed_mips[idx]
        start = self._packed_payload_offset + cmip.offset
        end = start + cmip.size
        chunk = self._data[start:end]
        if is_gdeflate_payload(chunk):
            expected_size = gdeflate_uncompressed_size(chunk, mh.size)
            return decompress_gdeflate(chunk, expected_size)[: mh.size]
        return chunk[: mh.size]


    def get_mip_map_data(self, level: int, image_index: int = 0):
        idx = image_index * self.header.mip_count + level
        mh = self.mips[idx]
        h = self.header
        w = max(1, h.width >> level)
        hh = max(1, h.height >> level)
        expected_size, expected_pitch, _ = self._expected_mip_layout(w, hh)
        raw_mip = self._read_mip_bytes(idx, mh)
        if mh.pitch > expected_pitch:
            data = self._read_mip_with_pitch(raw_mip, w, hh, mh.pitch)
        else:
            data = raw_mip[:min(mh.size, expected_size)]
        return type('Mip', (), {
            'width': w,
            'height': hh,
            'data': data
        })

    def _expected_mip_layout(self, w: int, h: int) -> Tuple[int, int, int]:
        if self.header.format_is_block_compressed():
            block_size = get_block_size_bytes(self.header.format)
            blocks_w = (w + 3) // 4
            blocks_h = (h + 3) // 4
            return blocks_w * blocks_h * block_size, blocks_w * block_size, 4

        bpp_bytes = max(1, self.header.bits_per_pixel // 8)
        return w * h * bpp_bytes, w * bpp_bytes, 1

    def _read_mip_with_pitch(self, raw_mip: bytes, w: int, h: int, source_pitch: int) -> bytes:
        expected_size, expected_pitch, row_step = self._expected_mip_layout(w, h)
        if expected_size <= 0:
            return b""

        out = bytearray(expected_size)
        src = memoryview(raw_mip)
        cursor = 0
        out_off = 0
        row_count = max(1, (h + (row_step - 1)) // row_step)
        stride_offset = source_pitch - expected_pitch

        for _ in range(row_count):
            out[out_off:out_off + expected_pitch] = src[cursor:cursor + expected_pitch]
            cursor += expected_pitch + stride_offset
            out_off += expected_pitch

        return bytes(out)

    def header_is_power_of_two(self) -> bool:
        return self.header.is_power_of_two

    def read_non_pot_level(self, level: int, image_index: int) -> Tuple[bytes, int, int]:
        idx = image_index * self.header.mip_count + level
        w = max(1, self.header.width >> level)
        h = max(1, self.header.height >> level)
        if h == 0 or w == 0:
            return b"", w, h

        return self._read_mip_with_pitch(self._read_mip_bytes(idx, self.mips[idx]), w, h, self.mips[idx].pitch), w, h
