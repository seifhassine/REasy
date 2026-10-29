"""Give one state its own copy of a shared transition event.

`mStates.values` holds object indexes into the `transition_events` block.  Clones produced by older
tools can share a single event instance between several states, and `event-edit` refuses to change a
shared instance because that would change every user.  This command clones the referenced event and
repoints the selected state's index at the copy, so the state becomes the only user and the copy can
be edited afterwards (or immediately through `--set`).

With `--clear` it instead **removes** the matching events from the selected states
(`mStates.values` filtered), which is how a freshly cloned derivation drops the turning / setup
options it inherited from its template.

"""


from file_handlers.motfsm.motfsm_file import MotfsmFile
from file_handlers.motfsm.rsz_adapter import BLOCK_NAMES
from file_handlers.motfsm.authoring import block_splice, clone_block_object, verify_block
from file_handlers.motfsm.serialization import serialize_nodes, splice_document
from tools.fsm.common import resolve_node, state_indexes
from tools.fsm.values import assignments

EVENTS_BLOCK = 'transition_events'


def configure(parser):
    parser.add_argument('--node', required=True, help='node owning the state')
    parser.add_argument('--states', required=True, help='state positions: 0-9 or 0,2,5')
    parser.add_argument('--class', dest='klass', required=True, help='event class name or suffix')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
    parser.add_argument('--clear', action='store_true',
                        help='remove the matching events from the selected states instead of copying them')


