r"""Import one Action instance from another FSM file and attach it to a node.

The template instance lives in a *different* FSM file, so the RSZ instance (global
type id + crc + parsed fields) has to be copied across blocks.  This works because
the type registry is global per game (``resources/data/dumps/rszmhrise.json``) and
``RszFile`` instances carry only ``type_id``/``crc`` plus their parsed fields.

Recipe
    1. locate the template Action in the source FSM (``--from-node`` narrows it),
    2. append a copy of its RSZ instance to the *like-named* block of the target
       file, giving it a fresh ``v1_ID`` identity,
    3. append one entry to the parallel ``action_ex_ids`` array (the array is indexed
       by the block's object-table position, so both must stay the same length),
    4. insert the Action reference into the target node's action list.

The target file may never have instantiated that class: nothing else is needed, the
type id already resolves through the shared registry (the embedded per-file type
table is rebuilt from it on write).

Verification: the untouched blocks stay byte-identical, every other node compares
equal, the reopened file resolves the new action to the source class with identical
fields, the data/object tables keep their length invariant, and REasy's own
``rebuild()`` is stable on the candidate.
"""
import copy
from pathlib import Path

from file_handlers.motfsm.motfsm_file import MotfsmFile, Action
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from tools.fsm.common import resolve_node


def configure(parser):
    parser.add_argument('--node', required=True, help='target node (unique name or 0x identity hash)')
    parser.add_argument('--class', dest='klass', required=True, help='action class substring to import')
    parser.add_argument('--exact', action='store_true', help='require an exact class name match')
    parser.add_argument('--from-fsm', required=True, type=Path, help='FSM file owning the template instance')
    parser.add_argument('--from-node', help='restrict the template search to this source node')
    parser.add_argument('--position', type=int, help='action slot to insert at (default: append)')
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check; the UVAR relation array is sized by a '
                             'heuristic terminator, so a tail shift can legitimately change how far it reads')


