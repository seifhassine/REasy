"""Clone an attack leaf with independent actions, conditions, and transition maps.

Fully independent clone, following REE-Content-Editor's BhvtEditing / BHVTNode.Clone recipe:
  * new node record, new name string, new Action instances, new Condition instances
  * new transition-event instances (transition_events is 1:1 with states in the source)
  * new transition map entry + cloned TransitionData (also 1:1 in the source)
  * selector: a childless node owns no selector (3794/3795 leaves use -1); --selector=clone copies one
Children and start transitions are dropped by request (leaf-node shape, like the game's own
attack leaves such as atk_109_1).

"""
import copy
import struct


from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode, ChildNode, Action
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from utils.hash_util import murmur3_hash

MOTION_CLASS = 'snow.PlayerPlayMotion2'
HIT_CLASS = 'snow.player.fsm.PlayerHitAction2'
EFFECT_SUFFIX = 'SetEffect'
TRANSITION_DATA_SIZE = 36          # id, data, exitFrame, startFrame, interpolationFrame, contOnLayer*4, 4B align
HEADER_MAP_COUNT, HEADER_DATA_COUNT = 48, 52
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node, node_string


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--template', required=True, help='template attack node selector')
    parser.add_argument('--parent', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--keep-hit-index', type=int, required=True)
    parser.add_argument('--selector', choices=('leaf', 'clone'), default='leaf')


def run(args):
    TARGET = args.source.resolve()
    source = TARGET.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes

    template_index = resolve_node(doc, args.template)
    template = nodes[template_index]
    parent_index = resolve_node(doc, args.parent)
    parent = nodes[parent_index]
    assert not any(n.name == args.name for n in nodes), f'node name already used: {args.name}'
    assert len(template.actions) >= 1, (template.name, len(template.actions))


    def next_identity(used):
        value = 1
        while value != 0xFFFFFFFF and value in used:
            value += 1
        if value == 0xFFFFFFFF:
            raise ValueError('no free identity')
        return value


    def fields_of(instance):
        return {field.name: field for field in instance.fields}


    def block_ids(block_names, field_index):
        """Condition id is field 0, Action v1_ID is field 1 (BhvtFile consts)."""
        used = set()
        for name in block_names:
            block = doc.rsz_blocks.get_block(name)
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
        if new_fields:
            for name, value in new_fields.items():
                native.parsed_elements[instance_index][name].value = value
        native.object_table.append(instance_index)
        return instance_index, len(block.object_table) - 1


    def block_splice(block):
        data = block.file.build_validated()
        return (block.offset, block.end, data + bytes((-len(data)) % 16))


    # ---------------------------------------------------------------- pick Actions
    kept, dropped = [], []
    effects = 0
    for position, reference in enumerate(template.actions):
        instance = doc.references.action(reference.id_hash, reference.ex_id)
        fields = fields_of(instance)
        if instance.class_name == HIT_CLASS and fields['_hitIndex'].value != args.keep_hit_index:
            dropped.append((position, instance.class_name, f"_hitIndex={fields['_hitIndex'].value}"))
            continue
        if instance.class_name.endswith(EFFECT_SUFFIX):
            effects += 1
            if effects > 1:
                dropped.append((position, instance.class_name, f"_ElementID={fields['_ElementID'].value}"))
                continue
        kept.append((position, reference, instance))

    # ---------------------------------------------------------------- identities
    new_node_id = next_identity({n.id_hash for n in nodes})
    cursor = block_ids(('actions', 'static_actions'), 1)
    new_action_ids = []
    for _ in kept:
        value = next_identity(cursor)
        cursor.add(value)
        new_action_ids.append(value)
    cursor = block_ids(('conditions', 'static_conditions', 'expression_tree_conditions',
                        'static_expression_tree_conditions'), 0)
    new_condition_ids = []
    for _ in template.states:
        value = next_identity(cursor)
        cursor.add(value)
        new_condition_ids.append(value)
    identity_map = doc.references.action_identities()

    # ---------------------------------------------------------------- new Action instances
    actions_block = doc.rsz_blocks.get_block('actions')
    native_actions = actions_block.file
    old_objects = list(actions_block.object_table)
    old_instances = len(native_actions.instance_infos)
    assert block_padding_is_zero(actions_block), 'actions block has non-zero padding'
    new_action_refs, motion_instance = [], None
    for (position, reference, instance), new_id in zip(kept, new_action_ids):
        block_name, instance_index = identity_map[(reference.id_hash, reference.ex_id)]
        extra = {'v1_ID': new_id}
        if instance.class_name == MOTION_CLASS:
            extra['v4_MotionID'] = args.motion
        new_index, _ = clone_block_object(actions_block, instance_index, extra,
                                         source_block=doc.rsz_blocks.get_block(block_name))
        if instance.class_name == MOTION_CLASS:
            motion_instance = new_index
        new_action_refs.append(Action(new_id, 0))
        print(f'  keep action[{position:2d}] {instance.class_name:52s} new v1_ID={new_id}')
    for position, class_name, reason in dropped:
        print(f'  drop action[{position:2d}] {class_name:52s} {reason}')
    assert motion_instance is not None, 'template has no PlayerPlayMotion2 action'
    action_splice = block_splice(actions_block)

    # ---------------------------------------------------------------- new Condition instances
    new_state_conditions, condition_blocks = [], {}
    for state, new_id in zip(template.states, new_condition_ids):
        raw = state.TransitionConditions & 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            new_state_conditions.append(-1)
            continue
        tag = raw >> 24
        block_name = 'static_conditions' if tag == 0x40 else 'conditions'
        block = doc.rsz_blocks.get_block(block_name)
        if block_name not in condition_blocks:
            assert block_padding_is_zero(block), f'{block_name} has non-zero padding'
            condition_blocks[block_name] = block
        template_instance = doc.references.object_instance('conditions', raw)
        _, new_object = clone_block_object(block, template_instance.index, {'v0_ID': new_id})
        new_state_conditions.append(new_object | 0x40000000 if tag == 0x40 else new_object)
        print(f'  new condition {template_instance.class_name:44s} ({block_name}) v0_ID={new_id} '
              f'object={new_object} raw=0x{new_state_conditions[-1]:08X}')

    # ---------------------------------------------------------------- new transition events
    events_block = doc.rsz_blocks.get_block('transition_events')
    assert block_padding_is_zero(events_block), 'transition_events block has non-zero padding'
    new_state_events = []
    for state in template.states:
        indices = []
        for value in state.mStates.values:
            template_instance = events_block.get_object(value)
            assert template_instance is not None, f'unknown transition event object {value}'
            _, new_object = clone_block_object(events_block, template_instance.index)
            indices.append(new_object)
        new_state_events.append(indices)
        if indices:
            print(f'  new transition event(s) {template_instance.class_name:44s} '
                  f'{state.mStates.values} -> {indices}')

    # ---------------------------------------------------------------- new transition maps + data
    map_offset, map_count = doc.transition_map_tbl_offset, doc.transition_map_count
    data_offset, data_count = doc.transition_data_tbl_offset, doc.transition_data_count
    map_entries = [struct.unpack_from('<Ii', source, map_offset + i * 8) for i in range(map_count)]
    assert all(map_entries[i][0] <= map_entries[i + 1][0] for i in range(map_count - 1)), 'map table unsorted'
    # Some states reference a transitionId that has no map entry, so mint above every id in use.
    used_transition_ids = {entry[0] for entry in map_entries}
    used_transition_ids.update(s.TransitionMaps for n in doc.bhvt.nodes for s in n.states if s.TransitionMaps)
    next_map_id = max(used_transition_ids) + 1
    next_data_id = max(struct.unpack_from('<I', source, data_offset + i * TRANSITION_DATA_SIZE)[0]
                       for i in range(data_count)) + 1
    new_map_bytes, new_data_bytes, new_state_transitions, new_state_data = b'', b'', [], []
    for state in template.states:
        if state.TransitionMaps == 0:
            new_state_transitions.append(0)
            new_state_data.append(None)
            continue
        entry = next(e for e in map_entries if e[0] == state.TransitionMaps)
        data_index = entry[1]
        blob = bytearray(source[data_offset + data_index * TRANSITION_DATA_SIZE:
                                data_offset + (data_index + 1) * TRANSITION_DATA_SIZE])
        struct.pack_into('<I', blob, 0, next_data_id)
        new_data_bytes += bytes(blob)
        new_data_index = data_count + len(new_data_bytes) // TRANSITION_DATA_SIZE - 1
        new_map_bytes += struct.pack('<Ii', next_map_id, new_data_index)
        new_state_transitions.append(next_map_id)
        new_state_data.append(new_data_index)
        print(f'  new transition map {state.TransitionMaps} (data {data_index}) -> id {next_map_id} '
              f'(data {new_data_index}, id {next_data_id})')
        next_map_id += 1
        next_data_id += 1

    # ---------------------------------------------------------------- selector
    selectors_block, new_selector_id = None, -1
    if args.selector == 'clone':
        selectors_block = doc.rsz_blocks.get_block('selectors')
        assert block_padding_is_zero(selectors_block), 'selectors block has non-zero padding'
        template_selector = doc.references.object_instance('selectors', template.selector_id & 0xFFFFFFFF)
        _, new_selector_id = clone_block_object(selectors_block, template_selector.index)
        print(f'  new selector {template_selector.class_name} object={new_selector_id}')

    # ---------------------------------------------------------------- new node record
    strings_start = doc.bhvt.offsets['strings']
    string_count = struct.unpack_from('<I', source, strings_start)[0]
    strings_end = strings_start + 4 + string_count * 2
    new_string = node_string(args.name)
    strings_data = struct.pack('<I', string_count + len(new_string) // 2) + \
        source[strings_start + 4:strings_end] + new_string

    new_states = copy.deepcopy(template.states)
    for state, raw, events, transition in zip(new_states, new_state_conditions, new_state_events,
                                              new_state_transitions):
        state.TransitionConditions = raw
        state.mStates.values = events
        state.TransitionMaps = transition
    new_node = BHVTNode(id_hash=new_node_id, ex_id=0, name_index=string_count, name=args.name,
                        parent=parent.id_hash, parent_ex=parent.ex_id,
                        children=[], selector_id=new_selector_id, selector_callers=[],
                        selector_caller_condition_id=template.selector_caller_condition_id,
                        actions=new_action_refs, priority=template.priority,
                        node_attribute=template.node_attribute, work_flags=template.work_flags,
                        name_hash=murmur3_hash(args.name.encode('utf-16le')),
                        fullname_hash=murmur3_hash(f'{parent.name}.{args.name}'.encode('utf-16le')),
                        tags=list(template.tags), is_branch=template.is_branch, is_end=template.is_end,
                        states=new_states, transitions=[], all_states=[],
                        reference_tree_index=template.reference_tree_index)
    new_node_index = len(nodes)
    nodes.append(new_node)
    parent.children.append(ChildNode(new_node_id, 0, -1))
    doc.bhvt.action_ex_ids = list(doc.bhvt.action_ex_ids) + [0] * len(new_action_refs)


    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    nodes.pop()
    parent.children.pop()
    doc.bhvt.action_ex_ids = doc.bhvt.action_ex_ids[:-len(new_action_refs)]
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    nodes.append(new_node)
    parent.children.append(ChildNode(new_node_id, 0, -1))
    doc.bhvt.action_ex_ids = list(doc.bhvt.action_ex_ids) + [0] * len(new_action_refs)

    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    splices = [(nodes_start, node_tail, nodes_data), action_splice, (strings_start, strings_end, strings_data)]
    splices += [block_splice(block) for block in condition_blocks.values()]
    splices.append(block_splice(events_block))
    if selectors_block is not None:
        splices.append(block_splice(selectors_block))
    map_insert = map_offset + map_count * 8
    data_insert = data_offset + data_count * TRANSITION_DATA_SIZE
    # The corpus keeps both tail tables 16-byte aligned; pad the map growth so the data table stays aligned.
    assert map_offset % 16 == 0 and data_offset % 16 == 0, 'tail tables are not 16-byte aligned in the source'
    old_gap = data_offset - map_insert
    assert old_gap in (0, 8), f'unexpected map/data gap {old_gap}'
    added_maps = len(new_map_bytes) // 8
    new_map_end = map_insert + len(new_map_bytes)
    new_gap = (16 - new_map_end % 16) % 16        # corpus keeps gap 0/8 with a 16-byte aligned data table
    assert new_gap in (0, 8), f'unexpected new gap {new_gap}'
    map_delta = (len(new_map_bytes) + new_gap) - old_gap
    # replace the old gap so the map table stays contiguous and the data table stays aligned
    splices += [(map_insert, data_offset, new_map_bytes + bytes(new_gap)),
                (data_insert, data_insert, new_data_bytes)]
    splices.sort()
    output = splice_document(doc, splices)
    struct.pack_into('<i', output, HEADER_MAP_COUNT, map_count + added_maps)
    struct.pack_into('<i', output, HEADER_DATA_COUNT, data_count + added_maps)
    tree_growth = len(output) - len(source) - map_delta - len(new_data_bytes)
    assert tree_growth > 0, 'expected the BHVT tree to grow'
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids + [0] * len(new_action_refs)
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    touched = {'actions', 'conditions', 'static_conditions', 'transition_events', 'selectors'}
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if name not in touched:
            assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
        else:
            assert new.object_table[:len(old.object_table)] == old.object_table, f'{name} object table reordered'
    new_block = verified.rsz_blocks.get_block('actions')
    assert len(new_block.object_table) == len(old_objects) + len(new_action_refs)
    assert new_block.object_table[:len(old_objects)] == old_objects
    assert len(new_block.file.instance_infos) == old_instances + len(new_action_refs)
    assert verified.transition_map_count == original.transition_map_count + added_maps
    assert verified.transition_data_count == original.transition_data_count + added_maps
    tree_growth = verified.transition_map_tbl_offset - original.transition_map_tbl_offset
    assert tree_growth > 0
    assert tree_growth == len(output) - len(source) - map_delta - len(new_data_bytes), 'tree growth mismatch'
    assert verified.transition_data_tbl_offset > original.transition_data_tbl_offset, 'transition data table did not shift'
    # appended data entries must be value-identical to their templates apart from the new id
    for state, old_index, new_index in zip(template.states, [next((e[1] for e in map_entries if e[0] == s.TransitionMaps), None)
                                                            for s in template.states], new_state_data):
        if new_index is None:
            continue
        base_new = verified.transition_data_tbl_offset
        old_blob = source[data_offset + old_index * TRANSITION_DATA_SIZE:
                          data_offset + (old_index + 1) * TRANSITION_DATA_SIZE]
        new_blob = output[base_new + new_index * TRANSITION_DATA_SIZE:
                          base_new + (new_index + 1) * TRANSITION_DATA_SIZE]
        assert old_blob[4:] == new_blob[4:], f'transition data {new_index} differs from template {old_index}'
        assert struct.unpack_from('<I', new_blob)[0] != struct.unpack_from('<I', old_blob)[0], 'transition data id reused'
    assert verified.transition_map_tbl_offset == original.transition_map_tbl_offset + tree_growth
    assert verified.transition_map_tbl_offset % 16 == 0, 'map table lost 16-byte alignment'
    assert verified.transition_data_tbl_offset % 16 == 0, 'transition data table lost 16-byte alignment'
    assert verified.transition_data_tbl_offset + verified.transition_data_count * TRANSITION_DATA_SIZE == len(output),     'transition data table must end at EOF'
    assert verified.transition_data_tbl_offset - (verified.transition_map_tbl_offset +
           verified.transition_map_count * 8) in (0, 8), 'map/data gap changed shape'
    assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    assert verified.rebuild() == output, 'rebuild is not stable on the candidate'

    for index, node in enumerate(original.bhvt.nodes):
        if index == parent_index:
            assert verified.bhvt.nodes[index].children == node.children + [ChildNode(new_node_id, 0, -1)]
        else:
            assert verified.bhvt.nodes[index] == node, f'node {index} {node.name} changed'
    for node in verified.bhvt.nodes:
        for child in node.children:
            verified.references.node_index(child.id_hash, child.ex_id)
        for reference in node.actions:
            verified.references.action(reference.id_hash, reference.ex_id)
        for state in node.states:
            verified.references.state_target(state.mTransitions)
            if state.TransitionConditions & 0xFFFFFFFF != 0xFFFFFFFF:
                verified.references.object_instance('conditions', state.TransitionConditions & 0xFFFFFFFF)
        for transition in node.transitions:
            verified.references.node_index(transition.mStartState, transition.mStartStateEx)
        for state in node.all_states:
            verified.references.node_index(state.mAllState, state.mAllStateEx)

    clone = verified.bhvt.nodes[new_node_index]
    clone_actions = [verified.references.action(a.id_hash, a.ex_id) for a in clone.actions]
    assert len(clone_actions) == len(kept) == len(new_action_refs)
    assert {a.id_hash for a in clone.actions}.isdisjoint({a.id_hash for n in original.bhvt.nodes for a in n.actions})
    assert len(verified.references.action_identities()) == len(original.references.action_identities()) + len(new_action_refs)
    motion = fields_of(clone_actions[[i.class_name for i in clone_actions].index(MOTION_CLASS)])
    assert motion['v4_MotionID'].value == args.motion
    template_motion = next(i for _, _, i in kept if i.class_name == MOTION_CLASS)
    assert motion['v3_BankID'].value == fields_of(template_motion)['v3_BankID'].value
    assert [s.TransitionConditions & 0xFFFFFFFF for s in clone.states] == [v & 0xFFFFFFFF for v in new_state_conditions]
    assert [s.TransitionMaps for s in clone.states] == new_state_transitions
    assert [s.mStates.values for s in clone.states] == new_state_events, 'transition events were not re-created'
    assert [s.mTransitions for s in clone.states] == [s.mTransitions for s in template.states], 'derivation target changed'
    assert not clone.children and not clone.transitions
    assert clone.selector_id == new_selector_id
    if args.selector == 'clone':
        assert clone.selector_id != template.selector_id, 'selector is still shared with the template'
    old_events = original.rsz_blocks.get_block('transition_events')
    old_event_objects = len(old_events.object_table)
    for value in [v for events in new_state_events for v in events]:
        assert value >= old_event_objects, f'transition event object {value} is not a newly appended slot'
    new_ids = {struct.unpack_from('<Ii', output, map_insert + tree_growth + i * 8)[0] for i in range(added_maps)}
    assert new_ids.isdisjoint({s.TransitionMaps for n in original.bhvt.nodes for s in n.states})
    assert clone.name_hash == murmur3_hash(args.name.encode('utf-16le'))

    return bytes(output)
