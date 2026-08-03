from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum


class ClipPropertyType(IntEnum):
    UNKNOWN = 0x00
    BOOL = 0x01
    S8 = 0x02
    U8 = 0x03
    S16 = 0x04
    U16 = 0x05
    S32 = 0x06
    U32 = 0x07
    S64 = 0x08
    U64 = 0x09
    F32 = 0x0A
    F64 = 0x0B
    STR8 = 0x0C
    STR16 = 0x0D
    ENUM = 0x0E
    QUATERNION = 0x0F
    ARRAY = 0x10
    NATIVE_ARRAY = 0x11
    CLASS = 0x12
    NATIVE_CLASS = 0x13
    STRUCT = 0x14
    VEC2 = 0x15
    VEC3 = 0x16
    VEC4 = 0x17
    COLOR = 0x18
    RANGE = 0x19
    FLOAT2 = 0x1A
    FLOAT3 = 0x1B
    FLOAT4 = 0x1C
    RANGEI = 0x1D
    POINT = 0x1E
    SIZE = 0x1F
    ASSET = 0x20
    ACTION = 0x21
    GUID = 0x22
    UINT2 = 0x23
    UINT3 = 0x24
    UINT4 = 0x25
    INT2 = 0x26
    INT3 = 0x27
    INT4 = 0x28
    OBB = 0x29
    MAT4 = 0x2A
    RECT = 0x2B
    PATH_POINT3D = 0x2C
    PLANE = 0x2D
    SPHERE = 0x2E
    CAPSULE = 0x2F
    AABB = 0x30
    NULLABLE = 0x31
    SFIX = 0x32
    SFIX2 = 0x33
    SFIX3 = 0x34
    SFIX4 = 0x35
    ANIMATION_CURVE = 0x36
    KEY_FRAME = 0x37
    GAME_OBJECT_REF = 0x38
    POSITION = 0x39
    USER_DATA_ASSET = 0x3A
    RESOURCE_PATH = 0x3B


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


CONTAINER_PROPERTY_TYPES = frozenset(
    {
        ClipPropertyType.QUATERNION,
        ClipPropertyType.ARRAY,
        ClipPropertyType.NATIVE_ARRAY,
        ClipPropertyType.CLASS,
        ClipPropertyType.NATIVE_CLASS,
        ClipPropertyType.STRUCT,
        ClipPropertyType.VEC2,
        ClipPropertyType.VEC3,
        ClipPropertyType.VEC4,
        ClipPropertyType.COLOR,
        ClipPropertyType.RANGE,
        ClipPropertyType.FLOAT2,
        ClipPropertyType.FLOAT3,
        ClipPropertyType.FLOAT4,
        ClipPropertyType.RANGEI,
        ClipPropertyType.POINT,
        ClipPropertyType.SIZE,
        ClipPropertyType.UINT2,
        ClipPropertyType.UINT3,
        ClipPropertyType.UINT4,
        ClipPropertyType.INT2,
        ClipPropertyType.INT3,
        ClipPropertyType.INT4,
        ClipPropertyType.OBB,
        ClipPropertyType.MAT4,
        ClipPropertyType.RECT,
        ClipPropertyType.PLANE,
        ClipPropertyType.SPHERE,
        ClipPropertyType.CAPSULE,
        ClipPropertyType.AABB,
        ClipPropertyType.NULLABLE,
        ClipPropertyType.SFIX2,
        ClipPropertyType.SFIX3,
        ClipPropertyType.SFIX4,
        ClipPropertyType.ANIMATION_CURVE,
        ClipPropertyType.KEY_FRAME,
        ClipPropertyType.POSITION,
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
