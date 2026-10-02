"""JSON field views of the client's RSZ data, without UI/global game state."""
from file_handlers.rsz.rsz_data_types import ArrayData, StructData, ObjectData, UserDataData
from file_handlers.rsz.utils.rsz_field_utils import VALUE_COMPONENTS
from utils.enum_manager import registry_enums


def scalar(field):
    if isinstance(field, (ArrayData, StructData)):
        return [scalar(item) for item in field.values]
    if isinstance(field, dict):
        return {name: scalar(value) for name, value in field.items()}
    for attribute in ('value', 'guid_str'):
        if hasattr(field, attribute):
            value = getattr(field, attribute)
            return value.rstrip('\0') if isinstance(value, str) else value
    if hasattr(field, 'raw_bytes'):
        return field.raw_bytes.hex()
    components = VALUE_COMPONENTS.get(type(field).__name__)
    if components:
        return {name: getattr(field, name) for name in components}
    components = {
        'AABBData': ('min', 'max'), 'CapsuleData': ('start', 'end', 'radius'),
        'AreaData': ('p0', 'p1', 'p2', 'p3', 'height', 'bottom'),
        'AreaDataOld': ('p0', 'p1', 'p2', 'p3', 'height', 'bottom'),
    }.get(type(field).__name__)
    if components:
        return {name: scalar(value) if hasattr(value, 'orig_type') else value
                for name in components for value in (getattr(field, name),)}
    raise ValueError(f'No structured value representation for {type(field).__name__}')


def field_record(field, registry=None, enums=None, *, native_type=None):
    """Keep raw values, with exact client enum matches and explicit child records.

    ``enums`` collects only used enum definitions for the enclosing query result.
    No registry is needed for write/read-back value comparisons.
    """
    native_type = native_type or getattr(field, 'orig_type', '')
    reference = isinstance(field, (ObjectData, UserDataData))
    array = isinstance(field, ArrayData)
    result = {'type': type(field).__name__, 'native_type': native_type,
              'value': scalar(field), 'reference': reference or
              (array and field.element_class in (ObjectData, UserDataData))}
    if isinstance(field, dict):
        result['fields'] = fields_record(field, registry, enums)
    elif isinstance(field, (ArrayData, StructData)):
        result['count'] = len(field.values)
        if array and registry is not None and enums is not None and not result['reference']:
            members = registry_enums(registry.json_path).get(native_type, [])
            if members:
                enums[native_type] = members
        result['items'] = [fields_record(item, registry, enums) if isinstance(item, dict)
                           else field_record(item, registry, enums,
                                             native_type=getattr(item, 'orig_type', '') or native_type)
                           for item in field.values]
    elif registry is not None and not reference and isinstance(result['value'], int) and not isinstance(result['value'], bool):
        members = registry_enums(registry.json_path).get(native_type, [])
        if members:
            if enums is not None:
                enums[native_type] = members
            value = result['value']
            names = [member['name'] for member in members if member['value'] == value]
            # The client also recognizes the opposite signedness of a 32-bit value.
            if not names:
                alternate = value
                if result['type'] == 'U32Data' and value > 0x7FFFFFFF:
                    alternate = value - 0x100000000
                elif result['type'] == 'S32Data' and value < 0:
                    alternate = value + 0x100000000
                names = [member['name'] for member in members if member['value'] == alternate]
                if names:
                    value = alternate
            result.update(enum_name=names[0] if names else None, enum_names=names,
                          enum_value=value, enum_matched=bool(names))
    return result


def fields_record(fields, registry=None, enums=None):
    return {name: field_record(value, registry, enums) for name, value in fields.items()}
