"""See docs/internals/clip-format.md."""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence

from file_handlers.native_math import f32, fadd, fdiv, fsub

from .model import Property
from .rules import InterpolationType, PropertyType, property_type

INTEGER_TYPES = frozenset(kind for kind in PropertyType if (info := property_type(kind)) is not None
                          and info.value is not None and info.value.numeric and info.value.kind == "int")
NUMERIC_TYPES = frozenset(kind for kind in PropertyType if (info := property_type(kind)) is not None
                          and info.value is not None and info.value.numeric)


def f32_negate(value: float) -> float:
    bits = struct.unpack("<I", struct.pack("<f", f32(value)))[0]
    return struct.unpack("<f", struct.pack("<I", bits ^ 0x80000000))[0]


def ordered_equal(left: float, right: float) -> bool:
    left, right = f32(left), f32(right)
    return not (math.isnan(left) or math.isnan(right)) and left == right


def ordered_greater(left: float, right: float) -> bool:
    return not (math.isnan(left) or math.isnan(right)) and left > right


def ordered_greater_equal(left: float, right: float) -> bool:
    return not (math.isnan(left) or math.isnan(right)) and left >= right


def sample(prop: Property, frame: float) -> object | None:
    keys = prop.keys
    if not keys:
        return None
    frame = f32(frame)
    left, right = surrounding_keys(keys, frame)
    if left is None:
        return right.value
    if right is None or left is right or ordered_equal(frame, left.frame):
        return left.value
    mode, kind = left.interpolation, prop.type
    if mode in (InterpolationType.EVENT, InterpolationType.PASS_EVENT):
        return None
    if kind not in NUMERIC_TYPES or not isinstance(right.value, (int, float)):
        return left.value
    if mode == InterpolationType.OFFSET_FRAME:
        value = fadd(interpolation_input(kind, left.value), fsub(frame, left.frame))
    elif mode in (InterpolationType.HERMITE, InterpolationType.BEZIER) and left.control is not None:
        out_x, out_y, in_x, in_y = left.control
        if mode == InterpolationType.HERMITE:
            parameter = hermite_parameter(left.frame, right.frame, frame, out_x, in_x)
            value = hermite_value(kind, left.value, right.value, parameter, out_y, in_y)
        else:
            span = fsub(right.frame, left.frame)
            value = bezier_value(left.value, right.value, fdiv(fsub(frame, left.frame), span) if span else 0.0,
                                 out_y, in_y)
    elif mode == InterpolationType.LINEAR:
        span = fsub(right.frame, left.frame)
        value = linear_value(kind, left.value, right.value, fdiv(fsub(frame, left.frame), span) if span else 0.0)
    else:
        return left.value
    return convert_number(kind, value)


def surrounding_keys(
    keys: Sequence,
    frame: float,
) -> tuple[object | None, object | None]:
    """Reproduce DMC5's midpoint search, including duplicate-frame policy."""

    first, last = f32(keys[0].frame), f32(keys[-1].frame)
    if ordered_greater(first, frame):
        return None, keys[0]
    if ordered_greater_equal(frame, last):
        return keys[-1], None
    lower, upper = 0, len(keys) - 1
    while lower < upper - 1:
        middle = lower + (upper - lower) // 2
        middle_frame = f32(keys[middle].frame)
        if ordered_equal(middle_frame, frame):
            return keys[middle], keys[middle]
        if math.isnan(middle_frame) or math.isnan(frame) or middle_frame <= frame:
            lower = middle
        else:
            upper = middle
    return keys[lower], keys[upper]


