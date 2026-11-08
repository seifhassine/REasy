"""Command adapter: Give an existing state an independent map ID, reusing its transition data."""
from dataclasses import fields
from file_handlers.motfsm.operations.state_own_map import StateOwnMapPlan, build


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', required=True, help='node owning the state')
    parser.add_argument('--target', required=True, help='node the state derives to (identifies the state)')


def run(args):
    plan = StateOwnMapPlan(**{field.name: getattr(args, field.name) for field in fields(StateOwnMapPlan)})
    return build(args.source.read_bytes(), plan)
