"""Format independent REE GUI property type categories """

from file_handlers.clip.enums import PropertyType


SIGNED_FORMATS = {
    PropertyType.S8: "b",
    PropertyType.S16: "h",
    PropertyType.S32: "i",
    PropertyType.S64: "q",
}
UNSIGNED_FORMATS = {
    PropertyType.U8: "B",
    PropertyType.U16: "H",
    PropertyType.U32: "I",
    PropertyType.U64: "Q",
}
FLOAT_VECTOR_COUNTS = {
    PropertyType.QUATERNION: 4,
    PropertyType.VEC2: 2,
    PropertyType.VEC3: 3,
    PropertyType.VEC4: 4,
    PropertyType.RANGE: 2,
    PropertyType.FLOAT2: 2,
    PropertyType.FLOAT3: 3,
    PropertyType.FLOAT4: 4,
    PropertyType.SIZE: 2,
    PropertyType.RECT: 4,
}
SIGNED_VECTOR_COUNTS = {
    PropertyType.RANGEI: 2,
    PropertyType.POINT: 2,
    PropertyType.INT2: 2,
    PropertyType.INT3: 3,
    PropertyType.INT4: 4,
}
UNSIGNED_VECTOR_COUNTS = {
    PropertyType.UINT2: 2,
    PropertyType.UINT3: 3,
    PropertyType.UINT4: 4,
}
WIDE_STRING_TYPES = {
    PropertyType.STR16,
    PropertyType.ASSET,
    PropertyType.RESOURCE_PATH,
    PropertyType.USER_DATA_ASSET,
    PropertyType.GAME_OBJECT_REF,
}
GUI_INTEGER_TYPES = frozenset((*SIGNED_FORMATS, *UNSIGNED_FORMATS))
GUI_FLOAT_TYPES = frozenset({PropertyType.F32, PropertyType.F64})
GUI_STRING_TYPES = frozenset(
    {PropertyType.STR8, PropertyType.ENUM, *WIDE_STRING_TYPES}
)
GUI_INTEGER_VECTOR_TYPES = frozenset(
    {PropertyType.COLOR, *SIGNED_VECTOR_COUNTS, *UNSIGNED_VECTOR_COUNTS}
)
