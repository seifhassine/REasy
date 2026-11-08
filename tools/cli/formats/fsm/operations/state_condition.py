"""Command adapter: Replace the condition instance of one BHVT state with a clone of another condition."""
from dataclasses import fields
from file_handlers.motfsm.operations.state_condition import StateConditionPlan, build


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', required=True, help='node owning the state to rewire')
    parser.add_argument('--target', required=True, help='node the state derives to (identifies the state)')
    parser.add_argument('--template-node', required=True)
    parser.add_argument('--template-state', type=int, default=0)
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')
    parser.add_argument('--copy-fields', action='append', default=[],
                        help='field names to take from the state being rewired instead of the template')


def run(args):
    plan = StateConditionPlan(**{field.name: getattr(args, field.name) for field in fields(StateConditionPlan)})
    return build(args.source.read_bytes(), plan)
