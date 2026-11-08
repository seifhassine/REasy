"""Rise Action pointers: PlayerHitAction2 hit requests and SetEffect elements."""
from file_handlers.motfsm.scalar_editing import action_targets, edit_instances, read_document
from file_handlers.motfsm.selection import resolve_node


def _single_action(doc, node, klass, position):
    owner = doc.bhvt.nodes[resolve_node(doc, node)]
    targets = action_targets(doc, owner, klass, True, position)
    if len(targets) != 1:
        raise ValueError(f'{owner.name} has {len(targets)} matching actions; use --position')
    return targets


def _check(targets, name, expected):
    value = {field.name: field for field in targets[0].fields}[name].value
    if expected is not None and value != expected:
        raise ValueError(f'Expected {name}={expected}, found {value}')


def set_hit_request(source: bytes, node: str, request: int, *, position=None, expect=None) -> bytes:
    """Set ``PlayerHitAction2._hitIndex`` to a native RCOL ``field0`` value."""
    doc = read_document(source)
    targets = _single_action(doc, node, 'PlayerHitAction2', position)
    _check(targets, '_hitIndex', expect)
    return edit_instances(doc, targets, [f'_hitIndex={request}'])


def set_effect(source: bytes, node: str, *, element=None, frame=None, container=None, position=None,
               expect_element=None) -> bytes:
    """Set the element, frame and/or container of a ``PlayerFsm2ActionSetEffect``."""
    doc = read_document(source)
    targets = _single_action(doc, node, 'PlayerFsm2ActionSetEffect', position)
    _check(targets, '_ElementID', expect_element)
    assignments = [f'{name}={value}' for name, value in (
        ('_ElementID', element), ('_Frame', frame), ('containerID', container)) if value is not None]
    return edit_instances(doc, targets, assignments)