def hermite_parameter(
    start_frame: float,
    end_frame: float,
    frame: float,
    outgoing_tangent: float,
    incoming_tangent: float,
) -> float:
    """DMC5's binary32 Newton solve for compact-CLIP mode 5."""

    x0, x1, target = f32(start_frame), f32(end_frame), f32(frame)
    tangent0 = f32(outgoing_tangent)
    tangent1 = f32_negate(incoming_tangent)
    if math.isnan(target) or math.isnan(x0) or math.isnan(x1):
        return math.nan
    if target <= x0:
        return 0.0
    if target >= x1:
        return 1.0
    parameter = fdiv(fsub(target, x0), fsub(x1, x0))
    coefficient_a = fadd(fadd(fsub(f32(x0 * 2.0), f32(x1 * 2.0)), tangent0), tangent1)
    if not coefficient_a or math.isnan(coefficient_a):
        return parameter
    coefficient_b = fsub(fsub(fsub(f32(x1 * 3.0), f32(x0 * 3.0)), f32(tangent0 * 2.0)), tangent1)
    b_over_a = fdiv(coefficient_b, coefficient_a)
    c_over_a = fdiv(tangent0, coefficient_a)
    constant = fdiv(fsub(x0, target), coefficient_a)
    denominator = fadd(f32(f32(parameter * 3.0) * parameter), f32(f32(b_over_a * 2.0) * parameter))
    denominator = fadd(denominator, c_over_a)
    if denominator and not math.isnan(denominator):
        tolerance = f32(1.0e-5)
        for _ in range(100):
            previous = parameter
            numerator = f32(f32(parameter * 2.0) * parameter)
            numerator = f32(numerator * parameter)
            numerator = fadd(numerator, f32(f32(parameter * b_over_a) * parameter))
            numerator = fsub(numerator, constant)
            denominator = fadd(
                f32(f32(parameter * 3.0) * parameter),
                f32(f32(b_over_a * 2.0) * parameter),
            )
            denominator = fadd(denominator, c_over_a)
            parameter = fdiv(numerator, denominator)
            if tolerance >= f32(abs(fsub(parameter, previous))):
                break
    if not 0.0 <= parameter <= 1.0:
        total = fadd(parameter, b_over_a)
        expression = fadd(fadd(f32(parameter * b_over_a), c_over_a), f32(parameter * parameter))
        discriminant = fsub(f32(total * total), f32(expression * 4.0))
        candidate_a = f32(fsub(discriminant, total) * 0.5)
        candidate_b = f32(fsub(f32_negate(total), discriminant) * 0.5)
        if 0.0 <= candidate_a <= 1.0:
            parameter = candidate_a
        elif 0.0 <= candidate_b <= 1.0:
            parameter = candidate_b
    return parameter


def hermite_value(kind: PropertyType, value0: float, value1: float, parameter: float, out_y: float,
                  in_y: float) -> float:
    squared = f32(parameter * parameter)
    cubed = f32(squared * parameter)
    tangent0, tangent1 = f32(out_y), f32_negate(in_y)
    tangent_term = f32(tangent0 * parameter)
    if kind == PropertyType.F64:
        value0, value1 = float(value0), float(value1)
        coefficient_a = value0 * 2.0 - value1 * 2.0
        coefficient_a += float(tangent0) + float(tangent1)
        coefficient_b = value1 * 3.0 - value0 * 3.0
        coefficient_b -= float(f32(tangent0 * 2.0)) + float(tangent1)
        return coefficient_a * float(cubed) + coefficient_b * float(squared) + float(tangent_term) + value0

    value0 = interpolation_input(kind, value0)
    value1 = interpolation_input(kind, value1)
    coefficient_a = fadd(fsub(f32(value0 * 2.0), f32(value1 * 2.0)), tangent0)
    coefficient_a = fadd(coefficient_a, tangent1)
    coefficient_b = fsub(f32(value1 * 3.0), f32(value0 * 3.0))
    coefficient_b = fsub(coefficient_b, f32(tangent0 * 2.0))
    coefficient_b = fsub(coefficient_b, tangent1)
    value = fadd(f32(coefficient_a * cubed), f32(coefficient_b * squared))
    return fadd(fadd(value, tangent_term), value0)


