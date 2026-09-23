"""Fixed-width edits through the document bindings used by the editor."""
from file_handlers.motfsm.motfsm_file import MotfsmFile
from .common import resolve_node, state_indexes
from .values import parse_field


def open_document(source):
    document = MotfsmFile()
    document.read(source.read_bytes())
    return document


def action_targets(document, node, klass, exact=False, position=None):
    found = []
    for index, reference in enumerate(node.actions):
        if position is not None and index != position:
            continue
        instance = document.references.action(reference.id_hash, reference.ex_id)
        if instance is None:
            continue
        name = instance.class_name
        matches = klass in (name, name.rsplit('.', 1)[-1]) if exact else klass.casefold() in name.casefold()
        if matches:
            found.append(instance)
    if not found:
        raise ValueError(f'{node.name} has no matching action: {klass!r}, position={position}')
    return found


def edit_instances(document, instances, assignments):
    if not assignments:
        raise ValueError('At least one --set FIELD=VALUE is required')
    edits = {}
    for instance in instances:
        fields = {field.name: field for field in instance.fields}
        for assignment in assignments:
            name, _, text = assignment.partition('=')
            name = name.strip()
            if name not in fields:
                raise ValueError(f'{instance.class_name} has no field {name}; fields: {", ".join(fields)}')
            binding = fields[name].binding
            if binding is None:
                raise ValueError(f'{instance.class_name}.{name} is not a fixed-width scalar')
            edits[binding.offset] = (binding, parse_field(fields[name], text))
    return save_edits(document, edits.values())


def save_edits(document, edits):
    edits = list(edits)
    for binding, value in edits:
        print(f'{binding.name}: {binding.value} -> {value}')
        document.edit_field(binding, value)
    output = document.rebuild()
    expected = bytearray(document.source)
    for binding, _ in edits:
        expected[binding.offset:binding.offset + binding.size] = binding.encode(binding.value)
    if output != bytes(expected):
        raise ValueError('Rebuild changed bytes outside the requested fields')
    reopened = MotfsmFile()
    reopened.read(output)
    if reopened.rebuild() != output:
        raise ValueError('Reopened candidate does not rebuild identically')
    return output


def action_edit(args):
    doc = open_document(args.source)
    node = doc.bhvt.nodes[resolve_node(doc, args.node)]
    targets = action_targets(doc, node, args.klass, args.exact, args.position)
    return edit_instances(doc, targets, args.set)


def condition_edit(args):
    doc = open_document(args.source)
    index = resolve_node(doc, args.node)
    node = doc.bhvt.nodes[index]
    selected = state_indexes(args.states)
    if any(i >= len(node.states) for i in selected):
        raise ValueError(f'State index out of range; {node.name} has {len(node.states)} states')
    wanted = {(index, 'state', i) for i in selected}
    owners = {}
    for n, other in enumerate(doc.bhvt.nodes):
        for kind, rows, attribute in (
            ('state', other.states, 'TransitionConditions'),
            ('child', other.children, 'condition_id'),
            ('start', other.transitions, 'mStartStateTransition'),
            ('all', other.all_states, 'mAllTransition'),
        ):
            for position, row in enumerate(rows):
                raw = getattr(row, attribute) & 0xFFFFFFFF
                owners.setdefault(raw, set()).add((n, kind, position))
    targets = []
    for position in selected:
        raw = node.states[position].TransitionConditions & 0xFFFFFFFF
        shared = owners[raw] - wanted
        if shared and not args.allow_shared:
            raise ValueError(f'State {position} shares its condition with {sorted(shared)}; use --allow-shared')
        condition = doc.references.object_instance('conditions', raw)
        if condition is None:
            raise ValueError(f'{node.name}.state[{position}] has no condition')
        targets.append(condition)
    return edit_instances(doc, targets, args.set)


