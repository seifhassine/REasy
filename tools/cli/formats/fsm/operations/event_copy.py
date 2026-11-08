"""Command adapter: Give one state its own copy of a shared transition event."""
from dataclasses import fields
from file_handlers.motfsm.operations.event_copy import EventCopyPlan, build


def configure(parser):
    parser.add_argument('--node', required=True, help='node owning the state')
    parser.add_argument('--states', required=True, help='state positions: 0-9 or 0,2,5')
    parser.add_argument('--class', dest='klass', required=True, help='event class name or suffix')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
    parser.add_argument('--clear', action='store_true',
                        help='remove the matching events from the selected states instead of copying them')


def run(args):
    plan = EventCopyPlan(**{field.name: getattr(args, field.name) for field in fields(EventCopyPlan)})
    return build(args.source.read_bytes(), plan)
