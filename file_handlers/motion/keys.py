"""See docs/internals/mot-format.md, Keys."""

from __future__ import annotations

import numpy as np

from .model import GpuMotion, GpuStream, Track
from .rules import Encoding, gpu_format

F32 = np.float32
COMPONENTS = {"float": 1, "vector3": 3, "quaternion": 4}
_ONE = F32(1.0)
_ZERO = F32(0.0)


def _stored(track: Track, enc: Encoding) -> np.ndarray:
    """The stored quantities as float32 (keys, coded components): raw floats, or integer codes."""
    if not enc.bits:
        return np.frombuffer(track.data, "<f4").reshape(-1, enc.size // 4).copy()
    raw = np.frombuffer(track.data, np.uint8).reshape(-1, enc.size).astype(np.uint64)
    if enc.endian == "big":
        raw = raw[:, ::-1]
    word = np.bitwise_or.reduce(raw << (np.arange(enc.size, dtype=np.uint64) * np.uint64(8)), axis=1)
    count = {"packed": 3 if enc.kind != "float" else 1, "pair": 2}.get(enc.decode, 1)
    mask = np.uint64((1 << enc.bits) - 1)
    return np.stack([(word >> np.uint64(enc.bits * i)) & mask for i in range(count)], axis=1).astype(F32)


def _scaled(code: np.ndarray, scale: F32, offset: F32, bits: int, order: str) -> np.ndarray:
    recip = F32(1.0 / ((1 << bits) - 1))
    if order == "code*recip*scale":
        return code * recip * scale + offset
    if order == "code*scale*recip":
        return code * scale * recip + offset
    return code * (scale * recip) + offset


def _with_w(xyz: np.ndarray) -> np.ndarray:
    x, y, z = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    rest = _ONE - (x * x + y * y + z * z + _ZERO)
    w = np.where(rest > _ZERO, np.sqrt(np.maximum(rest, _ZERO)), _ZERO)
    return np.column_stack([xyz, w]).astype(F32)


def _decoded(stored: np.ndarray, params: np.ndarray, enc: Encoding, order: str) -> np.ndarray:
    if enc.decode == "full":
        values = stored
    elif enc.decode == "packed":
        half = len(params) // 2
        values = np.column_stack([_scaled(stored[:, i], params[i], params[half + i], enc.bits, order)
                                  for i in range(stored.shape[1])])
    elif enc.decode == "uniform":
        value = _scaled(stored[:, 0], params[0], params[1], enc.bits, order) if enc.bits else stored[:, 0]
        values = np.column_stack([value] * 3)
    else:
        coded = (enc.axis,) if enc.decode == "axis" else enc.axes
        base = params[1 if enc.decode == "axis" else 2:] if enc.bits else params
        components = 3 if enc.kind == "vector3" else COMPONENTS[enc.kind] - 1
        if len(base) >= components:
            values = np.tile(base[:components], (len(stored), 1))
            offsets = [base[axis] for axis in coded]
        else:
            values = np.zeros((len(stored), components), F32)
            offsets = list(base) or [_ZERO] * len(coded)
        for i, axis in enumerate(coded):
            values[:, axis] = (_scaled(stored[:, i], params[i], offsets[i], enc.bits, order) if enc.bits
                               else stored[:, i])
    values = np.asarray(values, F32)
    return _with_w(values) if enc.kind == "quaternion" and values.shape[1] == 3 else values


def full_track(kind: str, frames: list[int], values, max_frame: float = 0.0) -> Track:
    values = np.asarray(values, "<f4").reshape(len(frames), COMPONENTS[kind])
    return Track(kind, [int(frame) for frame in frames], 0, (), values.tobytes(), max_frame)


def decode_keys(track: Track, enc: Encoding) -> np.ndarray:
    return _decoded(_stored(track, enc), np.asarray(track.parameters, F32), enc, enc.order)


def _nlerp(a: np.ndarray, b: np.ndarray, t: F32) -> np.ndarray:
    if a[0] * b[0] + a[1] * b[1] + a[2] * b[2] + a[3] * b[3] < _ZERO:
        b = -b
    m = (b - a) * t + a
    return m * (_ONE / np.sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2] + m[3] * m[3]))


def _nlerp2(a: np.ndarray, b: np.ndarray, t: F32, axis: int) -> np.ndarray:
    pa, pb = np.array([a[axis], a[3]]), np.array([b[axis], b[3]])
    if pa[0] * pb[0] + pa[1] * pb[1] < _ZERO:
        pb = -pb
    m = (pb - pa) * t + pa
    m = m / np.sqrt(m[0] * m[0] + m[1] * m[1])
    out = np.zeros(4, F32)
    out[axis], out[3] = m
    return out


