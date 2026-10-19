"""Each table entry lists every name its field has across the games' dumps."""

import functools
import json
import struct

lazy from file_handlers.rsz.rsz_data_types import BoolData, F32Data, F64Data, MaybeObject, RawBytesData
lazy from utils.app_paths import resource_path

DEFAULTS_RESOURCE = "resources/data/rsz_instance_defaults.json"


@functools.cache
def type_table(resource: str) -> dict:
    """Keys starting with ``_`` are comments."""
    with open(resource_path(resource, required=True), encoding="utf-8") as handle:
        table = json.load(handle)
    return {type_name: entry for type_name, entry in table.items() if not type_name.startswith("_")}


def apply_instance_defaults(fields: dict, type_info: dict) -> None:
    entries = type_table(DEFAULTS_RESOURCE).get(type_info.get("name", ""))
    if not entries:
        return
    field_defs = {field_def.get("name"): field_def for field_def in type_info.get("fields", [])}
    for entry in entries:
        name = next((name for name in entry["fields"] if name in fields), None)
        if name is not None:
            fields[name] = _with_value(fields[name], entry["value"], field_defs.get(name, {}))


def _with_value(field, value, field_def: dict):
    if isinstance(value, list):
        for axis, component in zip("xyzw", value):
            if hasattr(field, axis):
                setattr(field, axis, float(component))
        return field
    if isinstance(field, (RawBytesData, MaybeObject)):
        size = int(field_def.get("size", getattr(field, "field_size", 4)))
        return RawBytesData(_encode(value, size), size, getattr(field, "orig_type", ""))
    if isinstance(field, BoolData):
        field.value = bool(value)
    elif isinstance(field, (F32Data, F64Data)):
        field.value = float(value)
    else:
        field.value = int(value)
    return field


def _encode(value, size: int) -> bytes:
    if isinstance(value, float):
        return struct.pack("<d" if size == 8 else "<f", value)
    return int(value).to_bytes(size, "little", signed=int(value) < 0)
