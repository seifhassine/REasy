"""See docs/internals/scene-transforms.md, Engine math."""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
import struct

lazy import numpy as np

from utils.native_build import ensure_fastmesh

_fastmesh = ensure_fastmesh()
# FLT_EPSILON.
EPSILON = 1.1920928955078125e-07
BOX_CORNERS = np.array([[(corner >> axis) & 1 for axis in range(3)] for corner in range(8)])
_TWO_ARGUMENTS = frozenset({"atan2f", "fmodf", "powf"})


@cache
def crt(name: str):
    """The C runtime's float function ``name``, which the engine imports."""
    function = getattr(ctypes.CDLL("ucrtbase"), name)
    function.restype, function.argtypes = ctypes.c_float, (ctypes.c_float,) * (2 if name in _TWO_ARGUMENTS else 1)
    return function


@contextmanager
def flushed_denormals():
    """This thread's float mode flushing denormals to zero, as some engine threads run it; then the mode it had."""
    control, held = ctypes.CDLL("ucrtbase")._controlfp_s, ctypes.c_uint()
    control(ctypes.byref(held), 0, 0)
    control(ctypes.byref(ctypes.c_uint()), 0x01000000, 0x03000000)
    try:
        yield
    finally:
        control(ctypes.byref(ctypes.c_uint()), held.value & 0x03000000, 0x03000000)


def half_bits(value) -> int:
    """The engines' software float to half: truncated, small values flushed to 0."""
    bits = struct.unpack("<I", struct.pack("<f", float(value)))[0]
    sign, exponent, mantissa = (bits >> 16) & 0x8000, (bits >> 23) & 0xFF, bits & 0x7FFFFF
    if exponent <= 0x70:
        if exponent < 0x66:
            return 0
        return (((mantissa | 0x800000) >> (0x71 - exponent)) >> 13) | sign
    if exponent == 0xFF:
        return ((mantissa >> 13) | sign | (mantissa < 0x2000) | 0x7C00) if mantissa else sign | 0x7C00
    if exponent >= 0x8F:
        return sign | 0x7C00
    return (mantissa >> 13) | ((exponent - 0x70) << 10) | sign


def normalized(vector) -> np.ndarray:
    f = np.float32
    x, y, z = (f(value) for value in vector[:3])
    length = np.sqrt(x * x + y * y + z * z)
    if length < f(EPSILON):
        return np.array([x, y, z], f)
    inverse = f(1) / length
    return np.array([x * inverse, y * inverse, z * inverse], f)


def affine_inverse(world: np.ndarray) -> np.ndarray:
    f = np.float32
    m = np.asarray(world, f).reshape(4, 4)
    c00 = m[1, 1] * m[2, 2] - m[1, 2] * m[2, 1]
    c01 = m[2, 0] * m[1, 2] - m[1, 0] * m[2, 2]
    c02 = m[1, 0] * m[2, 1] - m[2, 0] * m[1, 1]
    determinant = m[0, 0] * c00 + m[0, 1] * c01 + m[0, 2] * c02
    out = np.eye(4, dtype=f)
    if not f(EPSILON) < abs(determinant):
        return out
    inverse = f(1) / determinant
    out[:3, :3] = [
        [inverse * c00, (m[0, 2] * m[2, 1] - m[0, 1] * m[2, 2]) * inverse,
         (m[0, 1] * m[1, 2] - m[0, 2] * m[1, 1]) * inverse],
        [inverse * c01, (m[0, 0] * m[2, 2] - m[0, 2] * m[2, 0]) * inverse,
         (m[0, 2] * m[1, 0] - m[0, 0] * m[1, 2]) * inverse],
        [inverse * c02, (m[0, 1] * m[2, 0] - m[0, 0] * m[2, 1]) * inverse,
         (m[0, 0] * m[1, 1] - m[0, 1] * m[1, 0]) * inverse]]
    for column in range(3):
        out[3, column] = -(m[3, 1] * out[1, column] + m[3, 0] * out[0, column] + m[3, 2] * out[2, column])
    return out


