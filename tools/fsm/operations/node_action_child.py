r"""Create a child node that carries a single Action moved off its parent.

Recipe (mirrors the native tree): a new leaf node whose `actions` holds one cloned
Action instance, appended as the first child of the parent with an unconditional
start transition.  The source node keeps every other Action -- the moved Action is
cloned with a fresh identity and its reference is removed from the parent, which is
equivalent to moving it.

Structure of the new node follows the game's own action-carrying children
(node_attribute = 0x23, selector_id = -1, no states / no children / is_branch = 0):

    parent --- child(actions=[moved])
             \-- child[0] condition=-1, and the parent gains
                 transitions[0] = always -> child

Verification: source node table is reproduced byte-exactly before the edit, every
untouched block keeps its bytes, the reopened node table matches the authored model,
and REasy's own rebuild() is stable on the candidate.
"""
import copy
import struct

from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode, ChildNode, Action, Transition, IndexList
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from utils.hash_util import murmur3_hash
from tools.fsm.common import resolve_node, node_string
from tools.fsm.values import parse_field


def configure(parser):
    parser.add_argument('--parent', required=True, help='node the action currently lives on')
    parser.add_argument('--name', required=True, help='name of the new child node')
    parser.add_argument('--action-class', dest='klass',
                        help='class name (or substring) of the Action to move')
    parser.add_argument('--no-action', action='store_true',
                        help="create the child with no Actions at all (pure shell node, like the game's own)")
    parser.add_argument('--exact', action='store_true', help='require an exact class name match')
    parser.add_argument('--no-transition', action='store_true',
                        help='do not add the start transition to the new node')
    parser.add_argument('--selector-from', metavar='NODE',
                        help='node owning the selector template; required when --parent has selector_id -1')
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check; the UVAR relation array is sized by a '
                             'heuristic terminator, so a tail shift can legitimately change how far it reads')
    parser.add_argument('--no-selector', action='store_true',
                        help='leave the parent selector as-is even though it gains a child')
    parser.add_argument('--transition-condition-from', type=int, metavar='STATE',
                        help="clone this state index's condition onto the start transition")
    parser.add_argument('--transition-set', action='append', default=[], metavar='FIELD=VALUE',
                        help='override scalar fields of the cloned transition condition (repeatable)')


