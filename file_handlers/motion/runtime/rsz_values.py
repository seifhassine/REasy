from __future__ import annotations

import math
from typing import Mapping

from file_handlers.rsz.rsz_data_types import ArrayData

from .model import MotionRuntimeContextError


MISSING = object()


def value(fields: Mapping[str, object], name: str, *, default=MISSING):
    field = fields.get(name)
    if field is None or not hasattr(field, "value"):
        if default is not MISSING:
            return default
        raise MotionRuntimeContextError(f"required PFB field {name!r} is missing")
    return field.value


def int_value(
    fields: Mapping[str, object],
    name: str,
    *,
    default=MISSING,
) -> int:
    result = value(fields, name, default=default)
    if isinstance(result, bool) or not isinstance(result, int):
        raise MotionRuntimeContextError(f"PFB field {name!r} is not an integer")
    return result


def bool_value(
    fields: Mapping[str, object],
    name: str,
    *,
    default=MISSING,
) -> bool:
    result = value(fields, name, default=default)
    if type(result) is not bool:
        raise MotionRuntimeContextError(f"PFB field {name!r} is not a boolean")
    return result


def finite_float(
    fields: Mapping[str, object],
    name: str,
    *,
    default=MISSING,
) -> float:
    result = value(fields, name, default=default)
    if not isinstance(result, (int, float)) or isinstance(result, bool):
        raise MotionRuntimeContextError(f"PFB field {name!r} is not numeric")
    result = float(result)
    if not math.isfinite(result):
        raise MotionRuntimeContextError(f"PFB field {name!r} is nonfinite")
    return result


def nonnegative_float(
    fields: Mapping[str, object],
    name: str,
    *,
    default=MISSING,
) -> float:
    result = finite_float(fields, name, default=default)
    if result < 0.0:
        raise MotionRuntimeContextError(f"PFB field {name!r} is negative")
    return result


def string_value(
    fields: Mapping[str, object],
    name: str,
    *,
    default=MISSING,
) -> str:
    result = value(fields, name, default=default)
    if not isinstance(result, str):
        raise MotionRuntimeContextError(f"PFB field {name!r} is not a string")
    return clean_path(result)


def int_array(
    fields: Mapping[str, object],
    name: str,
    *,
    default=MISSING,
) -> tuple[int, ...]:
    field = fields.get(name)
    if field is None:
        if default is not MISSING:
            return tuple(default)
        raise MotionRuntimeContextError(f"required PFB array {name!r} is missing")
    if not isinstance(field, ArrayData):
        raise MotionRuntimeContextError(f"PFB field {name!r} is not an array")
    result = []
    for item in field.values:
        item_value = getattr(item, "value", None)
        if isinstance(item_value, bool) or not isinstance(item_value, int):
            raise MotionRuntimeContextError(
                f"PFB field {name!r} contains a noninteger value"
            )
        result.append(item_value)
    return tuple(result)


def clean_path(value: str) -> str:
    return value.replace("\\", "/").strip().rstrip("\0")
