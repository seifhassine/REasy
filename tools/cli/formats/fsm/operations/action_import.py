"""Command adapter: Import one Action instance from another FSM file and attach it to a node."""
from dataclasses import fields
from pathlib import Path
from file_handlers.motfsm.operations.action_import import ActionImportPlan, build


def configure(parser):
    parser.add_argument('--node', required=True, help='target node (unique name or 0x identity hash)')
    parser.add_argument('--class', dest='klass', required=True, help='action class substring to import')
    parser.add_argument('--exact', action='store_true', help='require an exact class name match')
    parser.add_argument('--from-fsm', required=True, type=Path, help='FSM file owning the template instance')
    parser.add_argument('--from-node', help='restrict the template search to this source node')
    parser.add_argument('--position', type=int, help='action slot to insert at (default: append)')
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check; the UVAR relation array is sized by a '
                             'heuristic terminator, so a tail shift can legitimately change how far it reads')


def run(args):
    plan = ActionImportPlan(**{field.name: getattr(args, field.name) for field in fields(ActionImportPlan)
                               if field.name != 'donor_name'}, donor_name=args.from_fsm.name)
    return build(args.source.read_bytes(), args.from_fsm.read_bytes(), plan)
