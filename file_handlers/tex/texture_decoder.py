from __future__ import annotations

import importlib
import struct
from dataclasses import dataclass

from .dds import DDS_MAGIC
from .dxgi import (
    DXGI_FORMAT_A8_UNORM,
    DXGI_FORMAT_B5G5R5A1_UNORM,
    DXGI_FORMAT_B5G6R5_UNORM,
    DXGI_FORMAT_B8G8R8A8_UNORM,
    DXGI_FORMAT_B8G8R8A8_UNORM_SRGB,
    DXGI_FORMAT_BC1_UNORM,
    DXGI_FORMAT_BC1_UNORM_SRGB,
    DXGI_FORMAT_BC2_UNORM,
    DXGI_FORMAT_BC2_UNORM_SRGB,
    DXGI_FORMAT_BC3_UNORM,
    DXGI_FORMAT_BC3_UNORM_SRGB,
    DXGI_FORMAT_BC4_SNORM,
    DXGI_FORMAT_BC4_UNORM,
    DXGI_FORMAT_BC5_SNORM,
    DXGI_FORMAT_BC5_UNORM,
    DXGI_FORMAT_BC6H_SF16,
    DXGI_FORMAT_BC6H_UF16,
    DXGI_FORMAT_BC7_UNORM,
    DXGI_FORMAT_BC7_UNORM_SRGB,
    DXGI_FORMAT_R8_UNORM,
    DXGI_FORMAT_R8G8_UNORM,
    DXGI_FORMAT_R8G8B8A8_UNORM,
    DXGI_FORMAT_R8G8B8A8_UNORM_SRGB,
    DXGI_FORMAT_R10G10B10A2_UNORM,
    DXGI_FORMAT_R16G16B16A16_FLOAT,
    DXGI_FORMAT_R16G16B16A16_TYPELESS,
    DXGI_FORMAT_R16G16B16A16_UNORM,
    is_block_compressed,
    top_mip_size_bytes,
)

_T2D_MODULE = None
_T2D_IMPORT_ERROR: Exception | None = None


def _get_texture2ddecoder():
    global _T2D_MODULE, _T2D_IMPORT_ERROR

    if _T2D_MODULE is not None:
        return _T2D_MODULE

    try:
        _T2D_MODULE = importlib.import_module("texture2ddecoder")
        _T2D_IMPORT_ERROR = None
    except Exception as ex:
        _T2D_MODULE = None
        _T2D_IMPORT_ERROR = ex

    return _T2D_MODULE


@dataclass
class DecodedTexture:
    width: int
    height: int
    rgba: bytes


_BC_DECODERS = {
    DXGI_FORMAT_BC1_UNORM: "decode_bc1",
    DXGI_FORMAT_BC1_UNORM_SRGB: "decode_bc1",
    DXGI_FORMAT_BC2_UNORM: "decode_bc2",
    DXGI_FORMAT_BC2_UNORM_SRGB: "decode_bc2",
    DXGI_FORMAT_BC3_UNORM: "decode_bc3",
    DXGI_FORMAT_BC3_UNORM_SRGB: "decode_bc3",
    DXGI_FORMAT_BC4_UNORM: "decode_bc4",
    DXGI_FORMAT_BC4_SNORM: "decode_bc4",
    DXGI_FORMAT_BC5_UNORM: "decode_bc5",
    DXGI_FORMAT_BC5_SNORM: "decode_bc5",
    DXGI_FORMAT_BC6H_UF16: "decode_bc6",
    DXGI_FORMAT_BC6H_SF16: "decode_bc6",
    DXGI_FORMAT_BC7_UNORM: "decode_bc7",
    DXGI_FORMAT_BC7_UNORM_SRGB: "decode_bc7",
}

_DDS_FOURCC_DX10 = 0x30315844
_DDS_FOURCC_DXT1 = 0x31545844
_DDS_FOURCC_DXT3 = 0x33545844
_DDS_FOURCC_DXT5 = 0x35545844
_DDS_FOURCC_ATI1 = 0x31495441
_DDS_FOURCC_ATI2 = 0x32495441
_DDS_FOURCC_BC4U = 0x55344342
_DDS_FOURCC_BC5U = 0x55354342

