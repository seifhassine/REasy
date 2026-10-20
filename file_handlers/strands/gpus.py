from dataclasses import dataclass, field, fields
import struct

import numpy as np

from file_handlers.clsp.clsp_file import EVERY_TAG
lazy from utils.resource_file_utils import resource_version_from_path

_F = np.float32
# The only .gpus resource version known.
GPUS_VERSION = 13
# Where the engines' search for the end of a curve looks (the presets' sampler).
_FAR = _F(1e8)


@dataclass
class CurveKey:
    value: float
    kind: int
    time: float
    in_y: float = 0
    in_x: float = 0
    out_y: float = 0
    out_x: float = 0


def _solve_time(t0, t1, out_x, in_x, x):
    """The other roots as the engines compute them (no square root)."""
    if t0 >= x:
        return _F(0)
    if x >= t1:
        return _F(1)
    cubic = _F(_F(_F(_F(t0 + t0) - _F(t1 + t1)) + out_x) + in_x)
    b = _F(_F(_F(_F(_F(t0 * _F(-3)) + _F(t1 * _F(3))) - _F(out_x + out_x)) - in_x) / cubic)
    s = _F(_F(x - t0) / _F(t1 - t0))
    c0, c1 = _F(_F(t0 - x) / cubic), _F(out_x / cubic)
    two_b = _F(b + b)
    if _F(c1 + _F(_F(_F(s * _F(3)) * s) + _F(s * two_b))) != 0:
        for _ in range(100):
            old = s
            low = _F(_F(old * _F(old * _F(old + old))) + _F(old * _F(old * b)))
            slope = _F(_F(_F(old * two_b) + _F(old * _F(old * _F(3)))) + c1)
            s = _F(_F(low - c0) / slope)
            if abs(float(_F(s - old))) <= 1e-5:
                break
    if _F(0) <= s <= _F(1):
        return s
    p = _F(b + s)
    d = _F(_F(p * p) + _F(_F(_F(s * s) + _F(_F(b * s) + c1)) * _F(-4)))
    r = _F(_F(d - p) * _F(.5))
    if _F(0) <= r <= _F(1):
        return r
    r = _F(_F(_F(-p) - d) * _F(.5))
    return (r if r <= _F(1) else s) if _F(0) <= r else s


@dataclass
class AnimationCurve:
    keys: tuple[CurveKey, ...] = ()
    loop_count: int = 0
    loop_start: float = 0
    loop_end: float = 1
    minimum: float = 0
    maximum: float = 1
    wrap: int = 0
    flags: int = 1

    @classmethod
    def read(cls, data, offset):
        count, loops, start, end, low, high, wrap, flags = struct.unpack_from('<HH4fiI', data, offset)
        pointer = struct.unpack_from('<Q', data, offset + 32)[0]
        if count and (pointer < 264 or pointer % 8 or pointer + count*16 > len(data)):
            raise ValueError('Invalid GPUS curve key span')
        keys = tuple(CurveKey(*struct.unpack_from('<fH5e', data, pointer + i*16)) for i in range(count))
        curve = cls(keys, loops, start, end, low, high, wrap, flags)
        curve.validate()
        return curve

    def validate(self):
        values = [self.loop_start, self.loop_end, self.minimum, self.maximum]
        values.extend(v for k in self.keys for v in (k.value, k.time, k.in_y, k.in_x, k.out_y, k.out_x))
        if not np.isfinite(values).all() or self.minimum > self.maximum:
            raise ValueError('Invalid GPUS curve values')
        if any(a.time > b.time for a, b in zip(self.keys, self.keys[1:])):
            raise ValueError('GPUS curve keys are not ordered')
        if any(k.kind not in range(7) for k in self.keys):
            raise ValueError('Unsupported GPUS curve interpolation')

    def _key(self, x):
        later = next((i for i, key in enumerate(self.keys) if _F(key.time) > x), 0)
        return (later or len(self.keys)) - 1

    @property
    def endpoint(self):
        return float(self.keys[self._key(_FAR)].time) if self.keys else 0.0

    def at(self, x):
        if not self.keys:
            return _F(0)
        index = self._key(x)
        a = self.keys[index]
        if a.kind == 2:
            value = _F(a.value)
        else:
            b = self.keys[min(index + 1, len(self.keys) - 1)]
            t0, t1, v0, v1 = _F(a.time), _F(b.time), _F(a.value), _F(b.value)
            s = _F(1) if b is a else _F(_F(x - t0) / _F(t1 - t0))
            if a.kind == 1:
                value = _F(_F(_F(_F(1) - s) * v0) + _F(s * v1))
            else:
                if a.kind in (5, 6):
                    s = _solve_time(t0, t1, _F(a.out_x), _F(b.in_x), x)
                u, v = _F(a.out_y), _F(b.in_y)
                cubic = _F(_F(_F(_F(v0 + v0) - _F(v1 + v1)) + u) + v)
                square = _F(_F(_F(_F(v0 * _F(-3)) + _F(v1 * _F(3))) - _F(u + u)) - v)
                s2 = _F(s * s)
                value = _F(v0 + _F(_F(u * s) + _F(_F(cubic * _F(s * s2)) + _F(s2 * square))))
        if self.flags & 1:
            low, high = _F(self.minimum), _F(self.maximum)
            value = low if value < low else min(value, high)
        return value

    def evaluate(self, time):
        """Single precision, in the engines' order, without fused multiply-add."""
        return np.vectorize(self.at, otypes=[_F])(np.asarray(time, _F))