def run(args):
    TARGET = args.source.resolve()
    source = TARGET.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes
    target_index = resolve_node(doc, args.node)
    target = nodes[target_index]

    # ------------------------------------------------------------------ locate the template
    src_path = args.from_fsm.resolve()
    src_doc = MotfsmFile()
    src_doc.read(src_path.read_bytes())
    src_filter = resolve_node(src_doc, args.from_node) if args.from_node else None

    matches = []
    for index, node in enumerate(src_doc.bhvt.nodes):
        if src_filter is not None and index != src_filter:
            continue
        for reference in node.actions:
            try:
                instance = src_doc.references.action(reference.id_hash, reference.ex_id)
            except IndexError:
                continue
            if instance is None:
                continue
            name = instance.class_name or ''
            if (name == args.klass) if args.exact else (args.klass in name):
                matches.append((index, node, reference, instance))

    if not matches:
        raise ValueError(f'no action matching {args.klass!r} in {src_path.name}')
    identities = {(reference.id_hash, reference.ex_id) for _, _, reference, _ in matches}
    if len(identities) != 1:
        names = sorted(f'{node.name}[{index}]' for index, node, _, _ in matches)
        detail = ', '.join(names[:6]) + (f' … (+{len(names) - 6} more)' if len(names) > 6 else '')
        raise ValueError(f'{len(identities)} distinct templates match {args.klass!r} in {src_path.name}; '
                         f'narrow it with --from-node ({detail})')

    src_index, src_node, src_ref, src_instance = matches[0]
    klass = src_instance.class_name or ''
    src_block_name, src_instance_index = src_doc.references.action_identities()[(src_ref.id_hash, src_ref.ex_id)]
    print(f'  template {klass} from {src_path.name}: {src_node.name}[{src_index}] '
          f'(0x{src_ref.id_hash:08X}) {src_block_name}[{src_instance_index}]')

    # ------------------------------------------------------------------ guards on the target
    for reference in target.actions:
        try:
            existing = doc.references.action(reference.id_hash, reference.ex_id)
        except IndexError:
            continue
        if existing is not None and (existing.class_name or '') == klass:
            raise ValueError(f'{target.name} already carries {klass}')

    ex_ids_attribute = 'action_ex_ids' if src_block_name == 'actions' else 'static_action_ex_ids'
    target_block = doc.rsz_blocks.get_block(src_block_name)
    src_block = src_doc.rsz_blocks.get_block(src_block_name)
    if target_block is None or src_block is None:
        raise ValueError(f'missing {src_block_name} block')
    old_ex_ids = list(getattr(doc.bhvt, ex_ids_attribute))
    old_objects = list(target_block.object_table)
    if len(old_ex_ids) != len(old_objects):
        raise ValueError(f'{ex_ids_attribute} has {len(old_ex_ids)} entries but {src_block_name} has '
                         f'{len(old_objects)} objects')

    # ------------------------------------------------------------------ helpers
    def next_identity(used):
        value = 1
        while value != 0xFFFFFFFF and value in used:
            value += 1
        if value == 0xFFFFFFFF:
            raise ValueError('no free action identity')
        return value

    def block_ids(block_names, field_index):
        """Action v1_ID is field 1 on the RSZ instance."""
        used = set()
        for name in block_names:
            block = doc.rsz_blocks.get_block(name)
            if block is None:
                continue
            for index in block.object_table:
                instance = block.get_instance(index)
                if instance is None or len(instance.fields) <= field_index:
                    continue
                value = instance.fields[field_index].value
                if isinstance(value, int) and not isinstance(value, bool):
                    used.add(value)
        return used

    def clone_block_object(block, template_instance_index, new_fields=None, source_block=None):
        """Append a copy of one RSZ instance; returns (instance_index, object_index)."""
        native = block.file
        origin = (source_block or block).file
        instance_index = len(native.instance_infos)
        native.instance_infos.append(copy.deepcopy(origin.instance_infos[template_instance_index]))
        native.parsed_elements[instance_index] = copy.deepcopy(origin.parsed_elements[template_instance_index])
        for name, value in (new_fields or {}).items():
            native.parsed_elements[instance_index][name].value = value
        native.object_table.append(instance_index)
        return instance_index, len(block.object_table) - 1

    def block_padding_is_zero(block):
        """Pristine-only check: the rebuilt block must reproduce the region plus zero padding."""
        baseline = block.file.build_validated()
        padding = block.end - block.offset - len(baseline)
        if padding < 0:
            raise ValueError(f'{block.name} rebuild is longer than its region by {-padding} bytes')
        return source[block.offset:block.end] == baseline + bytes(padding)

    def block_splice(block):
        baseline = block.file.build_validated()
        return (block.offset, block.end, baseline + bytes((-len(baseline)) % 16))

    # ------------------------------------------------------------------ clone the instance
    assert block_padding_is_zero(target_block), f'{src_block_name} does not round-trip before the edit'
    new_action_id = next_identity(block_ids(('actions', 'static_actions'), 1))
    new_ex_id = int(src_ref.ex_id)
    _, new_object = clone_block_object(target_block, src_instance_index, {'v1_ID': new_action_id},
                                       source_block=src_block)
    assert target_block.object_table[:-1] == old_objects, 'cloning reordered the action table'
    reference = Action(new_action_id, new_ex_id)
    position = len(target.actions) if args.position is None else args.position
    if not 0 <= position <= len(target.actions):
        raise ValueError(f'--position {position} outside 0..{len(target.actions)}')
    new_ex_ids = old_ex_ids + [new_ex_id]
    action_splice = block_splice(target_block)
    print(f'  {target.name}[{target_index}] action slot {position}: {klass} '
          f'v1_ID {new_action_id} ex_id {new_ex_id} ({src_block_name} object {new_object})')

    # ------------------------------------------------------------------ node section
    old_actions = list(target.actions)
    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)

    def apply():
        target.actions = old_actions[:position] + [reference] + old_actions[position:]
        setattr(doc.bhvt, ex_ids_attribute, new_ex_ids)

    def undo():
        target.actions = list(old_actions)
        setattr(doc.bhvt, ex_ids_attribute, list(old_ex_ids))

    try:
        assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], \
            'node writer does not reproduce the source'
        apply()
        nodes_data = serialize_nodes(doc)
    finally:
        undo()
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    apply()

    splices = [(nodes_start, node_tail, nodes_data), action_splice]
    output = bytes(splice_document(doc, splices))

    # ------------------------------------------------------------------ verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert getattr(verified.bhvt, ex_ids_attribute) == new_ex_ids
    other_ex_ids = 'static_action_ex_ids' if ex_ids_attribute == 'action_ex_ids' else 'action_ex_ids'
    assert getattr(verified.bhvt, other_ex_ids) == getattr(original.bhvt, other_ex_ids)

    imported = verified.references.action(new_action_id, new_ex_id)
    assert imported is not None, 'imported action does not resolve after reopening'
    assert imported.class_name == klass, f'imported class mismatch: {imported.class_name}'
    assert [str(f.name) for f in imported.fields] == [str(f.name) for f in src_instance.fields], \
        'imported field names differ from the template'
    for new_field, old_field in zip(imported.fields, src_instance.fields):
        if str(new_field.name) == 'v1_ID':       # the fresh identity, deliberately not copied
            assert str(new_field.value) == str(new_action_id), 'fresh v1_ID was not written'
            continue
        assert str(new_field.value) == str(old_field.value), \
            f'imported field {new_field.name} differs from the template'

    reopened_block = verified.rsz_blocks.get_block(src_block_name)
    assert len(reopened_block.object_table) == len(getattr(verified.bhvt, ex_ids_attribute)), \
        f'{src_block_name} object table and {ex_ids_attribute} lengths diverged'
    assert reopened_block.object_table[:len(old_objects)] == old_objects, 'object table reordered'

    if not args.allow_uvar_drift:
        assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    assert verified.rebuild() == output, 'REasy rebuild is not stable on the candidate'

    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if old is None or new is None or name == src_block_name:
            continue
        assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'

    for index, node in enumerate(original.bhvt.nodes):
        if index == target_index:
            assert verified.bhvt.nodes[index].actions == old_actions[:position] + [reference] + old_actions[position:]
            assert verified.bhvt.nodes[index].name == node.name
        else:
            assert verified.bhvt.nodes[index] == node, f'node {index} {node.name} changed'
    return output
