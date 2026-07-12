from __future__ import annotations

import struct

from .enums import PropertyType, property_type_or_unknown
from .structures import ActionKey, BoolKey, Key, NoHermiteKey, Property, SpeedPoint
from utils.number_format import format_full_float


def key_payload_text(prop: Property | None, key) -> str:
    ptype = _ptype(prop)
    if isinstance(key, BoolKey):
        return "True" if key.bool_value else "False"
    if isinstance(key, ActionKey):
        return "Trigger"
    if isinstance(key, SpeedPoint):
        return format_full_float(key.rate)
    if getattr(key, "string_is_wide", -1) != -1:
        return key.string_value
    asset = getattr(key, "user_data_asset_ref", None)
    if asset is not None:
        return asset.path_unicode or asset.type_ascii
    if getattr(key, "oword_ref", None) is not None:
        return ", ".join(format_full_float(value) for value in key.oword_ref)
    if isinstance(key, (Key, NoHermiteKey)):
        return _scalar_text(ptype, _payload_bytes(key))
    return ""


def key_payload_editable(prop: Property | None, key) -> bool:
    ptype = _ptype(prop)
    return (
        isinstance(key, (BoolKey, SpeedPoint))
        or getattr(key, "string_is_wide", -1) != -1
        or getattr(key, "oword_ref", None) is not None
        or ptype in _EDITABLE_SCALARS
    )


def apply_key_payload_text(prop: Property | None, key, text: str) -> bool:
    ptype = _ptype(prop)
    if isinstance(key, BoolKey):
        key.bool_value = 1 if _parse_bool(text) else 0
        return True
    if isinstance(key, SpeedPoint):
        key.rate = float(text)
        return True
    if getattr(key, "string_is_wide", -1) != -1:
        key.string_value = text
        return True
    asset = getattr(key, "user_data_asset_ref", None)
    if asset is not None:
        return False
    if getattr(key, "oword_ref", None) is not None:
        values = [float(part.strip()) for part in text.split(",")]
        if len(values) != 4:
            raise ValueError("Expected four values")
        key.oword_ref = tuple(values)
        return True
    if isinstance(key, (Key, NoHermiteKey)) and ptype in _EDITABLE_SCALARS:
        key.raw0, key.raw1 = _encode_scalar(ptype, text)
        return True
    return False


def _ptype(prop: Property | None) -> PropertyType:
    return property_type_or_unknown(prop.property_type) if prop else PropertyType.UNKNOWN


def _payload_bytes(key) -> bytes:
    return struct.pack("<II", key.raw0 & 0xFFFFFFFF, key.raw1 & 0xFFFFFFFF)


def _set_payload_bytes(data: bytes) -> tuple[int, int]:
    return struct.unpack("<II", data)


def _scalar_text(ptype: PropertyType, data: bytes) -> str:
    if ptype == PropertyType.ACTION:
        return "Trigger"
    if ptype == PropertyType.BOOL:
        return "True" if struct.unpack("<b", data[:1])[0] else "False"
    if ptype in _INT_SCALAR_FORMATS:
        fmt = _INT_SCALAR_FORMATS[ptype]
        return str(struct.unpack(fmt, data[:struct.calcsize(fmt)])[0])
    if ptype in _FLOAT_SCALAR_FORMATS:
        fmt = _FLOAT_SCALAR_FORMATS[ptype]
        return format_full_float(struct.unpack(fmt, data[:struct.calcsize(fmt)])[0])
    if ptype == PropertyType.PATH_POINT3D:
        return f"OWord[{struct.unpack('<I', data[:4])[0]}]"
    return "Unmodeled payload"


def _encode_scalar(ptype: PropertyType, text: str) -> tuple[int, int]:
    if ptype == PropertyType.BOOL:
        return _set_payload_bytes(struct.pack("<q", 1 if _parse_bool(text) else 0))
    if ptype in _INT_SCALAR_FORMATS:
        value = int(text, 0)
        struct.pack(_INT_SCALAR_FORMATS[ptype], value)
        fmt = "<q" if ptype in _SIGNED_INT_SCALARS else "<Q"
        return _set_payload_bytes(struct.pack(fmt, value))
    if ptype in _FLOAT_SCALAR_FORMATS:
        return _set_payload_bytes(struct.pack(_FLOAT_SCALAR_FORMATS[ptype], float(text)).ljust(8, b"\0"))
    raise ValueError(f"Unsupported editable payload type: {ptype.name}")


def _parse_bool(text: str) -> bool:
    lowered = text.strip().lower()
    if lowered in {"true", "1", "yes", "on"}:
        return True
    if lowered in {"false", "0", "no", "off"}:
        return False
    return bool(int(text, 0))


_INT_SCALAR_FORMATS = {
    PropertyType.S8: "<b",
    PropertyType.U8: "<B",
    PropertyType.S16: "<h",
    PropertyType.U16: "<H",
    PropertyType.S32: "<i",
    PropertyType.U32: "<I",
    PropertyType.S64: "<q",
    PropertyType.U64: "<Q",
}
_SIGNED_INT_SCALARS = {PropertyType.S8, PropertyType.S16, PropertyType.S32, PropertyType.S64}

_FLOAT_SCALAR_FORMATS = {
    # RE_CLIP_TML.bt stores both logical F32 and F64 key payloads as qword doubles.
    PropertyType.F32: "<d",
    PropertyType.F64: "<d",
}

_EDITABLE_SCALARS = {
    PropertyType.BOOL,
    *_INT_SCALAR_FORMATS,
    *_FLOAT_SCALAR_FORMATS,
}
