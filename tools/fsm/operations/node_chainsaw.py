"""Add a Charge Axe chainsaw branch using an existing native branch as a template.

The branch requires children chainsaw_run/chainsaw_end/normal, a leading buff
transition, an unconditional normal exit, and a selector. ChainsawHit._HitId
selects RCOL field0, as does PlayerHitAction2._hitIndex.
Existing default transitions and selectors are retained.
"""
import copy
import struct


from file_handlers.motfsm.motfsm_file import (MotfsmFile, BHVTNode, ChildNode, Action,
                                              State, Transition, IndexList)
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from utils.hash_util import murmur3_hash

HEADER_MAP_COUNT, HEADER_DATA_COUNT = 48, 52
HIT_ACTION = 'ChargeAxeChainsawHit'
BUFF_CONDITION = 'ChainsawBuff'
END_CONDITION = 'ChainsawAttackEnd'
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', action='append', required=True, help='target node (repeatable)')
    parser.add_argument('--template-node', default='atk_axe_rush_strike_main',
                        help='attack node that already owns a chainsaw branch')
    parser.add_argument('--command', dest='pad_command', type=int, default=None, help='ChainsawHit._Command (default: template)')
    parser.add_argument('--hit-id', type=int, default=None, help='ChainsawHit._HitId (default: template)')
    parser.add_argument('--ref-work-id', type=int, default=None, help='ChainsawHit._RefWorkId (default: template)')
    parser.add_argument('--work-id', type=int, default=None, help='ChainsawHit._WorkId (default: template)')
    parser.add_argument('--pad-type', type=int, default=None, help='ChainsawHit._PadType (default: template)')
    parser.add_argument('--motion-id', type=int, default=None,
                        help="also retarget each node's PlayerPlayMotion2.v4_MotionID (e.g. to a freshly "
                             'duplicated slot that carries the shield chainsaw track)')