def camera_view(world: np.ndarray) -> np.ndarray:
    f = np.float32
    m = np.asarray(world, f).reshape(4, 4)
    eye, (i, j, k), out = m[3, :3], m[1, :3], np.eye(4, dtype=f)

    def unit(x, y, z):
        length = np.sqrt((x * x + y * y) + z * z)
        scale = f(1) if f(EPSILON) > length else f(1) / length
        return x * scale, y * scale, z * scale

    def along(x, y, z):
        return (eye[0] * x + eye[1] * y) + eye[2] * z
    a, b, c = unit(*((eye - m[2, :3]) - eye))
    p, q, r = unit(c * j - b * k, a * k - c * i, b * i - a * j)
    above = b * r - q * c, p * c - a * r, a * q - p * b
    out[:3, 0], out[:3, 1], out[:3, 2] = (-p, -q, -r), above, (-a, -b, -c)
    out[3, :3] = along(p, q, r), -along(*above), along(a, b, c)
    return out


def row_lengths(matrix: np.ndarray) -> np.ndarray:
    return np.array([np.sqrt(((a * a + b * b) + c * c) + d * d) for a, b, c, d in matrix[:3]], np.float32)


def engine_scale(world: np.ndarray, local_scale, absolute: bool) -> np.ndarray:
    if absolute:
        return np.asarray(local_scale, np.float32)
    return row_lengths(np.asarray(world, np.float32).reshape(4, 4))


def make_trs_matrix(
    position: tuple[float, float, float],
    rotation: tuple[float, float, float, float],
    scale: tuple[float, float, float],
) -> np.ndarray:
    """Rotation (x, y, z, w) as stored, not normalized."""
    return np.frombuffer(_fastmesh.make_trs(position, rotation, scale), np.float32).reshape(4, 4)


