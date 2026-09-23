r"""Remove an Action reference from a node's action list.

The node record stores its actions inline (``count`` + ``(id_hash, ex_id)`` columns) and the
parallel ``action_ex_ids`` array is indexed by the *action block's* object table, not by node
action slots -- so dropping a reference is a pure node-table edit: every RSZ block stays
byte-identical and the block instance remains as unreferenced data (harmless; deleting the
instance itself would renumber instance indices and is deliberately not attempted here).

Verification: the reopened node table matches the authored model, the removed identity is gone
from that node only, every other node compares equal, both ex_id arrays are untouched, all
blocks are byte-identical, the UVAR snapshot is unchanged, and REasy's ``rebuild()`` is stable.
"""
from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from tools.fsm.common import resolve_node


def configure(parser):
    parser.add_argument('--node', required=True, help='node owning the Action (unique name or 0x identity hash)')
    parser.add_argument('--class', dest='klass', required=True, help='action class substring to remove')
    parser.add_argument('--exact', action='store_true', help='require an exact class name match')
    parser.add_argument('--position', type=int, help='action slot to remove (required when several match)')
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check; the UVAR relation array is sized by a '
                             'heuristic terminator, so a tail shift can legitimately change how far it reads')


def run(args):
    source = args.source.resolve().read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    index = resolve_node(doc, args.node)
    node = doc.bhvt.nodes[index]

    # ------------------------------------------------------------------ locate the slots
    matches = []
    for position, reference in enumerate(node.actions):
        try:
            instance = doc.references.action(reference.id_hash, reference.ex_id)
        except IndexError:
            instance = None
        if instance is None:
            continue
        name = instance.class_name or ''
        if (name == args.klass) if args.exact else (args.klass in name):
            matches.append((position, reference, name))

    if not matches:
        raise ValueError(f'{node.name} carries no action matching {args.klass!r}')
    if args.position is not None:
        matches = [item for item in matches if item[0] == args.position]
        if not matches:
            raise ValueError(f'{node.name} action slot {args.position} does not match {args.klass!r}')
    if len(matches) > 1:
        slots = ', '.join(str(position) for position, _, _ in matches)
        raise ValueError(f'{node.name} has {len(matches)} matching actions (slots {slots}); use --position')

    position, reference, klass = matches[0]
    old_actions = list(node.actions)
    new_actions = old_actions[:position] + old_actions[position + 1:]
    print(f'  {node.name}[{index}] drop action slot {position}: {klass} '
          f'(0x{reference.id_hash:08X}, ex={reference.ex_id}); actions {len(old_actions)} -> {len(new_actions)}')

    # ------------------------------------------------------------------ node table
    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)

    def apply():
        node.actions = list(new_actions)

    def undo():
        node.actions = list(old_actions)

    try:
        assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], \
            'node writer does not reproduce the source'
        apply()
        nodes_data = serialize_nodes(doc)
    finally:
        undo()
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    apply()

    output = bytes(splice_document(doc, [(nodes_start, node_tail, nodes_data)]))

    # ------------------------------------------------------------------ verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    assert verified.bhvt.nodes[index].actions == new_actions
    remaining = {(a.id_hash, a.ex_id) for a in verified.bhvt.nodes[index].actions}
    assert (reference.id_hash, reference.ex_id) not in remaining, 'action reference survived the edit'
    assert len(verified.bhvt.nodes[index].actions) == len(old_actions) - 1

    for other, node_before in enumerate(original.bhvt.nodes):
        if other == index:
            continue
        assert verified.bhvt.nodes[other] == node_before, f'node {other} {node_before.name} changed'
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if old is None or new is None:
            continue
        assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'

    assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    assert verified.rebuild() == output, 'REasy rebuild is not stable on the candidate'
    return output
