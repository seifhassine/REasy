"""See docs/internals/strands-chain.md, LOD."""
from __future__ import annotations

import itertools
import struct
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from .gpus import AnimationCurve, CurveKey
lazy from .strands_file import StrandsFile

F = np.float32
_MAGIC, _HEAD, _CURVE, _KEYS = b"SLOD", 8, 0x20, 0x48
# The render size ``LODFactorAutoResolutionEnable`` measures against.
_FULL_SIZE, _FULL_PIXELS = (3840, 2160), F(8294400)
_GROWTH, _SHORT = F(0.1), F(2.0 ** -23)
_SPAN_RATIO = F(10)
_LEAST_FACTOR = F(0.009999999776482582)
# The engines start the bone union at the largest float, not infinity.
_LARGEST = np.finfo(F).max


@dataclass(frozen=True, slots=True)
class LodRecord:
    """``screen_max``/``screen_min`` are the ``ScreenRate`` range; ``offset`` and ``factor`` are ``LODFactorAuto``'s."""
    screen_max: F
    screen_min: F
    ratios: tuple[F, F, F]
    offset: F
    factor: F
    curve: AnimationCurve


NO_RECORD = LodRecord(F(0.019999999552965164), F(0.0010000000474974513), (F(1), F(0.4000000059604645), F(0.125)),
                      F(0.05000000074505806), F(1), AnimationCurve(minimum=0, maximum=0, flags=0))


def _spans(data: bytes):
    magic, extra = struct.unpack_from("<4sI", data)
    if magic != _MAGIC:
        raise ValueError("not a strands LOD settings file")
    at = 8
    for _ in range(extra + 1):
        key, count = struct.unpack_from("<II", data, at)
        yield key, at, at + _HEAD + _KEYS + 16 * count
        at += _HEAD + _KEYS + 16 * count
    if at != len(data):
        raise ValueError("the strands LOD settings file holds bytes past its records")


def lod_records(data: bytes) -> dict[int, LodRecord]:
    """Key 0 is the default record."""
    records = {}
    for key, at, _end in _spans(data):
        count, body = struct.unpack_from("<I", data, at + 4)[0], at + _HEAD
        screen_max, screen_min, *ratios, offset, factor = map(F, struct.unpack_from("<7f", data, body))
        length, loops, start, end, low, high, wrap, flags, keys_at = struct.unpack_from(
            "<HH4fII4xQ", data, body + _CURVE)
        if length != count or keys_at != _KEYS:
            raise ValueError(f"the strands LOD record {key:#x} holds its curve's keys elsewhere")
        keys = tuple(CurveKey(*struct.unpack_from("<fH5e", data, body + _KEYS + 16 * index)) for index in range(count))
        records[key] = LodRecord(screen_max, screen_min, tuple(ratios), offset, factor,
                                 AnimationCurve(keys, loops, start, end, low, high, wrap, flags))
    return records


def with_lod_record(data: bytes, key: int, record: LodRecord) -> bytes:
    record.curve.validate()
    spans = {held: (start, end) for held, start, end in _spans(data)}
    start, end = spans.get(key, spans[0])
    body = bytearray(data[start + _HEAD:start + _HEAD + _KEYS])
    curve = record.curve
    struct.pack_into("<7f", body, 0, record.screen_max, record.screen_min, *record.ratios, record.offset,
                     record.factor)
    struct.pack_into("<HH4fII", body, _CURVE, len(curve.keys), curve.loop_count, curve.loop_start, curve.loop_end,
                     curve.minimum, curve.maximum, curve.wrap, curve.flags)
    made = (struct.pack("<II", key, len(curve.keys)) + body.take_bytes() + b"".join(
        struct.pack("<fH5e", k.value, k.kind, k.time, k.in_y, k.in_x, k.out_y, k.out_x) for k in curve.keys))
    if key in spans:
        return data[:start] + made + data[end:]
    return data[:4] + struct.pack("<I", len(spans)) + data[8:] + made


def lod_factor_auto(record: LodRecord | None, screen_rate: F, factor: F, size: tuple[float, float] | None = None) -> F:
    resolution = F(1)
    if size is not None:
        width, height = (int(side) & 0xffffffff for side in size)
        if (width, height) == _FULL_SIZE:
            return F(factor)
        resolution = F((width * height) & 0xffffffff) / _FULL_PIXELS
    held = NO_RECORD if record is None else record
    time = (F(screen_rate) - held.screen_min) / (held.screen_max - held.screen_min)
    time = F(0) if time < 0 else time if time < 1 else F(1)
    value = held.offset + (F(factor) * F(resolution)) * (time if record is None else record.curve.at(time))
    value = F(0) if value < 0 else value if value < factor else F(factor)
    return _LEAST_FACTOR if _LEAST_FACTOR > value else value


