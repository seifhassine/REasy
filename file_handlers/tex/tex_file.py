from __future__ import annotations
from dataclasses import dataclass
from typing import List, Tuple

from utils.binary_handler import BinaryHandler
from .dxgi import get_bits_per_pixel, is_block_compressed, get_block_size_bytes

def get_internal_version(version: int) -> int:
    return 20 if version == 190820018 else version


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

    def get_internal_version(self) -> int:
        return get_internal_version(self.version)

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


class TexFile:
    def __init__(self) -> None:
        self.header = TexHeader()
        self.mips: List[MipHeader] = []
        self._data: bytes = b""

    def read(self, data: bytes) -> bool:
        self._data = data
        h = BinaryHandler(bytearray(data))
        magic = h.read_uint32()
        if magic != TEX_MAGIC:
            return False
        self.header.magic = magic
        self.header.version = h.read_int32()
        self.header.width = h.read_int16()
        self.header.height = h.read_int16()
        self.header.depth = h.read_int16()
        version_internal = self.header.get_internal_version()
        if version_internal > 20:
            self.header.image_count = h.read_uint8()
            self.header.mip_header_size = h.read_uint8()
            self.header.mip_count = self.header.mip_header_size // 16
        else:
            self.header.mip_count = h.read_uint8()
            self.header.image_count = h.read_uint8()
        self.header.format = h.read_int32()
        self.header.swizzle_control = h.read_int32()
        self.header.cubemap_marker = h.read_uint32()
        self.header.flags = h.read_int32()
        if version_internal > 27:
            self.header.swizzle_height_depth = h.read_uint8()
            self.header.swizzle_width = h.read_uint8()
            self.header.null1 = h.read_uint16()
            self.header.seven = h.read_uint16()
            self.header.one = h.read_uint16()

        total = self.header.mip_count * self.header.image_count
        self.mips.clear()
        for _ in range(total):
            offset = h.read_int64()
            pitch = h.read_int32()
            size = h.read_int32()
            self.mips.append(MipHeader(offset, pitch, size))
        return True

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
        start = mh.offset
        end = start + mh.size
        return type('Mip', (), {
            'width': w,
            'height': hh,
            'data': self._data[start:end]
        })

    def header_is_power_of_two(self) -> bool:
        return self.header.is_power_of_two

    def read_non_pot_level(self, level: int, image_index: int) -> Tuple[bytes, int, int]:
        idx = image_index * self.header.mip_count + level
        mh = self.mips[idx]
        w = max(1, self.header.width >> level)
        h = max(1, self.header.height >> level)
        block_size = get_block_size_bytes(self.header.format)
        if h == 0 or w == 0:
            return b"", w, h

        blocks_w = (w + 3) // 4
        blocks_h = (h + 3) // 4
        size = blocks_w * blocks_h * block_size
        real_pitch_size = size // h * 4

        src = memoryview(self._data)
        out = bytearray(size)
        off = 0
        stride_offset = mh.pitch - real_pitch_size
        cursor = mh.offset
        if stride_offset == 0:
            out[:] = src[cursor:cursor + size]
        else:
            for _row in range(0, h, 4):
                out[off:off + real_pitch_size] = src[cursor:cursor + real_pitch_size]
                cursor += real_pitch_size + stride_offset
                off += real_pitch_size
        return bytes(out), w, h

    @staticmethod
    def build_tex_bytes_from_dds(
        dxgi_format: int,
        width: int,
        height: int,
        mip_datas: List[bytes],
        pitches_override: List[int] | None = None,
        version_override: int | None = None,
    ) -> bytes:
        version = int(version_override) if version_override is not None else 28
        image_count = 1
        depth = 1
        swizzle_control = -1
        cubemap_marker = 0
        flags = 0x0580 if is_block_compressed(dxgi_format) else 0
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

