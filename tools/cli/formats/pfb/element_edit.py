"""Edit fields of an existing PFB effect element (e.g. nudge its Rotation in place).

The element ID must be one the referenced ``.efx`` actually declares - the id is the effect id
inside that file, so an invented id resolves to nothing.  Editing an existing element is therefore
the way to retune an effect (all movers referencing that element id share the change).

"""
import argparse
import copy
from pathlib import Path


from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData, UserDataData
from file_handlers.rsz.rsz_handler import RszHandler

from .common import open_pfb, scalar_value, snapshot, find_root, triple


def element_list(rsz, root):
    out = []
    for item in rsz.parsed_elements[root]['Elements'].values:
        element = getattr(item, 'value', 0)
        out.append((element, rsz.parsed_elements[element]['ID'].value, snapshot(rsz, element)))
    return out


def configure(parser):
    parser.add_argument('--id', type=int, required=True, dest='element_id')
    parser.add_argument('--rotate', type=triple, help='absolute Rotation X,Y,Z')
    parser.add_argument('--rotate-delta', type=triple, help='added to the current Rotation X,Y,Z')
    parser.add_argument('--joint', help='JointName')
    parser.add_argument('--unparent-frame', type=float, dest='unparent_frame')


def run(args):
    SOURCE = args.source.resolve()
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


    return bytes(output)