def _rectangle(box: tuple[Sequence[float], Sequence[float]], m: np.ndarray,
               least: list[F]) -> tuple[list[F], list[F]]:
    """As ``ScreenRate`` and vertex LOD project them."""
    most = [F(0), F(0)]
    for pick in itertools.product((0, 1), repeat=3):
        x, y, z = (F(box[side][axis]) for axis, side in enumerate(pick))
        w = ((x * m[0][3] + y * m[1][3]) + z * m[2][3]) + m[3][3]
        inverse = F(1) / w if w != 0 else F(0)
        for axis in (0, 1):
            value = F(0.5) * ((((x * m[0][axis] + y * m[1][axis]) + z * m[2][axis]) + m[3][axis]) * inverse)
            value = F(0.5) + value if axis == 0 else F(0.5) - value
            value = F(0) if value < 0 else value if value < 1 else F(1)
            least[axis] = value if value < least[axis] else least[axis]
            most[axis] = most[axis] if value < most[axis] else value
    return least, most


def screen_rate(box: tuple[Sequence[float], Sequence[float]], view_projection: np.ndarray) -> F:
    least, most = _rectangle(box, np.asarray(view_projection, F), [F(1), F(1)])
    return (most[0] - least[0]) * (most[1] - least[1])


def vertex_level(box: tuple[Sequence[float], Sequence[float]], m: np.ndarray, size: tuple[float, float],
                 ratios: Sequence[F]) -> int:
    """As the engines' ``AUTO`` vertex LOD picks it."""
    width, height = (F(side) for side in size)
    least, most = _rectangle(box, np.asarray(m, F), [width, height])
    with np.errstate(invalid="ignore"):
        rate = abs((width * most[0] - width * least[0]) * (height * most[1] - height * least[1])) / (width * height)
    return next((level for level, ratio in enumerate(ratios) if rate > F(ratio)), 3)


def frustum_planes(view_projection: np.ndarray) -> list[np.ndarray]:
    m = np.asarray(view_projection, F)
    c = [m[:, column] for column in range(4)]
    planes = []
    for plane in (c[0] + c[3], c[3] - c[0], c[3] - c[1], c[1] + c[3], c[2] + np.zeros(4, F), c[3] - c[2]):
        inverse = F(1) / F(np.sqrt((plane[0] * plane[0] + plane[1] * plane[1]) + plane[2] * plane[2]))
        planes.append(plane * inverse)
    return planes


def in_frustum(box: tuple[Sequence[float], Sequence[float]], planes: Sequence[np.ndarray]) -> bool:
    low, high = (np.asarray(side, F)[:3] for side in box)
    centre = (high + low) * F(0.5)
    half = high - centre
    for a, b, c, d in planes:
        reach = abs(c) * half[2] + (abs(a) * half[0] + abs(b) * half[1])
        if not reach + (((a * centre[0] + b * centre[1]) + c * centre[2]) + d) >= 0:
            return False
    return True


def seen(box: tuple[Sequence[float], Sequence[float]], planes: Sequence[np.ndarray]) -> bool:
    """Whether RE9's whole-run cull sees a box."""
    low, high = (np.asarray(side, F)[:3] for side in box)
    centre = (high + low) * F(0.5)
    half = high - centre
    for a, b, c, d in planes:
        if np.signbit(((((d + a * centre[0]) + b * centre[1]) + c * centre[2]) + abs(a) * half[0]) + abs(b) * half[1]
                      + abs(c) * half[2]):
            return False
    return True


