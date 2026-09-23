"""Add a new effect Element to an MHR PFB by deep-cloning an existing one.

A complete element is an instance of ``via.effect.script.EPVStandardData.Element`` plus its
nested sub-structures, which live in Object arrays and therefore have to be cloned explicitly:

* ``via.effect.script.EPVDataElement.GroupInfo``   (``GroupInfoList``)
* ``via.effect.script.GroupNameParameter``          (``GroupNameParameters``)
* ``via.effect.script.EffectCustomExternParameter`` (``ExternParameters``)
* ``via.effect.script.EffectManager.LODInfo``       (``LODLevels``)

The clone keeps the same effect resource (``v0``), gets a fresh GUID and a new ``ID``, and its
rotation can be nudged (``--rotate-delta``, e.g. a roll about the front-back axis) so a move can
reuse another move's effect under a different angle.  Everything else is verified unchanged.

"""
import argparse
import copy
import uuid
from pathlib import Path


from PySide6.QtWidgets import QApplication
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData
from file_handlers.rsz.rsz_handler import RszHandler
from file_handlers.rsz.rsz_object_operations import RszObjectOperations
from utils.hex_util import guid_le_to_str

from .common import open_pfb, scalar_value, snapshot, find_root, triple
from tools.cli.runtime import qt_application
ELEMENT_TYPE = 'via.effect.script.EPVStandardData.Element'
SCALAR_ATTRS = ('value', 'x', 'y', 'z', 'w', 'r', 'g', 'b', 'a', 'guid_str', 'raw_bytes', 'orig_type')


def element_list(rsz, registry, root):
    """Ordered snapshot of the root's Elements array (element IDs repeat, so no dict)."""
    out = []
    for item in rsz.parsed_elements[root]['Elements'].values:
        element = getattr(item, 'value', 0)
        out.append((rsz.parsed_elements[element]['ID'].value, snapshot(rsz, element)))
    return out


def find_element(rsz, root, element_id, position=None):
    """Resolve an element ID, using its array position when the ID is shared."""
    matches = [(index, getattr(item, 'value', 0)) for index, item in enumerate(rsz.parsed_elements[root]['Elements'].values)
               if getattr(item, 'value', 0)
               and (position is None or position == index)
               and rsz.parsed_elements[item.value].get('ID') is not None
               and rsz.parsed_elements[item.value]['ID'].value == element_id]
    if not matches:
        raise SystemExit(f'element ID {element_id} not found')
    if len(matches) > 1:
        raise ValueError(f'Element ID {element_id} is shared; use --clone-position')
    return matches[0]


def copy_scalar(source, target):
    for attribute in SCALAR_ATTRS:
        if hasattr(source, attribute) and hasattr(target, attribute):
            setattr(target, attribute, copy.copy(getattr(source, attribute)))


def configure(parser):
    parser.add_argument('--clone', type=int, required=True, help='source element ID to copy')
    parser.add_argument('--clone-position', type=int, help='array position, for duplicate element IDs')
    parser.add_argument('--new-id', type=int, required=True, help='ID for the new element')
    parser.add_argument('--rotate-delta', type=triple, default=None, help='added to the cloned Rotation, X,Y,Z degrees')
    parser.add_argument('--rotate', type=triple, default=None, help='absolute Rotation for the new element, X,Y,Z degrees')
    parser.add_argument('--keep-guid', action='store_true', help='reuse the source GUID instead of a fresh one')