def run(args):
    TARGET = args.source.resolve()
    source = TARGET.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes

    parent_index = resolve_node(doc, args.parent)
    parent = nodes[parent_index]
    assert not any(n.name == args.name for n in nodes), f'node name already used: {args.name}'

    # ------------------------------------------------------------------ locate the Action
    position, reference, instance = None, None, None
    if args.no_action and args.klass:
        raise SystemExit('pass either --action-class or --no-action, not both')
    if not args.no_action and not args.klass:
        raise SystemExit('pass --action-class <class> or --no-action')
    for index, candidate in (() if args.no_action else enumerate(parent.actions)):
        found = doc.references.action(candidate.id_hash, candidate.ex_id)
        if found is None:
            continue
        name = found.class_name or ''
        if (name == args.klass) if args.exact else (args.klass in name):
            position, reference, instance = index, candidate, found
            break
    if not args.no_action and instance is None:
        raise SystemExit(f'no action matching {args.klass!r} on {parent.name}')

    # ------------------------------------------------------------------ helpers
    def next_identity(used):
        value = 1
        while value != 0xFFFFFFFF and value in used:
            value += 1
        if value == 0xFFFFFFFF:
            raise ValueError('no free identity')
        return value

    def block_ids(block_names, field_index):
        """Condition id is field 0, Action v1_ID is field 1 (BhvtFile consts)."""
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

    def block_padding_is_zero(block):
        baseline = block.file.build_validated()
        return source[block.offset:block.end] == baseline + bytes(block.end - block.offset - len(baseline))

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

    def block_splice(block):
        data = block.file.build_validated()
        return (block.offset, block.end, data + bytes((-len(data)) % 16))

    # ------------------------------------------------------------------ clone the Action
    new_action_refs = []
    actions_block = None
    action_splice = None
    if args.no_action:
        print('  --no-action: the new node carries no Actions (pure shell)')
    else:
        identity_map = doc.references.action_identities()
        block_name, instance_index = identity_map[(reference.id_hash, reference.ex_id)]
        actions_block = doc.rsz_blocks.get_block(block_name)
        old_objects = list(actions_block.object_table)
        used_action_ids = block_ids(('actions', 'static_actions'), 1)
        new_action_id = next_identity(used_action_ids)
        _, new_object = clone_block_object(actions_block, instance_index, {'v1_ID': new_action_id},
                                           source_block=actions_block)
        new_action_refs = [Action(new_action_id, reference.ex_id)]
        assert actions_block.object_table[:-1] == old_objects, 'cloning reordered the action table'
        action_splice = block_splice(actions_block)
        print(f'  move action[{position:2d}] {instance.class_name}  v1_ID {reference.id_hash} -> {new_action_id} '
              f'({block_name} object {instance_index} -> {new_object})')

    # ------------------------------------------------------------------ selector for the parent
    new_selector_object = None
    selectors_block = None
    if not args.no_selector and parent.selector_id in (-1, 0xFFFFFFFF):
        if not args.selector_from:
            raise SystemExit(f'{parent.name} has selector_id -1 and now owns a child; '
                             'pass --selector-from <node with a selector>')
        template_node = nodes[resolve_node(doc, args.selector_from)]
        if template_node.selector_id in (-1, 0xFFFFFFFF):
            raise SystemExit(f'--selector-from node {template_node.name} has no selector to clone')
        selectors_block = doc.rsz_blocks.get_block('selectors')
        assert block_padding_is_zero(selectors_block), 'selectors block has non-zero padding'
        template_selector = doc.references.object_instance('selectors', template_node.selector_id & 0xFFFFFFFF)
        _, new_selector_object = clone_block_object(selectors_block, template_selector.index)
        print(f'  {parent.name}: selector -1 -> {new_selector_object} '
              f'(clone of {template_selector.class_name} from {template_node.name})')

    # ------------------------------------------------------------------ transition condition
    new_condition_raw, condition_block, condition_block_name = None, None, None
    if args.transition_condition_from is not None:
        if not 0 <= args.transition_condition_from < len(parent.states):
            raise SystemExit(f'{parent.name} has {len(parent.states)} states; '
                             f'--transition-condition-from {args.transition_condition_from} is out of range')
        raw = parent.states[args.transition_condition_from].TransitionConditions & 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            raise SystemExit(f'state {args.transition_condition_from} on {parent.name} has no condition to clone')
        tag = raw >> 24
        condition_block_name = 'static_conditions' if tag == 0x40 else 'conditions'
        condition_block = doc.rsz_blocks.get_block(condition_block_name)
        assert block_padding_is_zero(condition_block), f'{condition_block_name} has non-zero padding'
        template_condition = doc.references.object_instance('conditions', raw)
        new_condition_id = next_identity(block_ids(('conditions', 'static_conditions',
                                                    'expression_tree_conditions',
                                                    'static_expression_tree_conditions'), 0))
        field_map = {field.name: field for field in template_condition.fields}
        overrides = {'v0_ID': new_condition_id}
        for item in args.transition_set:
            key, _, text = item.partition('=')
            key = key.strip()
            if key not in field_map:
                raise SystemExit(f'{template_condition.class_name} has no field {key!r}; '
                                 f'available: {sorted(field_map)}')
            overrides[key] = parse_field(field_map[key], text)
        _, new_condition_object = clone_block_object(condition_block, template_condition.index, overrides)
        new_condition_raw = (new_condition_object | 0x40000000) if tag == 0x40 else new_condition_object
        print(f'  transition condition: clone of {template_condition.class_name} (state '
              f'{args.transition_condition_from}) v0_ID={new_condition_id} raw=0x{new_condition_raw:08X}')
        for item in args.transition_set:
            print(f'      set {item}')

    # ------------------------------------------------------------------ new node record
    strings_start = doc.bhvt.offsets['strings']
    string_count = struct.unpack_from('<I', source, strings_start)[0]
    strings_end = strings_start + 4 + string_count * 2
    new_string = node_string(args.name)
    strings_data = struct.pack('<I', string_count + len(new_string) // 2) + \
        source[strings_start + 4:strings_end] + new_string

    new_node_id = next_identity({n.id_hash for n in nodes})
    new_node = BHVTNode(
        id_hash=new_node_id, ex_id=0, name_index=string_count, name=args.name,
        parent=parent.id_hash, parent_ex=parent.ex_id,
        children=[], selector_id=-1, selector_callers=[], selector_caller_condition_id=-1,
        actions=list(new_action_refs), priority=parent.priority,
        node_attribute=0x23, work_flags=0,
        name_hash=murmur3_hash(args.name.encode('utf-16le')),
        fullname_hash=murmur3_hash(f'{parent.name}.{args.name}'.encode('utf-16le')),
        tags=[], is_branch=0, is_end=0,
        states=[], transitions=[], all_states=[], reference_tree_index=-1,
    )
    new_node_index = len(nodes)
    old_actions = list(parent.actions)
    old_children = list(parent.children)
    old_transitions = list(parent.transitions)
    old_selector = parent.selector_id
    old_ex_ids = list(doc.bhvt.action_ex_ids)

    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)

    new_transition = Transition(mStartTransitionEvent=IndexList(values=[]),
                               mStartState=new_node_id,
                               mStartStateTransition=-1 if new_condition_raw is None else new_condition_raw,
                               mStartStateEx=0)

    def apply():
        nodes.append(new_node)
        parent.actions = (list(old_actions) if args.no_action
                          else old_actions[:position] + old_actions[position + 1:])
        parent.children = old_children + [ChildNode(new_node_id, 0, -1)]
        parent.transitions = (list(old_transitions) if args.no_transition
                              else list(old_transitions) + [new_transition])
        if new_selector_object is not None:
            parent.selector_id = new_selector_object
        doc.bhvt.action_ex_ids = old_ex_ids + [0] * len(new_action_refs)

    def undo():
        nodes.pop()
        parent.actions = list(old_actions)
        parent.children = list(old_children)
        parent.transitions = list(old_transitions)
        parent.selector_id = old_selector
        doc.bhvt.action_ex_ids = list(old_ex_ids)

    try:
        assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], \
            'node writer does not reproduce the source'
        apply()
        nodes_data = serialize_nodes(doc)
    finally:
        undo()
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)

    apply()
    splices = [(nodes_start, node_tail, nodes_data), (strings_start, strings_end, strings_data)]
    if action_splice is not None:
        splices.append(action_splice)
    if condition_block is not None:
        splices.append(block_splice(condition_block))
    if selectors_block is not None:
        splices.append(block_splice(selectors_block))
    splices.sort()
    output = bytes(splice_document(doc, splices))
    print(f'  new node 0x{new_node_id:08X} "{args.name}" under {parent.name}; '
          f'{parent.name} actions {len(old_actions)} -> {len(parent.actions)}')

    # ------------------------------------------------------------------ verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert len(verified.bhvt.nodes) == len(original.bhvt.nodes) + 1
    assert verified.bhvt.action_ex_ids == doc.bhvt.action_ex_ids
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    if not args.allow_uvar_drift:
        assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    else:
        print('  note: UVAR snapshot equality skipped (--allow-uvar-drift)')
    assert verified.rebuild() == output, 'REasy rebuild is not stable on the candidate'
    from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
    touched = set() if action_splice is None else {block_name}
    if condition_block_name is not None:
        touched.add(condition_block_name)
    if selectors_block is not None:
        touched.add('selectors')
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if old is None or new is None:
            continue
        if name in touched:
            assert new.object_table[:len(old.object_table)] == old.object_table, f'{name} object table reordered'
        else:
            assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
    assert verified.transition_map_count == original.transition_map_count
    assert verified.transition_data_count == original.transition_data_count
    for index, node in enumerate(original.bhvt.nodes):
        if index == parent_index:
            assert verified.bhvt.nodes[index].children == node.children + [ChildNode(new_node_id, 0, -1)]
            tr = verified.bhvt.nodes[index].transitions
            if args.no_transition:
                assert tr == node.transitions, 'existing transitions were not preserved'
            else:
                assert tr[:-1] == node.transitions, 'existing transitions were not preserved'
                assert len(tr) == len(node.transitions) + 1, 'start transition was not appended'
                assert tr[-1].mStartState == new_node_id, 'start transition target mismatch'
                assert tr[-1].mStartStateTransition == (-1 if new_condition_raw is None else new_condition_raw), \
                    'start transition condition mismatch'
            if new_selector_object is not None:
                assert verified.bhvt.nodes[index].selector_id == new_selector_object, 'selector was not assigned'
            if not args.no_action:
                assert set((a.id_hash, a.ex_id) for a in node.actions) - set(
                    (a.id_hash, a.ex_id) for a in verified.bhvt.nodes[index].actions) == {(reference.id_hash, reference.ex_id)}
            else:
                assert not [a for a in verified.bhvt.nodes if (a.id_hash, a.ex_id) == (new_node_id, 0)][0].actions,                     'shell child should carry no Actions'
        else:
            assert verified.bhvt.nodes[index] == node, f'node {index} {node.name} changed'
    return output
