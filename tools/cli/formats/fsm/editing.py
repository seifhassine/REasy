"""Command adapters for fixed-width FSM edits."""
from argparse import Namespace

from file_handlers.motfsm.scalar_editing import edit_action, edit_condition, edit_event, retarget_state
from services.mhr.fsm.point_action import set_effect, set_hit_request


def action_edit(args):
    return edit_action(args.source.read_bytes(), args.node, args.klass, args.set,
                       exact=args.exact, position=args.position)


def condition_edit(args):
    return edit_condition(args.source.read_bytes(), args.node, args.states, args.set,
                          allow_shared=args.allow_shared)


def state_target(args):
    return retarget_state(args.source.read_bytes(), args.node, args.state, args.target)


def event_edit(args):
    return edit_event(args.source.read_bytes(), args.node, args.klass, args.set, states=args.states,
                      incoming=args.incoming, allow_shared=args.allow_shared)


def rcol_field0(path, request_id, field0):
    """Resolve an RCOL request selection to the field0 value the FSM stores."""
    from file_handlers.rcol.rcol_handler import RcolHandler
    from tools.cli.runtime import MHR_REGISTRY
    from tools.cli.formats.rcol.selection import resolve
    handler = RcolHandler()
    handler.filepath = str(path)
    handler.init_type_registry(str(MHR_REGISTRY))
    handler.read(path.read_bytes())
    index = resolve(handler.rcol, Namespace(request_id=request_id, field0=field0, index=None))
    return handler.rcol.request_sets[index].info.field0


def point_action(args):
    source = args.source.read_bytes()
    if args.command != 'hit-request':
        return set_effect(source, args.node, element=args.element, frame=args.frame, container=args.container,
                          position=args.position, expect_element=args.expect_element)
    request = args.request
    if args.request_id is not None and args.rcol is None:
        raise ValueError('--id requires --rcol; the FSM stores field0, not the authoring ID')
    if args.rcol:
        request = rcol_field0(args.rcol, args.request_id, request)
    return set_hit_request(source, args.node, request, position=args.position, expect=args.expect)