def state_target(args):
    doc = open_document(args.source)
    node = doc.bhvt.nodes[resolve_node(doc, args.node)]
    target_index = resolve_node(doc, args.target)
    target = doc.bhvt.nodes[target_index]
    if not 0 <= args.state < len(node.states):
        raise ValueError(f'State index out of range: {args.state}')
    if doc.references.state_target(target.id_hash) != target_index:
        raise ValueError('This target identity cannot be represented by a state hash')
    return save_edits(doc, [(doc.bindings.get(node.states[args.state], 'mTransitions'), target.id_hash)])


def event_edit(args):
    doc = open_document(args.source)
    index = resolve_node(doc, args.node)
    node = doc.bhvt.nodes[index]
    if args.incoming:
        selected = {(n, s) for n, other in enumerate(doc.bhvt.nodes) for s, state in enumerate(other.states)
                    if state.mTransitions == node.id_hash}
    else:
        positions = state_indexes(args.states)
        if any(position >= len(node.states) for position in positions):
            raise ValueError('State position is out of range')
        selected = {(index, position) for position in positions}
    if not selected:
        raise ValueError('No selected transitions')
    owners, targets = {}, {}
    for n, other in enumerate(doc.bhvt.nodes):
        for kind, rows, attribute in (('state', other.states, 'mStates'),
                                       ('start', other.transitions, 'mStartTransitionEvent')):
            for s, row in enumerate(rows):
                for raw in getattr(row, attribute).values:
                    event = doc.references.object_instance('transition_events', raw)
                    if event is None:
                        continue
                    key = event.start_offset
                    owners.setdefault(key, set()).add((n, kind, s))
                    if kind == 'state' and (n, s) in selected and args.klass in (event.class_name, event.class_name.rsplit('.', 1)[-1]):
                        targets[key] = event
    wanted = {(n, 'state', s) for n, s in selected}
    for key in targets:
        shared = owners[key] - wanted
        if shared and not args.allow_shared:
            raise ValueError(f'Event is shared with {sorted(shared)}; use --allow-shared or clone it first')
    if not targets:
        raise ValueError(f'No existing {args.klass} events on the selected transitions')
    return edit_instances(doc, targets.values(), args.set)


def point_action(args):
    doc = open_document(args.source)
    node = doc.bhvt.nodes[resolve_node(doc, args.node)]
    hit = args.command == 'hit-request'
    klass = 'PlayerHitAction2' if hit else 'PlayerFsm2ActionSetEffect'
    targets = action_targets(doc, node, klass, True, args.position)
    if len(targets) != 1:
        raise ValueError(f'{node.name} has {len(targets)} matching actions; use --position')
    fields = {field.name: field for field in targets[0].fields}
    expected = args.expect if hit else args.expect_element
    expected_field = '_hitIndex' if hit else '_ElementID'
    if expected is not None and fields[expected_field].value != expected:
        raise ValueError(f'Expected {expected_field}={expected}, found {fields[expected_field].value}')
    if hit:
        request = args.request
        if args.request_id is not None and args.rcol is None:
            raise ValueError('--id requires --rcol; the FSM stores field0, not the authoring ID')
        if args.rcol:
            from argparse import Namespace
            from file_handlers.rcol.rcol_handler import RcolHandler
            from tools.cli.runtime import MHR_REGISTRY
            from tools.cli.formats.rcol.selection import resolve
            handler = RcolHandler()
            handler.filepath = str(args.rcol)
            handler.init_type_registry(str(MHR_REGISTRY))
            handler.read(args.rcol.read_bytes())
            index = resolve(handler.rcol, Namespace(request_id=args.request_id, field0=request, index=None))
            request = handler.rcol.request_sets[index].info.field0
        assignments = [f'_hitIndex={request}']
    else:
        assignments = [f'{name}={value}' for name, value in (
            ('_ElementID', args.element), ('_Frame', args.frame), ('containerID', args.container)
        ) if value is not None]
    return edit_instances(doc, targets, assignments)
