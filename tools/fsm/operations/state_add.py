"""Add one derivation (state) to an existing BHVT node - e.g. "atk_135 can cancel into atk_621".

The template state is copied from `--node` by default; `--template-from OTHER --template-target NAME`
copies the state that already derives to NAME from another node (e.g. atk_152's sheathed roll).

Same machinery as REasy's own game-verified fixtures (`tools/motfsm_validation/`): the byte-exact
`serialize_nodes()` (asserted against the source before the edit), the RSZ block splices, the
`BhvtPointers` relocation pass and `verified.rebuild() == output`.

Native data keeps `TransitionMaps` strictly 1:1 with states (LongSword: 10684 states / 10684 ids /
0 shared) and the engine resolves a transition through that id, so a **new state must own a new map
id**: the tool appends one map-table entry (id -> data index) reusing the template's *data* record
(the data table itself is shared natively, e.g. data#0 backs thousands of transitions).  Sharing a
map id between two states silently retargets the newer one to the older state's target.

New objects: one Condition instance (clone of the template state's condition) and, unless
`--events drop`, one clone per transition event of the template state.  Every other node, every
other block and the whole tail stays byte-identical.

"""
import copy
import struct
import zlib


from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot

HEADER_MAP_COUNT, HEADER_DATA_COUNT = 48, 52
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node
from tools.fsm.values import assignments


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', required=True, help='node that gains the derivation, e.g. atk_135')
    parser.add_argument('--target', required=True, help='node the new derivation goes to, e.g. atk_621')
    parser.add_argument('--template-from', help='node the template state is copied from (default: --node)')
    parser.add_argument('--template-state', type=int, help='state index to copy (default: auto)')
    parser.add_argument('--template-target', help="pick the template state that already goes to this node")
    parser.add_argument('--condition-class', default='PlayerFsm2Command',
                        help='text the template state condition class must contain')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE',
                        help='override a condition field, e.g. --set CmdType=51')
    parser.add_argument('--events', choices=('copy', 'share', 'drop'), default='copy',
                        help='what to do with the template state transition events')
    parser.add_argument('--insert-before', type=int, help='insert before this state position (default: append)')


