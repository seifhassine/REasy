"""Clone a leaf-shaped BHVT node with fresh actions / conditions / identities.

  * `--copy-node` selects the node whose actions and attributes are cloned.
  * `--derive-from` selects the state template; `--derive-condition` filters condition classes.
  * `--drop-class` removes actions (e.g. the inherited hit / addlv).
  * `--set-effect N` keeps `N` SetEffect actions (extra copies of the first) and `--effect
    container:element:frame` retargets them.
  * `--calc-lv` adds a `PlayerFsm2ActionLongSwordCalcLv` cloned from `--calc-lv-from`, placed in
    the slot of the dropped action named by `--calc-lv-after` (default: the dropped AddLv).

Same machinery as REasy's own game-verified fixtures (`tools/motfsm_validation/`):
`serialize_nodes()` (byte-exact, asserted against the source), the RSZ block splices, the
`BhvtPointers` relocation pass and `verified.rebuild() == output` (REasy's own writer is stable on
the candidate). Each copied state owns a fresh transition map ID, referencing the
template data record. Existing nodes and their instances are not modified.

"""
import copy
import struct


from file_handlers.motfsm.motfsm_file import MotfsmFile, BHVTNode, ChildNode, Action
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from utils.hash_util import murmur3_hash

MOTION_CLASS = 'snow.PlayerPlayMotion2'
EFFECT_SUFFIX = 'SetEffect'
CALC_LV_SUFFIX = 'LongSwordCalcLv'
TRANSITION_DATA_SIZE = 36
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node, node_string


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--name', required=True)
    parser.add_argument('--copy-node', required=True, help='template node name, e.g. atk_620')
    parser.add_argument('--parent', required=True)
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--derive-from', help='node whose states are cloned; defaults to --copy-node')
    parser.add_argument('--derive-condition', action='append', default=[],
                        help='keep only states whose condition class contains this text (repeatable)')
    parser.add_argument('--drop-class', action='append', default=[], help='drop actions by class substring')
    parser.add_argument('--set-effect', type=int, default=1, help='number of SetEffect actions to keep')
    parser.add_argument('--effect', action='append', default=[], metavar='CONTAINER:ELEMENT:FRAME',
                        help='retarget the SetEffect actions in order')
    parser.add_argument('--calc-lv', type=int, help='add LongSwordCalcLv with this addLv (e.g. -1)')
    parser.add_argument('--calc-lv-from', default='atk_127', help='node owning the CalcLv template')
    parser.add_argument('--calc-lv-after', default='AddLv',
                        help='the dropped action (class substring) whose slot the CalcLv action takes')
    parser.add_argument('--selector', choices=('leaf', 'clone'), default='leaf')
    parser.add_argument('--allow-no-motion', action='store_true',
                        help='template may have no PlayerPlayMotion2 action (pure branch node)')


