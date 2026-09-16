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

usage: python add_pfb_element.py PFB OUTDIR --clone 10 --new-id 99 [--rotate-delta 0,0,10]
                                            [--rotate X,Y,Z] [--print-only]
"""
import argparse
import copy
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

from PySide6.QtWidgets import QApplication                                        # noqa: E402
from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData  # noqa: E402
from file_handlers.rsz.rsz_handler import RszHandler                              # noqa: E402
from file_handlers.rsz.rsz_object_operations import RszObjectOperations            # noqa: E402
from utils.hex_util import guid_le_to_str                                        # noqa: E402

REGISTRY = ROOT / 'resources/data/dumps/rszmhrise.json'
ELEMENT_TYPE = 'via.effect.script.EPVStandardData.Element'
ROOT_TYPE = 'via.effect.script.EPVStandardData'
SCALAR_ATTRS = ('value', 'x', 'y', 'z', 'w', 'r', 'g', 'b', 'a', 'guid_str', 'raw_bytes', 'orig_type')


class _Settings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _App:
    settings = _Settings({'rcol_json_path': str(REGISTRY)})
    _rsz_type_registry_override = None


def open_pfb(path):
    handler = RszHandler()
    handler.app = _App()
    handler.filepath = str(path)
    handler.read(path.read_bytes())
    handler.init_type_registry()
    return handler


def scalar_value(field):
    for attribute in ('value', 'guid_str', 'raw_bytes'):
        if hasattr(field, attribute):
            return getattr(field, attribute)
    if all(hasattr(field, attribute) for attribute in ('x', 'y', 'z')):
        return (round(float(field.x), 4), round(float(field.y), 4), round(float(field.z), 4))
    if all(hasattr(field, attribute) for attribute in ('r', 'g', 'b', 'a')):
        return (field.r, field.g, field.b, field.a)
    return repr(field)


def snapshot(rsz, registry, instance_id, depth=2):
    """Order-stable description of an instance tree (values only, no ids)."""
    fields = rsz.parsed_elements.get(instance_id) or {}
    out = {}
    for name, field in fields.items():
        if isinstance(field, (ObjectData, UserDataData)):
            out[name] = snapshot(rsz, registry, field.value, depth - 1) if field.value and depth else f'ref:{field.value}'
        elif isinstance(field, ArrayData):
            items = []
            for item in field.values:
                if isinstance(item, (ObjectData, UserDataData)) and getattr(item, 'value', 0):
                    items.append(snapshot(rsz, registry, item.value, depth - 1) if depth else f'ref:{item.value}')
                else:
                    items.append(scalar_value(item))
            out[name] = items
        else:
            out[name] = scalar_value(field)
    return out


def element_list(rsz, registry, root):
    """Ordered snapshot of the root's Elements array (element IDs repeat, so no dict)."""
    out = []
    for item in rsz.parsed_elements[root]['Elements'].values:
        element = getattr(item, 'value', 0)
        out.append((rsz.parsed_elements[element]['ID'].value, snapshot(rsz, registry, element)))
    return out


def find_element(rsz, root, element_id):
    """First element of the root's Elements array whose ID matches (IDs are not unique)."""
    matches = [getattr(item, 'value', 0) for item in rsz.parsed_elements[root]['Elements'].values
               if getattr(item, 'value', 0)
               and rsz.parsed_elements[item.value].get('ID') is not None
               and rsz.parsed_elements[item.value]['ID'].value == element_id]
    if not matches:
        raise SystemExit(f'element ID {element_id} not found')
    if len(matches) > 1:
        print(f'note: {len(matches)} elements share ID {element_id}, cloning the first')
    return matches[0]


def find_root(rsz, registry):
    for instance in range(1, len(rsz.instance_infos)):
        definition = registry.get_type_info(rsz.instance_infos[instance].type_id)
        if definition and definition['name'] == ROOT_TYPE:
            return instance
    raise SystemExit(f'{ROOT_TYPE} not found')


