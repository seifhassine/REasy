"""EPV effect-element operations on native PFB models, without a viewer."""
import uuid

from .graph_copy import copy_graph
from .rsz_data_types import ObjectData
from utils.hex_util import guid_le_to_str

ROOT_TYPE = 'via.effect.script.EPVStandardData'


def find_root(rsz, registry=None):
    registry = registry or rsz.type_registry
    roots = [i for i, info in enumerate(rsz.instance_infos)
             if i and (registry.get_type_info(info.type_id) or {}).get('name') == ROOT_TYPE]
    if len(roots) != 1:
        raise ValueError(f'Expected one {ROOT_TYPE}, found {len(roots)}')
    return roots[0]


def find_element(rsz, element_id, position=None):
    array = rsz.parsed_elements[find_root(rsz)]['Elements']
    matches = [(i, item.value) for i, item in enumerate(array.values)
               if item.value and (position is None or i == position)
               and rsz.parsed_elements[item.value]['ID'].value == element_id]
    if len(matches) != 1:
        raise ValueError(f'Element ID {element_id} resolves to {len(matches)} entries; select its array position')
    return matches[0]


def clone_element(rsz, element_id, new_id, *, position=None, rotation=None,
                  rotation_delta=None, keep_guid=False):
    root = find_root(rsz)
    elements = rsz.parsed_elements[root]['Elements']
    if any(rsz.parsed_elements[item.value]['ID'].value == new_id for item in elements.values if item.value):
        raise ValueError(f'Element ID {new_id} already exists')
    _, source = find_element(rsz, element_id, position)
    copied = copy_graph(rsz, rsz, [source], insertion=root)
    instance = copied.roots[0]
    fields = rsz.parsed_elements[instance]
    fields['ID'].value = new_id
    if rotation is not None:
        fields['Rotation'].x, fields['Rotation'].y, fields['Rotation'].z = rotation
    elif rotation_delta is not None:
        for axis, delta in zip(('x', 'y', 'z'), rotation_delta, strict=True):
            setattr(fields['Rotation'], axis, getattr(fields['Rotation'], axis) + delta)
    if not keep_guid:
        raw = uuid.uuid4().bytes_le
        fields['GUID'].raw_bytes = raw
        fields['GUID'].guid_str = guid_le_to_str(raw)
    elements.values.append(ObjectData(value=instance, orig_type=elements.orig_type))
    return instance