def run(args):
    delta = tuple(args.rotate_delta) if args.rotate_delta is not None else None
    SOURCE = args.source.resolve()
    source_bytes = SOURCE.read_bytes()

    app = qt_application()
    handler = open_pfb(SOURCE)
    rsz = handler.rsz_file
    registry = handler.type_registry
    viewer = handler.create_viewer()
    ops = RszObjectOperations(viewer)
    root = find_root(rsz, registry)
    before = element_list(rsz, registry, root)
    source_position, source_element = find_element(rsz, root, args.clone, args.clone_position)
    print(f'{SOURCE.name}: {len(before)} elements, root #{root}, clone source ID={args.clone} -> #{source_element}')
    if any(element_id == args.new_id for element_id, _ in before):
        raise SystemExit(f'element ID {args.new_id} already exists')

    created = []


    def clone_instance(source_id):
        """Create a same-type instance and copy every field value (children first, as natives do)."""
        source_fields = rsz.parsed_elements[source_id]
        definition = registry.get_type_info(rsz.instance_infos[source_id].type_id)
        planned = {}
        for name, field in source_fields.items():
            if isinstance(field, ArrayData) and field.element_class is ObjectData:
                planned[name] = [ObjectData(value=clone_instance(item.value) if getattr(item, 'value', 0) else 0,
                                            orig_type=getattr(item, 'orig_type', ''))
                                 for item in field.values]
            elif isinstance(field, (ObjectData, UserDataData)) and field.value:
                planned[name] = clone_instance(field.value)
        # The RSZ instance table is written "referencee first": an instance follows everything it
        # references, so the container root -- which references every element through its Elements
        # array -- is always the LAST instance.  That holds for every native PFB and for hand-made
        # ones; appending the clone at the end instead pushes it *behind* the root and the game then
        # refuses to resolve it (container 150, element 99 stays silent while element 15 works).
        # Inserting at the root's current index keeps the root last.
        new_id = ops._create_object_instance_with_nested_objects(definition, rsz.instance_infos[source_id].type_id,
                                                                 find_root(rsz, registry))
        created.append(new_id)
        target_fields = rsz.parsed_elements[new_id]
        for name, field in source_fields.items():
            target = target_fields.get(name)
            if target is None:
                continue
            if isinstance(field, ArrayData):
                target.element_class = field.element_class
                target.orig_type = field.orig_type
                if name in planned:
                    target.values = planned[name]
                else:
                    target.values = []
                    for item in field.values:
                        clone = type(item)()
                        copy_scalar(item, clone)
                        target.values.append(clone)
            elif isinstance(field, (ObjectData, UserDataData)):
                target.value = planned.get(name, 0)
            else:
                copy_scalar(field, target)
        return new_id


    new_element = clone_instance(source_element)
    fields = rsz.parsed_elements[new_element]
    fields['ID'].value = args.new_id
    rotation = fields['Rotation']
    if args.rotate is not None:
        rotation.x, rotation.y, rotation.z = args.rotate
    elif delta is not None:
        rotation.x += delta[0]
        rotation.y += delta[1]
        rotation.z += delta[2]
    if not args.keep_guid:
        raw = uuid.uuid4().bytes_le
        fields['GUID'].raw_bytes = raw
        fields['GUID'].guid_str = guid_le_to_str(raw)
    print(f'cloned {len(created)} instance(s); new element #{new_element}')
    print(f'  v0      = {[scalar_value(v) for v in fields["v0"].values]}')
    print(f'  ID      = {fields["ID"].value}')
    print(f'  Rotation= ({rotation.x:g}, {rotation.y:g}, {rotation.z:g})'
          + (f'   (delta {delta})' if delta else ''))
    print('  sub-structures: ' + ', '.join(
        f'{name}={len(fields[name].values)}'
        for name in ('GroupInfoList', 'GroupNameParameters', 'ExternParameters', 'LODLevels')))

    root = find_root(rsz, registry)          # the root moved forward when the clone was inserted before it
    elements = rsz.parsed_elements[root]['Elements']
    elements.values.append(ObjectData(value=new_element, orig_type=elements.orig_type))
    print(f'root Elements: {len(elements.values) - 1} -> {len(elements.values)}')

    output = handler.rebuild()

    # --- verify against a freshly parsed copy
    verifier = open_pfb(SOURCE, output)
    after_rsz = verifier.rsz_file
    after_root = find_root(after_rsz, verifier.type_registry)
    after = element_list(after_rsz, verifier.type_registry, after_root)
    assert len(after) == len(before) + 1, (len(after), len(before))
    for position, (element_id, description) in enumerate(before):
        assert after[position] == (element_id, description), f'element {position} (ID {element_id}) changed'
    print(f'existing elements unchanged: {len(before)} (ordered, duplicates included)')
    new_description = after[-1][1]
    source_description = before[source_position][1]
    assert after[-1][0] == args.new_id
    for name, value in new_description.items():
        if name in ('ID', 'Rotation', 'GUID'):
            continue
        assert value == source_description[name], f'clone differs in {name}'
    expected_rotation = tuple(round(float(getattr(rotation, axis)), 4) for axis in ('x', 'y', 'z'))
    assert new_description['Rotation'] == expected_rotation, 'rotation differs from the authored value'
    print(f'new element ID={args.new_id} matches the source except ID/Rotation/GUID')
    after_root_index = find_root(after_rsz, verifier.type_registry)
    assert after_root_index == len(after_rsz.instance_infos) - 1, \
        f'container root must stay the last instance, got #{after_root_index} of {len(after_rsz.instance_infos)}'
    print(f'container root is still the last instance: #{after_root_index} of {len(after_rsz.instance_infos)}')
    verifier_handler_output = verifier.rebuild()
    assert verifier_handler_output == output, 'not stable'


    return bytes(output)