def copy_scalar(source, target):
    for attribute in SCALAR_ATTRS:
        if hasattr(source, attribute) and hasattr(target, attribute):
            setattr(target, attribute, copy.copy(getattr(source, attribute)))


def triple(text):
    parts = [float(value) for value in text.split(',')]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError('expected X,Y,Z')
    return parts


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('pfb', type=Path)
parser.add_argument('output_directory', type=Path, nargs='?')
parser.add_argument('--clone', type=int, required=True, help='source element ID to copy')
parser.add_argument('--new-id', type=int, required=True, help='ID for the new element')
parser.add_argument('--rotate-delta', type=triple, default=None, help='added to the cloned Rotation, X,Y,Z degrees')
parser.add_argument('--rotate', type=triple, default=None, help='absolute Rotation for the new element, X,Y,Z degrees')
parser.add_argument('--keep-guid', action='store_true', help='reuse the source GUID instead of a fresh one')
parser.add_argument('--print-only', action='store_true')
args = parser.parse_args()
delta = args.rotate_delta if args.rotate_delta is not None else (None if args.rotate else (0.0, 0.0, 10.0))
SOURCE = args.pfb.resolve()
source_bytes = SOURCE.read_bytes()

app = QApplication.instance() or QApplication([])
handler = open_pfb(SOURCE)
rsz = handler.rsz_file
registry = handler.type_registry
viewer = handler.create_viewer()
ops = RszObjectOperations(viewer)
root = find_root(rsz, registry)
before = element_list(rsz, registry, root)
source_element = find_element(rsz, root, args.clone)
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
if args.print_only:
    raise SystemExit(0)

output = handler.rebuild()

# --- verify against a freshly parsed copy
verifier = open_pfb(SOURCE)
verifier.read(output)
after_rsz = verifier.rsz_file
after_root = find_root(after_rsz, verifier.type_registry)
after = element_list(after_rsz, verifier.type_registry, after_root)
assert len(after) == len(before) + 1, (len(after), len(before))
for position, (element_id, description) in enumerate(before):
    assert after[position] == (element_id, description), f'element {position} (ID {element_id}) changed'
print(f'existing elements unchanged: {len(before)} (ordered, duplicates included)')
source_position = next(position for position, (element, _) in enumerate(before) if element == args.clone)
new_description = after[-1][1]
source_description = before[source_position][1]
assert after[-1][0] == args.new_id
for name, value in new_description.items():
    if name in ('ID', 'Rotation', 'GUID'):
        continue
    assert value == source_description[name], f'clone differs in {name}'
assert new_description['Rotation'] != source_description['Rotation'] or delta in (None, (0, 0, 0)), \
    'rotation was not changed'
print(f'new element ID={args.new_id} matches the source except ID/Rotation/GUID')
after_root_index = find_root(after_rsz, verifier.type_registry)
assert after_root_index == len(after_rsz.instance_infos) - 1, \
    f'container root must stay the last instance, got #{after_root_index} of {len(after_rsz.instance_infos)}'
print(f'container root is still the last instance: #{after_root_index} of {len(after_rsz.instance_infos)}')
verifier_handler_output = verifier.rebuild()
assert verifier_handler_output == output, 'not stable'

folder = args.output_directory.resolve()
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / SOURCE.name
candidate.write_bytes(output)
(folder / 'manifest.json').write_text(json.dumps(dict(
    source=str(SOURCE), source_sha256=hashlib.sha256(source_bytes).hexdigest(), source_size=len(source_bytes),
    candidate=str(candidate), candidate_sha256=hashlib.sha256(output).hexdigest(), candidate_size=len(output),
    cloned_from=args.clone, new_element_id=args.new_id, rotation=[rotation.x, rotation.y, rotation.z],
    rotate_delta=list(delta) if delta else None, rotate_absolute=list(args.rotate) if args.rotate else None,
    fresh_guid=not args.keep_guid, created_instances=len(created), elements_before=len(before),
    elements_after=len(after), size_delta=len(output) - len(source_bytes)), indent=2), encoding='utf-8')
print(f'wrote {candidate}  ({len(source_bytes):,} -> {len(output):,} B)')
