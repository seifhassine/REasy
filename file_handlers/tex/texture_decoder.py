from __future__ import annotations

import importlib
import struct
from dataclasses import dataclass

import numpy as np

from .dds import CUBEMAP_FACES, DDS_DIMENSION_TEXTURE3D, DDS_MAGIC, DDS_MISC_TEXTURECUBE, DDSCAPS2_CUBEMAP, FOURCC_DX10
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
    UnknownTextureFormat,
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


def _bgra_to_rgba(data: bytes) -> bytes:
    if len(data) % 4 != 0:
        raise ValueError(f"Decoded BC payload has invalid size: {len(data)}")
    out = bytearray(data)
    out[0::4], out[2::4] = out[2::4], out[0::4]
    return bytes(out)


def _decode_uncompressed_rgba(dxgi_format: int, data: bytes, pixel_count: int) -> bytes:
    if dxgi_format in (DXGI_FORMAT_R8G8B8A8_UNORM, DXGI_FORMAT_R8G8B8A8_UNORM_SRGB):
        return data[: pixel_count * 4]
    if dxgi_format in (DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_B8G8R8A8_UNORM_SRGB):
        return _bgra_to_rgba(data[: pixel_count * 4])

    if dxgi_format == DXGI_FORMAT_R16G16B16A16_UNORM:
        values = np.frombuffer(data, dtype="<u2", count=pixel_count * 4)
        return (values >> 8).astype(np.uint8).tobytes()

    if dxgi_format in (DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_R16G16B16A16_TYPELESS):
        # Float32 first: value * 255 + 0.5 is exact there, keeping round-half-up.
        values = np.frombuffer(data, dtype="<f2", count=pixel_count * 4).astype(np.float32)
        if np.isnan(values).any():
            raise ValueError("cannot convert float NaN to integer")
        np.clip(values, 0.0, 1.0, out=values)
        values *= 255.0
        values += 0.5
        return values.astype(np.uint8).tobytes()

    out = bytearray(pixel_count * 4)

    if dxgi_format == DXGI_FORMAT_R8G8_UNORM:
        source = data[: pixel_count * 2]
        out[0::4], out[1::4] = source[0::2], source[1::2]
        out[3::4] = b"\xff" * pixel_count
        return bytes(out)

    if dxgi_format in (DXGI_FORMAT_R8_UNORM, DXGI_FORMAT_A8_UNORM):
        source = data[:pixel_count]
        out[0::4] = out[1::4] = out[2::4] = source
        out[3::4] = source if dxgi_format == DXGI_FORMAT_A8_UNORM else b"\xff" * pixel_count
        return bytes(out)

    pixels = np.frombuffer(out, dtype=np.uint8).reshape(pixel_count, 4)
    if dxgi_format in (DXGI_FORMAT_B5G6R5_UNORM, DXGI_FORMAT_B5G5R5A1_UNORM):
        words = np.frombuffer(data, dtype="<u2", count=pixel_count)
        pixels[:, 2] = (words & 31) * 255 // 31
        if dxgi_format == DXGI_FORMAT_B5G6R5_UNORM:
            pixels[:, 0] = (words >> 11) * 255 // 31
            pixels[:, 1] = ((words >> 5) & 63) * 255 // 63
            pixels[:, 3] = 255
        else:
            pixels[:, 0] = ((words >> 10) & 31) * 255 // 31
            pixels[:, 1] = ((words >> 5) & 31) * 255 // 31
            pixels[:, 3] = (words >> 15) * 255
        return bytes(out)

    if dxgi_format == DXGI_FORMAT_R10G10B10A2_UNORM:
        words = np.frombuffer(data, dtype="<u4", count=pixel_count)
        for channel, shift in enumerate((0, 10, 20)):
            pixels[:, channel] = ((words >> shift) & 1023) * 255 // 1023
        pixels[:, 3] = (words >> 30) * 85
        return bytes(out)

    raise ValueError(f"Unsupported format for direct decoding: {dxgi_format}")


def decode_texture_data(dxgi_format: int, width: int, height: int, data: bytes) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("Invalid texture dimensions")

    try:
        expected_input_size = top_mip_size_bytes(dxgi_format, width, height)
    except UnknownTextureFormat:
        raise ValueError(f"Unsupported format for direct decoding: {dxgi_format}") from None
    if len(data) < expected_input_size:
        raise ValueError(
            f"Texture payload is truncated: {len(data)} bytes "
            f"(expected at least {expected_input_size})"
        )

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