_DDS_LEGACY_FOURCC_TO_DXGI = {
    _DDS_FOURCC_DXT1: DXGI_FORMAT_BC1_UNORM,
    _DDS_FOURCC_DXT3: DXGI_FORMAT_BC2_UNORM,
    _DDS_FOURCC_DXT5: DXGI_FORMAT_BC3_UNORM,
    _DDS_FOURCC_ATI1: DXGI_FORMAT_BC4_UNORM,
    _DDS_FOURCC_BC4U: DXGI_FORMAT_BC4_UNORM,
    _DDS_FOURCC_ATI2: DXGI_FORMAT_BC5_UNORM,
    _DDS_FOURCC_BC5U: DXGI_FORMAT_BC5_UNORM,
}


def _float01_to_u8(value: float) -> int:
    if value <= 0.0:
        return 0
    if value >= 1.0:
        return 255
    return int(value * 255.0 + 0.5)


def _bgra_to_rgba(data: bytes) -> bytes:
    if len(data) % 4 != 0:
        raise ValueError(f"Decoded BC payload has invalid size: {len(data)}")
    out = bytearray(data)
    out[0::4], out[2::4] = out[2::4], out[0::4]
    return bytes(out)


def _decode_uncompressed_rgba(dxgi_format: int, data: bytes, pixel_count: int) -> bytes:
    if dxgi_format in (DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM_SRGB):
        return data[: pixel_count * 4]

    out = bytearray(pixel_count * 4)

    if dxgi_format in (DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB):
        for i in range(pixel_count):
            b, g, r, a = data[i * 4:i * 4 + 4]
            out[i * 4:i * 4 + 4] = bytes((r, g, b, a))
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_R8G8_UNORM:
        for i in range(pixel_count):
            r, g = data[i * 2:i * 2 + 2]
            out[i * 4:i * 4 + 4] = bytes((r, g, 0, 255))
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_R8_UNORM:
        for i, r in enumerate(data[:pixel_count]):
            out[i * 4:i * 4 + 4] = bytes((r, r, r, 255))
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_A8_UNORM:
        for i, a in enumerate(data[:pixel_count]):
            out[i * 4:i * 4 + 4] = bytes((a, a, a, a))
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_B5G6R5_UNORM:
        for i in range(pixel_count):
            v = struct.unpack_from("<H", data, i * 2)[0]
            r = ((v >> 11) & 0x1F) * 255 // 31
            g = ((v >> 5) & 0x3F) * 255 // 63
            b = (v & 0x1F) * 255 // 31
            out[i * 4:i * 4 + 4] = bytes((r, g, b, 255))
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_B5G5R5A1_UNORM:
        for i in range(pixel_count):
            v = struct.unpack_from("<H", data, i * 2)[0]
            b = (v & 0x1F) * 255 // 31
            g = ((v >> 5) & 0x1F) * 255 // 31
            r = ((v >> 10) & 0x1F) * 255 // 31
            a = 255 if (v >> 15) else 0
            out[i * 4:i * 4 + 4] = bytes((r, g, b, a))
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_R16G16B16A16_UNORM:
        for i in range(pixel_count):
            r16, g16, b16, a16 = struct.unpack_from("<HHHH", data, i * 8)
            out[i * 4:i * 4 + 4] = bytes((r16 >> 8, g16 >> 8, b16 >> 8, a16 >> 8))
        return bytes(out)

    if dxgi_format in (DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_R16G16B16A16_TYPELESS):
        for i in range(pixel_count):
            r, g, b, a = struct.unpack_from("<eeee", data, i * 8)
            out[i * 4:i * 4 + 4] = bytes((_float01_to_u8(r), _float01_to_u8(g), _float01_to_u8(b), _float01_to_u8(a)))
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_R10G10B10A2_UNORM:
        for i in range(pixel_count):
            packed = struct.unpack_from("<I", data, i * 4)[0]
            r = (packed & 0x3FF) * 255 // 1023
            g = ((packed >> 10) & 0x3FF) * 255 // 1023
            b = ((packed >> 20) & 0x3FF) * 255 // 1023
            a = ((packed >> 30) & 0x3) * 255 // 3
            out[i * 4:i * 4 + 4] = bytes((r, g, b, a))
        return bytes(out)

    raise ValueError(f"Unsupported format for direct decoding: {dxgi_format}")


def decode_texture_data(dxgi_format: int, width: int, height: int, data: bytes) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("Invalid texture dimensions")

    if decoder_name := _BC_DECODERS.get(dxgi_format):
        module = _get_texture2ddecoder()
        if module is None:
            detail = f": {_T2D_IMPORT_ERROR}" if _T2D_IMPORT_ERROR else ""
            raise RuntimeError(
                "texture2ddecoder is required to decode BC-compressed textures"
                f"{detail}. Install dependencies (e.g. pip install -r requirements.txt)."
            )
        decoder = getattr(module, decoder_name, None)
        if decoder is None:
            raise RuntimeError(f"texture2ddecoder does not provide {decoder_name}")
        decoded = decoder(data, width, height)
        expected_size = width * height * 4
        if len(decoded) != expected_size:
            raise RuntimeError(
                f"texture2ddecoder returned unexpected payload size: {len(decoded)} (expected {expected_size})"
            )
        return _bgra_to_rgba(decoded)

    return _decode_uncompressed_rgba(dxgi_format, data, width * height)


