"""Explicit complete-document construction for new and embedded EFX resources."""
import struct

from .efx_file import EfxFile, EfxEntry, EfxAttribute, EfxBone, EfxField, EfxRecord, EFX_MAGIC, schema_for_version
from .metadata import create_metadata


def document_from_data(version, payload, factory, coerce):
    if not isinstance(payload, dict) or payload.get('version', version) != version:
        raise ValueError('Embedded document must use the enclosing EFX version')
    dimension = payload.get('dimension', 1)
    if dimension not in (0, 1):
        raise ValueError('EFX dimension must be 0 or 1')
    document = EfxFile.from_bytes(struct.pack('<4s11i2i', EFX_MAGIC, dimension, *([0] * 12)), version=version)
    flags = coerce('U32', payload.get('header_flags', 0), {})
    document.header[11] = flags if flags < 0x80000000 else flags - 0x100000000
    for bone in payload.get('bones', []):
        document.bones.append(EfxBone(coerce('String', bone['name'], {}), coerce('U32', bone.get('value', 0), {})))
    for category in ('expression_parameters', 'field_parameters'):
        setattr(document, category, [create_metadata(category, version, row, factory, coerce) for row in payload.get(category, [])])
    for category in ('bones', 'expression_parameters', 'field_parameters'):
        names = [item.name for item in getattr(document, category)]
        if len(set(names)) != len(names):
            raise ValueError(f'Duplicate names in {category}')
    schema = schema_for_version(version)
    serial = 1
    for category in ('entries', 'actions'):
        for i, row in enumerate(payload.get(category, [])):
            entry = EfxEntry(coerce('S32', row.get('index', i + 1 if category == 'entries' else 0), {}),
                             coerce('String', row['name'], {'StringEncoding': 'utf-8'}),
                             coerce('S32', row.get('assignment', 2), {}) if category == 'entries' else 0, 0, [])
            for item in row.get('attributes', []):
                definition = schema['AttributeTypes'][item['type']]
                body = factory(version, definition['Classname'], item['fields'], definition)
                entry.attributes.append(EfxAttribute(definition['TypeID'], definition['Name'], serial, body))
                serial += 1
            getattr(document, category).append(entry)
        if category == 'entries' and len({entry.index for entry in document.entries}) != len(document.entries):
            raise ValueError('Embedded entry indices must be unique')
    for row in payload.get('groups', []):
        members = row.get('indices', row.get('fields', {}).get('indices', []))
        if not all(type(i) is int and 0 <= i < len(document.entries) for i in members):
            raise ValueError('Group members must be entry positions')
        document.groups.append(EfxRecord('EffectGroup', {
            'utf16Hash': EfxField('utf16Hash', 'U32', 0), 'utf8Hash': EfxField('utf8Hash', 'U32', 0),
            'count': EfxField('count', 'S32', len(members), {'CountOf': 'indices'}),
            'indices': EfxField('indices', 'Array', list(members), {'FieldType': 'S32', 'IsArray': True, 'ArrayLength': ['count']}),
        }, {}, version, name=coerce('String', row['name'], {'StringEncoding': 'utf-8'})))
    if 'uvar_groups' in payload:
        if len(payload['uvar_groups']) != 2:
            raise ValueError('An EFX document has two UVAR binding slots')
        document.uvar_groups = [create_metadata('uvar_groups', version, row, factory, coerce) for row in payload['uvar_groups']]
    from .graph import scopes
    for _, child, parent in scopes(document):
        if parent is not None:
            child.inherits_bones = True
            child.parent_bones = parent.effective_bones
    for serial, attribute in enumerate(document.all_attributes(recursive=True), 1):
        attribute.unique_id = serial
    return document
