"""Edit fields of an existing PFB effect element (e.g. nudge its Rotation in place).

The element ID must be one the referenced ``.efx`` actually declares - the id is the effect id
inside that file, so an invented id resolves to nothing.  Editing an existing element is therefore
the way to retune an effect (all movers referencing that element id share the change).

usage: python edit_pfb_element.py PFB OUTDIR --id 15 [--rotate 0,0,-80] [--rotate-delta 0,0,10]
                                             [--joint Cog] [--unparent-frame 7] [--print-only]
"""
import argparse
import copy
import hashlib
import json
import os
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData  # noqa: E402
from file_handlers.rsz.rsz_handler import RszHandler                              # noqa: E402

REGISTRY = ROOT / 'resources/data/dumps/rszmhrise.json'
ROOT_TYPE = 'via.effect.script.EPVStandardData'


class _Settings(dict):
    def get(self, key, default=None):
        return dict.get(self, key, default)


class _App:
    settings = _Settings({'rcol_json_path': str(REGISTRY)})
    _rsz_type_registry_override = None


def open_pfb(path, data=None):
    handler = RszHandler()
    handler.app = _App()
    handler.filepath = str(path)
    handler.read(path.read_bytes() if data is None else data)
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


def snapshot(rsz, instance_id, depth=2):
    out = {}
    for name, field in (rsz.parsed_elements.get(instance_id) or {}).items():
        if isinstance(field, (ObjectData, UserDataData)):
            out[name] = snapshot(rsz, field.value, depth - 1) if field.value and depth else f'ref:{field.value}'
        elif isinstance(field, ArrayData):
            items = []
            for item in field.values:
                if isinstance(item, (ObjectData, UserDataData)) and getattr(item, 'value', 0):
                    items.append(snapshot(rsz, item.value, depth - 1) if depth else f'ref:{item.value}')
                else:
                    items.append(scalar_value(item))
            out[name] = items
        else:
            out[name] = scalar_value(field)
    return out


def find_root(rsz, registry):
    for instance in range(1, len(rsz.instance_infos)):
        definition = registry.get_type_info(rsz.instance_infos[instance].type_id)
        if definition and definition['name'] == ROOT_TYPE:
            return instance
    raise SystemExit(f'{ROOT_TYPE} not found')


def element_list(rsz, root):
    out = []
    for item in rsz.parsed_elements[root]['Elements'].values:
        element = getattr(item, 'value', 0)
        out.append((element, rsz.parsed_elements[element]['ID'].value, snapshot(rsz, element)))
    return out


def triple(text):
    parts = [float(value) for value in text.split(',')]
    if len(parts) != 3:
        raise argparse.ArgumentTypeError('expected X,Y,Z')
    return parts


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('pfb', type=Path)
parser.add_argument('output_directory', type=Path, nargs='?')
parser.add_argument('--id', type=int, required=True, dest='element_id')
parser.add_argument('--rotate', type=triple, help='absolute Rotation X,Y,Z')
parser.add_argument('--rotate-delta', type=triple, help='added to the current Rotation X,Y,Z')
parser.add_argument('--joint', help='JointName')
parser.add_argument('--unparent-frame', type=float, dest='unparent_frame')
parser.add_argument('--print-only', action='store_true')
args = parser.parse_args()
SOURCE = args.pfb.resolve()
source_bytes = SOURCE.read_bytes()

handler = open_pfb(SOURCE)
rsz = handler.rsz_file
registry = handler.type_registry
root = find_root(rsz, registry)
before = element_list(rsz, root)
matches = [entry for entry in before if entry[1] == args.element_id]
if not matches:
    raise SystemExit(f'element ID {args.element_id} not found')
if len(matches) > 1:
    raise SystemExit(f'element ID {args.element_id} is not unique ({len(matches)} entries)')
instance, _element_id, _description = matches[0]
fields = rsz.parsed_elements[instance]
print(f'{SOURCE.name}: {len(before)} elements, editing element ID={args.element_id} (instance #{instance})')

changed = []
if args.rotate is not None or args.rotate_delta is not None:
    rotation = fields['Rotation']
    old = (rotation.x, rotation.y, rotation.z)
    if args.rotate is not None:
        rotation.x, rotation.y, rotation.z = args.rotate
    else:
        rotation.x += args.rotate_delta[0]
        rotation.y += args.rotate_delta[1]
        rotation.z += args.rotate_delta[2]
    changed.append(('Rotation', old, (rotation.x, rotation.y, rotation.z)))
if args.joint is not None:
    old = fields['JointName'].value
    fields['JointName'].value = args.joint
    changed.append(('JointName', old, args.joint))
if args.unparent_frame is not None:
    old = fields['UnparentFrame'].value
    fields['UnparentFrame'].value = args.unparent_frame
    changed.append(('UnparentFrame', old, args.unparent_frame))
if not changed:
    raise SystemExit('nothing to change')
for name, old, new in changed:
    print(f'  {name}: {old} -> {new}')
if args.print_only:
    raise SystemExit(0)
output = handler.rebuild()

verifier = open_pfb(SOURCE, output)
verifier_rsz = verifier.rsz_file
after_root = find_root(verifier_rsz, verifier.type_registry)
after = element_list(verifier_rsz, after_root)
assert len(after) == len(before)
edited = 0
for (old_instance, old_id, old_desc), (_new_instance, new_id, new_desc) in zip(before, after):
    if old_id != args.element_id:
        assert (old_id, old_desc) == (new_id, new_desc), f'element {old_id} changed'
        continue
    edited += 1
    differing = {name for name in old_desc if old_desc[name] != new_desc[name]}
    assert differing == {name for name, _old, _new in changed}, (differing, changed)
    print(f'  verified: only {sorted(differing)} differs on element {old_id}')
assert edited == 1
assert verifier.rebuild() == output, 'not stable'
diff = sum(1 for index in range(min(len(source_bytes), len(output))) if source_bytes[index] != output[index])
print(f'rebuild: {len(source_bytes):,} -> {len(output):,} B ({len(output) - len(source_bytes):+,} B), '
      f'{diff} byte(s) differ from the source')

folder = args.output_directory.resolve()
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / SOURCE.name
candidate.write_bytes(output)
(folder / 'manifest.json').write_text(json.dumps(dict(
    source=str(SOURCE), source_sha256=hashlib.sha256(source_bytes).hexdigest(), source_size=len(source_bytes),
    candidate=str(candidate), candidate_sha256=hashlib.sha256(output).hexdigest(), candidate_size=len(output),
    element_id=args.element_id,
    changed=[[name, old, new] for name, old, new in changed]), indent=2), encoding='utf-8')
print(f'wrote {candidate}')
