from __future__ import annotations

from .enums import PropertyType


INTERPOLATION_NAMES = {
    0x0: "Unknown",
    0x1: "Discrete",
    0x2: "Linear",
    0x3: "Event",
    0x4: "Slerp",
    0x5: "Hermite",
    0x6: "AutoHermite",
    0x7: "Bezier",
    0x8: "AutoBezier",
    0x9: "OffsetFrame",
    0xA: "OffsetSec",
    0xB: "PassEvent",
    0xC: "Bezier3D",
    0xD: "Range",
    0xE: "DiscreteToEnd",
    0xF: "RangeV2",
    0x10: "None",
}
INTERPOLATION_BY_NAME = {name.lower(): value for value, name in INTERPOLATION_NAMES.items()}
INTERPOLATION_DEFAULT_REFS = {
    0x5: (0.0, 0.0, 0.0, 0.0),
    0xC: (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
}

NODE_TYPE_NAMES = {
    0x0: "Unknown",
    0x1: "GameObject",
    0x2: "Component",
    0x3: "Folder",
}
NODE_TYPE_BADGES = {
    0x0: "UNK",
    0x1: "GO",
    0x2: "CMP",
    0x3: "FLD",
}
NODE_TYPE_COLORS = {
    0x0: "#6b7280",
    0x1: "#3f9f72",
    0x2: "#477fc2",
    0x3: "#9b7d33",
}

AUX_KEY_TABLE_NAMES = {
    0x0: "Main Keys",
    0x1: "Bool Keys",
    0x2: "Action Keys",
    0x3: "No-Hermite Keys",
}

EXTRA_KEY_FLAG_NAMES = {
    0x1: "Last Key",
    0x2: "Extra Key 1",
    0x4: "Extra Key 2",
    0x8: "Extra Key 3",
}

EXTRA_PROPERTY_MASK_NAMES = {
    0x2: "Extra Key 1",
    0x4: "Extra Key 2",
    0x8: "Extra Key 3",
}

_XY = ("X", "Y")
_XYZ = ("X", "Y", "Z")
_XYZW = ("X", "Y", "Z", "W")

COMPONENT_LABELS = {
    **dict.fromkeys((PropertyType.VEC2, PropertyType.FLOAT2, PropertyType.SFIX2, PropertyType.INT2, PropertyType.UINT2, PropertyType.POINT), _XY),
    **dict.fromkeys((PropertyType.VEC3, PropertyType.FLOAT3, PropertyType.SFIX3, PropertyType.INT3, PropertyType.UINT3, PropertyType.POSITION, PropertyType.QUATERNION), _XYZ),
    **dict.fromkeys((PropertyType.VEC4, PropertyType.FLOAT4, PropertyType.SFIX4, PropertyType.INT4, PropertyType.UINT4), _XYZW),
    PropertyType.COLOR: ("R", "G", "B", "A"),
    PropertyType.RANGE: ("Min", "Max"),
    PropertyType.RANGEI: ("Min", "Max"),
    PropertyType.RECT: ("Left", "Top", "Right", "Bottom"),
    PropertyType.PLANE: ("X", "Y", "Z", "Distance"),
    PropertyType.SPHERE: ("Center", "Radius"),
    PropertyType.AABB: ("Min", "Max"),
    PropertyType.CAPSULE: ("Start", "End", "Radius"),
    PropertyType.OBB: ("Transform", "Extent"),
    PropertyType.MAT4: tuple(f"Row {row}" for row in range(4)),
}


def _repeat(ptype: PropertyType, count: int) -> tuple[PropertyType, ...]:
    return (ptype,) * count


CONTAINER_CHILD_TYPES = {
    **{ptype: _repeat(PropertyType.F32, count) for ptype, count in (
        (PropertyType.VEC2, 2), (PropertyType.VEC3, 3), (PropertyType.VEC4, 4),
        (PropertyType.FLOAT2, 2), (PropertyType.FLOAT3, 3), (PropertyType.FLOAT4, 4),
        (PropertyType.QUATERNION, 3), (PropertyType.RANGE, 2), (PropertyType.RECT, 4),
    )},
    **{ptype: _repeat(PropertyType.SFIX, count) for ptype, count in ((PropertyType.SFIX2, 2), (PropertyType.SFIX3, 3), (PropertyType.SFIX4, 4))},
    **{ptype: _repeat(PropertyType.S32, count) for ptype, count in ((PropertyType.INT2, 2), (PropertyType.INT3, 3), (PropertyType.INT4, 4), (PropertyType.POINT, 2), (PropertyType.RANGEI, 2))},
    **{ptype: _repeat(PropertyType.U32, count) for ptype, count in ((PropertyType.UINT2, 2), (PropertyType.UINT3, 3), (PropertyType.UINT4, 4))},
    PropertyType.COLOR: _repeat(PropertyType.U8, 4),
    PropertyType.POSITION: _repeat(PropertyType.F64, 3),
    PropertyType.PLANE: (PropertyType.VEC3, PropertyType.F32),
    PropertyType.SPHERE: (PropertyType.FLOAT3, PropertyType.F32),
    PropertyType.AABB: (PropertyType.VEC3, PropertyType.VEC3),
    PropertyType.CAPSULE: (PropertyType.VEC3, PropertyType.VEC3, PropertyType.F32),
    PropertyType.OBB: (PropertyType.MAT4, PropertyType.VEC3),
    PropertyType.MAT4: _repeat(PropertyType.VEC4, 4),
    PropertyType.KEY_FRAME: (PropertyType.U32, PropertyType.U32, PropertyType.U32, PropertyType.F32),
    PropertyType.ANIMATION_CURVE: (PropertyType.F32, PropertyType.F32, PropertyType.F32, PropertyType.F32, PropertyType.NATIVE_ARRAY, PropertyType.U32, PropertyType.S32, PropertyType.BOOL),
    PropertyType.NULLABLE: (PropertyType.BOOL, PropertyType.F32),
}


def enum_text(value: int, names: dict[int, str]) -> str:
    value = int(value)
    return names.get(value, f"0x{value:X}")


def flags_text(value: int, names: dict[int, str]) -> str:
    value = int(value)
    labels = [name for bit, name in names.items() if value & bit]
    known = 0
    for bit in names:
        known |= bit
    unknown = value & ~known
    if unknown:
        labels.append(f"0x{unknown:X}")
    return ", ".join(labels) if labels else "None"
