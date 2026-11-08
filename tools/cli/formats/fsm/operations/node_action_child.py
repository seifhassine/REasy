"""Command adapter for the MHR node action child recipe."""
from dataclasses import fields
from services.mhr.fsm.node_action_child import NodeActionChildPlan, build


def configure(parser):
    parser.add_argument('--parent', required=True, help='node the action currently lives on')
    parser.add_argument('--name', required=True, help='name of the new child node')
    parser.add_argument('--action-class', dest='klass',
                        help='class name (or substring) of the Action to move')
    parser.add_argument('--no-action', action='store_true',
                        help="create the child with no Actions at all (pure shell node, like the game's own)")
    parser.add_argument('--exact', action='store_true', help='require an exact class name match')
    parser.add_argument('--no-transition', action='store_true',
                        help='do not add the start transition to the new node')
    parser.add_argument('--selector-from', metavar='NODE',
                        help='node owning the selector template; required when --parent has selector_id -1')
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check; the UVAR relation array is sized by a '
                             'heuristic terminator, so a tail shift can legitimately change how far it reads')
    parser.add_argument('--no-selector', action='store_true',
                        help='leave the parent selector as-is even though it gains a child')
    parser.add_argument('--transition-condition-from', type=int, metavar='STATE',
                        help="clone this state index's condition onto the start transition")
    parser.add_argument('--transition-set', action='append', default=[], metavar='FIELD=VALUE',
                        help='override scalar fields of the cloned transition condition (repeatable)')


def run(args):
    recipe = NodeActionChildPlan(**{field.name: getattr(args, field.name) for field in fields(NodeActionChildPlan)})
    return build(args.source.read_bytes(), recipe)