def decode_tex_mip(
    tex_file, image_index: int = 0, mip_index: int = 0, slice_index: int = 0
) -> DecodedTexture:
    header = tex_file.header
    data = tex_file.read_slice(mip_index, image_index, slice_index)
    if data is None:
        raise ValueError(
            f"mip {mip_index} stores {header.slice_count(mip_index)} slices; "
            f"slice {slice_index} is outside them"
        )
    width = max(1, header.width >> mip_index)
    height = max(1, header.height >> mip_index)
    return DecodedTexture(width, height, decode_texture_data(header.format, width, height, data))


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


@dataclass(frozen=True)
class DdsInfo:
    width: int
    height: int
    depth: int
    array_size: int
    image_count: int
    mip_count: int
    dxgi_format: int
    data_offset: int
    cubemap: bool
    is_volume: bool


def dds_info(dds_data: bytes) -> DdsInfo:
    if len(dds_data) < 128:
        raise ValueError("DDS data too small")
    if struct.unpack_from('<I', dds_data, 0)[0] != DDS_MAGIC:
        raise ValueError("Not a DDS file")

    width = struct.unpack_from('<I', dds_data, 16)[0]
    height = struct.unpack_from('<I', dds_data, 12)[0]
    depth = max(1, struct.unpack_from('<I', dds_data, 24)[0])
    mip_count = max(1, struct.unpack_from('<I', dds_data, 28)[0])

    fourcc = struct.unpack_from('<I', dds_data, 84)[0]
    if fourcc == FOURCC_DX10:
        if len(dds_data) < 148:
            raise ValueError("DDS DX10 data too small")
        dxgi_format = struct.unpack_from('<I', dds_data, 128)[0]
        dimension = struct.unpack_from('<I', dds_data, 132)[0]
        misc_flags = struct.unpack_from('<I', dds_data, 136)[0]
        depth_or_array = max(1, struct.unpack_from('<I', dds_data, 140)[0])
        is_volume = dimension == DDS_DIMENSION_TEXTURE3D
        array_size = 1 if is_volume else depth_or_array
        if is_volume:
            depth = depth_or_array
        cubemap = bool(misc_flags & DDS_MISC_TEXTURECUBE)
        data_offset = 148
    else:
        dxgi_format = _resolve_legacy_dds_dxgi(dds_data)
        caps2 = struct.unpack_from('<I', dds_data, 112)[0]
        cubemap = bool(caps2 & DDSCAPS2_CUBEMAP)
        array_size = 1
        is_volume = depth > 1
        data_offset = 128

    return DdsInfo(
        width,
        height,
        depth,
        array_size,
        array_size * CUBEMAP_FACES if cubemap else array_size,
        mip_count,
        dxgi_format,
        data_offset,
        cubemap,
        is_volume,
    )


def dds_subresources(info: DdsInfo) -> list[tuple[int, int]]:
    """Sizes count unpadded rows."""
    found = []
    offset = info.data_offset
    for _image in range(info.image_count):
        for level in range(info.mip_count):
            width, height = max(1, info.width >> level), max(1, info.height >> level)
            size = top_mip_size_bytes(info.dxgi_format, width, height) * max(1, info.depth >> level)
            found.append((offset, size))
            offset += size
    return found


def decode_dds_mip(
    dds_data: bytes, mip_index: int = 0, image_index: int = 0, slice_index: int = 0
) -> DecodedTexture:
    info = dds_info(dds_data)
    if not 0 <= mip_index < info.mip_count:
        raise ValueError(f"Mip index out of range: {mip_index}")
    if not 0 <= image_index < info.image_count:
        raise ValueError(f"Image index out of range: {image_index} of {info.image_count}")

    offset, _size = dds_subresources(info)[image_index * info.mip_count + mip_index]
    mip_w, mip_h = max(1, info.width >> mip_index), max(1, info.height >> mip_index)
    mip_slices = max(1, info.depth >> mip_index)
    if not 0 <= slice_index < mip_slices:
        raise ValueError(f"Slice index out of range: {slice_index} of {mip_slices}")

    slice_size = top_mip_size_bytes(info.dxgi_format, mip_w, mip_h)
    start = offset + slice_index * slice_size
    end = start + slice_size
    if end > len(dds_data):
        raise ValueError("DDS mip payload out of bounds")

    rgba = decode_texture_data(info.dxgi_format, mip_w, mip_h, dds_data[start:end])
    return DecodedTexture(width=mip_w, height=mip_h, rgba=rgba)
