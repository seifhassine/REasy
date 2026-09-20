"""Shared BHVT node-table serialization for structural edits."""
import struct
from .rebuild import BhvtPointers


def pack(fmt, *values):
    return struct.pack('<' + fmt, *values)


def array(values, fmt='I'):
    return pack('I', len(values)) + pack(fmt * len(values), *values)


def columns(values, fields):
    return b''.join(pack(fmt * len(values), *(getattr(v, name) for v in values)) for name, fmt in fields)


def serialize_node(document, n):
    data = pack('5I', n.id_hash, n.ex_id, n.name_index, n.parent, n.parent_ex)
    data += pack('I', len(n.children)) + columns(n.children, [('id_hash', 'I'), ('ex_id', 'I'), ('condition_id', 'i')])
    data += pack('i', n.selector_id) + array(n.selector_callers, 'i') + pack('i', n.selector_caller_condition_id)
    data += pack('I', len(n.actions)) + columns(n.actions, [('id_hash', 'I'), ('ex_id', 'I')])
    data += pack('iHH', n.priority, n.node_attribute, n.work_flags)
    if n.is_fsm:
        data += pack('II', n.name_hash, n.fullname_hash) + array(n.tags) + pack('BB', n.is_branch, n.is_end)
    data += pack('I', len(n.states)) + b''.join(array(s.mStates.values, 'i') for s in n.states)
    data += columns(n.states, [('mTransitions', 'I'), ('TransitionConditions', 'i'), ('TransitionMaps', 'I'),
                               ('mTransitionAttributes', 'I'), ('mStatesEx', 'I')])
    data += pack('I', len(n.transitions))
    for t in n.transitions:
        data += array(t.mStartTransitionEvent.values, 'i') if document.layout.transition_event_lists is not False \
            else pack('I', t.mStartTransitionEvent.values[0])
    data += columns(n.transitions, [('mStartState', 'I'), ('mStartStateTransition', 'i')])
    if document.layout.transition_state_ex:
        data += columns(n.transitions, [('mStartStateEx', 'I')])
    if not n.has_reference_tree:
        data += pack('I', len(n.all_states)) + columns(n.all_states, [('mAllState', 'I'), ('mAllTransition', 'i'),
                     ('mAllTransitionID', 'I'), ('mAllStateEx', 'I'), ('mAllTransitionAttributes', 'i')])
    return data + pack('i', n.reference_tree_index)


def serialize_nodes(document):
    nodes = document.bhvt.nodes
    return (pack("I", len(nodes)) + b"".join(serialize_node(document, n) for n in nodes)
            + array(document.bhvt.action_ex_ids) + array(document.bhvt.static_action_ex_ids))


def splice_document(document, splices):
    """Apply disjoint section replacements and relocate the native pointer tables."""
    splices = sorted(splices)
    source = document.source
    output, cursor = bytearray(), 0
    for start, end, payload in splices:
        if not cursor <= start <= end <= len(source):
            raise ValueError(f'Invalid or overlapping BHVT splice: {start}:{end}')
        output.extend(source[cursor:start])
        output.extend(payload)
        cursor = end
    output.extend(source[cursor:])

    def relocate(offset):
        delta = 0
        for start, end, payload in splices:
            if offset < start:
                break
            if offset == start and start != end:
                return offset + delta
            if offset < end:
                raise ValueError(f'Pointer inside replaced data at 0x{offset:X}')
            delta += len(payload) - (end - start)
        return offset + delta

    base = document.tree_data_offset
    for slot in BhvtPointers(document).collect():
        value = struct.unpack_from('<Q', source, slot)[0]
        struct.pack_into('<Q', output, relocate(slot), relocate(base + value) - relocate(base) if value else 0)
    for slot in (16, 24, 32, 40):
        value = struct.unpack_from('<Q', source, slot)[0]
        struct.pack_into('<Q', output, slot, relocate(value) if value else 0)
    struct.pack_into('<I', output, relocate(document.tree_info_ptr),
                     relocate(base + document.tree_data_size) - relocate(base))
    return output
