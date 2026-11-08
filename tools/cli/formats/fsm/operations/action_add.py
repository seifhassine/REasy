"""Command adapter: Append one more Action to an existing BHVT node, cloned from a native template instance."""
from dataclasses import fields
from file_handlers.motfsm.operations.action_add import ActionAddPlan, build


def configure(parser):
    parser.add_argument('--node', required=True, help='target node (unique name or 0x identity hash)')
    parser.add_argument('--class', dest='klass', required=True, help='template action class substring')
    parser.add_argument('--from', dest='source_node', help='node owning the template action')
    parser.add_argument('--exact', action='store_true', help='require an exact class name match')
    parser.add_argument('--set', action='append', default=[], metavar='FIELD=VALUE')


def run(args):
    plan = ActionAddPlan(**{field.name: getattr(args, field.name) for field in fields(ActionAddPlan)})
    return build(args.source.read_bytes(), plan)
