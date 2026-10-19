import struct
from .dxgi import get_bits_per_pixel, is_block_compressed, top_mip_size_bytes

DDS_MAGIC = 0x20534444

DDSD_CAPS = 0x1
DDSD_HEIGHT = 0x2
DDSD_WIDTH = 0x4
DDSD_PITCH = 0x8
DDSD_PIXELFORMAT = 0x1000
DDSD_MIPMAPCOUNT = 0x20000
DDSD_LINEARSIZE = 0x80000
DDSD_DEPTH = 0x800000

DDPF_FOURCC = 0x4

DDSCAPS_COMPLEX = 0x8
DDSCAPS_TEXTURE = 0x1000
DDSCAPS_MIPMAP = 0x400000

DDSCAPS2_CUBEMAP = 0x200
DDSCAPS2_CUBEMAP_ALLFACES = 0xFC00
DDSCAPS2_VOLUME = 0x200000

DDS_MISC_TEXTURECUBE = 0x4
CUBEMAP_FACES = 6

DDS_DIMENSION_TEXTURE2D = 3
DDS_DIMENSION_TEXTURE3D = 4

FOURCC_DX10 = 0x30315844


def build_dds_dx10(
    width: int,
    height: int,
    mip_count: int,
    dxgi_format: int,
    array_size: int = 1,
    depth: int = 1,
    misc_flags: int = 0,
    misc_flags2: int = 0,
) -> bytes:
    is_volume = depth > 1
    is_cubemap = bool(misc_flags & DDS_MISC_TEXTURECUBE)

    size = 124
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT
    if mip_count > 1:
        flags |= DDSD_MIPMAPCOUNT
    if is_block_compressed(dxgi_format):
        flags |= DDSD_LINEARSIZE
        pitch_or_linear = top_mip_size_bytes(dxgi_format, width, height)
    else:
        flags |= DDSD_PITCH
        pitch_or_linear = width * (get_bits_per_pixel(dxgi_format) // 8)
    if is_volume:
        flags |= DDSD_DEPTH

    pixel_format = struct.pack(
        '<I I I I I I I I',
        32,               
        DDPF_FOURCC,       
        FOURCC_DX10,      
        0, 0, 0, 0, 0
    )

    caps1 = DDSCAPS_TEXTURE
    if mip_count > 1:
        caps1 |= DDSCAPS_MIPMAP
    if is_cubemap or is_volume or array_size > 1:
        caps1 |= DDSCAPS_COMPLEX
    caps2 = 0
    if is_cubemap:
        caps2 |= DDSCAPS2_CUBEMAP | DDSCAPS2_CUBEMAP_ALLFACES
    if is_volume:
        caps2 |= DDSCAPS2_VOLUME
    caps3 = 0
    caps4 = 0
    reserved2 = 0

    reserved1 = b'\x00' * 44

    header = struct.pack(
        '<I'   # size
        'I'    # flags
        'I'    # height
        'I'    # width
        'I'    # pitchOrLinearSize
        'I'    # depth
        'I'    # mipMapCount
        '44s'  # reserved1[11]
        '32s'  # pixel format
        'I'    # caps
        'I'    # caps2
        'I'    # caps3
        'I'    # caps4
        'I',   # reserved2
        size,
        flags,
        height,
        width,
        pitch_or_linear,
        depth,
        mip_count,
        reserved1,
        pixel_format,
        caps1,
        caps2,
        caps3,
        caps4,
        reserved2,
    )
    header = struct.pack('<I', DDS_MAGIC) + header

    # For a cubemap, DX10 arraySize counts cubes (faces / 6).
    dimension = DDS_DIMENSION_TEXTURE3D if is_volume else DDS_DIMENSION_TEXTURE2D
    depth_or_array = depth if is_volume else array_size
    dx10_header = struct.pack(
        '<I I I I I', dxgi_format, dimension, misc_flags, depth_or_array, misc_flags2
    )
    return header + dx10_header


