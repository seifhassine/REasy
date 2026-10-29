"""Clone an attack leaf with independent actions, conditions, and transition maps.

Fully independent clone, following REE-Content-Editor's BhvtEditing / BHVTNode.Clone recipe:
  * new node record, new name string, new Action instances, new Condition instances
  * new transition-event instances (transition_events is 1:1 with states in the source)
  * new transition map entry + cloned TransitionData (also 1:1 in the source)
  * selector: copy the template selector when present; --selector=leaf explicitly removes it
Children and start transitions are dropped by request (leaf-node shape, like the game's own
attack leaves such as atk_109_1).

"""
import copy
import struct
from dataclasses import dataclass


from file_handlers.motfsm.motfsm_file import BHVTNode, ChildNode, Action
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.authoring import (
    block_ids, block_padding_is_zero, block_splice, clone_block_object,
    next_identity, read_document, verify_block,
)
from file_handlers.motfsm.validation import assert_uvar_stable, variable_snapshot
from utils.hash_util import murmur3_hash

MOTION_CLASS = 'snow.PlayerPlayMotion2'
HIT_CLASS = 'snow.player.fsm.PlayerHitAction2'
EFFECT_SUFFIX = 'SetEffect'
TRANSITION_DATA_SIZE = 36          # id, data, exitFrame, startFrame, interpolationFrame, contOnLayer*4, 4B align
from file_handlers.motfsm.serialization import serialize_nodes
from file_handlers.motfsm.selection import resolve_node, node_string
from file_handlers.motfsm.selectors import clone_selector


@dataclass(frozen=True)
class AttackClonePlan:
    template: str
    parent: str
    name: str
    motion: int
    keep_hit_index: int
    allow_uvar_drift: bool = False
    selector: str = 'clone'


