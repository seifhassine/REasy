"""Fixed-width FSM edits with exact unrelated-byte preservation.

Every edit function takes the source bytes and returns verified candidate bytes.
"""
from .motfsm_file import MotfsmFile
from .selection import resolve_node, state_indexes
from .values import parse_field


def read_document(data: bytes) -> MotfsmFile:
    document = MotfsmFile()
    document.read(data)
    return document


def open_document(path):
    return read_document(path.read_bytes())

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
        raise ValueError('At least one FIELD=VALUE assignment is required')
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


def edit_action(source: bytes, node: str, klass: str, assignments, *, exact=False, position=None) -> bytes:
    """Set scalar fields on the matching Action(s) of ``node``."""
    doc = read_document(source)
    owner = doc.bhvt.nodes[resolve_node(doc, node)]
    return edit_instances(doc, action_targets(doc, owner, klass, exact, position), assignments)


def edit_condition(source: bytes, node: str, states: str, assignments, *, allow_shared=False) -> bytes:
    """Set scalar fields on the conditions of the selected states (``0-9`` or ``0,2,5``)."""
    doc = read_document(source)
    index = resolve_node(doc, node)
    owner = doc.bhvt.nodes[index]
    selected = state_indexes(states)
    if any(i >= len(owner.states) for i in selected):
        raise ValueError(f'State index out of range; {owner.name} has {len(owner.states)} states')
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
        raw = owner.states[position].TransitionConditions & 0xFFFFFFFF
        shared = owners[raw] - wanted
        if shared and not allow_shared:
            raise ValueError(f'State {position} shares its condition with {sorted(shared)}; use --allow-shared')
        condition = doc.references.object_instance('conditions', raw)
        if condition is None:
            raise ValueError(f'{owner.name}.state[{position}] has no condition')
        targets.append(condition)
    return edit_instances(doc, targets, assignments)


def retarget_state(source: bytes, node: str, state: int, target: str) -> bytes:
    """Point state ``state`` of ``node`` at ``target`` (hash and ex_id)."""
    doc = read_document(source)
    owner = doc.bhvt.nodes[resolve_node(doc, node)]
    destination = doc.bhvt.nodes[resolve_node(doc, target)]
    if not 0 <= state < len(owner.states):
        raise ValueError(f'State index out of range: {state}')
    row = owner.states[state]
    return save_edits(doc, [(doc.bindings.get(row, 'mTransitions'), destination.id_hash),
                           (doc.bindings.get(row, 'mTransitionAttributes'), destination.ex_id)])


def edit_event(source: bytes, node: str, klass: str, assignments, *, states=None, incoming=False,
               allow_shared=False) -> bytes:
    """Set scalar fields on ``klass`` events of the selected (or all incoming) transitions."""
    doc = read_document(source)
    index = resolve_node(doc, node)
    owner = doc.bhvt.nodes[index]
    if incoming:
        selected = {(n, s) for n, other in enumerate(doc.bhvt.nodes) for s, row in enumerate(other.states)
                    if (row.mTransitions, row.mTransitionAttributes) == (owner.id_hash, owner.ex_id)}
    else:
        positions = state_indexes(states)
        if any(position >= len(owner.states) for position in positions):
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
                    if kind == 'state' and (n, s) in selected and klass in (event.class_name, event.class_name.rsplit('.', 1)[-1]):
                        targets[key] = event
    wanted = {(n, 'state', s) for n, s in selected}
    for key in targets:
        shared = owners[key] - wanted
        if shared and not allow_shared:
            raise ValueError(f'Event is shared with {sorted(shared)}; use --allow-shared or clone it first')
    if not targets:
        raise ValueError(f'No existing {klass} events on the selected transitions')
    return edit_instances(doc, targets.values(), assignments)
