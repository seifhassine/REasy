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


PROPERTY_TYPES_WITH_CHILDREN = {
    PropertyType.NATIVE_ARRAY,
    PropertyType.NULLABLE,
    PropertyType.ANIMATION_CURVE,
    PropertyType.KEY_FRAME,
    PropertyType.NATIVE_CLASS,
    PropertyType.VEC2,
    PropertyType.VEC3,
    PropertyType.VEC4,
    PropertyType.QUATERNION,
    PropertyType.SPHERE,
    PropertyType.PLANE,
    PropertyType.CAPSULE,
    PropertyType.AABB,
    PropertyType.COLOR,
    PropertyType.RANGE,
    PropertyType.RANGEI,
    PropertyType.POINT,
    PropertyType.UINT2,
    PropertyType.UINT3,
    PropertyType.UINT4,
    PropertyType.INT2,
    PropertyType.INT3,
    PropertyType.INT4,
    PropertyType.SFIX2,
    PropertyType.SFIX3,
    PropertyType.SFIX4,
    PropertyType.FLOAT2,
    PropertyType.FLOAT3,
    PropertyType.FLOAT4,
    PropertyType.OBB,
    PropertyType.MAT4,
    PropertyType.RECT,
    PropertyType.POSITION,
}
