"""MOT v495 compact joint tracks (Rise/Sunbreak).

Layout references: alphazolam/RE-Engine-010-Templates and
alphazolam/fmt_RE_MESH-Noesis-Plugin. The 40/56-bit streams are big endian;
the ordinary 16/32/64-bit codewords are little endian.
"""
from __future__ import annotations

import math
import struct

import numpy as np

from .binary import ReadContext
from .errors import MotionParseError, MotionWriteError
from .mot.model import KeyTrack, TrackFamily


def decode_values(c: ReadContext, offset: int, count: int, family: TrackFamily,
                  mode: int, parameter_offset: int) -> list[tuple]:
    rotation = family == TrackFamily.QUATERNION
    packed = {0x20: (2, 5), 0x40: (4, 10), 0x80: (8, 21)}
    if rotation:
        packed.update({0x30: (3, 8), 0x50: (5, 13), 0x60: (6, 16), 0x70: (7, 18)})
    if mode in packed:
        width, bits = packed[mode]
        c.require(offset, count * width, "packed v495 track")
        needed = 7 if rotation else 6
        c.require(parameter_offset, needed * 4, "v495 unpack parameters")
        params = np.frombuffer(c.data, '<f4', needed, parameter_offset)
        if width in (2, 4, 8):
            codes = np.frombuffer(c.data, f'<u{width}', count, offset).astype(np.uint64)
        else:
            order = 'big' if width in (5, 7) else 'little'
            codes = np.fromiter((int.from_bytes(c.data[offset+i*width:offset+(i+1)*width], order)
                                 for i in range(count)), dtype=np.uint64, count=count)
        values = np.column_stack([((codes >> (i*bits)) & ((1 << bits)-1)).astype(np.float64)
                                  for i in range(3)]) / ((1 << bits)-1)
        values = values * params[:3] + params[4:7] if rotation else values * params[:3] + params[3:6]
    elif 0x21 <= mode <= 0x23 or (not rotation and mode == 0x24):
        axis = mode - 0x21
        c.require(offset, count * 2, "axis v495 track")
        needed = 2 if rotation else 4
        c.require(parameter_offset, needed * 4, "axis unpack parameters")
        params = np.frombuffer(c.data, '<f4', needed, parameter_offset)
        codes = np.frombuffer(c.data, '<u2', count, offset).astype(np.float64)
        if axis == 3:
            values = np.repeat((codes / 65535.0 * params[0] + params[1])[:, None], 3, axis=1)
        else:
            values = np.zeros((count, 3)) if rotation else np.tile(params[1:4], (count, 1))
            values[:, axis] = codes / 65535.0 * params[0] + params[1 if rotation else axis+1]
    elif 0x41 <= mode <= 0x43:
        axis = mode - 0x41
        c.require(offset, count * 4, "float axis v495 track")
        if rotation:
            values = np.zeros((count, 3))
        else:
            c.require(parameter_offset, 12, "axis defaults")
            values = np.tile(np.frombuffer(c.data, '<f4', 3, parameter_offset), (count, 1))
        values[:, axis] = np.frombuffer(c.data, '<f4', count, offset)
    elif not rotation and mode == 0x44:
        c.require(offset, count * 4, "uniform float v495 track")
        values = np.repeat(np.frombuffer(c.data, '<f4', count, offset)[:, None], 3, axis=1)
    elif mode == 0 or (rotation and mode == 0xC0):
        components = 4 if rotation and mode == 0 else 3
        c.require(offset, count * components * 4, "full v495 track")
        values = np.frombuffer(c.data, '<f4', count * components, offset).reshape(count, components)
    else:
        raise MotionParseError(f"{c.label}: unsupported v495 {family.name} compression 0x{mode:02X}")
    if not np.isfinite(values).all():
        raise MotionParseError(f"{c.label}: non-finite v495 track values")
    if rotation and values.shape[1] == 3:
        values = np.column_stack((values, np.sqrt(np.maximum(0, 1 - np.sum(values*values, axis=1)))))
    return [tuple(row) for row in values.tolist()]


def decode_track(c: ReadContext, offset: int, base: int, family: TrackFamily) -> KeyTrack:
    c.require(offset, 20, "v495 track header")
    flags, count, frames, values, params = struct.unpack_from('<5I', c.data, offset)
    solver = 0x112 if family == TrackFamily.QUATERNION else 0xF2
    if flags & 0xFFF != solver:
        raise MotionParseError(f"{c.label}: incompatible track solver 0x{flags & 0xFFF:X}")
    frame_type = (flags >> 20) & 0xF
    dtype = {2: '<u1', 4: '<u2', 5: '<u4'}.get(frame_type)
    if dtype is None or not count or (not frames and count != 1) or not values:
        raise MotionParseError(f"{c.label}: invalid v495 key table")
    if frames:
        c.require(base + frames, count * np.dtype(dtype).itemsize, "v495 key frames")
        times = np.frombuffer(c.data, dtype, count, base + frames).tolist()
    else:
        times = [0]
    if any(a > b for a, b in zip(times, times[1:])):
        raise MotionParseError(f"{c.label}: v495 key frames are not in chronological order")
    mode = (flags >> 12) & 0xFF
    parameter_free = mode == 0 or (family == TrackFamily.QUATERNION and mode in (0xC0, 0x41, 0x42, 0x43)) or (family == TrackFamily.VECTOR3 and mode == 0x44)
    if not params and not parameter_free:
        raise MotionParseError(f"{c.label}: missing unpack parameters")
    return KeyTrack(family, times, decode_values(c, base + values, count, family, mode, base + params))


def encode_track(track: KeyTrack) -> tuple[int, bytes, bytes]:
    """Write edited keys losslessly as native full-precision v495 channels."""
    if not track.frames or len(track.frames) != len(track.values):
        raise MotionWriteError("Track frames and values must have the same nonzero length")
    if any(isinstance(f, bool) or not isinstance(f, int) or not 0 <= f <= 0xFFFFFFFF for f in track.frames):
        raise MotionWriteError("Key frames must be unsigned 32-bit integers")
    if any(a > b for a, b in zip(track.frames, track.frames[1:])):
        raise MotionWriteError("Key frames must be in chronological order")
    fmt, frame_type = ('B', 2) if max(track.frames) <= 255 else ('H', 4) if max(track.frames) <= 65535 else ('I', 5)
    rotation = track.family == TrackFamily.QUATERNION
    components = 4 if rotation else 3
    if any(len(v) != components or not all(math.isfinite(x) for x in v) for v in track.values):
        raise MotionWriteError("Track key components must be finite")
    flags = (frame_type << 20) | (0x112 if rotation else 0xF2)
    return flags, struct.pack('<' + fmt * len(track.frames), *track.frames), np.asarray(track.values, dtype='<f4').tobytes()
