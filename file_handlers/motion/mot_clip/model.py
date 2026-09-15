from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from file_handlers.clip.enums import PROPERTY_TYPES_WITH_CHILDREN, PropertyType

ClipPropertyType = PropertyType


class ClipInterpolation(IntEnum):
    UNKNOWN = 0
    DISCRETE = 1
    LINEAR = 2
    EVENT = 3
    SLERP = 4
    HERMITE = 5
    AUTO_HERMITE = 6
    BEZIER = 7
    AUTO_BEZIER = 8
    OFFSET_FRAME = 9
    OFFSET_SECONDS = 10
    PASS_EVENT = 11
    BEZIER_3D = 12
    RANGE = 13
    DISCRETE_TO_END = 14
    RANGE_V2 = 15
    NONE = 16


CONTAINER_PROPERTY_TYPES = frozenset(PROPERTY_TYPES_WITH_CHILDREN)

ASCII_VALUE_PROPERTY_TYPES = frozenset(
    {ClipPropertyType.STR8, ClipPropertyType.ENUM}
)

UTF16_VALUE_PROPERTY_TYPES = frozenset(
    {
        ClipPropertyType.STR16,
        ClipPropertyType.ASSET,
        ClipPropertyType.GUID,
        ClipPropertyType.GAME_OBJECT_REF,
        ClipPropertyType.USER_DATA_ASSET,
        ClipPropertyType.RESOURCE_PATH,
    }
)


@dataclass(slots=True)
class HermiteCurve:
    values: tuple[float, float, float, float]


@dataclass(slots=True)
class Bezier3DCurve:
    values: tuple[float, float, float, float, float, float, float, float]


Curve = HermiteCurve | Bezier3DCurve
ClipValue = bool | int | float | str | tuple[float, float, float] | None


@dataclass(slots=True)
class ClipKey:
    frame: float = 0.0
    rate: float = 0.0
    interpolation: ClipInterpolation = ClipInterpolation.UNKNOWN
    offset_frame: bool = False
    value: ClipValue = None
    curve: Curve | None = None


@dataclass(slots=True)
class SpeedPoint:
    frame: float = 0.0
    rate: float = 0.0
    interpolation: ClipInterpolation = ClipInterpolation.UNKNOWN
    curve: Curve | None = None


@dataclass(slots=True)
class ClipProperty:
    name: str
    property_type: ClipPropertyType
    start_frame: float = 0.0
    end_frame: float = 0.0
    array_index: int = -1
    enum_closed: bool = False
    set_after_end_frame: bool = False
    restoration: bool = False
    set_delegate_enable: bool = False
    prev_diff_frame_set: bool = False
    next_diff_frame_set: bool = False
    prev_key_value_set: bool = False
    children: list["ClipProperty"] = field(default_factory=list)
    keys: list[ClipKey] = field(default_factory=list)
    last_key: ClipKey | None = None
    speed_points: list[SpeedPoint] = field(default_factory=list)


@dataclass(slots=True, eq=False)
class ClipNode:
    name: str
    start_frame: float = 0.0
    end_frame: float = 0.0
    root_guid: bytes = bytes(16)
    extra_guid: bytes = bytes(16)
    properties: list[ClipProperty] = field(default_factory=list)
    children: list["ClipNode"] = field(default_factory=list)


@dataclass(slots=True)
class ClipInterval:
    begin_frame: float | None
    frame_span: int


@dataclass(slots=True)
class ClipExtraRange:
    owner: ClipNode
    intervals: list[ClipInterval] = field(default_factory=list)


@dataclass(slots=True)
class CompactMotClip:
    total_frame: float = 0.0
    root: ClipNode = field(default_factory=lambda: ClipNode(""))
    extra_ranges: list[ClipExtraRange] = field(default_factory=list)
