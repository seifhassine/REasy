"""Command adapter: Remove an Action reference from a node's action list."""
from dataclasses import fields
from file_handlers.motfsm.operations.action_remove import ActionRemovePlan, build


def configure(parser):
    parser.add_argument('--node', required=True, help='node owning the Action (unique name or 0x identity hash)')
    parser.add_argument('--class', dest='klass', required=True, help='action class substring to remove')
    parser.add_argument('--exact', action='store_true', help='require an exact class name match')
    parser.add_argument('--position', type=int, help='action slot to remove (required when several match)')
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check; the UVAR relation array is sized by a '
                             'heuristic terminator, so a tail shift can legitimately change how far it reads')


def run(args):
    plan = ActionRemovePlan(**{field.name: getattr(args, field.name) for field in fields(ActionRemovePlan)})
    return build(args.source.read_bytes(), plan)
