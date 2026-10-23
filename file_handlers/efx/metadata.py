"""Construct named parameters and UVAR bindings without guessing unknown fields."""
import copy
import struct

from .efx_file import EfxField, EfxRecord, EfxFile, signature
from utils.hash_util import murmur3_hash, murmur3_hash_utf16le


FIELD_PATH_TYPES = frozenset((110, 144, 183, 184, 202, 194, 215, 217))
PARAMETER_TYPES = {0: 'Float', 1: 'Color', 2: 'Range', 3: 'Float2'}


def parameter_value(record):
    kind = record['type']
    if kind == 1:
        field = record.fields['value1']
        raw = field.raw if field.unchanged() and field.raw else struct.pack('<f', field.value)
        return struct.unpack('<I', raw)[0]
    if kind == 0:
        return record['value1']
    return [record[f'value{i}'] for i in range(1, 4 if kind == 2 else 3)]


def set_parameter_value(record, value, coerce):
    kind = record['type']
    if kind == 1:
        value = coerce('U32', value, {})
        raw = struct.pack('<I', value)
        float_value = struct.unpack('<f', raw)[0]
        record.fields['value1'] = EfxField('value1', 'F32', float_value, raw=raw, original=signature(float_value))
    else:
        count = {0: 1, 2: 3, 3: 2}.get(kind)
        if count is None:
            raise ValueError('Unknown expression parameter type; edit its explicit raw fields')
        values = [value] if count == 1 else value
        if not isinstance(values, (list, tuple)) or len(values) != count:
            raise ValueError(f'Parameter value requires {count} component(s)')
        for i, item in enumerate(values, 1):
            record[f'value{i}'] = coerce('F32', item, {})


def create_metadata(category, version, payload, factory, coerce):
    if not isinstance(payload, dict):
        raise ValueError('Metadata requires an object')
    if category == 'uvar_groups':
        kind = coerce('S32', payload['type'], {})
        if kind not in (0, 1, 2):
            raise ValueError('UVAR binding type must be 0, 1, or 2')
        fields = {'type': EfxField('type', 'S32', kind)}
        if kind == 2:
            for key in ('path', 'group'):
                descriptor = {'StringEncoding': 'utf-16le', 'StringLengthIsBytes': True}
                fields[key] = EfxField(key, 'String', coerce('String', payload[key], descriptor), descriptor)
        return EfxRecord('UserVariableGroup', fields, {}, version)
    name = coerce('String', payload['name'], {'StringEncoding': 'utf-8'})
    values = dict(payload.get('fields', {}))
    if category == 'expression_parameters':
        kind = payload.get('type', values.get('type', 0))
        descriptors = [{'Name': key, 'FieldType': dtype} for key, dtype in (
            ('utf16Hash', 'U32'), ('utf8Hash', 'U32'), ('type', 'S32'),
            ('value1', 'F32'), ('value2', 'F32'), ('value3', 'F32'))]
        values = {'value1': 0., 'value2': 0., 'value3': 0., **values,
                  'utf16Hash': murmur3_hash_utf16le(name), 'utf8Hash': murmur3_hash(name.encode('utf-8')), 'type': kind}
        record = factory(version, 'ExpressionParameter', values, {'Fields': descriptors})
        if 'value' in payload:
            set_parameter_value(record, payload['value'], coerce)
    elif category == 'field_parameters':
        kind = values['type']
        descriptors = [{'Name': key, 'FieldType': dtype} for key, dtype in (
            ('unknown0', 'U32'), ('nameHash', 'U32'), ('unknown2', 'U32'), ('type', 'U32'),
            ('unknown4', 'U32'), ('value1', 'S32'))]
        values['nameHash'] = murmur3_hash(name.encode('utf-8'))
        if kind == 196:
            descriptors[-1].update(Flag='StringLength', FlagTarget='filePath')
            descriptors.append({'Name': 'filePath', 'FieldType': 'String', 'StringEncoding': 'utf-16le', 'StringLength': ['value1']})
        else:
            descriptors += [{'Name': key, 'FieldType': dtype} for key, dtype in (
                ('value2', 'U32'), ('value3', 'U32'), ('value4', 'F32'), ('value5', 'F32'), ('value6', 'F32'))]
            if kind in FIELD_PATH_TYPES:
                descriptors.append({'Name': 'filePath', 'FieldType': 'String', 'StringEncoding': 'utf-16le'})
        record = factory(version, 'FieldParameter', values, {'Fields': descriptors})
    else:
        raise ValueError(f'Unknown metadata collection {category}')
    record.name = name
    return record


def preserve_unchanged(original, replacement, bone_map=None):
    """Retain native NaN payloads and string terminators when editing a whole record."""
    if isinstance(original, EfxFile) and isinstance(replacement, EfxFile):
        replacement.names = copy.deepcopy(original.names)
        replacement.inherits_bones = original.inherits_bones
        bone_map = dict(bone_map or {id(b): b for b in original.parent_bones})
        replacement.parent_bones = [bone_map.get(id(b), b) for b in original.parent_bones]
        for old, new in zip(original.bones, replacement.bones):
            new.name_hash, new.original_name = old.name_hash, old.original_name
            bone_map[id(old)] = new
        for category in ('expression_parameters', 'field_parameters', 'groups', 'uvar_groups'):
            for old, new in zip(getattr(original, category), getattr(replacement, category)):
                preserve_unchanged(old, new, bone_map)
        for category in ('entries', 'actions'):
            old_entries = getattr(original, category)
            for position, entry in enumerate(getattr(replacement, category)):
                previous = next((e for e in old_entries if e.index == entry.index), None) if category == 'entries' else old_entries[position] if position < len(old_entries) else None
                if previous is None:
                    continue
                entry.original_name, entry.name_hash = previous.original_name, previous.name_hash
                previous_attributes = {a.type_id: a for a in previous.attributes}
                for attr in entry.attributes:
                    old = previous_attributes.get(attr.type_id)
                    if old is None:
                        continue
                    attr.unique_id = old.unique_id
                    preserve_unchanged(old.body, attr.body, bone_map)
                    for slot, bone in enumerate(old.bone_bindings):
                        name = old.binding_name if slot == 0 else old.extra_binding_names[slot - 1]
                        attr.set_bone_reference(slot, bone_map.get(id(bone), bone), name)
        return replacement
    if not isinstance(original, EfxRecord) or not isinstance(replacement, EfxRecord) or original.classname != replacement.classname:
        return replacement
    replacement.original_name = original.original_name
    if original.name is not None and original.name == replacement.name:
        for name in ('utf8Hash', 'utf16Hash', 'nameHash'):
            if name in original.fields and name in replacement.fields:
                replacement.fields[name] = copy.deepcopy(original.fields[name])
    for name, new in replacement.fields.items():
        old = original.fields.get(name)
        if old is None or old.kind != new.kind:
            continue
        if isinstance(new.value, (EfxRecord, EfxFile)):
            preserve_unchanged(old.value, new.value, bone_map)
        elif new.kind == 'Array':
            if len(old.value) == len(new.value):
                new.elements = copy.deepcopy(old.elements)
                for a, b in zip(old.value, new.value):
                    preserve_unchanged(a, b, bone_map)
        elif signature(old.value) == signature(new.value) and (not new.raw or new.raw == old.raw):
            new.raw, new.original, new.terminators = old.raw, old.original, old.terminators
    return replacement
