"""Append one more transition event to an existing BHVT state, cloned from a native event.

Transition events are what carry the per-derivation extras: `PlayerFsm2EventSetStateAppendType`
(run/walk entry mode) and `PlayerFsm2EventStateInitOption` (`_AngleSetType` / `_LimitAngle` = the
turn limit in degrees, e.g. 180).  A state's `mStates.values` holds **object indexes** into the
`transition_events` block, so a new event is one cloned instance plus one index appended there.

"""
import copy
import struct


from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES

EVENTS_BLOCK = 'transition_events'
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node
from tools.fsm.values import assignments


def configure(parser):
    parser.add_argument('--node', required=True, help='target node (unique name or 0x hash)')
    parser.add_argument('--state', type=int, required=True)
    parser.add_argument('--from-node', required=True, help='node owning the template state')
    parser.add_argument('--from-state', type=int, required=True)
    parser.add_argument('--from-event', type=int, default=0, help='index inside the template event list')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')


def run(args):
    TARGET = args.source.resolve()
    source = TARGET.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes


    def node_index(text):
        return resolve_node(doc, text)


    node = nodes[node_index(args.node)]
    template_node = nodes[node_index(args.from_node)]
    assert 0 <= args.state < len(node.states), f'state index out of range: {args.state}'
    assert 0 <= args.from_state < len(template_node.states), 'template state index out of range'
    values = list(getattr(template_node.states[args.from_state].mStates, 'values', []) or [])
    assert values, f'{template_node.name}.state[{args.from_state}] has no transition event'
    if not 0 <= args.from_event < len(values):
        raise ValueError(f'Template event index out of range: {args.from_event}')
    template_object = values[args.from_event]

    events_block = doc.rsz_blocks.get_block(EVENTS_BLOCK)
    old_objects = list(events_block.object_table)
    old_instances = len(events_block.file.instance_infos)
    template_instance = doc.references.object_instance(EVENTS_BLOCK, template_object)
    assert template_instance is not None, f'event object {template_object} does not resolve'
    file_index = template_instance.index
    template_block_name = 'static_transition_events' if (template_object & 0xFFFFFFFF) >> 24 == 0x40 else EVENTS_BLOCK
    template_native = doc.rsz_blocks.get_block(template_block_name).file
    print(f'  template event: {template_instance.class_name}')

    overrides = assignments(template_instance, args.set)
    used_uids = set()
    for object_index in events_block.object_table:
        inst = events_block.get_instance(object_index)
        if inst is None:
            continue
        for field in inst.fields:
            if field.name == 'v0_UID' and isinstance(field.value, int):
                used_uids.add(field.value)
    uid = 1
    while uid in used_uids:
        uid += 1
    overrides.setdefault('v0_UID', uid)
    overrides.setdefault('v1_Enabled', True)

    native = events_block.file
    instance_index = len(native.instance_infos)
    native.instance_infos.append(copy.deepcopy(template_native.instance_infos[file_index]))
    native.parsed_elements[instance_index] = copy.deepcopy(template_native.parsed_elements[file_index])
    for name, value in overrides.items():
        assert name in native.parsed_elements[instance_index], f'unknown field {name}'
        native.parsed_elements[instance_index][name].value = value
    native.object_table.append(instance_index)
    new_object = len(native.object_table) - 1
    print(f'  new event instance={new_object} ' + ', '.join(f'{k}={v}' for k, v in overrides.items()))


    def block_splice(block):
        data = block.file.build_validated()
        return (block.offset, block.end, data + bytes((-len(data)) % 16))


    events_splice = block_splice(events_block)
    node.states[args.state].mStates.values.append(new_object)


    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    node.states[args.state].mStates.values.pop()
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    node.states[args.state].mStates.values.append(new_object)

    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    splices = sorted([(nodes_start, node_tail, nodes_data), events_splice])
    output = splice_document(doc, splices)
    tree_growth = len(output) - len(source)
    assert tree_growth > 0, 'expected the BHVT tree to grow'
    output = bytes(output)

    verified = MotfsmFile()
    original = MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if name == EVENTS_BLOCK:
            assert new.object_table[:len(old.object_table)] == old.object_table, f'{name} reordered'
            assert len(new.object_table) == len(old.object_table) + 1
        else:
            assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
    check = verified.bhvt.nodes[node_index(args.node)]
    assert list(check.states[args.state].mStates.values)[-1] == new_object
    new_event = verified.references.object_instance(EVENTS_BLOCK, new_object)
    assert new_event.class_name == template_instance.class_name
    expected_fields = {field.name: field.value for field in template_instance.fields}
    expected_fields.update(overrides)
    assert {field.name: field.value for field in new_event.fields} == expected_fields

    return bytes(output)
