"""Command adapter: Set TransitionData fields (EndType / exitFrame / startFrame / ...) of one state."""
from dataclasses import fields
from file_handlers.motfsm.operations.state_transition import StateTransitionPlan, build


def configure(parser):
    parser.add_argument('--node', required=True, help='node owning the state row')
    parser.add_argument('--state', type=int, required=True, help='state position on --node')
    parser.add_argument('--set', action='append', required=True, metavar='FIELD=VALUE',
                        help='e.g. EndType=2 --set exitFrame=54 (repeatable)')


def run(args):
    plan = StateTransitionPlan(**{field.name: getattr(args, field.name) for field in fields(StateTransitionPlan)})
    return build(args.source.read_bytes(), plan)