def build(source: bytes, recipe: AttackClonePlan, *, registry=None) -> bytes:
    doc = read_document(source, registry)
    nodes = doc.bhvt.nodes

    template_index = resolve_node(doc, recipe.template)
    template = nodes[template_index]
    parent_index = resolve_node(doc, recipe.parent)
    parent = nodes[parent_index]
    assert not any(n.name == recipe.name for n in nodes), f'node name already used: {recipe.name}'
    assert len(template.actions) >= 1, (template.name, len(template.actions))


    def fields_of(instance):
        return {field.name: field for field in instance.fields}


    # ---------------------------------------------------------------- pick Actions
    kept, dropped = [], []
    effects = 0
    for position, reference in enumerate(template.actions):
        instance = doc.references.action(reference.id_hash, reference.ex_id)
        fields = fields_of(instance)
        if instance.class_name == HIT_CLASS and fields['_hitIndex'].value != recipe.keep_hit_index:
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
    cursor = block_ids(doc, ('actions', 'static_actions'), 1)
    new_action_ids = []
    for _ in kept:
        value = next_identity(cursor)
        cursor.add(value)
        new_action_ids.append(value)
    cursor = block_ids(doc, ('conditions', 'static_conditions', 'expression_tree_conditions',
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
    assert block_padding_is_zero(actions_block), 'actions block has non-zero padding'
    new_action_refs, motion_instance = [], None
    for (position, reference, instance), new_id in zip(kept, new_action_ids):
        block_name, instance_index = identity_map[(reference.id_hash, reference.ex_id)]
        extra = {'v1_ID': new_id}
        if instance.class_name == MOTION_CLASS:
            extra['v4_MotionID'] = recipe.motion
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
            template_instance = doc.references.object_instance('transition_events', value)
            assert template_instance is not None, f'unknown transition event object {value}'
            _, new_object = clone_block_object(events_block, template_instance.index, source_block=template_instance.block)
            indices.append(new_object)
        new_state_events.append(indices)
        if indices:
            print(f'  new transition event(s) {template_instance.class_name:44s} '
                  f'{state.mStates.values} -> {indices}')

    # ---------------------------------------------------------------- new transition maps + data
    map_offset, map_count = doc.transition_map_tbl_offset, doc.transition_map_count
    data_offset, data_count = doc.transition_data_tbl_offset, doc.transition_data_count
    from file_handlers.motfsm.transitions import TransitionCopies
    copies = TransitionCopies(doc)
    map_entries = [(identity, target) for identity, (_, target) in copies.tables.maps.items()]
    cloned_maps = [copies.clone(state.TransitionMaps, independent_data=True) for state in template.states]
    new_state_transitions = [identity for identity, _ in cloned_maps]
    new_state_data = [index for _, index in cloned_maps]
    new_map_bytes, new_data_bytes = copies.map_bytes, copies.data_bytes

    # ---------------------------------------------------------------- selector
    selectors_block, new_selector_id = None, -1
    if recipe.selector == 'clone' and template.selector_id & 0xFFFFFFFF != 0xFFFFFFFF:
        selectors_block = doc.rsz_blocks.get_block('selectors')
        assert block_padding_is_zero(selectors_block), 'selectors block has non-zero padding'
        new_selector_id = clone_selector(doc, template.selector_id)
        print(f'  new selector object={new_selector_id}')

    # ---------------------------------------------------------------- new node record
    strings_start = doc.bhvt.offsets['strings']
    string_count = struct.unpack_from('<I', source, strings_start)[0]
    strings_end = strings_start + 4 + string_count * 2
    new_string = node_string(recipe.name)
    strings_data = struct.pack('<I', string_count + len(new_string) // 2) + \
        source[strings_start + 4:strings_end] + new_string

    new_states = copy.deepcopy(template.states)
    for state, raw, events, transition in zip(new_states, new_state_conditions, new_state_events,
                                              new_state_transitions):
        state.TransitionConditions = raw
        state.mStates.values = events
        state.TransitionMaps = transition
    new_node = BHVTNode(id_hash=new_node_id, ex_id=0, name_index=string_count, name=recipe.name,
                        parent=parent.id_hash, parent_ex=parent.ex_id,
                        children=[], selector_id=new_selector_id, selector_callers=[],
                        selector_caller_condition_id=template.selector_caller_condition_id,
                        actions=new_action_refs, priority=template.priority,
                        node_attribute=template.node_attribute, work_flags=template.work_flags,
                        name_hash=murmur3_hash(recipe.name.encode('utf-16le')),
                        fullname_hash=murmur3_hash(f'{parent.name}.{recipe.name}'.encode('utf-16le')),
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
    # The corpus keeps both tail tables 16-byte aligned; pad the map growth so the data table stays aligned.
    assert map_offset % 16 == 0 and data_offset % 16 == 0, 'tail tables are not 16-byte aligned in the source'
    old_gap = data_offset - map_insert
    assert old_gap in (0, 8), f'unexpected map/data gap {old_gap}'
    added_maps = len(new_map_bytes) // 8
    new_map_end = map_insert + len(new_map_bytes)
    new_gap = (16 - new_map_end % 16) % 16        # corpus keeps gap 0/8 with a 16-byte aligned data table
    assert new_gap in (0, 8), f'unexpected new gap {new_gap}'
    map_delta = (len(new_map_bytes) + new_gap) - old_gap
    output = copies.apply(splices)
    tree_growth = len(output) - len(source) - map_delta - len(new_data_bytes)
    assert tree_growth > 0, 'expected the BHVT tree to grow'
    output = bytes(output)

    # ---------------------------------------------------------------- verification
    verified, original = read_document(output, registry), read_document(source, registry)
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
            verify_block(doc, verified, name)
    new_block = verified.rsz_blocks.get_block('actions')
    assert len(new_block.object_table) == len(old_objects) + len(new_action_refs)
    assert new_block.object_table[:len(old_objects)] == old_objects
    assert len(new_block.file.instance_infos) == len(native_actions.instance_infos)
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
    assert_uvar_stable(variable_snapshot(original), variable_snapshot(verified), recipe.allow_uvar_drift)
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
            verified.references.state_target(state.mTransitions, state.mTransitionAttributes)
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
    assert motion['v4_MotionID'].value == recipe.motion
    template_motion = next(i for _, _, i in kept if i.class_name == MOTION_CLASS)
    assert motion['v3_BankID'].value == fields_of(template_motion)['v3_BankID'].value
    assert [s.TransitionConditions & 0xFFFFFFFF for s in clone.states] == [v & 0xFFFFFFFF for v in new_state_conditions]
    assert [s.TransitionMaps for s in clone.states] == new_state_transitions
    assert [s.mStates.values for s in clone.states] == new_state_events, 'transition events were not re-created'
    assert [s.mTransitions for s in clone.states] == [s.mTransitions for s in template.states], 'derivation target changed'
    assert not clone.children and not clone.transitions
    assert clone.selector_id == new_selector_id
    if recipe.selector == 'clone' and template.selector_id & 0xFFFFFFFF != 0xFFFFFFFF:
        assert clone.selector_id != template.selector_id, 'selector is still shared with the template'
    old_events = original.rsz_blocks.get_block('transition_events')
    old_event_objects = len(old_events.object_table)
    for value in [v for events in new_state_events for v in events]:
        assert value >= old_event_objects, f'transition event object {value} is not a newly appended slot'
    new_ids = {struct.unpack_from('<Ii', output, map_insert + tree_growth + i * 8)[0] for i in range(added_maps)}
    assert new_ids.isdisjoint({s.TransitionMaps for n in original.bhvt.nodes for s in n.states})
    assert clone.name_hash == murmur3_hash(recipe.name.encode('utf-16le'))

    return bytes(output)