def bezier_value(value0: float, value1: float, parameter: float, out_y: float, in_y: float) -> float:
    squared = f32(parameter * parameter)
    cubed = f32(squared * parameter)
    basis0 = fadd(fadd(fsub(f32(cubed * -1.0), f32(squared * -3.0)), f32(parameter * -3.0)), 1.0)
    basis1 = fadd(fadd(f32(cubed * 3.0), f32(squared * -6.0)), f32(parameter * 3.0))
    basis2 = fadd(f32(cubed * -3.0), f32(squared * 3.0))
    basis3 = cubed
    value0, value1 = f32(float(value0)), f32(float(value1))
    outgoing = fadd(value0, out_y)
    incoming = fsub(value1, in_y)
    value = fadd(f32(basis0 * value0), f32(basis1 * value1))
    return fadd(fadd(value, f32(basis2 * incoming)), f32(basis3 * outgoing))


def linear_value(kind: PropertyType, value0: float, value1: float, ratio: float) -> float:
    inverse = fsub(1.0, ratio)
    start = interpolation_input(kind, value0)
    end = interpolation_input(kind, value1)
    return fadd(f32(inverse * start), f32(ratio * end))


def convert_number(kind: PropertyType, value: float) -> int | float:
    if kind == PropertyType.F32:
        return f32(value)
    if kind == PropertyType.F64:
        return float(value)
    if kind not in INTEGER_TYPES:
        return value
    signed = kind in {PropertyType.S8, PropertyType.S16, PropertyType.S32, PropertyType.S64}
    width = {PropertyType.S8: 8, PropertyType.U8: 8, PropertyType.S16: 16,
             PropertyType.U16: 16, PropertyType.S32: 32, PropertyType.U32: 32,
             PropertyType.S64: 64, PropertyType.U64: 64}[kind]
    rounded = fadd(value, 0.5)
    if kind == PropertyType.U64:
        raw = _native_cvttss2ui64(rounded)
    else:
        conversion_width = 64 if width == 64 or kind == PropertyType.U32 else 32
        raw = _native_cvttss2si(rounded, conversion_width)
    mask = (1 << width) - 1
    raw &= mask
    return raw - (1 << width) if signed and raw & (1 << (width - 1)) else raw


def interpolation_input(kind: PropertyType, value: int | float) -> float:
    if kind == PropertyType.S64:
        return _native_integer_to_f32(int(value))
    if kind == PropertyType.U64:
        return _native_u64_to_f32(int(value))
    return f32(float(value))


def _native_cvttss2si(value: float, width: int) -> int:
    value = f32(value)
    indefinite = 1 << (width - 1)
    if not math.isfinite(value):
        return indefinite
    converted = math.trunc(value)
    if not -(1 << (width - 1)) <= converted < 1 << (width - 1):
        return indefinite
    return converted & ((1 << width) - 1)


def _native_integer_to_f32(value: int) -> float:
    integer = int(value)
    if integer == 0:
        return 0.0
    sign, magnitude = (1, -integer) if integer < 0 else (0, integer)
    exponent = magnitude.bit_length() - 1
    if exponent <= 23:
        significand = magnitude << (23 - exponent)
    else:
        shift = exponent - 23
        significand = magnitude >> shift
        remainder = magnitude & ((1 << shift) - 1)
        halfway = 1 << (shift - 1)
        if remainder > halfway or remainder == halfway and significand & 1:
            significand += 1
            if significand == 1 << 24:
                significand >>= 1
                exponent += 1
    bits = (sign << 31) | ((exponent + 127) << 23) | (significand & 0x7FFFFF)
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def _native_u64_to_f32(value: int) -> float:
    raw = int(value) & 0xFFFF_FFFF_FFFF_FFFF
    signed = raw - (1 << 64) if raw & (1 << 63) else raw
    result = _native_integer_to_f32(signed)
    return fadd(result, f32(float(1 << 64))) if raw & (1 << 63) else result


def _native_cvttss2ui64(value: float) -> int:
    adjusted = f32(value)
    threshold = f32(float(1 << 63))
    bias = 0
    if not math.isnan(adjusted) and adjusted >= threshold:
        adjusted = fsub(adjusted, threshold)
        if not math.isnan(adjusted) and adjusted < threshold:
            bias = 1 << 63
    return (_native_cvttss2si(adjusted, 64) + bias) & 0xFFFF_FFFF_FFFF_FFFF
