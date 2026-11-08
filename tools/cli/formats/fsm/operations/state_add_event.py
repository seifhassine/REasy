"""Command adapter: Append one more transition event to an existing BHVT state, cloned from a native event."""
from dataclasses import fields
from file_handlers.motfsm.operations.state_add_event import StateAddEventPlan, build


def configure(parser):
    parser.add_argument('--node', required=True, help='target node (unique name or 0x hash)')
    parser.add_argument('--state', type=int, required=True)
    parser.add_argument('--from-node', required=True, help='node owning the template state')
    parser.add_argument('--from-state', type=int, required=True)
    parser.add_argument('--from-event', type=int, default=0, help='index inside the template event list')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')


def run(args):
    plan = StateAddEventPlan(**{field.name: getattr(args, field.name) for field in fields(StateAddEventPlan)})
    return build(args.source.read_bytes(), plan)