def _value(offset, kind, default):
    return field(default=default, metadata={'offset': offset, 'kind': kind})


def _curve(offset):
    return field(default_factory=AnimationCurve, metadata={'offset': offset, 'kind': 'curve'})


@dataclass
class GpuStrandsPreset:
    radius: float = _value(0x04, 'f', .0001)
    density: float = _value(0x08, 'f', 1320)
    stretch_stiffness: float = _value(0x0c, 'f', 1)
    bend_stiffness: float = _value(0x10, 'f', 3e-7)
    viscosity: float = _value(0x14, 'f', 0)
    use_deformation_weight: int = _value(0x18, 'I', 0)
    use_relative_distance: int = _value(0x1c, 'I', 0)
    strand_weight_curve: AnimationCurve = _curve(0x20)
    static_friction: float = _value(0x48, 'f', 1e-6)
    kinetic_friction: float = _value(0x4c, 'f', 1e-6)
    responsive_curve: AnimationCurve = _curve(0x50)
    use_responsive_curve: int = _value(0x78, 'H', 0)
    use_static_friction_curve: int = _value(0x7a, 'H', 0)
    use_swing_limit: int = _value(0x7c, 'H', 0)
    use_twist_limit: int = _value(0x7e, 'H', 0)
    swing_limit: float = _value(0x80, 'f', 1)
    swing_limit_stiffness: float = _value(0x84, 'f', .1)
    twist_limit: float = _value(0x88, 'f', 1)
    twist_limit_stiffness: float = _value(0x8c, 'f', .1)
    use_volume_preservation: int = _value(0x90, 'H', 0)
    use_inter_smoothing: int = _value(0x92, 'H', 0)
    repulsive_strength: float = _value(0x94, 'f', .1)
    inter_smoothing_strength: float = _value(0x98, 'f', .1)
    density_cell_width: float = _value(0x9c, 'f', .1)
    velocity_cell_width: float = _value(0xa0, 'f', .1)
    linear_damping: float = _value(0xa4, 'f', .03)
    angular_damping: float = _value(0xa8, 'f', .001)
    linear_ema: float = _value(0xac, 'f', .5)
    angular_ema: float = _value(0xb0, 'f', .9)
    use_damping_scaling_curve: int = _value(0xb4, 'I', 0)
    damping_scaling_curve: AnimationCurve = _curve(0xb8)
    static_friction_weights: tuple[float, ...] = field(default_factory=lambda: (1.0,)*32)
    clump_stray_amount: float = _value(0xe8, 'f', 1)
    clump_tightness: float = _value(0xec, 'f', 0)
    clump_limit: float = _value(0xf0, 'f', 0)
    restitution: float = _value(0xf4, 'f', 0)
    collision_tag: int = _value(0x100, 'Q', EVERY_TAG)
    _original: bytes = field(default=b'', repr=False, compare=False)

    @classmethod
    def read(cls, data, version=GPUS_VERSION):
        if version != GPUS_VERSION:
            raise ValueError(f'Unsupported GPUS version: {version}')
        if len(data) < 264 or data[:4] != b'GSTR':
            raise ValueError('Invalid or truncated GPUS header')
        values = {}
        for f in fields(cls):
            if f.metadata:
                offset, kind = f.metadata['offset'], f.metadata['kind']
                values[f.name] = (AnimationCurve.read(data, offset) if kind == 'curve'
                                  else struct.unpack_from('<'+kind, data, offset)[0])
        offset = struct.unpack_from('<Q', data, 0xe0)[0]
        if offset < 264 or offset % 8 or offset + 128 > len(data):
            raise ValueError('Invalid GPUS friction curve span')
        result = cls(**values, static_friction_weights=struct.unpack_from('<32f', data, offset), _original=bytes(data))
        result.validate()
        return result

    def validate(self):
        values = list(self.static_friction_weights)
        for f in fields(self):
            kind = f.metadata.get('kind')
            if kind == 'curve':
                getattr(self, f.name).validate()
            elif kind == 'f':
                values.append(getattr(self, f.name))
        if len(self.static_friction_weights) != 32 or not np.isfinite(values).all():
            raise ValueError('Invalid GPUS material values')
        if min(self.radius, self.density, self.stretch_stiffness, self.bend_stiffness) < 0:
            raise ValueError('Invalid GPUS radius, density or stiffness')

    def to_bytes(self):
        self.validate()
        data = bytearray(self._original or bytes(264))
        data[:4] = b'GSTR'
        spans = [(struct.unpack_from('<Q', data, f.metadata['offset']+32)[0],
                  16*struct.unpack_from('<H', data, f.metadata['offset'])[0])
                 for f in fields(self) if f.metadata.get('kind') == 'curve']
        spans.append((struct.unpack_from('<Q', data, 0xe0)[0], 128))

        def write_payload(pointer, old_size, payload):
            shared = sum(a < pointer+old_size and pointer < a+n for a, n in spans if n) > 1
            if len(payload) != old_size or (payload and not pointer) or (shared and data[pointer:pointer+old_size] != payload):
                data.extend(bytes((-len(data)) % 8))
                pointer = len(data)
                data.extend(bytes(len(payload)))
            data[pointer:pointer+len(payload)] = payload
            return pointer

        for f in fields(self):
            if not f.metadata:
                continue
            offset, kind, value = f.metadata['offset'], f.metadata['kind'], getattr(self, f.name)
            if kind != 'curve':
                struct.pack_into('<'+kind, data, offset, value)
                continue
            old_count = struct.unpack_from('<H', data, offset)[0]
            pointer = struct.unpack_from('<Q', data, offset+32)[0]
            payload = b''.join(struct.pack('<fH5e', k.value, k.kind, k.time, k.in_y, k.in_x, k.out_y, k.out_x) for k in value.keys)
            pointer = write_payload(pointer, 16*old_count, payload)
            struct.pack_into('<HH4fiI', data, offset, len(value.keys), value.loop_count, value.loop_start,
                             value.loop_end, value.minimum, value.maximum, value.wrap, value.flags)
            struct.pack_into('<Q', data, offset+32, pointer)
        pointer = struct.unpack_from('<Q', data, 0xe0)[0]
        pointer = write_payload(pointer, 128 if pointer else 0, struct.pack('<32f', *self.static_friction_weights))
        struct.pack_into('<Q', data, 0xe0, pointer)
        return data.take_bytes()


def gpus_version(path: str) -> int:
    return resource_version_from_path(path, 'gpus') or GPUS_VERSION

