from __future__ import annotations

from collections.abc import Callable, Container, Mapping

lazy from .rsz_data_types import (AABBData, AreaData, ArrayData, CapsuleData, ColorData, ConeData, CylinderData,
                             LineSegmentData, Mat4Data, OBBData, ObjectData, RangeData, RangeIData, RawBytesData,
                             RectData, SizeData, SphereData, StructData, UserDataData, _GuidBytesData, _ValueData)


def plain_fields(document, component, names: Container[str] | None = None) -> dict:
    instance = document.rsz_file.parsed_elements.__getitem__
    return {name: plain_value(value, instance) for name, value in component.fields.items()
            if names is None or name in names}


def plain_value(data, instance: Callable[[int], Mapping[str, object]] | None = None):
    if isinstance(data, (bool, int, float)):
        return data
    if isinstance(data, str):
        return data.rstrip("\0")
    if isinstance(data, _ValueData):
        return plain_value(data.value)
    if isinstance(data, ArrayData):
        return [plain_value(item, instance) for item in data.values]
    if isinstance(data, StructData):
        return [{name: plain_value(value, instance) for name, value in entry.items()} for entry in data.values]
    if isinstance(data, ObjectData):
        if instance is None:
            raise TypeError("a reference to an instance has no plain value without the instances")
        return {name: plain_value(value, instance) for name, value in instance(data.value).items()}
    if isinstance(data, UserDataData):
        return plain_value(data.string)
    if isinstance(data, (AABBData, RangeData, RangeIData)):
        return plain_value(data.min), plain_value(data.max)
    if isinstance(data, LineSegmentData):
        return plain_value(data.start), plain_value(data.end)
    if isinstance(data, RectData):
        return (data.min_x, data.min_y), (data.max_x, data.max_y)
    if isinstance(data, SizeData):
        return data.width, data.height
    if isinstance(data, ColorData):
        return data.r, data.g, data.b, data.a
    if isinstance(data, SphereData):
        return plain_value(data.center), data.radius
    if isinstance(data, CapsuleData):
        return plain_value(data.start), plain_value(data.end), data.radius
    if isinstance(data, CylinderData):
        return plain_value(data.center), data.radius, data.height
    if isinstance(data, ConeData):
        return plain_value(data.position), plain_value(data.direction), data.angle, data.distance
    if isinstance(data, AreaData):
        return (*(plain_value(corner) for corner in (data.p0, data.p1, data.p2, data.p3)), data.height, data.bottom)
    if isinstance(data, _GuidBytesData):
        return data.guid_str
    if isinstance(data, (Mat4Data, OBBData)):
        return tuple(data.values)
    if isinstance(data, RawBytesData):
        return data.raw_bytes
    if hasattr(data, "x"):
        return tuple(getattr(data, axis) for axis in "xyzw" if hasattr(data, axis))
    raise TypeError(f"an RSZ field of {type(data).__name__} has no plain value")


def root_fields(rsz) -> dict | None:
    return {name: plain_value(value, rsz.parsed_elements.__getitem__) for name, value in rsz.parsed_elements[
        rsz.object_table[0]].items()} if rsz.object_table else None


def typed_fields(rsz, type_name: str) -> dict:
    registry = rsz.type_registry
    return next(({name: plain_value(value, rsz.parsed_elements.__getitem__) for name, value in fields.items()}
                 for index, fields in rsz.parsed_elements.items()
                 if (registry.get_type_info(rsz.instance_infos[index].type_id) or {}).get("name") == type_name), {})


def user_path(rsz, value) -> str:
    if isinstance(value, UserDataData) and value.string:
        return value.string.rstrip("\0")
    instance = int(getattr(value, "value", 0 if value is None else value) or 0)
    return next((text.rstrip("\0") for info, text in rsz._rsz_userdata_str_map.items()
                 if info.instance_id == instance), "") if instance and rsz is not None else ""


__all__ = ["plain_fields", "plain_value", "root_fields", "typed_fields", "user_path"]
