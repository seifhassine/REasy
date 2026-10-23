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
    if n.has_reference_tree and n.all_states:
        raise ValueError(f'Node {n.name}: reference-tree nodes cannot store AllStates')
    if not n.is_fsm and (n.name_hash or n.fullname_hash or n.tags or n.is_branch or n.is_end):
        raise ValueError(f'Node {n.name}: FSM metadata on a non-FSM node')
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
        if document.layout.transition_event_lists is None and t.mStartTransitionEvent.values:
            raise ValueError('Choose an explicit TransitionEvent list/scalar layout before adding events')
        if document.layout.transition_event_lists is False and len(t.mStartTransitionEvent.values) != 1:
            raise ValueError('Scalar TransitionEvent layout requires exactly one hash')
        if not document.layout.transition_state_ex and t.mStartStateEx:
            raise ValueError('This BHVT layout cannot encode start-state extension IDs')
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


def splice_document(document, splices, source=None, *, pointer_exclusions=()):
    """Apply disjoint section replacements and relocate the native pointer tables."""
    splices = sorted(splices)
    source = document.source if source is None else source
    output, cursor = bytearray(), 0
    for start, end, payload in splices:
        if not cursor <= start <= end <= len(source):
            raise ValueError(f'Invalid or overlapping BHVT splice: {start}:{end}')
        output.extend(source[cursor:start])
        output.extend(payload)
        cursor = end
    output.extend(source[cursor:])
    if all(len(payload) == end - start for start, end, payload in splices):
        return output

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
        if any(start <= slot < end for start, end in pointer_exclusions):
            continue
        value = struct.unpack_from('<Q', source, slot)[0]
        struct.pack_into('<Q', output, relocate(slot), relocate(base + value) - relocate(base) if value else 0)
    for slot in (16, 24, 32, 40):
        value = struct.unpack_from('<Q', source, slot)[0]
        struct.pack_into('<Q', output, slot, relocate(value) if value else 0)
    struct.pack_into('<I', output, relocate(document.tree_info_ptr),
                     relocate(base + document.tree_data_size) - relocate(base))
    return output


def padded_section(payload, original, original_payload_size):
    """Keep the next section's original alignment and existing padding bytes."""
    padding = original[original_payload_size:]
    count = (len(original) - len(payload)) % 16
    return payload + ((bytes(max(0, count - len(padding))) + padding)[-count:] if count else b'')


def rebuild_graph(document):
    """Serialize every native node column and each changed embedded RSZ block.

    Unchanged sections stay byte-exact. Like RszFile.build, this is the raw
    writer; build_validated also checks reference semantics and preservation.
    Unknown BHVT sections, nonempty prefab tables, embedded UVARs and UVAR node
    extensions cannot be relocated. Aliased RSZ tables cannot be structurally
    edited. These layouts remain byte-preservable when no relocation is needed.
    """
    from .rsz_adapter import BLOCK_NAMES
    start = document.bhvt.offsets['nodes']
    end = document._section_ends[start]
    splices = []
    # Node fields are serialized from the current graph, never stale bindings.
    exclusions = [(start, end)]
    identities = [(node.id_hash, node.ex_id) for node in document.bhvt.nodes]
    if len(identities) != len(set(identities)):
        raise ValueError('Duplicate BHVT node identity')
    payload = serialize_nodes(document)
    original = document.source[start:end]
    if payload != original[:document.bhvt.node_data_end - start]:
        splices.append((start, end, padded_section(payload, original, document.bhvt.node_data_end - start)))

    pool = document.bhvt.string_pool
    if len(pool) % 2:
        raise ValueError('BHVT string pool must contain UTF-16 code units')
    for node in document.bhvt.nodes:
        if not 0 <= node.name_index < len(pool) // 2:
            raise ValueError(f'Invalid BHVT name index: {node.name_index}')
        tail = pool[node.name_index * 2:]
        terminator = next((i for i in range(0, len(tail), 2) if tail[i:i+2] == b'\0\0'), None)
        if terminator is None or tail[:terminator].decode('utf-16le') != node.name:
            raise ValueError(f'Node {node.name}: name and string-pool index disagree; use rename_node')
    start = document.bhvt.offsets['strings']
    end = document._section_ends[start]
    size = 4 + struct.unpack_from('<I', document.source, start)[0] * 2
    payload = pack('I', len(pool) // 2) + pool
    if payload != document.source[start:start+size]:
        splices.append((start, end, padded_section(payload, document.source[start:end], size)))

    seen = set()
    for name in BLOCK_NAMES:
        block = document.rsz_blocks.get_block(name)
        if block._file is not None:
            # The native graph owns these values. Bindings from replaced/deleted
            # fields must never patch a detached owner's value back into the file.
            exclusions.append((block.offset, block.end))
        if id(block) in seen or not block.modified:
            continue
        seen.add(id(block))
        aliases = [n for n in BLOCK_NAMES if document.rsz_blocks.get_block(n) is block]
        if len(aliases) > 1:
            raise ValueError(f'Editing aliased RSZ sections is not supported: {aliases}')
        # Require the shared codec to reproduce this layout before replacing it.
        baseline = type(block.file)()
        baseline.type_registry = document.type_registry
        baseline.game_version = ''
        baseline.read_headless(document.source[block.offset:block.end],
                               validate_type_registry=True, absolute_offset=block.offset)
        data = baseline.build_validated()
        padding = block.end - block.offset - len(data)
        if padding < 0 or document.source[block.offset:block.end] != data + bytes(padding):
            raise ValueError(f'{name}: native RSZ writer does not reproduce the source layout')
        data = block.file.build_validated()
        splices.append((block.offset, block.end, data + bytes((-len(data)) % 16)))

    for name, extensions in (('actions', document.bhvt.action_ex_ids),
                             ('static_actions', document.bhvt.static_action_ex_ids)):
        block = document.rsz_blocks.get_block(name)
        count = (len(block.object_table) if block._file is not None else
                 struct.unpack_from('<I', document.source, block.offset + 8)[0])
        if len(extensions) != count:
            raise ValueError(f'{name}: extension table count does not match RSZ object table')
    patched = document.bindings.rebuild(exclude_ranges=exclusions)
    pointer_exclusions = []
    if document._root_variables is not None:
        from file_handlers.uvar.base_model import FileHandler
        start = document.bhvt.offsets['variables']
        end = document._section_ends[start]
        delta = sum(len(data) - (b - a) for a, b, data in splices if b <= start)
        relative = start + delta - document.tree_data_offset
        writer = FileHandler(bytearray(relative))
        writer.seek(relative)
        document._root_variables.do_write(writer)
        payload = writer.get_bytes()[relative:]
        splices.append((start, end, payload + bytes((-len(payload) + end - start) % 16)))
        pointer_exclusions.append((start, end))
    if not splices:
        return patched
    return bytes(splice_document(document, splices, source=patched, pointer_exclusions=pointer_exclusions))
