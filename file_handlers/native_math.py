"""Native-style binary32 arithmetic."""

from __future__ import annotations

import math
import struct


def f32(value: float) -> float:
    """Round a value as a native binary32 operation would."""

    try:
        return struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def f16(value: float) -> float:
    """A value through RE Engine's via.f16 and back: the mantissa truncated, below the half range +0 or a truncated
    subnormal, beyond it infinity, NaN kept."""
    bits = struct.unpack("<I", struct.pack("<f", f32(value)))[0]
    sign, exponent, mantissa = (bits >> 16) & 0x8000, ((bits >> 23) & 0xFF) - 0x70, bits & 0x7FFFFF
    if exponent <= 0:
        half = 0 if exponent < -10 else ((mantissa | 0x800000) >> (1 - exponent) >> 13) | sign
    elif exponent == 0x8F:
        half = 0x7C00 | sign | (mantissa >> 13 or bool(mantissa))
    else:
        half = 0x7C00 | sign if exponent > 0x1E else (mantissa >> 13) | (exponent << 10) | sign
    return struct.unpack("<e", struct.pack("<H", half))[0]


def fadd(left: float, right: float) -> float:
    return f32(f32(left) + f32(right))


def fsub(left: float, right: float) -> float:
    return f32(f32(left) - f32(right))


def fmul(left: float, right: float) -> float:
    return f32(f32(left) * f32(right))


def fdiv(numerator: float, denominator: float) -> float:
    numerator, denominator = f32(numerator), f32(denominator)
    if math.isnan(numerator) or math.isnan(denominator):
        return math.nan
    if not denominator:
        if not numerator:
            return math.nan
        negative = math.signbit(numerator) != math.signbit(denominator)
        return -math.inf if negative else math.inf
    return f32(numerator / denominator)