def decode_tex_mip(tex_file, image_index: int = 0, mip_index: int = 0) -> DecodedTexture:
    header = tex_file.header
    if header.format_is_block_compressed() and not tex_file.header_is_power_of_two():
        mip_data, width, height = tex_file.read_non_pot_level(mip_index, image_index)
    else:
        mip = tex_file.get_mip_map_data(mip_index, image_index)
        mip_data, width, height = mip.data, mip.width, mip.height
    rgba = decode_texture_data(header.format, width, height, mip_data)
    return DecodedTexture(width=width, height=height, rgba=rgba)


def _resolve_legacy_dds_dxgi(dds_data: bytes) -> int:
    fourcc = struct.unpack_from('<I', dds_data, 84)[0]
    if fourcc in _DDS_LEGACY_FOURCC_TO_DXGI:
        return _DDS_LEGACY_FOURCC_TO_DXGI[fourcc]

    flags = struct.unpack_from('<I', dds_data, 80)[0]
    rgb_bpp = struct.unpack_from('<I', dds_data, 88)[0]
    rmask = struct.unpack_from('<I', dds_data, 92)[0]
    gmask = struct.unpack_from('<I', dds_data, 96)[0]
    bmask = struct.unpack_from('<I', dds_data, 100)[0]
    amask = struct.unpack_from('<I', dds_data, 104)[0]

    ddpf_rgb = 0x40
    if (flags & ddpf_rgb) and rgb_bpp == 32:
        if (rmask, gmask, bmask, amask) == (0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000):
            return DXGI_FORMAT_B8G8R8A8_UNORM
        if (rmask, gmask, bmask, amask) == (0x000000FF, 0x0000FF00, 0x00FF0000, 0xFF000000):
            return DXGI_FORMAT_R8G8B8A8_UNORM

    raise ValueError("Unsupported DDS pixel format")


def decode_dds_mip(dds_data: bytes, mip_index: int = 0, image_index: int = 0) -> DecodedTexture:
    if len(dds_data) < 128:
        raise ValueError("DDS data too small")
    if struct.unpack_from('<I', dds_data, 0)[0] != DDS_MAGIC:
        raise ValueError("Not a DDS file")

    width = struct.unpack_from('<I', dds_data, 16)[0]
    height = struct.unpack_from('<I', dds_data, 12)[0]
    mip_count = max(1, struct.unpack_from('<I', dds_data, 28)[0])
    if mip_index < 0 or mip_index >= mip_count:
        raise ValueError(f"Mip index out of range: {mip_index}")

    fourcc = struct.unpack_from('<I', dds_data, 84)[0]
    if fourcc == _DDS_FOURCC_DX10:
        if len(dds_data) < 148:
            raise ValueError("DDS DX10 data too small")
        dxgi_format = struct.unpack_from('<I', dds_data, 128)[0]
        array_size = max(1, struct.unpack_from('<I', dds_data, 140)[0])
        data_offset = 148
    else:
        dxgi_format = _resolve_legacy_dds_dxgi(dds_data)
        array_size = 1
        data_offset = 128

    if image_index < 0 or image_index >= array_size:
        raise ValueError(f"Image index out of range: {image_index}")

    mip_sizes: list[int] = []
    mip_dims: list[tuple[int, int]] = []
    w = width
    h = height
    for _ in range(mip_count):
        mip_dims.append((w, h))
        mip_sizes.append(top_mip_size_bytes(dxgi_format, w, h))
        w = max(1, w >> 1)
        h = max(1, h >> 1)

    image_span = sum(mip_sizes)
    start = data_offset + image_index * image_span + sum(mip_sizes[:mip_index])
    size = mip_sizes[mip_index]
    end = start + size
    if end > len(dds_data):
        raise ValueError("DDS mip payload out of bounds")

    mip_w, mip_h = mip_dims[mip_index]
    rgba = decode_texture_data(dxgi_format, mip_w, mip_h, dds_data[start:end])
    return DecodedTexture(width=mip_w, height=mip_h, rgba=rgba)
