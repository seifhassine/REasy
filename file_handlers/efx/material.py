"""Typed views over native MDF property payloads stored inside EFX."""
import struct


def texture_index(prop):
    return struct.unpack('<4i', bytes(prop['rawValue']))[1]


def set_texture_index(prop, index):
    words = list(struct.unpack('<4i', bytes(prop['rawValue'])))
    words[1] = index
    prop['rawValue'] = list(struct.pack('<4i', *words))


def material_value(prop):
    if prop['parameterType'] == 3:
        return texture_index(prop)
    if prop['parameterType'] in (1, 2):
        return list(struct.unpack('<4f', bytes(prop['rawValue'])))
    return list(prop['rawValue'])


def set_material_value(prop, value):
    if prop.classname.rsplit('.', 1)[-1] != 'MdfProperty':
        raise ValueError('Select an MDF property')
    if prop['parameterType'] == 3:
        if type(value) is not int or value < 0:
            raise ValueError('Texture value must be a nonnegative texture slot')
        set_texture_index(prop, value)
    elif prop['parameterType'] in (1, 2):
        if not isinstance(value, (list, tuple)) or len(value) != 4 or any(type(v) not in (int, float) for v in value):
            raise ValueError('Material Float/Range values contain four floats')
        prop['rawValue'] = list(struct.pack('<4f', *value))
    else:
        raise ValueError('This material parameter has no known typed value')


def synchronize_textures(record):
    if not {'properties', 'texPaths'} <= record.fields.keys():
        return
    paths = record['texPaths']
    for prop in record['properties']:
        if prop['parameterType'] != 3:
            continue
        words = list(struct.unpack('<4i', bytes(prop['rawValue'])))
        if not 0 <= words[1] < len(paths):
            raise ValueError(f'Material texture slot {words[1]} is outside the texture table')
        words[0] = len(paths[words[1]].encode('utf-16le')) // 2 + 1
        prop['rawValue'] = list(struct.pack('<4i', *words))


def remap_textures(record, order):
    mapping = {old: new for new, old in enumerate(order) if old is not None}
    for prop in record['properties']:
        if prop['parameterType'] == 3:
            index = texture_index(prop)
            if index not in mapping:
                raise ValueError('Texture slot is still referenced by a material property')
            set_texture_index(prop, mapping[index])
