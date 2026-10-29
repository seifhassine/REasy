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
from file_handlers.rsz.effect_elements import clone_element, find_element
from .common import open_pfb, snapshot, find_root, triple


def element_list(rsz, registry, root):
    """Ordered snapshot of the root's Elements array (element IDs repeat, so no dict)."""
    out = []
    for item in rsz.parsed_elements[root]['Elements'].values:
        element = getattr(item, 'value', 0)
        out.append((rsz.parsed_elements[element]['ID'].value, snapshot(rsz, element)))
    return out


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
    handler = open_pfb(SOURCE)
    rsz, registry = handler.rsz_file, handler.type_registry
    root = find_root(rsz, registry)
    before = element_list(rsz, registry, root)
    source_position, _ = find_element(rsz, args.clone, args.clone_position)
    new_element = clone_element(rsz, args.clone, args.new_id, position=args.clone_position,
                                rotation=args.rotate, rotation_delta=delta, keep_guid=args.keep_guid)
    rotation = rsz.parsed_elements[new_element]['Rotation']

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
