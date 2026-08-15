"""
Utility functions for RSZ field operations.

This module provides common field manipulation utilities to reduce code duplication.
"""

import copy
import math
import uuid

from file_handlers.rsz.rsz_data_types import (
    AABBData,
    ArrayData,
    GameObjectRefData,
    GuidData,
    ObjectData,
    RawBytesData,
    ResourceData,
    StructData,
    UserDataData,
    get_type_class,
    is_reference_type,
    is_array_type,
)
from utils.enum_manager import EnumManager


INTEGER_BOUNDS = {
    "S8Data": (-0x80, 0x7F),
    "U8Data": (0, 0xFF),
    "S16Data": (-0x8000, 0x7FFF),
    "U16Data": (0, 0xFFFF),
    "S32Data": (-0x80000000, 0x7FFFFFFF),
    "U32Data": (0, 0xFFFFFFFF),
    "S64Data": (-0x8000000000000000, 0x7FFFFFFFFFFFFFFF),
    "U64Data": (0, 0xFFFFFFFFFFFFFFFF),
}


VALUE_COMPONENTS = {
    "Vec2Data": ("x", "y"),
    "Float2Data": ("x", "y"),
    "PointData": ("x", "y"),
    "Int2Data": ("x", "y"),
    "Uint2Data": ("x", "y"),
    "Vec3Data": ("x", "y", "z"),
    "Vec3ColorData": ("x", "y", "z"),
    "Float3Data": ("x", "y", "z"),
    "PositionData": ("x", "y", "z"),
    "Int3Data": ("x", "y", "z"),
    "Uint3Data": ("x", "y", "z"),
    "Vec4Data": ("x", "y", "z", "w"),
    "Float4Data": ("x", "y", "z", "w"),
    "QuaternionData": ("x", "y", "z", "w"),
    "Int4Data": ("x", "y", "z", "w"),
    "Int4ColorData": ("x", "y", "z", "w"),
    "ColorData": ("r", "g", "b", "a"),
    "RangeData": ("min", "max"),
    "RangeIData": ("min", "max"),
    "SizeData": ("width", "height"),
    "RectData": ("min_x", "min_y", "max_x", "max_y"),
}


def create_default_field_value(
    data_class,
    original_type: str,
    is_array: bool = False,
    field_size: int = 1,
    *,
    has_embedded_rsz: bool = False,
):
    """Create the same default RSZ value used by the normal editor."""
    if is_array:
        return ArrayData([], data_class, original_type)
    if data_class is ObjectData:
        return ObjectData(0, original_type)
    if data_class is UserDataData:
        value = UserDataData(0, "", original_type)
        if has_embedded_rsz:
            value._needs_embedded_rsz = True
        return value
    if data_class is RawBytesData:
        return RawBytesData(bytes(field_size), field_size, original_type)
    try:
        return data_class(orig_type=original_type)
    except TypeError:
        return data_class()


def create_field_value_from_definition(field_def, *, has_embedded_rsz=False):
    """Create a field value directly from one registry field definition."""
    field_name = field_def.get("name", "")
    if not field_name:
        return None
    field_type = str(field_def.get("type", "unknown")).lower()
    field_size = int(field_def.get("size", 4))
    field_native = bool(field_def.get("native", False))
    field_array = bool(field_def.get("array", False))
    field_align = int(field_def.get("align", 4) or 4)
    original_type = str(field_def.get("original_type", "") or "")
    field_class = get_type_class(
        field_type,
        field_size,
        field_native,
        field_array,
        field_align,
        original_type,
        field_name,
    )
    field_obj = create_default_field_value(
        field_class,
        original_type,
        field_array,
        field_size,
        has_embedded_rsz=has_embedded_rsz,
    )
    return field_name, field_obj, original_type, field_array, field_class


def is_type_assignable(registry, actual_type: str, expected_type: str) -> bool:
    """Return whether an RSZ instance type satisfies a declared reference type."""
    actual = str(actual_type or "").strip()
    expected = str(expected_type or "").strip()
    if not actual:
        return False
    if not expected:
        return True
    if actual.casefold() == expected.casefold():
        return True
    parent_getter = getattr(registry, "getTypeParents", None)
    if not callable(parent_getter):
        return False
    return any(
        str(parent).casefold() == expected.casefold()
        for parent in parent_getter(actual)
    )


def validate_reference_type(registry, instance_infos, expected_type: str, instance_id: int):
    """Validate a non-null reference ID and return its concrete type name."""
    if instance_id == 0:
        return ""
    if not 0 < instance_id < len(instance_infos):
        raise ValueError("RSZ reference points outside the instance table.")
    type_info = registry.get_type_info(instance_infos[instance_id].type_id) if registry else None
    actual_type = str(type_info.get("name", "") or "") if type_info else ""
    if not is_type_assignable(registry, actual_type, expected_type):
        raise ValueError(
            f"RSZ reference type {actual_type or '<unknown>'} is not assignable to "
            f"{expected_type or '<unspecified>'}."
        )
    return actual_type


