"""Encode generated, linear-layout 2D Rise TEX 28 textures."""
import struct

import numpy as np

from .tex_file import TexFile, TEX_MAGIC


def encode_tex28(mips, *, compression='bc7', srgb=True):
    if compression not in ('bc7', 'rgba8'):
        raise ValueError('Compression must be bc7 or rgba8')
    if not mips or len(mips) > 15:
        raise ValueError('TEX 28 requires 1–15 mip levels')
    height, width = mips[0].shape[:2]
    if not 1 <= width <= 16384 or not 1 <= height <= 16384:
        raise ValueError('Texture dimensions must be between 1 and 16384')
    payloads, table = [], []
    offset = 40 + 16 * len(mips)
    for level, mip in enumerate(mips):
        w, h = max(1, width >> level), max(1, height >> level)
        if mip.shape != (h, w, 4) or mip.dtype != np.uint8:
            raise ValueError(f'Invalid RGBA8 mip dimensions or dtype at level {level}')
        if compression == 'bc7':
            import ispc_texcomp as encoder
            padded = np.pad(mip, ((0, (-h) % 4), (0, (-w) % 4), (0, 0)), mode='edge')
            surface = encoder.RGBASurface(padded.tobytes(), padded.shape[1], padded.shape[0])
            raw = encoder.compress_blocks_bc7(surface, encoder.BC7EncSettings.from_profile('alpha_basic'))
            pitch = ((w + 3) // 4) * 16
            expected = pitch * ((h + 3) // 4)
        else:
            raw = mip.tobytes()
            pitch, expected = w * 4, w * h * 4
        if len(raw) != expected:
            raise ValueError('Texture encoder returned an invalid payload size')
        table.append(struct.pack('<qii', offset, pitch, len(raw)))
        payloads.append(raw)
        offset += len(raw)
    format_id = (99 if srgb else 98) if compression == 'bc7' else (29 if srgb else 28)
    header = struct.pack('<IihhhBBiiIi', TEX_MAGIC, 28, width, height, 1, 1, len(mips) * 16, format_id, -1, 0, 256)
    data = header + bytes(8) + b''.join(table + payloads)
    check = TexFile()
    if not check.read(data, file_version=28):
        raise ValueError('Encoded TEX failed to reopen')
    for level in range(len(mips)):
        decoded = check.get_mip_map_data(level)
        if len(decoded.data) != check._expected_mip_layout(decoded.width, decoded.height)[0]:
            raise ValueError('Encoded TEX has an invalid mip layout')
    return data
