"""Command adapter: Reorder existing states while preserving identities and relative semantics."""
from dataclasses import fields
from file_handlers.motfsm.operations.state_order import StateOrderPlan, build


def configure(parser):
    parser.add_argument('--node', required=True)
    parser.add_argument('--order', required=True, help='complete state permutation, e.g. 2,0,1')


def run(args):
    plan = StateOrderPlan(**{field.name: getattr(args, field.name) for field in fields(StateOrderPlan)})
    return build(args.source.read_bytes(), plan)