def contains_resource_value(value) -> bool:
    """Return whether a value tree contains a ResourceData field."""
    if isinstance(value, ResourceData):
        return True
    if isinstance(value, (ArrayData, StructData)):
        return any(contains_resource_value(item) for item in value.values)
    if isinstance(value, dict):
        return any(contains_resource_value(item) for item in value.values())
    return False


def _integer_value(value, label: str, bounds: tuple[int, int]) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer.")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label} must be an integer.")
    try:
        parsed = int(value, 0) if isinstance(value, str) else int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if not bounds[0] <= parsed <= bounds[1]:
        raise ValueError(f"{label} is outside its supported numeric range.")
    return parsed


def _float_value(value, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric.") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite.")
    return parsed


def _enum_value(data_obj, value):
    if not isinstance(value, str) or not getattr(data_obj, "orig_type", ""):
        return value
    members = EnumManager.instance().get_enum_values(data_obj.orig_type)
    matches = [
        member
        for member in members
        if str(member.get("name", "")).casefold() == value.casefold()
    ]
    return matches[0].get("value") if len(matches) == 1 else value


def _sequence(value, names, label: str):
    names = tuple(names)
    if isinstance(value, dict):
        if set(value) != set(names):
            raise ValueError(f"{label} must provide exactly: {', '.join(names)}")
        return {name: value[name] for name in names}
    if not isinstance(value, list) or len(value) != len(names):
        raise ValueError(f"{label} must contain {len(names)} values.")
    return dict(zip(names, value))


