"""Native-style binary32 arithmetic shared by GUI runtime projections."""

from __future__ import annotations

import math
import struct


def f32(value: float) -> float:
    """Round a value as a native binary32 operation would."""

    try:
        return struct.unpack("<f", struct.pack("<f", float(value)))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


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
        negative = math.copysign(1.0, numerator) != math.copysign(
            1.0,
            denominator,
        )
        return -math.inf if negative else math.inf
    return f32(numerator / denominator)
