from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation
from numbers import Integral, Real


def format_full_float(value, precision: int | None = None) -> str:
    """Format a finite float without scientific notation."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return str(value)

    if not math.isfinite(numeric):
        return str(numeric)

    text = repr(numeric) if precision is None else f"{numeric:.{precision}g}"
    text = _expand_exponent(text)

    if "." in text:
        text = text.rstrip("0").rstrip(".")

    return "0" if text in ("-0", "-0.0") else text


def format_display_value(value, precision: int | None = None) -> str:
    """Format scalar values for UI text, expanding finite floats."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + format_float_sequence(value, precision) + "]"
    if isinstance(value, Real) and not isinstance(value, Integral):
        return format_full_float(value, precision)
    return str(value)


def format_float_sequence(values, precision: int | None = None, separator: str = ", ") -> str:
    return separator.join(format_display_value(value, precision) for value in values)


def _expand_exponent(text: str) -> str:
    if "e" not in text.lower():
        return text
    try:
        return format(Decimal(text), "f")
    except (InvalidOperation, ValueError):
        return text
