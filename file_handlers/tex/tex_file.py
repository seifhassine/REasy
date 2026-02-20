from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple

from utils.binary_handler import BinaryHandler
from .dxgi import get_bits_per_pixel, is_block_compressed, get_block_size_bytes

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


def get_internal_version(version: int) -> int:
    serializer = get_serializer_version(version)
    if serializer == SERIALIZER_RE7:
        return 10
    if serializer == SERIALIZER_MHRISE:
        return 28
    return 241106027


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
GDEFLATE_MAGIC = 0xFB04
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

    @property
    def _mip_span_size(self) -> int:
        if not self.mips:
            return 0
        first = self.mips[0]
        last = self.mips[-1]
        return (last.offset + last.size) - first.offset

    def read(self, data: bytes, file_version: int = 0) -> bool:
        self._data = data
        self._file_version_hint = int(file_version or 0)
        h = BinaryHandler(bytearray(data))
        magic = h.read_uint32()
        if magic != TEX_MAGIC:
            return False
        self.header.magic = magic
        self.header.version = h.read_int32()
        self.header.width = h.read_int16()
        self.header.height = h.read_int16()
        self.header.depth = h.read_int16()
        serializer_version = get_serializer_version(self.header.version, self._file_version_hint)
        if serializer_version >= SERIALIZER_MHRISE:
            self.header.image_count = h.read_uint8()
            self.header.mip_header_size = h.read_uint8()
            self.header.mip_count = self.header.mip_header_size // 16
            if self.header.image_count == 0 and self.header.mip_count > 0:
                self.header.image_count = 1
        else:
            self.header.mip_count = h.read_uint8()
            self.header.image_count = h.read_uint8()
        self.header.format = h.read_int32()
        self.header.swizzle_control = h.read_int32()
        self.header.cubemap_marker = h.read_uint32()
        self.header.flags = h.read_int32()
        if serializer_version >= SERIALIZER_MHRISE:
            self.header.swizzle_height_depth = h.read_uint8()
            self.header.swizzle_width = h.read_uint8()
            self.header.null1 = h.read_uint16()
            self.header.seven = h.read_uint16()
            self.header.one = h.read_uint16()

        total = self.header.mip_count * self.header.image_count
        self.mips.clear()
        self.packed_mips.clear()
        for _ in range(total):
            offset = h.read_int64()
            pitch = h.read_int32()
            size = h.read_int32()
            self.mips.append(MipHeader(offset, pitch, size))

        if self.uses_packed_mips and self.mips:
            if self._read_packed_mip_headers(h, total):
                self._ensure_packed_mips_are_uncompressed()

        return True

    def _read_packed_mip_headers(self, h: BinaryHandler, total: int) -> bool:
        h.seek(self.mips[0].offset)
        candidate: List[PackedMipHeader] = []
        for _ in range(total):
            candidate.append(PackedMipHeader(size=h.read_int32(), offset=h.read_int32()))

        packed_payload_offset = self.mips[0].offset + (total * 8)
        max_payload = len(self._data) - packed_payload_offset
        if max_payload < 0:
            return False

        prev_offset = -1
        for i, cmip in enumerate(candidate):
            if cmip.size < 0 or cmip.offset < 0:
                return False
            if i == 0 and cmip.offset != 0:
                return False
            if cmip.offset < prev_offset:
                return False
            if cmip.offset + cmip.size > max_payload:
                return False
            prev_offset = cmip.offset

        self.packed_mips = candidate
        return True

    def _ensure_packed_mips_are_uncompressed(self) -> None:
        if not self.packed_mips:
            return

        for i, (mip, cmip) in enumerate(zip(self.mips, self.packed_mips)):
            src_start = self._packed_payload_offset + cmip.offset
            src_end = src_start + cmip.size
            chunk = self._data[src_start:src_end]

            if len(chunk) >= 2 and int.from_bytes(chunk[:2], "little") == GDEFLATE_MAGIC:
                raise RuntimeError(
                    f"TEX file contains gdeflate-compressed mip data (mip {i}); external helper required"
                )

    def _read_mip_bytes(self, idx: int, mh: MipHeader) -> bytes:
        if not self.packed_mips:
            start = mh.offset
            end = start + mh.size
            return self._data[start:end]

        cmip = self.packed_mips[idx]
        start = self._packed_payload_offset + cmip.offset
        end = start + cmip.size
        return self._data[start:end]

    def export_header_dict(self) -> dict:
        h = self.header
        return {
            'version': h.version,
            'width': h.width,
            'height': h.height,
            'depth': h.depth,
            'imageCount': h.image_count,
            'mipCount': h.mip_count,
            'format': h.format,
            'swizzleControl': h.swizzle_control,
            'cubemapMarker': h.cubemap_marker,
            'flags': h.flags,
            'swizzleHeightDepth': h.swizzle_height_depth,
            'swizzleWidth': h.swizzle_width,
            'null1': h.null1,
            'seven': h.seven,
            'one': h.one,
        }

    def get_mip_map_data(self, level: int, image_index: int = 0):
        idx = image_index * self.header.mip_count + level
        mh = self.mips[idx]
        h = self.header
        w = max(1, h.width >> level)
        hh = max(1, h.height >> level)
        expected_size, expected_pitch, row_step = self._expected_mip_layout(w, hh)
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

    @staticmethod
    def build_tex_bytes_from_dds(
        dxgi_format: int,
        width: int,
        height: int,
        mip_datas: List[bytes],
        pitches_override: List[int] | None = None,
        version_override: int | None = None,
        depth: int = 1,
        array_size: int = 1,
        misc_flags: int = 0,
    ) -> bytes:
        version = int(version_override) if version_override is not None else 28
        
        image_count = max(1, array_size)
        depth = max(1, depth)
        swizzle_control = -1
        
        D3D11_RESOURCE_MISC_TEXTURECUBE = 0x4
        cubemap_marker = 1 if (misc_flags & D3D11_RESOURCE_MISC_TEXTURECUBE) else 0
        
        internal_version = get_internal_version(version)
        if internal_version <= 20:
            flags = 0x0001
        else:
            if is_block_compressed(dxgi_format):
                flags = 0x0580
            else:
                flags = 0x0080
        swizzle_height_depth = 0
        swizzle_width = 0
        null1 = 0
        seven = 0
        one = 0

        mip_count = len(mip_datas)
        mip_header_size = mip_count * 16

        pitches: List[int] = []
        sizes: List[int] = []
        for level, data in enumerate(mip_datas):
            if pitches_override and level < len(pitches_override) and pitches_override[level] > 0:
                real_pitch = int(pitches_override[level])
            else:
                w = max(1, width >> level)
                block_size = get_block_size_bytes(dxgi_format)
                if block_size:
                    blocks_w = (w + 3) // 4
                    real_pitch = blocks_w * block_size
                else:
                    bpp = get_bits_per_pixel(dxgi_format) // 8
                    real_pitch = w * bpp
            pitches.append(real_pitch)
            sizes.append(len(data))

        bh = BinaryHandler(bytearray())
        if version < -2147483648 or version > 2147483647:
            raise ValueError(f"TEX version out of 32-bit signed range: {version}")
        if width < -32768 or width > 32767 or height < -32768 or height > 32767:
            raise ValueError(f"TEX dimensions out of 16-bit signed range: {width}x{height}")
        bh.write_uint32(TEX_MAGIC)
        bh.write_int32(version)
        bh.write_int16(width)
        bh.write_int16(height)
        bh.write_int16(depth)

        internal_version = get_internal_version(version)
        if internal_version > 20:
            bh.write_uint8(image_count)
            bh.write_uint8(mip_header_size)
        else:
            bh.write_uint8(mip_count)
            bh.write_uint8(image_count)

        bh.write_int32(dxgi_format)
        bh.write_int32(swizzle_control)
        bh.write_uint32(cubemap_marker)
        bh.write_int32(flags)

        if internal_version > 27:
            bh.write_uint8(swizzle_height_depth)
            bh.write_uint8(swizzle_width)
            bh.write_uint16(null1)
            bh.write_uint16(seven)
            bh.write_uint16(one)

        mip_header_pos: List[int] = []
        for i in range(mip_count):
            mip_header_pos.append(bh.tell)
            bh.write_int64(0) 
            bh.write_int32(pitches[i])
            bh.write_int32(sizes[i])

        offsets: List[int] = []
        for i in range(mip_count):
            offsets.append(bh.tell)
            bh.write_bytes(mip_datas[i])

        for i in range(mip_count):
            bh.write_at(mip_header_pos[i], '<q', offsets[i])

        return bh.get_all_bytes()