def transform_product(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    """``left @ right`` as the engine multiplies matrices: float32, each element's four products summed in pairs."""
    a, b = np.asarray(left, np.float32), np.asarray(right, np.float32)
    return (a[..., :, 0, None] * b[..., None, 0, :] + a[..., :, 1, None] * b[..., None, 1, :]) + (
        a[..., :, 2, None] * b[..., None, 2, :] + a[..., :, 3, None] * b[..., None, 3, :])


def chain_world(matrices, world: np.ndarray) -> np.ndarray:
    matrix = matrices[0]
    for parent in matrices[1:]:
        matrix = transform_product(parent, matrix)
    return transform_product(world, matrix)


def chain_rotation(rotations, root) -> tuple[float, float, float, float]:
    for local in reversed(rotations):
        root = quaternion_product(local, root)
    return root


def quaternion_product(local, parent) -> tuple[float, float, float, float]:
    return _fastmesh.quaternion_product(local, parent)


def matrix_rotation(m) -> tuple[float, float, float, float]:
    f = np.float32
    half, one = f(0.5), f(1)
    trace = (m[1, 1] + m[0, 0]) + m[2, 2]
    if trace > 0:
        root = np.sqrt(trace + one)
        factor = half / root
        rotation = ((m[1, 2] - m[2, 1]) * factor, (m[2, 0] - m[0, 2]) * factor, (m[0, 1] - m[1, 0]) * factor,
                    root * half)
    else:
        i = 1 if m[1, 1] > m[0, 0] else 0
        i = 2 if m[2, 2] > m[i, i] else i
        j = (1, 2, 0)[i]
        k = (1, 2, 0)[j]
        root = np.sqrt(((m[i, i] - m[j, j]) - m[k, k]) + one)
        factor = half / root
        values = [f(0)] * 4
        values[i] = root * half
        values[3] = (m[j, k] - m[k, j]) * factor
        values[j] = (m[i, j] + m[j, i]) * factor
        values[k] = (m[i, k] + m[k, i]) * factor
        rotation = tuple(values)
    return tuple(float(value) for value in rotation)


def joint_descriptor(local) -> tuple[tuple[float, float, float], tuple[float, float, float, float],
                                      tuple[float, float, float]]:
    f = np.float32
    m = np.asarray(local, dtype=f).reshape(4, 4).copy()
    scale = [np.sqrt(((m[row, 0] * m[row, 0] + m[row, 1] * m[row, 1]) + m[row, 2] * m[row, 2]) + m[row, 3] * m[row, 3])
             for row in range(3)]
    for row in range(3):
        m[row] = m[row] * (f(1) / scale[row])
    return (tuple(float(value) for value in m[3, :3]), matrix_rotation(m), tuple(float(value) for value in scale))


def mathex_rotation(matrix) -> tuple[float, float, float, float]:
    f = np.float32
    m = np.array([normalized_row(row) for row in np.asarray(matrix, dtype=f).reshape(4, 4)[:3]])
    one, half, quarter, two = f(1), f(0.5), f(0.25), f(2)
    trace = (m[1, 1] + m[0, 0]) + m[2, 2]
    if trace > 0:
        k = half / np.sqrt(trace + one)
        rotation = ((m[1, 2] - m[2, 1]) * k, (m[2, 0] - m[0, 2]) * k, (m[0, 1] - m[1, 0]) * k, quarter / k)
    elif m[0, 0] > m[1, 1] and m[0, 0] > m[2, 2]:
        s = np.sqrt(((m[0, 0] + one) - m[1, 1]) - m[2, 2]) * two
        rotation = (s * quarter, (m[0, 1] + m[1, 0]) / s, (m[2, 0] + m[0, 2]) / s, (m[1, 2] - m[2, 1]) / s)
    elif m[1, 1] > m[2, 2]:
        s = np.sqrt(((m[1, 1] + one) - m[0, 0]) - m[2, 2]) * two
        rotation = ((m[0, 1] + m[1, 0]) / s, s * quarter, (m[1, 2] + m[2, 1]) / s, (m[2, 0] - m[0, 2]) / s)
    else:
        s = np.sqrt(((m[2, 2] + one) - m[0, 0]) - m[1, 1]) * two
        rotation = ((m[2, 0] + m[0, 2]) / s, (m[1, 2] + m[2, 1]) / s, s * quarter, (m[0, 1] - m[1, 0]) / s)
    return tuple(float(value) for value in rotation)


def normalized_row(row) -> np.ndarray:
    f = np.float32
    row = np.asarray(row, dtype=f)
    length = np.sqrt(((row[0] * row[0] + row[1] * row[1]) + row[2] * row[2]) + row[3] * row[3])
    return row if length < f(EPSILON) else row * (f(1) / length)


def axis_rotation(axis, half) -> tuple[float, float, float, float]:
    f = np.float32
    sine, cosine = f(crt("sinf")(float(half))), f(crt("cosf")(float(half)))
    return quaternion_product((0.0, 0.0, 0.0, 1.0), (*(float(f(value) * sine) for value in axis), float(cosine)))


def axis_turn(axis, half) -> tuple[float, float, float, float]:
    f = np.float32
    sine = f(crt("sinf")(float(half)))
    return (*(float(f(value) * sine) for value in axis[:3]), float(f(crt("cosf")(float(half)))))


def turned_vector(vector, rotation) -> np.ndarray:
    f = np.float32
    x, y, z = (f(value) for value in vector[:3])
    qx, qy, qz, qw = (f(value) for value in rotation)
    two = f(2)
    ty = (x * qz - z * qx) * two
    tx = (z * qy - y * qz) * two
    tz = (y * qx - x * qy) * two
    return np.array([(tz * qy - ty * qz) + (x + tx * qw), (tx * qz - tz * qx) + (y + ty * qw),
                     (ty * qx - tx * qy) + (z + tz * qw)], f)


def euler_rotation(angles) -> tuple[float, float, float, float]:
    f = np.float32
    sx, sy, sz = (f(crt("sinf")(value)) for value in angles)
    cx, cy, cz = (f(crt("cosf")(value)) for value in angles)
    return matrix_rotation(np.array([[(sx * sy) * sz + cy * cz, sz * cx, (sx * sz) * cy - sy * cz],
                                     [(sx * cz) * sy - sz * cy, cx * cz, (sx * cy) * cz + sy * sz],
                                     [sy * cx, -sx, cx * cy]], dtype=f))


def rotation_euler(rotation) -> tuple[float, float, float]:
    m = make_trs_matrix((0.0, 0.0, 0.0), rotation, (1.0, 1.0, 1.0))[:3, :3].T.astype(float)
    quarter = float(np.float32(np.pi / 2))
    if not m[2, 1] < 1:
        return -quarter, 0.0, -crt("atan2f")(m[0, 2], m[0, 0])
    if not m[2, 1] > -1:
        return quarter, 0.0, crt("atan2f")(m[0, 2], m[0, 0])
    return -crt("asinf")(m[2, 1]), -crt("atan2f")(-m[2, 0], m[2, 2]), -crt("atan2f")(-m[0, 1], m[1, 1])


def euler_quaternion(angles) -> tuple[float, float, float, float]:
    f = np.float32
    halves = [f(value) * f(0.5) for value in angles]
    cx, cy, cz = (f(crt("cosf")(float(value))) for value in halves)
    sx, sy, sz = (f(crt("sinf")(float(value))) for value in halves)
    return (float((cz * sx) * cy + (sz * cx) * sy), float((cz * cx) * sy - (sz * sx) * cy),
            float((sz * cx) * cy - (cz * sx) * sy), float((sz * sx) * sy + (cz * cx) * cy))


def axes_quaternion(angles) -> tuple[float, float, float, float]:
    f = np.float32

    def turn(angle, axis):
        half = float(f(angle) * f(0.5))
        sine = f(crt("sinf")(half))
        return (*(float(sine * f(value)) for value in axis), float(f(crt("cosf")(half))))
    x, y, z = angles
    return quaternion_product(quaternion_product(turn(z, (0.0, 0.0, 1.0)), turn(x, (1.0, 0.0, 0.0))),
                              turn(y, (0.0, 1.0, 0.0)))


def quaternion_euler(rotation) -> tuple[float, float, float]:
    f = np.float32
    x, y, z, w = (f(value) for value in rotation)
    s = (x * w - y * z) * f(2)
    rest = (f(0.5) - x * x) - y * y
    if not f(1) > s:
        return float(f(np.pi / 2)), 0.0, crt("atan2f")(float(z * x - y * w), float(rest))
    if not s > f(-1):
        return float(f(-np.pi / 2)), 0.0, crt("atan2f")(float(y * w - z * x), float(rest))
    turn = f(0.5) - x * x
    return (crt("asinf")(float(s)), crt("atan2f")(float(z * x + y * w), float(turn - y * y)),
            crt("atan2f")(float(y * x + z * w), float(turn - z * z)))


def transform_coord(point, rows) -> tuple[float, float, float]:
    f = np.float32
    x, y, z = (f(value) for value in point)
    m = np.asarray(rows, dtype=f)
    w = ((x * m[0, 3] + y * m[1, 3]) + z * m[2, 3]) + m[3, 3]
    scale = f(0) if w == 0 else f(1) / w
    return tuple(float((((x * m[0, column] + y * m[1, column]) + z * m[2, column]) + m[3, column]) * scale)
                 for column in range(3))


def half_float(bits: int) -> np.float32:
    return np.float32(np.array([bits & 0xFFFF], np.uint16).view(np.float16)[0])


def curve_key(vector) -> tuple:
    """A via.KeyFrame's four words: value, kind and time, in tangent and time, out tangent and time."""
    words = struct.unpack("<4I", struct.pack("<4f", *(float(value) for value in vector)))
    kind = words[1] & 0xFFFF
    return (np.float32(struct.unpack("<f", struct.pack("<I", words[0]))[0]), kind - 0x10000 if kind & 0x8000 else kind,
            half_float(words[1] >> 16), half_float(words[2]), half_float(words[2] >> 16), half_float(words[3]),
            half_float(words[3] >> 16))


def hermite(p0, p1, m0, m1, s) -> np.float32:
    f = np.float32
    p0, p1, m0, m1, s = (f(value) for value in (p0, p1, m0, m1, s))
    s2 = s * s
    s3 = s2 * s
    cubic = (((p0 * f(2) - p1 * f(2)) + m0) + m1) * s3
    square = (((p1 * f(3) - p0 * f(3)) - m0 * f(2)) - m1) * s2
    return ((cubic + square) + m0 * s) + p0


@dataclass(frozen=True, slots=True)
class CurveRule:
    constant: frozenset[int]
    linear: frozenset[int]
    handles: frozenset[int]
    steps: int
    tolerance: float

    @classmethod
    def of(cls, curve) -> CurveRule:
        kinds, solver = curve["kinds"], curve["solver"]
        return cls(*(frozenset(kinds[name]) for name in ("constant", "linear", "handles")), int(solver["steps"]),
                   float(solver["tolerance"]))


def bezier_rate(a, b, c, d, x, steps: int, tolerance: float) -> np.float32:
    f = np.float32
    a, b, c, d, x = (f(value) for value in (a, b, c, d, x))
    if not a < x:
        return f(0)
    if not x < b:
        return f(1)
    s = (x - a) / (b - a)
    whole = ((a * f(2) - b * f(2)) + c) + d
    square = (((b * f(3) - a * f(3)) - c * f(2)) - d) / whole
    linear = c / whole
    constant = (a - x) / whole
    twice = square * f(2)
    if (s * f(3)) * s + twice * s + linear != 0:
        for _ in range(steps):
            old = s
            s = ((((s * f(2)) * s) * s + (s * square) * s) - constant) / (((s * f(3)) * s + twice * s) + linear)
            if tolerance >= float(abs(s - old)):
                break
    if s < 0 or not f(1) >= s:
        shifted = s + square
        discriminant = shifted * shifted - (((s * square) + linear) + s * s) * f(4)
        high, low = (discriminant - shifted) * f(0.5), (-shifted - discriminant) * f(0.5)
        if high >= 0 and f(1) >= high:
            s = high
        elif low >= 0 and f(1) >= low:
            s = low
    return s


def curve_value(keys, time, clamp: bool, minimum, maximum, rule: CurveRule) -> np.float32:
    f = np.float32
    time = f(time)
    index = next((number - 1 for number, key in enumerate(keys) if key[2] > time), None)
    if index is None or index < 0:
        index = len(keys) - 1
        if index < 0:
            return f(0)
    key = keys[index]
    if key[1] in rule.constant:
        value = key[0]
    else:
        following = min(index + 1, len(keys) - 1)
        other = keys[following]
        s = f(1) if following == index else (time - key[2]) / (other[2] - key[2])
        if key[1] in rule.linear:
            value = (f(1) - s) * key[0] + s * other[0]
        else:
            if key[1] in rule.handles:
                s = bezier_rate(key[2], other[2], key[6], other[4], time, rule.steps, rule.tolerance)
            value = hermite(key[0], other[0], key[5], other[3], s)
    if clamp:
        minimum, maximum = f(minimum), f(maximum)
        value = minimum if minimum > value else value if value < maximum else maximum
    return value


__all__ = ["CurveRule", "affine_inverse", "axes_quaternion", "axis_rotation", "axis_turn", "bezier_rate",
           "chain_rotation", "chain_world", "crt", "curve_key", "curve_value", "engine_scale", "EPSILON",
           "euler_quaternion", "euler_rotation", "half_bits", "half_float", "hermite", "joint_descriptor",
           "make_trs_matrix", "mathex_rotation", "matrix_rotation", "normalized", "normalized_row", "quaternion_euler",
           "quaternion_product", "rotation_euler", "row_lengths", "transform_coord", "transform_product",
           "turned_vector"]


def engine_sorted(pairs: list[tuple]) -> list[tuple]:
    """Largest weight first, as the engine's weight-list update leaves them: a Shell sort (gaps 3h + 1) to 32 pairs,
    which swaps equal weights, MSVC's ``std::sort`` past that."""
    a = list(pairs)
    if len(a) <= 32:
        _shell_sort(a)
    else:
        _msvc_sort(a)
    return a


def _shell_sort(a: list[tuple]) -> None:
    n = len(a)
    gap = 1
    while gap < n:
        gap = gap * 3 + 1
    while gap:
        for i in range(gap, n):
            j = i
            while j >= gap and not a[j - gap][0] > a[j][0]:
                a[j - gap], a[j] = a[j], a[j - gap]
                j -= gap
        gap //= 3


def _msvc_sort(a: list[tuple]) -> None:
    n = len(a)
    _msvc_introsort(a, 0, n, n)


def _msvc_swap(a: list[tuple], i, j):
    a[i], a[j] = a[j], a[i]


def _msvc_med3(a: list[tuple], first, mid, last):
    if a[mid][0] > a[first][0]:
        _msvc_swap(a, mid, first)
    if a[last][0] > a[mid][0]:
        _msvc_swap(a, last, mid)
        if a[mid][0] > a[first][0]:
            _msvc_swap(a, mid, first)


def _msvc_partition(a: list[tuple], first, last):
    mid = first + ((last - first) >> 1)
    count = last - 1 - first
    if count > 40:
        step = (count + 1) >> 3
        two = step << 1
        _msvc_med3(a, first, first + step, first + two)
        _msvc_med3(a, mid - step, mid, mid + step)
        _msvc_med3(a, last - 1 - two, last - 1 - step, last - 1)
        _msvc_med3(a, first + step, mid, last - 1 - step)
    else:
        _msvc_med3(a, first, mid, last - 1)
    pfirst, plast = mid, mid + 1
    while first < pfirst and not a[pfirst - 1][0] > a[pfirst][0] and not a[pfirst][0] > a[pfirst - 1][0]:
        pfirst -= 1
    while plast < last and not a[plast][0] > a[pfirst][0] and not a[pfirst][0] > a[plast][0]:
        plast += 1
    gfirst, glast = plast, pfirst
    while True:
        while gfirst < last:
            if a[pfirst][0] > a[gfirst][0]:
                gfirst += 1
                continue
            if a[gfirst][0] > a[pfirst][0]:
                break
            if plast != gfirst:
                _msvc_swap(a, plast, gfirst)
            plast += 1
            gfirst += 1
        while first < glast:
            if a[glast - 1][0] > a[pfirst][0]:
                glast -= 1
                continue
            if a[pfirst][0] > a[glast - 1][0]:
                break
            pfirst -= 1
            if pfirst != glast - 1:
                _msvc_swap(a, pfirst, glast - 1)
            glast -= 1
        if glast == first and gfirst == last:
            return pfirst, plast
        if glast == first:
            if plast != gfirst:
                _msvc_swap(a, pfirst, plast)
            plast += 1
            _msvc_swap(a, pfirst, gfirst)
            pfirst += 1
            gfirst += 1
        elif gfirst == last:
            glast -= 1
            pfirst -= 1
            if glast != pfirst:
                _msvc_swap(a, glast, pfirst)
            plast -= 1
            _msvc_swap(a, pfirst, plast)
        else:
            glast -= 1
            _msvc_swap(a, gfirst, glast)
            gfirst += 1


def _msvc_insertion(a: list[tuple], first, last):
    for mid in range(first + 1, last):
        value = a[mid]
        if value[0] > a[first][0]:
            a[first + 1:mid + 1] = a[first:mid]
            a[first] = value
            continue
        hole = mid
        while value[0] > a[hole - 1][0]:
            a[hole] = a[hole - 1]
            hole -= 1
        a[hole] = value


def _msvc_pop_hole(a: list[tuple], first, hole, bottom, value):
    top = index = hole
    last_parent = (bottom - 1) >> 1
    while index < last_parent:
        index = 2 * index + 2
        if a[first + index][0] > a[first + index - 1][0]:
            index -= 1
        a[first + hole] = a[first + index]
        hole = index
    if index == last_parent and bottom % 2 == 0:
        a[first + hole] = a[first + bottom - 1]
        hole = bottom - 1
    parent = (hole - 1) >> 1
    while top < hole and a[first + parent][0] > value[0]:
        a[first + hole] = a[first + parent]
        hole = parent
        parent = (hole - 1) >> 1
    a[first + hole] = value


def _msvc_introsort(a: list[tuple], first, last, ideal):
    while True:
        if last - first <= 32:
            _msvc_insertion(a, first, last)
            return
        if ideal <= 0:
            bottom = last - first
            for hole in range((bottom >> 1) - 1, -1, -1):
                _msvc_pop_hole(a, first, hole, bottom, a[first + hole])
            while last - first >= 2:
                last -= 1
                value = a[last]
                a[last] = a[first]
                _msvc_pop_hole(a, first, 0, last - first, value)
            return
        pfirst, plast = _msvc_partition(a, first, last)
        ideal = (ideal >> 1) + (ideal >> 2)
        if pfirst - first < last - plast:
            _msvc_introsort(a, first, pfirst, ideal)
            first = plast
        else:
            _msvc_introsort(a, plast, last, ideal)
            last = pfirst


def _crt_value(name: str, *values) -> np.float32:
    return np.float32(crt(name)(*(float(value) for value in values)))


def _scaled(value, at, count: int):
    for _ in range(count):
        value = value * at
    return value


def _raised(at, count: int):
    return _scaled(at, at, count - 1)


def _in_out(start, end, at, inner, outer):
    half, twice = (end - start) * np.float32(0.5), at * np.float32(2.0)
    return inner(half, twice) if np.float32(1.0) > twice else outer(half, twice)


def _out_bounce(start, end, at):
    span = end - start
    if np.float32(0.3636363744735718) > at:
        return at * np.float32(7.5625) * at * span + start
    for limit, shift, lift in ((0.7272727489471436, -0.5454545617103577, 0.75),
                               (0.9090909361839294, -0.8181818127632141, 0.9375)):
        if np.float32(limit) > at:
            break
    else:
        shift, lift = -0.9545454382896423, 0.984375
    moved = at + np.float32(shift)
    return (moved * np.float32(7.5625) * moved + np.float32(lift)) * span + start


def _elastic(start, end, at, rising: bool, falling: bool):
    if not at:
        return start
    if at == 1:
        return end
    span, twice = end - start, at * np.float32(2.0)
    if rising and falling:
        rising = np.float32(1.0) > twice
        at, gap, period, part = (twice - np.float32(1.0), np.float32(0.11250000447034836), np.float32(
            0.45000001788139343), np.float32(0.5))
    else:
        at = at - np.float32(1.0) if rising else at
        gap, period, part = np.float32(0.075), np.float32(0.3), np.float32(1.0)
    power = _crt_value("powf", 2.0, at * np.float32(10.0 if rising else -10.0)) * span
    wave = _crt_value("sinf", (at - gap) * np.float32(6.2831854820251465) / period)
    if part != 1:
        power = power * part
    return start - power * wave if rising else power * wave + span + start


_BACK, _BACK_SHIFT = np.float32(2.701580047607422), np.float32(1.7015800476074219)
EASES = {
    "Linear": lambda a, b, t: (b - a) * t + a,
    "InSine": lambda a, b, t: (b - a) - _crt_value("cosf", t * np.float32(1.5707963705062866)) * (b - a) + a,
    "OutSine": lambda a, b, t: _crt_value("sinf", t * np.float32(1.5707963705062866)) * (b - a) + a,
    "InOutSine": lambda a, b, t: a - (_crt_value("cosf", t * np.float32(3.1415927410125732)) - np.float32(1.0)) * (
        (b - a) * np.float32(0.5)),
    "InQuad": lambda a, b, t: _scaled(b - a, t, 2) + a,
    "OutQuad": lambda a, b, t: a - (b - a) * t * (t - np.float32(2.0)),
    "InOutQuad": lambda a, b, t: _in_out(a, b, t, lambda half, x: _scaled(half, x, 2) + a, lambda half, x: a - (
        ((x - np.float32(1.0)) - np.float32(2.0)) * (x - np.float32(1.0)) - np.float32(1.0)) * half),
    "InCubic": lambda a, b, t: _scaled(b - a, t, 3) + a,
    "OutCubic": lambda a, b, t: (_raised(t - np.float32(1.0), 3) + np.float32(1.0)) * (b - a) + a,
    "InOutCubic": lambda a, b, t: _in_out(a, b, t, lambda half, x: _scaled(half, x, 3) + a, lambda half, x: (
        _raised(x - np.float32(2.0), 3) + np.float32(2.0)) * half + a),
    "InQuart": lambda a, b, t: _scaled(b - a, t, 4) + a,
    "OutQuart": lambda a, b, t: a - (_raised(t - np.float32(1.0), 4) - np.float32(1.0)) * (b - a),
    "InOutQuart": lambda a, b, t: _in_out(a, b, t, lambda half, x: _scaled(half, x, 4) + a, lambda half, x: a - (
        _raised(x - np.float32(2.0), 4) - np.float32(2.0)) * half),
    "InQuint": lambda a, b, t: _scaled(b - a, t, 5) + a,
    "OutQuint": lambda a, b, t: (_raised(t - np.float32(1.0), 5) + np.float32(1.0)) * (b - a) + a,
    "InOutQuint": lambda a, b, t: _in_out(a, b, t, lambda half, x: _scaled(half, x, 5) + a, lambda half, x: (
        _raised(x - np.float32(2.0), 5) + np.float32(2.0)) * half + a),
    "InExpo": lambda a, b, t: _crt_value("powf", 2.0, (t - np.float32(1.0)) * np.float32(10.0)) * (b - a) + a,
    "OutExpo": lambda a, b, t: (np.float32(1.0) - _crt_value("powf", 2.0, t * np.float32(-10.0))) * (b - a) + a,
    "InOutExpo": lambda a, b, t: _in_out(a, b, t, lambda half, x: _crt_value(
        "powf", 2.0, (x - np.float32(1.0)) * np.float32(10.0)) * half + a, lambda half, x: (np.float32(2.0) - _crt_value(
            "powf", 2.0, (x - np.float32(1.0)) * np.float32(-10.0))) * half + a),
    "InCirc": lambda a, b, t: a - (_crt_value("sqrtf", np.float32(1.0) - t * t) - np.float32(1.0)) * (b - a),
    "OutCirc": lambda a, b, t: _crt_value("sqrtf", np.float32(1.0) - (t - np.float32(1.0)) * (t - np.float32(1.0))) * (
        b - a) + a,
    "InOutCirc": lambda a, b, t: _in_out(a, b, t, lambda half, x: a - (_crt_value("sqrtf", np.float32(1.0) - x * x) - (
        np.float32(1.0))) * half, lambda half, x: (_crt_value("sqrtf", np.float32(1.0) - (x - np.float32(2.0)) * (
            x - np.float32(2.0))) + np.float32(1.0)) * half + a),
    "InBack": lambda a, b, t: (b - a) * t * t * (t * _BACK - _BACK_SHIFT) + a,
    "OutBack": lambda a, b, t: ((t - np.float32(1.0)) * (t - np.float32(1.0)) * ((t - np.float32(1.0)) * _BACK + (
        _BACK_SHIFT)) + np.float32(1.0)) * (b - a) + a,
    "InOutBack": lambda a, b, t: _in_out(a, b, t, lambda half, x: x * x * (x * _BACK - _BACK_SHIFT) * half + a * (
        np.float32(1.524999976158142)), lambda half, x: ((x - np.float32(2.0)) * (x - np.float32(2.0)) * ((
            x - np.float32(2.0)) * _BACK + _BACK_SHIFT) + np.float32(2.0)) * half + a * np.float32(1.524999976158142)),
    "InElastic": lambda a, b, t: _elastic(a, b, t, True, False),
    "OutElastic": lambda a, b, t: _elastic(a, b, t, False, True),
    "InOutElastic": lambda a, b, t: _elastic(a, b, t, True, True),
    "InBounce": lambda a, b, t: (b - a) - _out_bounce(np.float32(0.0), b - a, np.float32(1.0) - t) + a,
    "OutBounce": _out_bounce,
    "InOutBounce": lambda a, b, t: ((b - a) - _out_bounce(np.float32(0.0), b - a, np.float32(1.0) - t * np.float32(
        2.0)) + np.float32(0.0)) * np.float32(0.5) + a if np.float32(0.5) > t else _out_bounce(
        np.float32(0.0), b - a, t * np.float32(2.0) - np.float32(1.0)) * np.float32(0.5) + (b - a) * np.float32(
        0.5) + a,
}


def ease(kind: str, start: float, end: float, at: float) -> float:
    return float(EASES[kind](np.float32(start), np.float32(end), np.float32(at)))
