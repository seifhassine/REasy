"""Command adapter: Add one derivation (state) to an existing BHVT node - e.g. "atk_135 can cancel into atk_621"."""
from dataclasses import fields
from file_handlers.motfsm.operations.state_add import StateAddPlan, build


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', required=True, help='node that gains the derivation, e.g. atk_135')
    parser.add_argument('--target', required=True, help='node the new derivation goes to, e.g. atk_621')
    parser.add_argument('--template-from', help='node the template state is copied from (default: --node)')
    parser.add_argument('--template-state', type=int, help='state index to copy (default: auto)')
    parser.add_argument('--template-target', help="pick the template state that already goes to this node")
    parser.add_argument('--condition-class', default='PlayerFsm2Command',
                        help='text the template state condition class must contain')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE',
                        help='override a condition field, e.g. --set CmdType=51')
    parser.add_argument('--events', choices=('copy', 'share', 'drop'), default='copy',
                        help='what to do with the template state transition events')
    parser.add_argument('--insert-before', type=int, help='insert before this state position (default: append)')


def run(args):
    plan = StateAddPlan(**{field.name: getattr(args, field.name) for field in fields(StateAddPlan)})
    return build(args.source.read_bytes(), plan)
