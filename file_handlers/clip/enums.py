from __future__ import annotations

from enum import IntEnum


CLIP_MAGIC = 0x50494C43  # 'CLIP'


class PropertyType(IntEnum):
    UNKNOWN = 0x0
    BOOL = 0x1
    S8 = 0x2
    U8 = 0x3
    S16 = 0x4
    U16 = 0x5
    S32 = 0x6
    U32 = 0x7
    S64 = 0x8
    U64 = 0x9
    F32 = 0xA
    F64 = 0xB
    STR8 = 0xC
    STR16 = 0xD
    ENUM = 0xE
    QUATERNION = 0xF
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


def property_type_or_unknown(value: int) -> PropertyType:
    return PropertyType(value) if value in PropertyType._value2member_map_ else PropertyType.UNKNOWN


# Property::isPrimitive per CLIP version: these types own keys, every other type owns child properties.
_KEY_OWNER_TYPES_SINCE = (
    (0, frozenset({*range(PropertyType.BOOL, PropertyType.QUATERNION), PropertyType.ASSET, PropertyType.ACTION,
                   PropertyType.GUID, PropertyType.PATH_POINT3D, PropertyType.SFIX})),
    (40, frozenset({PropertyType.GAME_OBJECT_REF})),
    (62, frozenset({PropertyType.USER_DATA_ASSET})),
    (85, frozenset({PropertyType.RESOURCE_PATH})),
)


def property_type_has_children(property_type: int, version: int) -> bool:
    return not any(version >= since and int(property_type) in types for since, types in _KEY_OWNER_TYPES_SINCE)


PROPERTY_TYPES_WITH_CHILDREN = frozenset(
    ptype for ptype in PropertyType if ptype is not PropertyType.UNKNOWN and property_type_has_children(ptype, 85)
)
