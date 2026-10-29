"""Command adapter for the MHR attack clone recipe."""
from dataclasses import fields
from services.mhr.fsm.attack_clone import AttackClonePlan, build


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--template', required=True, help='template attack node selector')
    parser.add_argument('--parent', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--keep-hit-index', type=int, required=True)
    parser.add_argument('--selector', choices=('leaf', 'clone'), default='clone')


def run(args):
    recipe = AttackClonePlan(**{field.name: getattr(args, field.name) for field in fields(AttackClonePlan)})
    return build(args.source.read_bytes(), recipe)