def bone_union(boxes: Sequence[tuple[Sequence[float], Sequence[float]]], remap: Sequence[int],
               joints: Sequence[np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    if not len(boxes):
        return np.full(3, _LARGEST, F), np.full(3, -_LARGEST, F)
    rows = np.asarray([joints[joint] for _box, joint in zip(boxes, remap)], F).reshape(-1, 4, 4)[:, :, :3]
    least, most = (np.asarray([box[side][:3] for box in boxes[:len(rows)]], F) for side in (0, 1))
    ax, ay, az = rows[:, 0] * most[:, :1], rows[:, 1] * most[:, 1:2], rows[:, 3] + rows[:, 2] * most[:, 2:]
    nx, ny, nz = rows[:, 0] * least[:, :1], rows[:, 1] * least[:, 1:2], rows[:, 3] + rows[:, 2] * least[:, 2:]
    # Each bone's eight corners in fold order.
    corners = np.stack([xy + depth for depth in (nz, az) for xy in (nx + ny, ax + ny, nx + ay, ax + ay)],
                       axis=1).reshape(-1, 3)
    return _folded(corners, _LARGEST, np.less), _folded(corners, -_LARGEST, np.greater)


def _folded(corners: np.ndarray, start: F, beyond) -> np.ndarray:
    out = np.full(3, start, F)
    for axis in range(3):
        column = corners[:, axis]
        kept = column[beyond(column, start)]
        if len(kept):
            furthest = kept.max() if beyond is np.greater else kept.min()
            out[axis] = kept[np.flatnonzero(kept == furthest)[0]]
    return out


def extent_scaled(box: tuple[Sequence[float], Sequence[float]],
                  scale: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """A mesh box as its status job leaves it, scaled by ``AutoBoundingBoxExtentScaling``."""
    low, high, s = (np.asarray(side, F)[:3] for side in (*box, scale))
    centre, half = (low + high) * F(0.5), (high - low) * F(0.5)
    extent = np.array([s[0] * half[0], s[1] * half[1], half[2] * s[2]], F)
    return centre - extent, centre + extent


def grown_box(box: tuple[Sequence[float], Sequence[float]],
              rest: tuple[Sequence[float], Sequence[float]]) -> tuple[np.ndarray, np.ndarray]:
    """PRAGMATA's failsafe box."""
    low, high = (np.asarray(side, F)[:3] for side in box)
    least, most = (np.asarray(side, F)[:3] for side in rest)
    half, reach = high + F(-0.5) * (low + high), most + F(-0.5) * (most + least)
    span, length = _length(reach), _length(half)
    step = span * (half * (F(1) if length < _SHORT else F(1) / length))
    return low - step, high + step


def drawn_segments(factor: F, segments: int) -> int:
    """As the engines truncate it."""
    product = float(F(factor)) * float(segments)
    return int(product) & 0xffffffff if abs(product) < 2.0 ** 63 else 0


def first_lod(strands: StrandsFile, limit: int) -> int:
    """The first LOD the strands file loader loads under the build's limit."""
    counts = {level.index: level.segment_count for level in strands.lods}
    whole = len(strands.extra_sizes) == 5 and all(strands.extra_sizes)
    found = next((index for index in range(3) if counts.get(index) and not (limit > index and not whole)), None)
    if found is None and counts.get(3) and (whole or limit < 4):
        found = 3
    if found is None:
        raise ValueError("the strands file loader loads no LOD of the file")
    return found


def _decoded(word: int) -> F:
    """Bounds ring words hold floats in an order-preserving encoding."""
    bits = word ^ 0x80000000 if word >> 31 else ~word & 0xffffffff
    return np.frombuffer(struct.pack("<I", bits), F)[0]


def _length(vector: Sequence[F]) -> F:
    return F(np.sqrt(vector[2] * vector[2] + (vector[0] * vector[0] + vector[1] * vector[1])))


def readback_box(words: Sequence[int]) -> tuple[list[F], list[F]] | None:
    """Grown as the physics filler does; on None the engines use the rest bounds."""
    low, high = [_decoded(word) for word in words[:3]], [_decoded(word) for word in words[3:]]
    centre = [(a + b) * F(0.5) for a, b in zip(low, high)]
    to_low, to_high = [a - c for a, c in zip(low, centre)], [b - c for b, c in zip(high, centre)]
    length = _length(to_low)
    inverse_low, inverse_high = (F(1) if side < _SHORT else F(1) / side for side in (length, _length(to_high)))
    grown_low = [((inverse_low * d) * length) * _GROWTH + a for d, a in zip(to_low, low)]
    grown_high = [_GROWTH * (length * (inverse_high * e)) + b for e, b in zip(to_high, high)]
    if length == 0 or not np.all(np.isfinite(grown_low + grown_high)):
        return None
    return grown_low, grown_high


def spring_box(words: Sequence[int] | None, rest: tuple[Sequence[float], Sequence[float]],
               held: tuple[Sequence[float], Sequence[float]]) -> tuple[list[F], list[F]]:
    if words is None:
        return [F(value) for value in held[0]], [F(value) for value in held[1]]
    box = readback_box(words)
    least, most = ([F(value) for value in side] for side in rest)
    if box is None:
        return least, most
    span = _length([b - a for a, b in zip(least, most)])
    reach = _length([b - a for a, b in zip(*box)])
    if span * _SPAN_RATIO < reach or reach * _SPAN_RATIO < span:
        return least, most
    return box


def world_box(box: tuple[Sequence[float], Sequence[float]], origin: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    rows = np.asarray(origin, F)[:, :3]
    (x0, y0, z0), (x1, y1, z1) = (np.asarray(side, F) for side in box)
    corners = [(rows[0] * x + rows[1] * y) + rows[2] * z for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    return np.min(corners, axis=0) + rows[3], np.max(corners, axis=0) + rows[3]


__all__ = ["NO_RECORD", "LodRecord", "bone_union", "drawn_segments", "extent_scaled",
           "first_lod", "frustum_planes", "grown_box", "in_frustum", "lod_factor_auto", "lod_records", "readback_box",
           "screen_rate", "seen", "spring_box", "vertex_level", "world_box"]