def run(args):
    TARGET = args.source.resolve()
    source = TARGET.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes

    def node_index(text):
        return resolve_node(doc, text)

    template_index = node_index(args.copy_node)
    template = nodes[template_index]
    derive_index = node_index(args.derive_from) if args.derive_from else template_index
    derive = nodes[derive_index]
    parent_index = node_index(args.parent)
    parent = nodes[parent_index]
    assert not any(n.name == args.name for n in nodes), f'node name already used: {args.name}'

    def next_identity(used):
        value = 1
        while value != 0xFFFFFFFF and value in used:
            value += 1
        if value == 0xFFFFFFFF:
            raise ValueError('no free identity')
        return value

    def fields_of(instance):
        return {field.name: field for field in instance.fields}

    def class_of(reference):
        return doc.references.action(reference.id_hash, reference.ex_id)

    def condition_class(state):
        raw = state.TransitionConditions & 0xFFFFFFFF
        if raw == 0xFFFFFFFF:
            return None
        instance = doc.references.object_instance('conditions', raw)
        if instance is None:
            raise ValueError(f'state condition 0x{raw:08X} does not resolve')
        return instance

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
        """Append a copy of one RSZ instance; returns (instance_index, object_index).

        `source_block` names the block whose instance table `template_instance_index` belongs to. Every
        RSZ block is parsed as its own table, so an index resolved inside `static_actions` must not be
        read back through the `actions` table - that silently copies an unrelated instance.
        """
        native = block.file
        source = (source_block or block).file
        instance_index = len(native.instance_infos)
        native.instance_infos.append(copy.deepcopy(source.instance_infos[template_instance_index]))
        native.parsed_elements[instance_index] = copy.deepcopy(source.parsed_elements[template_instance_index])
        if new_fields:
            for name, value in new_fields.items():
                native.parsed_elements[instance_index][name].value = value
        native.object_table.append(instance_index)
        return instance_index, len(block.object_table) - 1

    def block_splice(block):
        data = block.file.build_validated()
        return (block.offset, block.end, data + bytes((-len(data)) % 16))

    # ---------------------------------------------------------------- plan the actions
    kept, dropped, effect_indices, calc_plan_index = [], [], [], None
    template_effects = []
    for position, reference in enumerate(template.actions):
        if class_of(reference).class_name.endswith(EFFECT_SUFFIX):
            template_effects.append(position)
    keep_effects = set(template_effects[:max(0, args.set_effect)])
    for position, reference in enumerate(template.actions):
        instance = class_of(reference)
        class_name = instance.class_name
        if class_name.endswith(EFFECT_SUFFIX) and position not in keep_effects:
            dropped.append((position, class_name, f'_ElementID={fields_of(instance)["_ElementID"].value}'))
            continue
        if any(text.lower() in class_name.lower() for text in args.drop_class):
            dropped.append((position, class_name, ''))
            if calc_plan_index is None and args.calc_lv is not None and args.calc_lv_after.lower() in class_name.lower():
                calc_plan_index = len(kept)
            continue
        if class_name.endswith(EFFECT_SUFFIX):
            effect_indices.append(len(kept))
        kept.append((position, reference, instance))

    motion_count = sum(1 for _, _, i in kept if i.class_name == MOTION_CLASS)
    expected_motion = 0 if args.allow_no_motion else 1
    assert motion_count == expected_motion, (expected_motion, motion_count)
    assert not args.set_effect or effect_indices, f'template node {template.name} has no {EFFECT_SUFFIX} action'

    # clone plan: kept actions, extra SetEffect copies right behind the first one, CalcLv in the AddLv slot
    plan = [('keep', item) for item in kept]
    extra_effect_copies = max(0, args.set_effect - len(effect_indices))
    if extra_effect_copies:
        first_effect_plan_index = effect_indices[0]
        for offset in range(extra_effect_copies):
            plan.insert(first_effect_plan_index + 1 + offset, ('effect_copy', kept[first_effect_plan_index]))
            effect_indices.append(first_effect_plan_index + 1 + offset)
    calc_template, calc_template_block, calc_owner = None, None, None
    if args.calc_lv is not None:
        candidates = [node_index(args.calc_lv_from)]
        for candidate in candidates:
            for reference in nodes[candidate].actions:
                instance = class_of(reference)
                if instance.class_name.endswith(CALC_LV_SUFFIX):
                    block_name, instance_index = doc.references.action_identities()[(reference.id_hash, reference.ex_id)]
                    assert block_name == 'actions', f'CalcLv action lives in {block_name}'
                    calc_template, calc_template_block = instance, instance_index
                    calc_owner = nodes[candidate].name
                    break
            if calc_template is not None:
                break
        if calc_template is None:
            raise SystemExit(f'no {CALC_LV_SUFFIX} action found in {args.calc_lv_from}')
        plan.insert(len(kept) if calc_plan_index is None else calc_plan_index, ('calc', None))

    # ---------------------------------------------------------------- identities
    new_node_id = next_identity({n.id_hash for n in nodes})
    cursor = block_ids(('actions', 'static_actions'), 1)
    new_action_ids = []
    for _ in plan:
        value = next_identity(cursor)
        cursor.add(value)
        new_action_ids.append(value)
    cursor = block_ids(('conditions', 'static_conditions', 'expression_tree_conditions',
                        'static_expression_tree_conditions'), 0)
    derive_states = list(derive.states)
    if args.derive_condition:
        derive_states = [state for state in derive.states
                         if condition_class(state) is not None and
                         any(text.lower() in condition_class(state).class_name.lower()
                             for text in args.derive_condition)]
        if not derive_states:
            raise SystemExit(f'{derive.name} has no state matching {args.derive_condition}')
    new_condition_ids = []
    for _ in derive_states:
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
    new_action_refs, plan_instances, motion_instance, first_effect_instance = [], [], None, None
    for kind, item in plan:
        new_id = new_action_ids[len(new_action_refs)]
        if kind == 'calc':
            clone_block_object(actions_block, calc_template_block, {'v1_ID': new_id, 'addLv': args.calc_lv})
            new_action_refs.append(Action(new_id, 0))
            plan_instances.append(len(native_actions.instance_infos) - 1)
            print(f'  add  action {CALC_LV_SUFFIX:35s} new v1_ID={new_id} addLv={args.calc_lv} '
                  f'(template {calc_owner})')
            continue
        position, reference, instance = item
        if kind == 'effect_copy':
            clone_block_object(actions_block, first_effect_instance, {'v1_ID': new_id})
            new_action_refs.append(Action(new_id, 0))
            plan_instances.append(len(native_actions.instance_infos) - 1)
            print(f'  add  action [{position:2d}] {instance.class_name:52s} new v1_ID={new_id} (copy)')
            continue
        # Locate the template instance by its own identity inside this document, remembering which
        # block's instance table the index belongs to.
        block_name, action_instance_index = identity_map[(reference.id_hash, reference.ex_id)]
        template_block = doc.rsz_blocks.get_block(block_name)
        extra = {'v1_ID': new_id}
        if instance.class_name == MOTION_CLASS:
            extra['v4_MotionID'] = args.motion
        clone_block_object(actions_block, action_instance_index, extra, source_block=template_block)
        new_index = len(native_actions.instance_infos) - 1
        plan_instances.append(new_index)
        if instance.class_name == MOTION_CLASS:
            motion_instance = new_index
        if instance.class_name.endswith(EFFECT_SUFFIX) and first_effect_instance is None:
            first_effect_instance = new_index
        new_action_refs.append(Action(new_id, 0))
        print(f'  keep action[{position:2d}] {instance.class_name:52s} new v1_ID={new_id}')
    for position, class_name, reason in dropped:
        print(f'  drop action[{position:2d}] {class_name:52s} {reason}')
    assert args.allow_no_motion or motion_instance is not None, 'template has no PlayerPlayMotion2 action'

    # ---------------------------------------------------------------- retarget the SetEffect actions
    effect_indices = [i for i, (kind, item) in enumerate(plan)
                      if kind != 'calc' and item[2].class_name.endswith(EFFECT_SUFFIX)]
    for order, plan_index in enumerate(effect_indices):
        if order >= len(args.effect):
            continue
        container, element, frame = (int(part) for part in args.effect[order].split(':'))
        fields = native_actions.parsed_elements[plan_instances[plan_index]]
        fields['containerID'].value, fields['_ElementID'].value, fields['_Frame'].value = container, element, float(frame)
        print(f'  point SetEffect[{order}] containerID={container} _ElementID={element} _Frame={frame}')
    action_splice = block_splice(actions_block)

    # ---------------------------------------------------------------- new Condition instances
    new_state_conditions, condition_blocks = [], {}
    for state, new_id in zip(derive_states, new_condition_ids):
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

    # ---------------------------------------------------------------- independent maps, shared transition data
    map_offset, map_count = doc.transition_map_tbl_offset, doc.transition_map_count
    data_offset, data_count = doc.transition_data_tbl_offset, doc.transition_data_count
    entries = dict(struct.unpack_from('<Ii', source, map_offset+i*8) for i in range(map_count))
    cursor = max(set(entries) | {s.TransitionMaps for n in nodes for s in n.states}, default=0) + 1
    new_state_transitions = list(range(cursor, cursor+len(derive_states)))
    new_map_bytes = b''.join(struct.pack('<Ii', fresh, entries[state.TransitionMaps])
                             for fresh, state in zip(new_state_transitions, derive_states))
    new_state_events = [list(state.mStates.values) for state in derive_states]
    for fresh, state in zip(new_state_transitions, derive_states):
        print(f'  new transition map {fresh} -> data#{entries[state.TransitionMaps]}')

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

    new_states = copy.deepcopy(derive_states)
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
    original_ex_ids = list(doc.bhvt.action_ex_ids)
    nodes.append(new_node)
    parent.children.append(ChildNode(new_node_id, 0, -1))
    doc.bhvt.action_ex_ids = list(doc.bhvt.action_ex_ids) + [0] * len(new_action_refs)

    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    nodes.pop()
    parent.children.pop()
    # Preserve the source extension table even for templates without actions.
    doc.bhvt.action_ex_ids = original_ex_ids
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    nodes.append(new_node)
    parent.children.append(ChildNode(new_node_id, 0, -1))
    doc.bhvt.action_ex_ids = list(doc.bhvt.action_ex_ids) + [0] * len(new_action_refs)

    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    splices = [(nodes_start, node_tail, nodes_data), action_splice, (strings_start, strings_end, strings_data)]
    splices += [block_splice(block) for block in condition_blocks.values()]
    if selectors_block is not None:
        splices.append(block_splice(selectors_block))
    added_maps = len(new_state_transitions)
    assert map_offset % 16 == 0 and data_offset % 16 == 0, 'tail tables are not 16-byte aligned in the source'
    assert data_offset - (map_offset + map_count * 8) in (0, 8), 'unexpected map/data gap'
    map_insert = map_offset + map_count * 8
    map_gap = (-(map_insert + len(new_map_bytes))) % 16
    if added_maps:
        splices.append((map_insert, data_offset, new_map_bytes + bytes(map_gap)))
    splices.sort()
    output = splice_document(doc, splices)
    struct.pack_into('<i', output, 48, map_count + added_maps)
    tree_growth = len(output) - len(source)
    assert tree_growth > 0, 'expected the BHVT tree to grow'
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = MotfsmFile(), MotfsmFile()
    verified.read(output)
    original.read(source)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids + [0] * len(new_action_refs)
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    touched = {'actions', 'conditions', 'static_conditions', 'selectors'}
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
    # Existing maps and all transition data stay unchanged.
    assert verified.transition_map_count == original.transition_map_count + added_maps
    assert verified.transition_data_count == original.transition_data_count
    expected_maps = source[map_offset:map_offset+map_count*8] + new_map_bytes
    assert output[verified.transition_map_tbl_offset:verified.transition_map_tbl_offset+len(expected_maps)] == expected_maps
    assert output[verified.transition_data_tbl_offset:
                  verified.transition_data_tbl_offset + verified.transition_data_count * TRANSITION_DATA_SIZE] ==     source[original.transition_data_tbl_offset:
               original.transition_data_tbl_offset + original.transition_data_count * TRANSITION_DATA_SIZE],     'transition data table changed'
    tree_growth_check = verified.transition_map_tbl_offset - original.transition_map_tbl_offset
    map_growth = (len(new_map_bytes)+map_gap-(data_offset-map_insert)) if added_maps else 0
    assert tree_growth_check > 0 and tree_growth_check + map_growth == tree_growth, 'tree growth mismatch'
    assert verified.transition_data_tbl_offset > original.transition_data_tbl_offset, 'transition data table did not shift'
    assert verified.transition_map_tbl_offset % 16 == 0, 'map table lost 16-byte alignment'
    assert verified.transition_data_tbl_offset % 16 == 0, 'transition data table lost 16-byte alignment'
    assert verified.transition_data_tbl_offset + verified.transition_data_count * TRANSITION_DATA_SIZE == len(output), \
        'transition data table must end at EOF'
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
        for state in node.all_states:
            verified.references.node_index(state.mAllState, state.mAllStateEx)

    clone = verified.bhvt.nodes[new_node_index]
    clone_actions = [verified.references.action(a.id_hash, a.ex_id) for a in clone.actions]
    assert len(clone_actions) == len(new_action_refs)
    assert {a.id_hash for a in clone.actions}.isdisjoint({a.id_hash for n in original.bhvt.nodes for a in n.actions})
    assert len(verified.references.action_identities()) == len(original.references.action_identities()) + len(new_action_refs)
    if not args.allow_no_motion:
        motion = fields_of(clone_actions[[i.class_name for i in clone_actions].index(MOTION_CLASS)])
        template_motion = next(i for _, _, i in kept if i.class_name == MOTION_CLASS)
        assert motion['v4_MotionID'].value == args.motion
        assert motion['v3_BankID'].value == fields_of(template_motion)['v3_BankID'].value
    clone_effects = [fields_of(a) for a in clone_actions if a.class_name.endswith(EFFECT_SUFFIX)]
    assert len(clone_effects) == args.set_effect, (len(clone_effects), args.set_effect)
    for order, fields in enumerate(clone_effects):
        if order < len(args.effect):
            container, element, frame = (int(part) for part in args.effect[order].split(':'))
            assert (fields['containerID'].value, fields['_ElementID'].value, fields['_Frame'].value) == \
                (container, element, float(frame)), fields
    if calc_template is not None:
        clone_calc = [fields_of(a) for a in clone_actions if a.class_name.endswith(CALC_LV_SUFFIX)]
        assert len(clone_calc) == 1 and clone_calc[0]['addLv'].value == args.calc_lv, clone_calc
    for text in args.drop_class:
        assert not any(text.lower() in a.class_name.lower() for a in clone_actions), text
    assert [s.TransitionConditions & 0xFFFFFFFF for s in clone.states] == [v & 0xFFFFFFFF for v in new_state_conditions]
    assert [s.TransitionMaps for s in clone.states] == new_state_transitions
    assert [s.mStates.values for s in clone.states] == new_state_events, 'transition events were not re-created'
    assert [s.mTransitions for s in clone.states] == [s.mTransitions for s in derive_states], 'derivation target changed'
    assert not clone.children and not clone.transitions and not clone.all_states
    assert clone.name_hash == murmur3_hash(args.name.encode('utf-16le'))
    old_events = original.rsz_blocks.get_block('transition_events')
    # Transition events are shared by identity, so the clone may legitimately reuse pre-existing
    # event objects; the round-trip assert above already proved the values resolve.
    event_slots = max(len(old_events.object_table), len(old_events.file.parsed_elements))
    for value in [v for events in new_state_events for v in events]:
        assert value < event_slots, f'transition event object {value} out of range'
    assert len(set(new_state_transitions)) == len(clone.states)
    assert set(new_state_transitions).isdisjoint(s.TransitionMaps for n in original.bhvt.nodes for s in n.states)

    return bytes(output)