def coerce_field_value(
    template,
    value,
    *,
    instance_infos,
    userdata_strings,
    registry,
    label: str,
    context=None,
    allow_references: bool = False,
):
    """Coerce external data into an RSZ value using the shared type model."""
    memo = {}
    if context is not None:
        memo[id(context)] = context
    for attribute in ("_container_array", "_owning_context", "_container_context"):
        related = getattr(template, attribute, None)
        if related is not None:
            memo[id(related)] = related
    clone = copy.deepcopy(template, memo)
    class_name = clone.__class__.__name__

    if isinstance(clone, ObjectData):
        if not allow_references:
            raise ValueError(
                f"{label} is an RSZ reference and must use the normal structural editor workflow."
            )
        clone.value = _integer_value(value, label, (0, max(0, len(instance_infos) - 1)))
        validate_reference_type(registry, instance_infos, clone.orig_type, clone.value)
        return clone
    if isinstance(clone, UserDataData):
        if not allow_references:
            raise ValueError(
                f"{label} is RSZ userdata and must use the normal structural editor workflow."
            )
        instance_value = value.get("instance_id") if isinstance(value, dict) else value
        if isinstance(value, dict) and set(value) != {"instance_id"}:
            raise ValueError(f"{label} must contain only instance_id.")
        clone.value = _integer_value(
            instance_value, label, (0, max(0, len(instance_infos) - 1))
        )
        if clone.value and clone.value not in userdata_strings:
            raise ValueError(f"{label} must reference an existing userdata instance.")
        validate_reference_type(registry, instance_infos, clone.orig_type, clone.value)
        clone.string = userdata_strings.get(clone.value, "")
        return clone
    if isinstance(clone, (GuidData, GameObjectRefData)):
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"{label} must be a GUID.") from exc
        clone.guid_str = str(parsed)
        clone.raw_bytes = parsed.bytes_le
        return clone
    if isinstance(clone, RawBytesData):
        try:
            raw = bytes.fromhex(value) if isinstance(value, str) else bytes(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be hexadecimal bytes.") from exc
        if len(raw) != clone.field_size:
            raise ValueError(f"{label} must contain exactly {clone.field_size} bytes.")
        clone.raw_bytes = raw
        return clone
    if isinstance(clone, (ArrayData, StructData)):
        if not isinstance(value, list):
            raise ValueError(f"{label} must be an array.")
        clone.values = [
            new_collection_element(
                clone,
                item,
                instance_infos=instance_infos,
                userdata_strings=userdata_strings,
                registry=registry,
                label=f"{label}[{index}]",
                context=context,
                allow_references=allow_references,
                has_embedded_rsz=context is not None,
            )
            for index, item in enumerate(value)
        ]
        return clone
    if isinstance(clone, dict):
        if not isinstance(value, dict) or set(value) != set(clone):
            raise ValueError(f"{label} must provide every struct field.")
        return {
            name: coerce_field_value(
                child,
                value[name],
                instance_infos=instance_infos,
                userdata_strings=userdata_strings,
                registry=registry,
                label=f"{label}.{name}",
                context=context,
                allow_references=allow_references,
            )
            for name, child in clone.items()
        }
    if isinstance(clone, ResourceData) or class_name in {"StringData", "RuntimeTypeData"}:
        if not isinstance(value, str):
            raise ValueError(f"{label} must be text.")
        clone.value = value
        return clone
    if class_name == "BoolData":
        if not isinstance(value, bool):
            raise ValueError(f"{label} must be true or false.")
        clone.value = value
        return clone
    if class_name in INTEGER_BOUNDS:
        clone.value = _integer_value(
            _enum_value(clone, value), label, INTEGER_BOUNDS[class_name]
        )
        return clone
    if class_name in {"F32Data", "F64Data"}:
        clone.value = _float_value(value, label)
        return clone

    components = VALUE_COMPONENTS.get(class_name)
    if components:
        values = _sequence(value, components, label)
        integer = class_name.startswith(("Int", "Uint")) or class_name in {
            "ColorData",
            "RangeIData",
        }
        for name in components:
            if integer:
                if class_name == "ColorData":
                    bounds = (0, 0xFF)
                elif class_name.startswith("Uint"):
                    bounds = (0, 0xFFFFFFFF)
                else:
                    bounds = (-0x80000000, 0x7FFFFFFF)
                setattr(clone, name, _integer_value(values[name], f"{label}.{name}", bounds))
            else:
                setattr(clone, name, _float_value(values[name], f"{label}.{name}"))
        return clone
    if class_name in {"Mat4Data", "OBBData"}:
        expected = len(clone.values)
        if not isinstance(value, list) or len(value) != expected:
            raise ValueError(f"{label} must contain {expected} values.")
        clone.values = [_float_value(item, label) for item in value]
        return clone
    if isinstance(clone, AABBData):
        if not isinstance(value, dict) or set(value) != {"min", "max"}:
            raise ValueError(f"{label} must provide min and max.")
        for bound in ("min", "max"):
            values = _sequence(value[bound], ("x", "y", "z"), f"{label}.{bound}")
            target = getattr(clone, bound)
            for name, item in values.items():
                setattr(target, name, _float_value(item, f"{label}.{bound}.{name}"))
        return clone
    if class_name == "CapsuleData":
        if not isinstance(value, dict) or set(value) != {"start", "end", "radius"}:
            raise ValueError(f"{label} must provide start, end, and radius.")
        for endpoint in ("start", "end"):
            values = _sequence(value[endpoint], ("x", "y", "z"), f"{label}.{endpoint}")
            target = getattr(clone, endpoint)
            for name, item in values.items():
                setattr(target, name, _float_value(item, f"{label}.{endpoint}.{name}"))
        clone.radius = _float_value(value["radius"], f"{label}.radius")
        return clone
    if class_name in {"AreaData", "AreaDataOld"}:
        names = {"p0", "p1", "p2", "p3", "height", "bottom"}
        if not isinstance(value, dict) or set(value) != names:
            raise ValueError(f"{label} must provide four points, height, and bottom.")
        for point in ("p0", "p1", "p2", "p3"):
            values = _sequence(value[point], ("x", "y"), f"{label}.{point}")
            target = getattr(clone, point)
            target.x = _float_value(values["x"], f"{label}.{point}.x")
            target.y = _float_value(values["y"], f"{label}.{point}.y")
        clone.height = _float_value(value["height"], f"{label}.height")
        clone.bottom = _float_value(value["bottom"], f"{label}.bottom")
        return clone
    raise ValueError(f"RSZ storage type is not editable: {class_name}")


def new_collection_element(
    collection,
    value,
    *,
    instance_infos,
    userdata_strings,
    registry,
    label: str,
    context=None,
    allow_references: bool = False,
    has_embedded_rsz: bool = False,
):
    """Create and coerce one collection element using registry definitions."""
    if isinstance(collection, StructData):
        type_info, _ = registry.find_type_by_name(collection.orig_type) if registry else (None, None)
        if not type_info:
            raise ValueError(f"RSZ struct type was not found: {collection.orig_type}")
        template = {}
        for field_def in type_info.get("fields", ()):
            created = create_field_value_from_definition(
                field_def, has_embedded_rsz=has_embedded_rsz
            )
            if created is not None:
                field_name, field_obj, _original, _is_array, _field_class = created
                template[field_name] = field_obj
    else:
        element_class = getattr(collection, "element_class", None)
        if element_class is None:
            raise ValueError(f"RSZ array has no element type: {label}")
        if element_class is RawBytesData:
            raise ValueError(
                "Raw-byte array insertion requires replacing the complete array field."
            )
        template = create_default_field_value(
            element_class,
            collection.orig_type,
            has_embedded_rsz=has_embedded_rsz,
        )
    return coerce_field_value(
        template,
        value,
        instance_infos=instance_infos,
        userdata_strings=userdata_strings,
        registry=registry,
        label=label,
        context=context,
        allow_references=allow_references,
    )


def iter_field_reference_entries(fields):
    """
    Yield references from direct fields and one level of ArrayData elements.

    Yields:
        tuple: (field_name, reference_object, array_index)
        array_index is None for direct fields.
    """
    for field_name, field_data in fields.items():
        if is_reference_type(field_data):
            yield field_name, field_data, None
        elif is_array_type(field_data):
            for index, element in enumerate(field_data.values):
                if is_reference_type(element):
                    yield field_name, element, index


def get_reference_id_and_type(obj):
    """Return (instance_id, reference_type) for ObjectData/UserDataData references."""
    if isinstance(obj, ObjectData) and obj.value > 0:
        return obj.value, "object"
    if isinstance(obj, UserDataData) and obj.value > 0:
        return obj.value, "userdata"
    return 0, None


def create_field_from_definition(viewer, field_def):
    """
    Build a field object from a type-registry field definition.

    Returns:
        tuple: (field_name, field_obj, field_orig_type, field_array, field_class)
        or None when the definition has no field name.
    """
    return create_field_value_from_definition(
        field_def,
        has_embedded_rsz=bool(getattr(getattr(viewer, "scn", None), "has_embedded_rsz", False)),
    )


def iter_field_references(fields):
    """Yield all direct and array-element reference objects in fields."""
    for _, ref_obj, _ in iter_field_reference_entries(fields):
        yield ref_obj


def update_field_references(fields, reference_updater):
    """
    Update all references in fields using the provided updater function.
    
    Args:
        fields: Dictionary of field_name -> field_data
        reference_updater: Function that takes (field_data) and updates its references
    """
    for ref_obj in iter_field_references(fields):
        reference_updater(ref_obj)


def collect_reference_values(fields, reference_type=None, positive_only=True):
    """
    Collect reference values from fields.

    Args:
        fields: Dictionary of field_name -> field_data
        reference_type: Optional class to filter by (ObjectData/UserDataData)
        positive_only: When True, only collect values greater than 0

    Returns:
        set: Collected reference values
    """
    values = set()
    for ref_obj in iter_field_references(fields):
        if reference_type is not None and not isinstance(ref_obj, reference_type):
            continue
        if positive_only and ref_obj.value <= 0:
            continue
        values.add(ref_obj.value)
    return values


def collect_object_reference_values(fields, positive_only=True):
    """Collect ObjectData reference values from fields."""
    return collect_reference_values(fields, ObjectData, positive_only)


def collect_userdata_reference_values(fields, positive_only=True):
    """Collect UserDataData reference values from fields."""
    return collect_reference_values(fields, UserDataData, positive_only)


def update_references_with_mapping(fields, id_mapping, deleted_ids=None):
    """
    Update all references in fields based on ID mapping and deleted IDs.
    
    Args:
        fields: Dictionary of field_name -> field_data
        id_mapping: Dictionary mapping old IDs to new IDs
        deleted_ids: Optional set of deleted IDs (will be set to 0)
    """
    def updater(ref_obj):
        if ref_obj.value > 0:
            if deleted_ids and ref_obj.value in deleted_ids:
                ref_obj.value = 0
            elif ref_obj.value in id_mapping:
                ref_obj.value = id_mapping[ref_obj.value]
    
    update_field_references(fields, updater)


def update_references_of_type(fields, id_mapping, reference_type):
    """
    Update references of a specific reference class using an ID mapping.

    Args:
        fields: Dictionary of field_name -> field_data
        id_mapping: Dictionary mapping old IDs to new IDs
        reference_type: Reference class to update
    """
    for ref_obj in iter_field_references(fields):
        if isinstance(ref_obj, reference_type) and ref_obj.value in id_mapping:
            ref_obj.value = id_mapping[ref_obj.value]


def shift_references_above_threshold(fields, threshold, offset=1):
    """
    Shift all references above a threshold by the given offset.
    
    Args:
        fields: Dictionary of field_name -> field_data
        threshold: References >= this value will be shifted
        offset: Amount to shift by (default: 1)
    """
    def updater(ref_obj):
        if ref_obj.value >= threshold:
            ref_obj.value += offset
    
    update_field_references(fields, updater)