def run(args):
    TARGET_FILE = args.source.resolve()
    source = TARGET_FILE.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes


    def node_index(text):
        return resolve_node(doc, text)


    node = nodes[node_index(args.node)]
    target = nodes[node_index(args.target)]
    assert target.id_hash not in (0, 0xFFFFFFFF)
    assert all(state.mTransitions != target.id_hash for state in node.states), \
        f'{node.name} already derives to {target.name}'


    def fields_of(instance):
        return {field.name: field for field in instance.fields}


    def condition_of(state):
        raw = state.TransitionConditions & 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            return None
        return doc.references.object_instance('conditions', raw)


    template_node = nodes[node_index(args.template_from)] if args.template_from else node
    template_state = args.template_state
    if template_state is None and args.template_target:
        wanted = nodes[node_index(args.template_target)].id_hash
        matches = [i for i, state in enumerate(template_node.states) if state.mTransitions == wanted]
        if len(matches) != 1:
            raise SystemExit(f'{template_node.name} has {len(matches)} derivations to {args.template_target}')
        template_state = matches[0]
    if template_state is None:
        candidates = []
        for position, state in enumerate(template_node.states):
            instance = condition_of(state)
            if instance is None or args.condition_class.lower() not in instance.class_name.lower():
                continue
            values = fields_of(instance)
            if values.get('atkType') is None:                     # not a PlayerFsm2Command-style condition
                continue
            if values['atkType'].value or values['replaceType'].value or values['_IsForMr'].value:
                continue
            if values['StartFrame'].value <= 0:
                continue
            candidates.append((values['StartFrame'].value, position))
        if not candidates:
            raise SystemExit(f'{template_node.name} has no derivable {args.condition_class} state to copy')
        template_state = min(candidates)[1]
    assert 0 <= template_state < len(template_node.states), f'state index out of range: {template_state}'
    template = template_node.states[template_state]
    template_condition = condition_of(template)
    assert template_condition is not None, 'the template state has no condition'
    overrides = assignments(template_condition, args.set)
    insertion = len(node.states) if args.insert_before is None else args.insert_before
    if not 0 <= insertion <= len(node.states):
        raise ValueError('Insertion position is outside the state list')
    raw = template.TransitionConditions & 0xFFFFFFFF
    tag = raw >> 24
    block_name = 'static_conditions' if tag == 0x40 else 'conditions'
    if tag not in (0, 0x40):
        raise SystemExit(f'unsupported condition reference 0x{raw:08X}')
    print(f'  template {template_node.name}.state[{template_state}] condition {template_condition.class_name} '
          f'({block_name}) -> target 0x{template.mTransitions:08X}')
    for name, value in fields_of(template_condition).items():
        print(f'      {name} = {value.value}')
    assert not overrides or set(overrides) <= set(fields_of(template_condition)), \
        f'unknown field(s): {sorted(set(overrides) - set(fields_of(template_condition)))}'

    # ---------------------------------------------------------------- new identities
    def block_ids(block_names, field_index):
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


    cursor = block_ids(('conditions', 'static_conditions', 'expression_tree_conditions',
                        'static_expression_tree_conditions'), 0)
    new_condition_id = 1
    while new_condition_id in cursor:
        new_condition_id += 1
    uid_cursor = set()
    for index in doc.rsz_blocks.get_block('transition_events').object_table:
        instance = doc.rsz_blocks.get_block('transition_events').get_instance(index)
        values = {field.name: field.value for field in instance.fields}
        if isinstance(values.get('v0_UID'), int):
            uid_cursor.add(values['v0_UID'])


    def block_padding_is_zero(block):
        baseline = block.file.build_validated()
        return source[block.offset:block.end] == baseline + bytes(block.end - block.offset - len(baseline))


    def clone_block_object(block, template_instance_index, new_fields=None):
        native = block.file
        instance_index = len(native.instance_infos)
        native.instance_infos.append(copy.deepcopy(native.instance_infos[template_instance_index]))
        native.parsed_elements[instance_index] = copy.deepcopy(native.parsed_elements[template_instance_index])
        if new_fields:
            for name, value in new_fields.items():
                native.parsed_elements[instance_index][name].value = value
        native.object_table.append(instance_index)
        return instance_index, len(block.object_table) - 1


    def block_splice(block):
        data = block.file.build_validated()
        return (block.offset, block.end, data + bytes((-len(data)) % 16))


    condition_block = doc.rsz_blocks.get_block(block_name)
    assert block_padding_is_zero(condition_block), f'{block_name} has non-zero padding'
    new_condition_instance, new_condition_object = clone_block_object(
        condition_block, template_condition.index, {'v0_ID': new_condition_id, **overrides})
    new_condition_raw = new_condition_object | 0x40000000 if tag == 0x40 else new_condition_object
    print(f'  new condition instance {template_condition.index} -> {new_condition_object} '
          f'(v0_ID={new_condition_id}, raw=0x{new_condition_raw:08X})')

    new_event_objects, events_splice = [], None
    if args.events != 'drop' and template.mStates.values:
        events_block = doc.rsz_blocks.get_block('transition_events')
        if args.events == 'copy':
            assert block_padding_is_zero(events_block), 'transition_events block has non-zero padding'
            for value in template.mStates.values:
                template_event = events_block.get_object(value)
                assert template_event is not None, f'unknown transition event object {value}'
                event_fields = fields_of(template_event)
                fresh = {}
                if 'v0_UID' in event_fields:
                    uid = zlib.crc32(f'{node.name}.from.{template_node.name}{template_state}.to.{target.name}'
                                     f'.event.{len(new_event_objects)}.20260917'.encode('utf-8'))
                    while uid in uid_cursor or uid in (0, 0xFFFFFFFF):
                        uid = (uid + 1) & 0xFFFFFFFF
                    uid_cursor.add(uid)
                    fresh['v0_UID'] = uid
                _, new_object = clone_block_object(events_block, template_event.index, fresh)
                new_event_objects.append(new_object)
            events_splice = block_splice(events_block)
        else:
            new_event_objects = list(template.mStates.values)
        print(f'  transition events {list(template.mStates.values)} -> {new_event_objects} ({args.events})')

    # ---------------------------------------------------------------- the new state owns a fresh map id
    map_offset, map_count = doc.transition_map_tbl_offset, doc.transition_map_count
    data_offset, data_count = doc.transition_data_tbl_offset, doc.transition_data_count
    map_entries = [struct.unpack_from('<Ii', source, map_offset + i * 8) for i in range(map_count)]
    assert all(map_entries[i][0] <= map_entries[i + 1][0] for i in range(map_count - 1)), 'map table unsorted'
    assert map_offset % 16 == 0 and data_offset % 16 == 0, 'tail tables are not 16-byte aligned'
    old_gap = data_offset - (map_offset + map_count * 8)
    assert old_gap in (0, 8), f'unexpected map/data gap {old_gap}'
    entry = next((item for item in map_entries if item[0] == template.TransitionMaps), None)
    assert entry is not None, f'template map {template.TransitionMaps} is not in the map table'
    new_map_id = max({item[0] for item in map_entries} |
                     {state.TransitionMaps for n in nodes for state in n.states if state.TransitionMaps}) + 1
    new_map_bytes = struct.pack('<Ii', new_map_id, entry[1])
    print(f'  new transition map {new_map_id} -> data#{entry[1]} (one map id per state, like native data)')

    # ---------------------------------------------------------------- new state + node table
    new_state = copy.deepcopy(template)
    new_state.TransitionConditions = new_condition_raw
    new_state.mTransitions = target.id_hash
    new_state.TransitionMaps = new_map_id
    new_state.mStates.values = new_event_objects
    node.states.insert(insertion, new_state)
    print(f'  {node.name}: states {len(node.states) - 1} -> {len(node.states)}; '
          f'new state target 0x{target.id_hash:08X} ({target.name}), own map {new_map_id}')


    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    node.states.pop(insertion)
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    node.states.insert(insertion, new_state)
    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    map_insert = map_offset + map_count * 8
    new_gap = (16 - (map_insert + len(new_map_bytes)) % 16) % 16
    assert new_gap in (0, 8), f'unexpected new gap {new_gap}'
    splices = [(nodes_start, node_tail, nodes_data), block_splice(condition_block),
               (map_insert, data_offset, new_map_bytes + bytes(new_gap))]
    if events_splice is not None:
        splices.append(events_splice)
    splices.sort()
    map_delta = (len(new_map_bytes) + new_gap) - old_gap
    output = splice_document(doc, splices)
    struct.pack_into('<i', output, HEADER_MAP_COUNT, map_count + 1)
    assert struct.unpack_from('<i', output, HEADER_DATA_COUNT)[0] == data_count, 'data count changed'
    assert len(output) - len(source) - map_delta > 0, 'expected the BHVT tree to grow'
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    touched = {block_name} | ({'transition_events'} if args.events == 'copy' and template.mStates.values else set())
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if name not in touched:
            assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
        else:
            assert new.object_table[:len(old.object_table)] == old.object_table, f'{name} object table reordered'
    # the map table gains exactly one entry; the data table is only shifted
    assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    assert verified.rebuild() == output, 'REasy rebuild is not stable on the candidate'

    node_index_verified = node_index(args.node)
    for index, previous in enumerate(original.bhvt.nodes):
        current = verified.bhvt.nodes[index]
        if index == node_index_verified:
            assert current.states[:insertion] + current.states[insertion+1:] == previous.states, 'existing states changed'
            assert len(current.states) == len(previous.states) + 1
            assert current.actions == previous.actions and current.children == previous.children
            assert (current.id_hash, current.name, current.parent, current.node_attribute) == \
                (previous.id_hash, previous.name, previous.parent, previous.node_attribute)
        else:
            assert current == previous, f'node {index} {previous.name} changed'
    for index, current in enumerate(verified.bhvt.nodes):
        for child in current.children:
            verified.references.node_index(child.id_hash, child.ex_id)
        for reference in current.actions:
            verified.references.action(reference.id_hash, reference.ex_id)
        for state in current.states:
            verified.references.state_target(state.mTransitions)
            if state.TransitionConditions & 0xFFFFFFFF != 0xFFFFFFFF:
                verified.references.object_instance('conditions', state.TransitionConditions & 0xFFFFFFFF)

    # Existing derivations retain their relative priority.
    verified_node = verified.bhvt.nodes[node_index_verified]
    clone_state = verified_node.states[insertion]
    assert len(verified_node.states) == len(original.bhvt.nodes[node_index_verified].states) + 1
    assert clone_state.mTransitions == target.id_hash, 'derivation target changed'
    assert clone_state.TransitionMaps == new_map_id, 'the new state must own a new map id'
    assert new_map_id not in {state.TransitionMaps for n in original.bhvt.nodes for state in n.states},     'the new map id is already used by another state'
    assert verified.transition_map_count == original.transition_map_count + 1
    assert verified.transition_data_count == original.transition_data_count
    expected_map_table = source[original.transition_map_tbl_offset:
                                original.transition_map_tbl_offset + original.transition_map_count * 8] +     new_map_bytes
    assert output[verified.transition_map_tbl_offset:
                  verified.transition_map_tbl_offset + len(expected_map_table)] == expected_map_table,     'map table is not the original plus the new entry'
    assert output[verified.transition_data_tbl_offset:
                  verified.transition_data_tbl_offset + verified.transition_data_count * 36] ==     source[original.transition_data_tbl_offset:
               original.transition_data_tbl_offset + original.transition_data_count * 36],     'transition data table changed'
    seen = {}
    for n in verified.bhvt.nodes:
        for i, state in enumerate(n.states):
            if not state.TransitionMaps:
                continue
            if state.TransitionMaps in seen and seen[state.TransitionMaps][0] != state.mTransitions:
                raise AssertionError(f'transition map {state.TransitionMaps} conflicts: '
                                     f'{seen[state.TransitionMaps]} vs {(state.mTransitions, n.name, i)}')
            seen[state.TransitionMaps] = (state.mTransitions, n.name, i)
    assert clone_state.mStates.values == new_event_objects, 'transition events were not re-created'
    assert clone_state.mTransitionAttributes == template.mTransitionAttributes
    assert clone_state.mStatesEx == template.mStatesEx
    verified_condition = verified.references.object_instance('conditions', clone_state.TransitionConditions & 0xFFFFFFFF)
    assert verified_condition is not None and verified_condition.class_name == template_condition.class_name
    verified_fields = {field.name: field.value for field in verified_condition.fields}
    expected = {field.name: field.value for field in template_condition.fields}
    expected.update(overrides)
    expected['v0_ID'] = new_condition_id
    assert verified_fields == expected, (verified_fields, expected)
    # every field must be the template's value unless it was overridden on the command line
    for field_name, field in fields_of(template_condition).items():
        if field_name in overrides or field_name == 'v0_ID':
            continue
        assert verified_fields[field_name] == field.value, f'{field_name} was not copied from the template'
    for field_name, value in overrides.items():
        assert verified_fields[field_name] == value, f'{field_name} override did not stick'
    if args.events == 'copy' and template.mStates.values:
        old_events = original.rsz_blocks.get_block('transition_events')
        for value in new_event_objects:
            assert value >= len(old_events.object_table), f'event object {value} is not a new slot'

    return bytes(output)