class Curve:
    def __init__(self, track: Track, enc: Encoding):
        self.enc = enc
        self.frames = np.asarray(track.frames, F32)
        self.params = np.asarray(track.parameters, F32)
        self.stored = _stored(track, enc)
        self.values = _decoded(self.stored, self.params, enc, enc.order)
        self.blend_values = (self.values if enc.blend_order == enc.order or enc.blend == "code"
                             else _decoded(self.stored, self.params, enc, enc.blend_order))

    def sample(self, frame: float) -> np.ndarray:
        frames, time = self.frames, F32(frame)
        last = len(self.values) - 1
        if last == 0:
            return self.values[0]
        index = int(np.searchsorted(frames, time, side="right")) - 1
        if index >= last:
            return self.values[last]
        start = frames[index] if index >= 0 else _ZERO
        index = max(index, 0)
        t = (time - start) / (frames[index + 1] - start)
        if t <= _ZERO:
            return self.values[index]
        if t >= _ONE:
            return self.values[index + 1]
        return self._blend(index, t)

    def sample_many(self, times) -> np.ndarray:
        times = np.asarray(times, F32)
        values, last = self.values, len(self.values) - 1
        if last == 0:
            return np.repeat(values[:1], len(times), 0)
        found = np.searchsorted(self.frames, times, side="right") - 1
        index = np.clip(found, 0, last - 1)
        start = np.where(found >= 0, self.frames[index], _ZERO)
        with np.errstate(divide="ignore", invalid="ignore"):
            t = (times - start) / (self.frames[index + 1] - start)
            out = self._blend_rows(index, t[:, None])
        out = np.where((t <= _ZERO)[:, None], values[index], out)
        out = np.where((t >= _ONE)[:, None], values[index + 1], out)
        return np.where((found >= last)[:, None], values[last], out)

    def _blend(self, index: int, t: F32) -> np.ndarray:
        enc = self.enc
        if enc.blend == "code":
            a, b = self.stored[index], self.stored[index + 1]
            return _decoded(((b - a) * t + a)[None, :], self.params, enc, enc.blend_order)[0]
        a, b = self.blend_values[index], self.blend_values[index + 1]
        if enc.blend == "nlerp":
            return _nlerp(a, b, t)
        if enc.blend == "nlerp2":
            return _nlerp2(a, b, t, enc.axis)
        if enc.blend == "mix":
            return a * (_ONE - t) + b * t
        return (b - a) * t + a

    def _blend_rows(self, index: np.ndarray, t: np.ndarray) -> np.ndarray:
        enc = self.enc
        if enc.blend == "code":
            a, b = self.stored[index], self.stored[index + 1]
            return _decoded((b - a) * t + a, self.params, enc, enc.blend_order)
        a, b = self.blend_values[index], self.blend_values[index + 1]
        if enc.blend == "nlerp":
            dot = a[:, 0] * b[:, 0] + a[:, 1] * b[:, 1] + a[:, 2] * b[:, 2] + a[:, 3] * b[:, 3]
            m = (np.where(dot[:, None] < _ZERO, -b, b) - a) * t + a
            return m * (_ONE / np.sqrt(m[:, 0] * m[:, 0] + m[:, 1] * m[:, 1] + m[:, 2] * m[:, 2] + m[:, 3] * m[:, 3]))[
                :, None]
        if enc.blend == "nlerp2":
            pa, pb = a[:, [enc.axis, 3]], b[:, [enc.axis, 3]]
            m = (np.where((pa[:, 0] * pb[:, 0] + pa[:, 1] * pb[:, 1])[:, None] < _ZERO, -pb, pb) - pa) * t + pa
            m = m / np.sqrt(m[:, 0] * m[:, 0] + m[:, 1] * m[:, 1])[:, None]
            out = np.zeros((len(m), 4), F32)
            out[:, enc.axis], out[:, 3] = m[:, 0], m[:, 1]
            return out
        if enc.blend == "mix":
            return a * (_ONE - t) + b * t
        return (b - a) * t + a


def _gpu_stream(stream: GpuStream, name: str, joints: int) -> np.ndarray:
    spec = gpu_format(name, stream.format)
    if spec["decode"] == "full":
        return np.frombuffer(stream.data, "<f4").reshape(-1, joints, spec["size"] // 4).copy()
    raw = np.frombuffer(stream.data, np.uint8).reshape(-1, joints, spec["size"]).astype(np.uint64)
    word = np.bitwise_or.reduce(raw << (np.arange(spec["size"], dtype=np.uint64) * np.uint64(8)), axis=2)
    mask = np.uint64((1 << spec["bits"]) - 1)
    codes = np.stack([(word >> np.uint64(shift)) & mask for shift in spec["shifts"]], axis=2).astype(F32)
    ranges = np.asarray(stream.ranges, F32).reshape(joints, 2, -1)
    xyz = codes * ranges[:, 1] + ranges[:, 0]
    if name == "translation":
        return xyz
    x, y, z = xyz[..., 0], xyz[..., 1], xyz[..., 2]
    w = np.sqrt(np.abs(_ONE - (x * x + y * y + z * z)))
    return np.concatenate([xyz, w[..., None]], axis=2)


class GpuCurves:
    def __init__(self, gpu: GpuMotion):
        joints = len(gpu.joint_hashes)
        self.translations = _gpu_stream(gpu.translation, "translation", joints)
        self.rotations = _gpu_stream(gpu.rotation, "rotation", joints)

    def sample(self, frame: float) -> tuple[np.ndarray, np.ndarray]:
        last = len(self.translations) - 1
        frame = F32(min(max(frame, 0.0), last))
        low, high = int(np.floor(frame)), int(np.ceil(frame))
        t = frame - F32(low)
        a, b = self.translations[low], self.translations[high]
        translations = (b - a) * t + a
        qa, qb = self.rotations[low], self.rotations[high]
        dot = qa[:, 0] * qb[:, 0] + qa[:, 1] * qb[:, 1] + qa[:, 2] * qb[:, 2] + qa[:, 3] * qb[:, 3]
        qb = np.where((dot < _ZERO)[:, None], -qb, qb)
        m = (qb - qa) * t + qa
        length = np.sqrt(m[:, 0] * m[:, 0] + m[:, 1] * m[:, 1] + m[:, 2] * m[:, 2] + m[:, 3] * m[:, 3])
        return translations, m * (_ONE / length)[:, None]
