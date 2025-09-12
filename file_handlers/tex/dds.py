import struct
from .dxgi import is_block_compressed, top_mip_size_bytes

DDS_MAGIC = 0x20534444

DDSD_CAPS = 0x1
DDSD_HEIGHT = 0x2
DDSD_WIDTH = 0x4
DDSD_PITCH = 0x8
DDSD_PIXELFORMAT = 0x1000
DDSD_MIPMAPCOUNT = 0x20000
DDSD_LINEARSIZE = 0x80000

DDPF_FOURCC = 0x4

DDSCAPS_TEXTURE = 0x1000
DDSCAPS_MIPMAP = 0x400000

FOURCC_DX10 = 0x30315844


def build_dds_dx10(
    width: int,
    height: int,
    mip_count: int,
    dxgi_format: int,
    array_size: int = 1,
    misc_flags: int = 0,
    misc_flags2: int = 0,
) -> bytes:
    size = 124
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT
    if is_block_compressed(dxgi_format):
        flags |= DDSD_LINEARSIZE
        pitch_or_linear = top_mip_size_bytes(dxgi_format, width, height)
    else:
        flags |= DDSD_PITCH
        pitch_or_linear = width * 4

    pixel_format = struct.pack(
        '<I I I I I I I I',
        32,               
        DDPF_FOURCC,       
        FOURCC_DX10,      
        0, 0, 0, 0, 0     
    )

    caps1 = DDSCAPS_TEXTURE | (DDSCAPS_MIPMAP if mip_count > 1 else 0)
    caps2 = 0
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
        1,
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

    dx10_header = struct.pack('<I I I I I', dxgi_format, 3, misc_flags, max(1, array_size), misc_flags2)
    return header + dx10_header