def run(args):
    TARGET = args.source.resolve()
    source = TARGET.read_bytes()
    doc = MotfsmFile()
    doc.read(source)
    nodes = doc.bhvt.nodes

    node = nodes[resolve_node(doc, args.node)]
    positions = state_indexes(args.states)
    if any(position >= len(node.states) for position in positions):
        raise ValueError(f'State position out of range; {node.name} has {len(node.states)} states')

    events_block = doc.rsz_blocks.get_block(EVENTS_BLOCK)
    users = {}
    for node_index, other in enumerate(nodes):
        lists = [('state', i, state.mStates.values) for i, state in enumerate(other.states)]
        if doc.layout.transition_event_lists is not False:
            lists += [('start', i, row.mStartTransitionEvent.values) for i, row in enumerate(other.transitions)]
        for kind, position, values in lists:
            for slot, raw in enumerate(values):
                event = doc.references.object_instance(EVENTS_BLOCK, raw)
                if event is not None:
                    users.setdefault((event.block.name, event.index), set()).add((node_index, kind, position, slot))
    print(f'  {node.name}: {len(users)} distinct transition events in use')

    plan = []
    for position in positions:
        state = node.states[position]
        values = list(getattr(state.mStates, 'values', []) or [])
        matches = [i for i, raw in enumerate(values)
                   if (lambda event: event is not None and args.klass in (
                       event.class_name, (event.class_name or '').rsplit('.', 1)[-1]))(
                       doc.references.object_instance(EVENTS_BLOCK, raw))]
        if not matches:
            print(f'  {node.name}[{position}]: no {args.klass} event, skipped')
            continue
        if len(matches) > 1:
            raise ValueError(f'{node.name}[{position}] has {len(matches)} matching events; not supported')
        slot = matches[0]
        old_object = values[slot]
        old_instance = doc.references.object_instance(EVENTS_BLOCK, old_object)
        shared = users.get((old_instance.block.name, old_instance.index), set())
        if len(shared) <= 1 and not args.clear:
            print(f'  {node.name}[{position}]: {old_instance.class_name.rsplit(".", 1)[-1]} is already private, skipped')
            continue
        if args.clear:
            plan.append((position, slot, old_object, old_instance, sorted(shared)))
            continue
        plan.append((position, slot, old_object, old_instance, sorted(shared)))
    if args.clear:
        removed = 0
        for position, slot, old_object, old_instance, _shared in plan:
            values = list(node.states[position].mStates.values)
            values = [raw for raw in values if raw != old_object]
            node.states[position].mStates.values = values
            print(f'  {node.name}[{position}]: removed {old_instance.class_name.rsplit(".", 1)[-1]} '
                  f'(object {old_object}), {len(values)} event(s) left')
            removed += 1
        nodes_start = doc.bhvt.offsets['nodes']
        node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
        nodes_data = serialize_nodes(doc)
        nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
        output = bytes(splice_document(doc, [(nodes_start, node_tail, nodes_data)]))
        verified = MotfsmFile()
        original = MotfsmFile()
        verified.read(output)
        original.read(source)
        assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
        check = verified.bhvt.nodes[resolve_node(verified, args.node)]
        for position, _slot, _old, _inst, _shared in plan:
            left = [doc.references.object_instance(EVENTS_BLOCK, raw)
                    for raw in check.states[position].mStates.values]
            assert not any(ev is not None and args.klass in (
                ev.class_name, (ev.class_name or '').rsplit('.', 1)[-1]) for ev in left),                 f'{node.name}[{position}] still references {args.klass}'
        for name in BLOCK_NAMES:
            old_block, new_block = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
            assert output[new_block.offset:new_block.end] == source[old_block.offset:old_block.end],                 f'{name} changed unexpectedly'
        print(f'  verified {removed} event(s) cleared; every other block unchanged')
        return output

    if not plan:
        raise ValueError('Nothing to copy')

    native = events_block.file
    old_event_objects = len(native.object_table)
    created = []
    for position, slot, old_object, old_instance, shared in plan:
        overrides = assignments(old_instance, args.set)
        if 'v0_UID' in {field.name for field in old_instance.fields}:
            used_uids = set()
            for object_index in events_block.object_table:
                # object_table entries are instance indexes (see RSZBlock.get_object)
                instance = events_block.get_instance(object_index)
                if instance is None:
                    continue
                for field in instance.fields:
                    if field.name == 'v0_UID' and isinstance(field.value, int):
                        used_uids.add(field.value)
            uid = 1
            while uid in used_uids:
                uid += 1
            overrides.setdefault('v0_UID', uid)
        description = ', '.join(f'{name}={value}' for name, value in overrides.items()) or 'no overrides'
        print(f'  {node.name}[{position}]: shared with {shared} -> private copy ({description})')
        origin = doc.rsz_blocks.get_block(
            'static_transition_events' if (old_object & 0xFFFFFFFF) >> 24 == 0x40 else EVENTS_BLOCK)
        instance_index, new_object = clone_block_object(events_block, old_instance.index, overrides, origin)
        created.append((position, slot, old_object, new_object, overrides, {field.name: field.value for field in old_instance.fields}))
        values = list(getattr(node.states[position].mStates, 'values', []) or [])
        assert values[slot] == old_object, 'state event slot changed while copying'
        values[slot] = new_object
        node.states[position].mStates.values = values
        assert len(node.states[position].mStates.values) == len(values)


    events_splice = block_splice(events_block)

    nodes_start = doc.bhvt.offsets['nodes']
    node_tail = min(offset for offset in doc.bhvt.offsets.values() if offset >= doc.bhvt.node_data_end)
    for position, slot, old_object, new_object, _overrides, _fields in created:
        values = list(node.states[position].mStates.values)
        values[slot] = old_object
        node.states[position].mStates.values = values
    assert serialize_nodes(doc) == source[nodes_start:doc.bhvt.node_data_end], 'node writer does not reproduce the source'
    for position, slot, _old, new_object, _overrides, _fields in created:
        values = list(node.states[position].mStates.values)
        values[slot] = new_object
        node.states[position].mStates.values = values

    nodes_data = serialize_nodes(doc)
    nodes_data += bytes((node_tail - nodes_start - len(nodes_data)) % 16)
    splices = sorted([(nodes_start, node_tail, nodes_data), events_splice])
    output = bytes(splice_document(doc, splices))

    verified = MotfsmFile()
    original = MotfsmFile()
    verified.read(output)
    original.read(source)
    assert len(verified.rsz_blocks.get_block(EVENTS_BLOCK).object_table) == old_event_objects + len(created)
    assert verified.bhvt.nodes == doc.bhvt.nodes, 'reopened node table differs from the authored model'
    assert verified.bhvt.action_ex_ids == original.bhvt.action_ex_ids
    assert verified.bhvt.static_action_ex_ids == original.bhvt.static_action_ex_ids
    check = verified.bhvt.nodes[resolve_node(verified, args.node)]
    for position, slot, old_object, new_object, overrides, fields in created:
        values = list(check.states[position].mStates.values)
        assert values[slot] == new_object, 'state does not reference its private copy'
        copy_instance = verified.references.object_instance(EVENTS_BLOCK, new_object)
        expected = dict(fields)
        expected.update(overrides)
        verify_block(doc, verified, EVENTS_BLOCK)
        kept = verified.references.object_instance(EVENTS_BLOCK, old_object)
        assert kept is not None and {field.name: field.value for field in kept.fields} == fields
    for name in BLOCK_NAMES:
        if name == EVENTS_BLOCK:
            continue
        old_block, new_block = original.rsz_blocks.get_block(name), verified.rsz_blocks.get_block(name)
        assert output[new_block.offset:new_block.end] == source[old_block.offset:old_block.end], f'{name} changed unexpectedly'
    print(f'  verified {len(created)} private event copies; other blocks unchanged')
    return output
