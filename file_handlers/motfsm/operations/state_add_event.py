"""Append one more transition event to an existing BHVT state, cloned from a native event.

Transition events are what carry the per-derivation extras: `PlayerFsm2EventSetStateAppendType`
(run/walk entry mode) and `PlayerFsm2EventStateInitOption` (`_AngleSetType` / `_LimitAngle` = the
turn limit in degrees, e.g. 180).  A state's `mStates.values` holds **object indexes** into the
`transition_events` block, so a new event is one cloned instance plus one index appended there.

"""
import logging
from dataclasses import dataclass

from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.authoring import block_splice, clone_block_object, verify_block

EVENTS_BLOCK = 'transition_events'
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from file_handlers.motfsm.selection import resolve_node
from file_handlers.motfsm.values import assignments

log = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class StateAddEventPlan:
    node: str
    state: int
    from_node: str
    from_state: int
    from_event: int = 0
    set: tuple[str, ...] = ()


def build(source: bytes, plan: StateAddEventPlan) -> bytes:
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes


    def node_index(text):
        return resolve_node(doc, text)


    node = nodes[node_index(plan.node)]
    template_node = nodes[node_index(plan.from_node)]
    assert 0 <= plan.state < len(node.states), f'state index out of range: {plan.state}'
    assert 0 <= plan.from_state < len(template_node.states), 'template state index out of range'
    values = list(getattr(template_node.states[plan.from_state].mStates, 'values', []) or [])
    assert values, f'{template_node.name}.state[{plan.from_state}] has no transition event'
    if not 0 <= plan.from_event < len(values):
        raise ValueError(f'Template event index out of range: {plan.from_event}')
    template_object = values[plan.from_event]

    events_block = doc.rsz_blocks.get_block(EVENTS_BLOCK)
    template_instance = doc.references.object_instance(EVENTS_BLOCK, template_object)
    assert template_instance is not None, f'event object {template_object} does not resolve'
    file_index = template_instance.index
    template_block_name = 'static_transition_events' if (template_object & 0xFFFFFFFF) >> 24 == 0x40 else EVENTS_BLOCK
    log.info(f'  template event: {template_instance.class_name}')

    overrides = assignments(template_instance, plan.set)
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

    _, new_object = clone_block_object(
        events_block, file_index, overrides,
        source_block=doc.rsz_blocks.get_block(template_block_name))
    log.info(f'  new event instance={new_object} ' + ', '.join(f'{k}={v}' for k, v in overrides.items()))


    events_splice = block_splice(events_block)
    node.states[plan.state].mStates.values.append(new_object)


    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    node.states[plan.state].mStates.values.pop()
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    node.states[plan.state].mStates.values.append(new_object)

    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    splices = sorted([(nodes_start, node_tail, nodes_data), events_splice])
    output = splice_document(doc, splices)
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
    check = verified.bhvt.nodes[node_index(plan.node)]
    assert list(check.states[plan.state].mStates.values)[-1] == new_object
    new_event = verified.references.object_instance(EVENTS_BLOCK, new_object)
    assert new_event.class_name == template_instance.class_name
    verify_block(doc, verified, EVENTS_BLOCK)

    return bytes(output)