def run(args):
    SOURCE = args.source.resolve()
    source = SOURCE.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes

    def node_index(text):
        return resolve_node(doc, text)

    def child_of(node, name):
        for child in node.children:
            index = doc.references.node_index(child.id_hash, child.ex_id)
            if nodes[index].name == name:
                return index, child
        raise SystemExit(f'{node.name} has no child named {name!r}')

    def fields_of(instance):
        return {field.name: field for field in instance.fields}

    def condition_instance(raw):
        raw &= 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            return None
        return doc.references.object_instance('conditions', raw)

    def class_of_condition(raw):
        instance = condition_instance(raw)
        return (instance.class_name or '') if instance else ''

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

    def next_identity(used):
        value = 1
        while value != 0xFFFFFFFF and value in used:
            value += 1
        if value == 0xFFFFFFFF:
            raise ValueError('no free identity')
        return value

    # ---------------------------------------------------------------- template analysis
    template_index = node_index(args.template_node)
    template = nodes[template_index]
    run_index, run_child = child_of(template, 'chainsaw_run')
    end_index, _ = child_of(template, 'chainsaw_end')
    run_template, end_template = nodes[run_index], nodes[end_index]
    normal_index, _ = child_of(template, 'normal')
    normal_template = nodes[normal_index]
    if normal_template.actions or normal_template.states or normal_template.children:
        raise SystemExit(f'{template.name}.normal is expected to be an empty landing node')

    if len(run_template.actions) != 1:
        raise SystemExit(f'{template.name}.chainsaw_run is expected to carry exactly one action')
    hit_reference = run_template.actions[0]
    hit_instance = doc.references.action(hit_reference.id_hash, hit_reference.ex_id)
    if HIT_ACTION not in (hit_instance.class_name or ''):
        raise SystemExit(f'{template.name}.chainsaw_run action is {hit_instance.class_name}, not {HIT_ACTION}')
    hit_fields = fields_of(hit_instance)

    if len(run_template.states) != 1:
        raise SystemExit(f'{template.name}.chainsaw_run is expected to carry exactly one state')
    run_state_template = run_template.states[0]
    end_condition = condition_instance(run_state_template.TransitionConditions)
    if end_condition is None or END_CONDITION not in (end_condition.class_name or ''):
        raise SystemExit(f'{template.name}.chainsaw_run state condition is not {END_CONDITION}')

    buff_transition = None
    for transition in template.transitions:
        if BUFF_CONDITION in class_of_condition(transition.mStartStateTransition):
            buff_transition = transition
            break
    if buff_transition is None:
        raise SystemExit(f'{template.name} has no {BUFF_CONDITION} start transition')
    buff_condition = condition_instance(buff_transition.mStartStateTransition)
    assert buff_transition.mStartState == run_child.id_hash, 'chainsaw branch is not the buff transition target'

    wanted = {'_Command': args.pad_command, '_HitId': args.hit_id,
              '_RefWorkId': args.ref_work_id, '_WorkId': args.work_id, '_PadType': args.pad_type}
    hit_values = {name: (wanted[name] if wanted[name] is not None else hit_fields[name].value)
                  for name in wanted}
    print(f'template {template.name}: chainsaw_run action {hit_instance.class_name}')
    for name in ('_Command', '_RefWorkId', '_HitId', '_WorkId', '_PadType'):
        star = ' (override)' if wanted[name] is not None else ''
        print(f'  {name:12s} = {hit_values[name]}{star}')

    targets = []
    for name in args.node:
        index = node_index(name)
        node = nodes[index]
        if any(nodes[doc.references.node_index(c.id_hash, c.ex_id)].name == 'chainsaw_run' for c in node.children):
            raise SystemExit(f'{name} already owns a chainsaw_run child')
        targets.append((index, node))

    # ---------------------------------------------------------------- identities
    node_ids_used = {n.id_hash for n in nodes}
    action_ids_used = block_ids(('actions', 'static_actions'), 1)
    condition_ids_used = block_ids(('conditions', 'static_conditions',
                                    'expression_tree_conditions', 'static_expression_tree_conditions'), 0)
    action_ids, condition_ids, node_ids, normal_ids = [], [], [], []
    for _index, _node in targets:
        normal_ids.append(None)
    for _ in targets:
        value = next_identity(action_ids_used)
        action_ids_used.add(value)
        action_ids.append(value)
        value = next_identity(condition_ids_used)
        condition_ids_used.add(value)
        buff_id = value
        value = next_identity(condition_ids_used)
        condition_ids_used.add(value)
        condition_ids.append((buff_id, value))
        pair = []
        for _ in range(2):
            value = next_identity(node_ids_used)
            node_ids_used.add(value)
            pair.append(value)
        node_ids.append(tuple(pair))
    for order, (index, node) in enumerate(targets):
        if not node.transitions:
            value = next_identity(node_ids_used)
            node_ids_used.add(value)
            normal_ids[order] = value

    # ---------------------------------------------------------------- new Action instance
    actions_block = doc.rsz_blocks.get_block('actions')
    assert block_padding_is_zero(actions_block), 'actions block has non-zero padding'
    old_action_objects = list(actions_block.object_table)
    old_action_instances = len(actions_block.file.instance_infos)
    # ---------------------------------------------------------------- retarget the played motion
    retargeted = []
    if args.motion_id is not None:
        identity_map = doc.references.action_identities()
        for index, node in targets:
            done = False
            for reference in node.actions:
                instance = doc.references.action(reference.id_hash, reference.ex_id)
                if instance is None:
                    continue
                name = instance.class_name or ''
                if 'PlayerPlayMotion2' in name and 'Override' not in name and 'SetWeaponPhoto' not in name:
                    block_name, instance_index = identity_map[(reference.id_hash, reference.ex_id)]
                    fields = doc.rsz_blocks.get_block(block_name).file.parsed_elements[instance_index]
                    previous = fields['v4_MotionID'].value
                    fields['v4_MotionID'].value = args.motion_id
                    print(f'  {node.name}: v4_MotionID {previous} -> {args.motion_id}')
                    retargeted.append((node.name, previous, args.motion_id))
                    done = True
                    break
            if not done:
                raise SystemExit(f'{node.name} has no PlayerPlayMotion2 action to retarget')

    for target_name, new_action_id in zip(args.node, action_ids):
        extra = {'v1_ID': new_action_id}
        extra.update(hit_values)
        clone_block_object(actions_block, hit_instance.index, extra)
        print(f'  new action {HIT_ACTION} for {target_name}: v1_ID={new_action_id}')
    action_splice = block_splice(actions_block)
    if len(actions_block.object_table) != len(old_action_objects) + len(targets):
        raise SystemExit('unexpected actions object count')

    # ---------------------------------------------------------------- new Condition instances
    conditions_block = doc.rsz_blocks.get_block('conditions')
    assert block_padding_is_zero(conditions_block), 'conditions block has non-zero padding'
    old_condition_objects = list(conditions_block.object_table)
    buff_objects, end_objects = [], []
    for (buff_id, end_id) in condition_ids:
        _, buff_object = clone_block_object(conditions_block, buff_condition.index, {'v0_ID': buff_id})
        buff_objects.append(buff_object)
        _, end_object = clone_block_object(conditions_block, end_condition.index, {'v0_ID': end_id})
        end_objects.append(end_object)
    condition_splice = block_splice(conditions_block)
    for buff_object, end_object, (buff_id, end_id) in zip(buff_objects, end_objects, condition_ids):
        print(f'  new conditions {BUFF_CONDITION} v0_ID={buff_id} object={buff_object}; '
              f'{END_CONDITION} v0_ID={end_id} object={end_object}')

    # ---------------------------------------------------------------- transition map table
    map_offset, map_count = doc.transition_map_tbl_offset, doc.transition_map_count
    data_offset, data_count = doc.transition_data_tbl_offset, doc.transition_data_count
    map_entries = [struct.unpack_from('<Ii', source, map_offset + i * 8) for i in range(map_count)]
    assert all(map_entries[i][0] <= map_entries[i + 1][0] for i in range(map_count - 1)), 'map table unsorted'
    assert map_offset % 16 == 0 and data_offset % 16 == 0, 'tail tables are not 16-byte aligned'
    old_gap = data_offset - (map_offset + map_count * 8)
    assert old_gap in (0, 8), f'unexpected map/data gap {old_gap}'
    entry = next((item for item in map_entries if item[0] == run_state_template.TransitionMaps), None)
    assert entry is not None, f'template map {run_state_template.TransitionMaps} is not in the map table'
    first_map_id = max({item[0] for item in map_entries} |
                       {state.TransitionMaps for n in nodes for state in n.states if state.TransitionMaps}) + 1
    new_map_ids = [first_map_id + i for i in range(len(targets))]
    new_map_bytes = b''.join(struct.pack('<Ii', map_id, entry[1]) for map_id in new_map_ids)
    print(f'  new transition maps {new_map_ids[0]}..{new_map_ids[-1]} -> data#{entry[1]} (one map id per state, like native)')

    # ---------------------------------------------------------------- selectors

    selectors_block = None
    selector_clones = []
    needing_selector = [(index, node) for index, node in targets if node.selector_id == -1]
    if needing_selector:
        if template.selector_id == -1:
            raise SystemExit(f'template {template.name} has no selector to clone')
        selectors_block = doc.rsz_blocks.get_block('selectors')
        if not block_padding_is_zero(selectors_block):
            raise SystemExit('selectors block has non-zero padding')
        template_selector = doc.references.object_instance('selectors', template.selector_id & 0xFFFFFFFF)
        for index, node in needing_selector:
            _, new_selector = clone_block_object(selectors_block, template_selector.index)
            selector_clones.append((node, new_selector))
            print(f'  {node.name}: selector -1 -> {new_selector} (clone of {template_selector.class_name})')

    # ---------------------------------------------------------------- new node records
    new_runs, planned = [], []
    for order, ((index, node), (run_id, end_id), new_action_id, buff_object, end_object) in enumerate(zip(
            targets, node_ids, action_ids, buff_objects, end_objects)):
        run_state = copy.deepcopy(run_state_template)
        run_state.mTransitions = end_id
        run_state.TransitionConditions = end_object
        run_state.TransitionMaps = new_map_ids[order]
        run_name = run_template.name
        new_run = BHVTNode(id_hash=run_id, ex_id=0, name_index=run_template.name_index, name=run_name,
                           parent=node.id_hash, parent_ex=node.ex_id,
                           children=[], selector_id=run_template.selector_id, selector_callers=[],
                           selector_caller_condition_id=run_template.selector_caller_condition_id,
                           actions=[Action(new_action_id, 0)],
                           priority=run_template.priority, node_attribute=run_template.node_attribute,
                           work_flags=run_template.work_flags,
                           name_hash=murmur3_hash(run_name.encode('utf-16le')),
                           fullname_hash=murmur3_hash(f'{node.name}.{run_name}'.encode('utf-16le')),
                           tags=list(run_template.tags), is_branch=run_template.is_branch,
                           is_end=run_template.is_end, states=[run_state], transitions=[], all_states=[],
                           reference_tree_index=run_template.reference_tree_index)
        end_name = end_template.name
        new_end = BHVTNode(id_hash=end_id, ex_id=0, name_index=end_template.name_index, name=end_name,
                           parent=node.id_hash, parent_ex=node.ex_id,
                           children=[], selector_id=end_template.selector_id, selector_callers=[],
                           selector_caller_condition_id=end_template.selector_caller_condition_id,
                           actions=[], priority=end_template.priority,
                           node_attribute=end_template.node_attribute, work_flags=end_template.work_flags,
                           name_hash=murmur3_hash(end_name.encode('utf-16le')),
                           fullname_hash=murmur3_hash(f'{node.name}.{end_name}'.encode('utf-16le')),
                           tags=list(end_template.tags), is_branch=end_template.is_branch,
                           is_end=end_template.is_end, states=[], transitions=[], all_states=[],
                           reference_tree_index=end_template.reference_tree_index)
        new_normal = None
        new_normal = None
        if normal_ids[order] is not None:
            normal_name = normal_template.name
            new_normal = BHVTNode(id_hash=normal_ids[order], ex_id=0, name_index=normal_template.name_index,
                                  name=normal_name, parent=node.id_hash, parent_ex=node.ex_id,
                                  children=[], selector_id=normal_template.selector_id, selector_callers=[],
                                  selector_caller_condition_id=normal_template.selector_caller_condition_id,
                                  actions=[], priority=normal_template.priority,
                                  node_attribute=normal_template.node_attribute,
                                  work_flags=normal_template.work_flags,
                                  name_hash=murmur3_hash(normal_name.encode('utf-16le')),
                                  fullname_hash=murmur3_hash(f'{node.name}.{normal_name}'.encode('utf-16le')),
                                  tags=list(normal_template.tags), is_branch=normal_template.is_branch,
                                  is_end=normal_template.is_end, states=[], transitions=[], all_states=[],
                                  reference_tree_index=normal_template.reference_tree_index)
        new_runs.append((new_run, new_end, new_normal))
        planned.append((node, run_id, end_id, buff_object, run_name))

    # ---------------------------------------------------------------- byte-exact writer

    nodes_start = doc.bhvt.offsets['nodes']
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'

    # ---------------------------------------------------------------- apply the edit
    for (node, run_id, end_id, buff_object, run_name), (new_run, new_end, new_normal), nid in zip(planned, new_runs, normal_ids):
        before_children, before_transitions = len(node.children), len(node.transitions)
        node.children.append(ChildNode(run_id, 0, run_child.condition_id))
        node.children.append(ChildNode(end_id, 0, -1))
        if new_normal is not None:
            node.children.append(ChildNode(nid, 0, -1))
        node.transitions.insert(0, Transition(
            mStartTransitionEvent=IndexList(list(buff_transition.mStartTransitionEvent.values)),
            mStartState=run_id, mStartStateTransition=buff_object, mStartStateEx=0))
        if new_normal is not None:
            node.transitions.append(Transition(
                mStartTransitionEvent=IndexList(list(buff_transition.mStartTransitionEvent.values)),
                mStartState=nid, mStartStateTransition=-1, mStartStateEx=0))
        nodes.append(new_run)
        nodes.append(new_end)
        if new_normal is not None:
            nodes.append(new_normal)
        print(f'  {node.name}: children {before_children} -> {len(node.children)}, '
              f'transitions {before_transitions} -> {len(node.transitions)} (ChainsawBuff first{", normal default appended" if new_normal else ""})')
    for _node, _selector in selector_clones:
        _node.selector_id = _selector
        print(f'  {_node.name}: selector = {_selector}')
    doc.bhvt.action_ex_ids = list(doc.bhvt.action_ex_ids) + [0] * len(targets)

    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)

    map_insert = map_offset + map_count * 8
    new_gap = (16 - (map_insert + len(new_map_bytes)) % 16) % 16
    assert new_gap in (0, 8), f'unexpected new gap {new_gap}'
    splices = [(nodes_start, node_tail, nodes_data), action_splice, condition_splice,
               (map_insert, data_offset, new_map_bytes + bytes(new_gap))]
    if selectors_block is not None:
        splices.append(block_splice(selectors_block))
    splices.sort()
    map_delta = (len(new_map_bytes) + new_gap) - old_gap
    output = splice_document(doc, splices)
    struct.pack_into('<i', output, HEADER_MAP_COUNT, map_count + len(targets))
    assert struct.unpack_from('<i', output, HEADER_DATA_COUNT)[0] == data_count, 'data count changed'
    tree_growth = len(output) - len(source) - map_delta
    assert tree_growth > 0, 'expected the BHVT tree to grow'
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)

    def verified_class_of_condition(raw):
        raw &= 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            return ''
        instance = verified.references.object_instance('conditions', raw)
        return (instance.class_name or '') if instance else ''

    new_indexes, _cursor = [], len(original.bhvt.nodes)
    for _run in new_runs:
        new_indexes.append(_cursor)
        _cursor += 2 + (1 if _run[2] is not None else 0)
    target_indexes = {index for index, _ in targets}
    for index, node in enumerate(original.bhvt.nodes):
        if index in target_indexes:
            continue
        assert verified.bhvt.nodes[index] == node, f'node {index} {node.name} changed'
    for (index, node), run_index, end_index in zip(targets, new_indexes, [i + 1 for i in new_indexes]):
        after = verified.bhvt.nodes[index]
        assert after.children == node.children, f'{node.name} children mismatch'
        added_transitions = len(after.transitions) - len(original.bhvt.nodes[index].transitions)
        assert added_transitions in (1, 2), added_transitions
        assert after.transitions[0].mStartState == verified.bhvt.nodes[run_index].id_hash
        assert BUFF_CONDITION in verified_class_of_condition(after.transitions[0].mStartStateTransition)
        for position, transition in enumerate(after.transitions[1:1 + len(original.bhvt.nodes[index].transitions)], start=1):
            assert transition.mStartState == original.bhvt.nodes[index].transitions[position - 1].mStartState
        if original.bhvt.nodes[index].transitions:
            assert after.transitions[0].mStartStateTransition !=             original.bhvt.nodes[index].transitions[0].mStartStateTransition
        if original.bhvt.nodes[index].selector_id == -1:
            assert after.selector_id != -1, f'{node.name}: selector was not assigned'
        for field in ('priority', 'node_attribute', 'work_flags', 'actions', 'states',
                      'all_states', 'is_branch', 'is_end', 'tags'):
            assert getattr(after, field) == getattr(original.bhvt.nodes[index], field), (node.name, field)

    for (index, node), run_index, end_index in zip(targets, new_indexes, [i + 1 for i in new_indexes]):
        new_run, new_end = verified.bhvt.nodes[run_index], verified.bhvt.nodes[end_index]
        assert new_run.name == run_template.name and new_end.name == end_template.name
        assert new_run.parent == verified.bhvt.nodes[index].id_hash == new_end.parent
        assert new_run.parent_ex == verified.bhvt.nodes[index].ex_id == new_end.parent_ex
        assert new_run.name_index == run_template.name_index and new_end.name_index == end_template.name_index
        assert new_run.children == [] and new_run.transitions == [] and new_run.all_states == []
        assert new_end.actions == [] and new_end.states == [] and new_end.children == []
        assert len(new_run.actions) == 1 and len(new_run.states) == 1
        hit = verified.references.action(new_run.actions[0].id_hash, new_run.actions[0].ex_id)
        assert HIT_ACTION in (hit.class_name or ''), hit.class_name
        fields = fields_of(hit)
        for name, value in hit_values.items():
            assert fields[name].value == value, (name, fields[name].value, value)
    if args.motion_id is not None:
        for reference in after.actions:
            instance = verified.references.action(reference.id_hash, reference.ex_id)
            name = instance.class_name or ''
            if 'PlayerPlayMotion2' in name and 'Override' not in name and 'SetWeaponPhoto' not in name:
                assert fields_of(instance)['v4_MotionID'].value == args.motion_id, (node.name, name)
                break
        else:
            raise SystemExit(f'{node.name}: retargeted motion not found in the candidate')
        state = new_run.states[0]
        assert state.mTransitions == new_end.id_hash, 'chainsaw_run state does not target its own chainsaw_end'
        assert END_CONDITION in verified_class_of_condition(state.TransitionConditions)
        assert state.TransitionMaps in new_map_ids
        assert state.mStates.values == run_state_template.mStates.values

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
            if transition.mStartStateTransition & 0xFFFFFFFF != 0xFFFFFFFF:
                verified.references.object_instance('conditions', transition.mStartStateTransition & 0xFFFFFFFF)

    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids + [0] * len(targets)
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    blocks = {'actions', 'conditions', 'selectors'}
    for name in BLOCK_NAMES:
        old, new = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        if name not in blocks:
            assert source[old.offset:old.end] == output[new.offset:new.end], f'{name} changed unexpectedly'
        else:
            assert new.object_table[:len(old.object_table)] == old.object_table, f'{name} object table reordered'
    new_block = verified.rsz_blocks.get_block('actions')
    assert new_block.object_table[:len(old_action_objects)] == old_action_objects
    assert len(new_block.file.instance_infos) == old_action_instances + len(targets)
    assert verified.transition_map_count == original.transition_map_count + len(targets)
    assert verified.transition_data_count == original.transition_data_count
    kept_map_bytes = original.transition_map_count * 8
    assert output[verified.transition_map_tbl_offset:
                  verified.transition_map_tbl_offset + kept_map_bytes] == \
        source[original.transition_map_tbl_offset:
               original.transition_map_tbl_offset + kept_map_bytes], 'existing map entries changed'
    assert output[verified.transition_map_tbl_offset + kept_map_bytes:
                  verified.transition_data_tbl_offset] == new_map_bytes + bytes(new_gap), 'new map entries wrong'
    assert output[verified.transition_data_tbl_offset:] == source[original.transition_data_tbl_offset:], \
        'transition data table changed'
    assert verified.transition_data_tbl_offset - (verified.transition_map_tbl_offset +
           verified.transition_map_count * 8) in (0, 8), 'map/data gap changed shape'
    assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), args.allow_uvar_drift)
    assert verified.rebuild() == output, 'rebuild is not stable on the candidate'

    return bytes(output)
