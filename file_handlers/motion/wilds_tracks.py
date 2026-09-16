"""MOT 932 channels. Sparse vector packs are separate from the MOT 495 dialect.

Packing reference: kagenocookie/RE-Engine-Lib, MotFile.cs (61f0cce).
Sparse axis selectors follow XY/YZ/ZX across bit widths, as confirmed against
the native full-precision Rise tracks shared by the Wilds corpus.
"""
import numpy as np

from .mhr_tracks import decode_values as decode_495_values, decode_track as read_track
from .mot.model import TrackFamily
from .errors import MotionParseError


def _codes(c, offset, count, width, endian):
    c.require(offset, count * width, 'MOT 932 packed values')
    return np.fromiter((int.from_bytes(c.data[offset+i*width:offset+(i+1)*width], endian)
                        for i in range(count)), dtype=np.uint64, count=count)


def decode_values(c, offset, count, family, mode, parameter_offset):
    rotation = family == TrackFamily.QUATERNION
    def parameters(n):
        c.require(parameter_offset, n*4, 'MOT 932 unpack parameters')
        return np.frombuffer(c.data, '<f4', n, parameter_offset).astype(np.float64)

    if 0x31 <= mode <= 0x33:
        axis = mode - 0x31
        p = parameters(2 if rotation else 4)
        values = np.zeros((count, 3)) if rotation else np.tile(p[1:4], (count, 1))
        values[:, axis] = _codes(c, offset, count, 3, 'big') / 0xFFFFFF * p[0] + p[1 if rotation else axis+1]
        if rotation:
            values = np.column_stack((values, np.sqrt(np.maximum(0, 1-(values*values).sum(axis=1)))))
    elif rotation and mode == 0x60:
        # MOT 932 changed quaternion-48 to a big-endian codeword. MOT 495
        # instead stores three little-endian uint16 components.
        p = parameters(7)
        codes = _codes(c, offset, count, 6, 'big')
        xyz = np.column_stack([((codes >> (i*16)) & 65535).astype(np.float64)
                               for i in range(3)]) / 65535 * p[:3] + p[4:7]
        values = np.column_stack((xyz, np.sqrt(np.maximum(0, 1-(xyz*xyz).sum(axis=1)))))
    elif rotation:
        if mode not in (0, 0x20, 0x21, 0x22, 0x23, 0x30, 0x40, 0x41, 0x42, 0x43, 0x50, 0x70, 0x80, 0xC0):
            raise MotionParseError(f'{c.label}: unsupported MOT 932 rotation compression 0x{mode:02X}')
        return decode_495_values(c, offset, count, family, mode, parameter_offset)
    elif mode in (0x30, 0x50, 0x60, 0x70):
        width, bits = {0x30: (3, 8), 0x50: (5, 13), 0x60: (6, 16), 0x70: (7, 18)}[mode]
        codes = _codes(c, offset, count, width, 'little' if width == 3 else 'big')
        p = parameters(6)
        values = np.column_stack([((codes >> (i*bits)) & ((1 << bits)-1)).astype(np.float64)
                                  for i in range(3)]) / ((1 << bits)-1)
        values = values*p[:3] + p[3:6]
    elif mode == 0x24:
        p = parameters(2)
        values = np.repeat((_codes(c, offset, count, 2, 'little') / 65535 * p[0] + p[1])[:, None], 3, axis=1)
    elif mode in (0x85, 0x86, 0x87):
        axes = {0x85: (0, 1), 0x86: (1, 2), 0x87: (2, 0)}[mode]
        values = np.tile(parameters(3), (count, 1))
        c.require(offset, count*8, 'MOT 932 two-axis float values')
        values[:, axes] = np.frombuffer(c.data, '<f4', count*2, offset).reshape(count, 2)
    elif mode in SPARSE_VECTORS:
        axes, width, bits, endian = SPARSE_VECTORS[mode]
        p = parameters(5)
        codes = _codes(c, offset, count, width, endian)
        values = np.tile(p[2:5], (count, 1))
        for component, axis in enumerate(axes):
            values[:, axis] += ((codes >> (component*bits)) & ((1 << bits)-1)).astype(np.float64) / ((1 << bits)-1) * p[component]
    else:
        if mode not in (0, 0x20, 0x21, 0x22, 0x23, 0x40, 0x41, 0x42, 0x43, 0x44, 0x80):
            raise MotionParseError(f'{c.label}: unsupported MOT 932 vector compression 0x{mode:02X}')
        return decode_495_values(c, offset, count, family, mode, parameter_offset)
    if not np.isfinite(values).all():
        raise MotionParseError(f'{c.label}: non-finite MOT 932 {family.name} values for compression 0x{mode:02X}')
    return [tuple(row) for row in values.tolist()]


SPARSE_VECTORS = {
    0x25: ((0, 1), 2, 8, 'little'), 0x26: ((1, 2), 2, 8, 'little'), 0x27: ((2, 0), 2, 8, 'little'),
    0x35: ((0, 1), 3, 12, 'big'), 0x36: ((1, 2), 3, 12, 'big'), 0x37: ((2, 0), 3, 12, 'big'),
    0x45: ((0, 1), 4, 16, 'little'), 0x46: ((1, 2), 4, 16, 'little'), 0x47: ((2, 0), 4, 16, 'little'),
    0x55: ((0, 1), 5, 20, 'big'), 0x56: ((1, 2), 5, 20, 'big'), 0x57: ((2, 0), 5, 20, 'big'),
    0x65: ((0, 1), 6, 24, 'big'), 0x66: ((1, 2), 6, 24, 'big'), 0x67: ((2, 0), 6, 24, 'big'),
    0x76: ((1, 2), 7, 28, 'big'), 0x77: ((2, 0), 7, 28, 'big'),
}


def decode_track(c, offset, base, family):
    return read_track(c, offset, base, family, values_decoder=decode_values)
