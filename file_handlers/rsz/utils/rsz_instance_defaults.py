"""Each table entry lists every name its field has across the games' dumps."""

import functools
import json
import struct
import uuid

lazy from file_handlers.rsz.rsz_data_types import (BoolData, F32Data, F64Data, GameObjectRefData, GuidData, Mat4Data,
                                                   MaybeObject, OBBData, RawBytesData)
lazy from utils.app_paths import resource_path

DEFAULTS_RESOURCE = "resources/data/rsz_instance_defaults.json"


@functools.cache
def type_table(resource: str) -> dict:
    with open(resource_path(resource, required=True), encoding="utf-8") as handle:
        return json.load(handle)


def apply_instance_defaults(fields: dict, type_info: dict) -> None:
    entries = type_table(DEFAULTS_RESOURCE).get(type_info.get("name", ""))
    if not entries:
        return
    field_defs = {field_def.get("name"): field_def for field_def in type_info.get("fields", [])}
    for entry in entries:
        name = next((name for name in entry["fields"] if name in fields), None)
        if name is not None:
            fields[name] = with_value(fields[name], entry["value"], field_defs.get(name, {}))


def with_value(field, value, field_def: dict):
    if isinstance(field, (Mat4Data, OBBData)):
        field.values = [float(component) for component in value]
        return field
    if isinstance(value, list):
        for name, component in zip([key for key in vars(field) if not key.startswith("_") and key != "orig_type"],
                                   value):
            setattr(field, name, component)
        return field
    if isinstance(field, (RawBytesData, MaybeObject)):
        size = int(field_def.get("size", getattr(field, "field_size", 4)))
        return RawBytesData(_encode(value, size), size, getattr(field, "orig_type", ""))
    if isinstance(field, (GuidData, GameObjectRefData)):
        field.guid_str, field.raw_bytes = value, uuid.UUID(value).bytes_le
    elif isinstance(field, BoolData):
        field.value = bool(value)
    elif isinstance(field, (F32Data, F64Data)) and isinstance(value, str):
        field.value = struct.unpack("<d" if isinstance(field, F64Data) else "<f", bytes.fromhex(value))[0]
    elif isinstance(field, (F32Data, F64Data)):
        field.value = float(value)
    else:
        field.value = value if isinstance(value, str) else int(value)
    return field


def _encode(value, size: int) -> bytes:
    if isinstance(value, float):
        return struct.pack("<d" if size == 8 else "<f", value)
    return int(value).to_bytes(size, "little", signed=int(value) < 0)
