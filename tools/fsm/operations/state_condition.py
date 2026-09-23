"""Replace the condition instance of one BHVT state with a clone of another condition.

Adds a capability the state did not have (e.g. swap a plain `PlayerFsm2Command` for the
LongSword-specific `PlayerFsm2CommandLongSword`, which carries `_conditionWpEnum` /
`_GaugeLv`), while keeping the state itself - its target, its transition map and every other
node - untouched.  The new condition is appended to the RSZ `conditions` block with a fresh
`v0_ID`; nothing else in the file changes.

"""
import copy
import struct


from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node
from tools.fsm.values import assignments


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', required=True, help='node owning the state to rewire')
    parser.add_argument('--target', required=True, help='node the state derives to (identifies the state)')
    parser.add_argument('--template-node', required=True)
    parser.add_argument('--template-state', type=int, default=0)
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
    parser.add_argument('--copy-fields', action='append', default=[],
                        help='field names to take from the state being rewired instead of the template')


def run(args):
    TARGET_FILE = args.source.resolve()
    source = TARGET_FILE.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes

    def node_index(text):
        return resolve_node(doc, text)

    def condition_of(state):
        raw = state.TransitionConditions & 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            return None
        return doc.references.object_instance('conditions', raw)

    node = nodes[node_index(args.node)]
    target = nodes[node_index(args.target)]
    states = [i for i, state in enumerate(node.states) if state.mTransitions == target.id_hash]
    if len(states) != 1:
        raise SystemExit(f'{node.name} has {len(states)} states pointing at {target.name}')
    position = states[0]
    old_raw = node.states[position].TransitionConditions & 0xFFFFFFFF
    old_condition = condition_of(node.states[position])
    template_node = nodes[node_index(args.template_node)]
    if not 0 <= args.template_state < len(template_node.states):
        raise ValueError(f'Template state index out of range: {args.template_state}')
    template_condition = condition_of(template_node.states[args.template_state])
    assert old_condition is not None and template_condition is not None
    overrides = assignments(template_condition, args.set)
    old_fields = {field.name: field.value for field in old_condition.fields}
    template_fields = {field.name: field.value for field in template_condition.fields}
    print(f'  {node.name}[{position}] -> {target.name}')
    print(f'    now   : {old_condition.class_name}')
    print(f'    after : {template_condition.class_name}  (template {template_node.name}[{args.template_state}])')
    copy_fields = {}
    for name in args.copy_fields:
        if name not in template_fields:
            raise SystemExit(f'--copy-fields {name}: not a field of the template condition')
        if name not in old_fields:
            raise SystemExit(f'--copy-fields {name}: not a field of the current condition')
        copy_fields[name] = old_fields[name]
    for name in overrides:
        if name not in template_fields:
            raise SystemExit(f'--set {name}: not a field of the template condition '
                             f'(have {sorted(template_fields)})')
    print(f'    fields: from current {copy_fields} | overrides {overrides}')

    # ---------------------------------------------------------------- new condition instance
    block = doc.rsz_blocks.get_block('conditions')

    def block_ids(block_names, field_index):
        used = set()
        for name in block_names:
            candidate = doc.rsz_blocks.get_block(name)
            for index in candidate.object_table:
                instance = candidate.get_instance(index)
                if instance is None or len(instance.fields) <= field_index:
                    continue
                value = instance.fields[field_index].value
                if isinstance(value, int) and not isinstance(value, bool):
                    used.add(value)
        return used

    cursor = block_ids(('conditions', 'static_conditions', 'expression_tree_conditions',
                        'static_expression_tree_conditions'), 0)
    new_id = 1
    while new_id in cursor:
        new_id += 1
    baseline = block.file.build_validated()
    assert source[block.offset:block.end] == baseline + bytes(block.end - block.offset - len(baseline)), \
        'conditions block has non-zero padding'
    new_fields = dict(copy_fields)
    new_fields.update(overrides)
    new_fields['v0_ID'] = new_id
    native = block.file
    template_raw = template_node.states[args.template_state].TransitionConditions & 0xFFFFFFFF
    template_block_name = 'static_conditions' if template_raw >> 24 == 0x40 else 'conditions'
    template_native = doc.rsz_blocks.get_block(template_block_name).file
    instance_index = len(native.instance_infos)
    native.instance_infos.append(copy.deepcopy(template_native.instance_infos[template_condition.index]))
    native.parsed_elements[instance_index] = copy.deepcopy(template_native.parsed_elements[template_condition.index])
    for name, value in new_fields.items():
        native.parsed_elements[instance_index][name].value = value
    native.object_table.append(instance_index)
    new_object = len(block.object_table) - 1
    block_data = native.build_validated()
    block_data += bytes((-len(block_data)) % 16)
    print(f'  new condition instance {instance_index} object {new_object} v0_ID={new_id}')

    # ---------------------------------------------------------------- node table
    node.states[position].TransitionConditions = new_object

    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    node.states[position].TransitionConditions = old_raw
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    node.states[position].TransitionConditions = new_object
    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    splices = sorted([(nodes_start, node_tail, nodes_data), (block.offset, block.end, block_data)])
    output = splice_document(doc, splices)
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if name != 'conditions':
            assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
        else:
            assert new.object_table[:len(old.object_table)] == old.object_table, 'conditions reordered'
    assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    assert verified.rebuild() == output, 'REasy rebuild is not stable on the candidate'
    index = node_index(args.node)
    for other, previous in enumerate(original.bhvt.nodes):
        current = verified.bhvt.nodes[other]
        if other != index:
            assert current == previous, f'node {other} {previous.name} changed'
        else:
            for i, (a, b) in enumerate(zip(previous.states, current.states)):
                if i == position:
                    assert (b.mTransitions, b.TransitionMaps, b.mStates.values, b.mTransitionAttributes, b.mStatesEx) == \
                        (a.mTransitions, a.TransitionMaps, a.mStates.values, a.mTransitionAttributes, a.mStatesEx)
                    assert b.TransitionConditions == new_object
                else:
                    assert a == b, f'{args.node} state[{i}] changed'
    new_condition = verified.references.object_instance('conditions', new_object)
    fields = {field.name: field.value for field in new_condition.fields}
    expected = dict(template_fields)
    expected.update(copy_fields)
    expected.update(overrides)
    expected['v0_ID'] = new_id
    assert fields == expected, (fields, expected)
    # the old instance must be untouched, the new one must be a fresh slot
    assert new_object >= len(original.rsz_blocks.get_block('conditions').object_table)
    assert original.references.object_instance('conditions', old_raw) is not None
    print(f'  Verified {args.node}[{position}].TransitionConditions; other nodes and blocks unchanged')
    print(f'  New condition fields: {fields}')

    return bytes(output)
