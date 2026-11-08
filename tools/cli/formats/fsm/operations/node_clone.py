"""Command adapter for the MHR node clone recipe."""
from dataclasses import fields
from services.mhr.fsm.node_clone import NodeClonePlan, build


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--name', required=True)
    parser.add_argument('--copy-node', required=True, help='template node name, e.g. atk_620')
    parser.add_argument('--parent', required=True)
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--derive-from', help='node whose states are cloned; defaults to --copy-node')
    parser.add_argument('--derive-condition', action='append', default=[],
                        help='keep only states whose condition class contains this text (repeatable)')
    parser.add_argument('--drop-class', action='append', default=[], help='drop actions by class substring')
    parser.add_argument('--set-effect', type=int, default=1, help='number of SetEffect actions to keep')
    parser.add_argument('--effect', action='append', default=[], metavar='CONTAINER:ELEMENT:FRAME',
                        help='retarget the SetEffect actions in order')
    parser.add_argument('--calc-lv', type=int, help='add LongSwordCalcLv with this addLv (e.g. -1)')
    parser.add_argument('--calc-lv-from', default='atk_127', help='node owning the CalcLv template')
    parser.add_argument('--calc-lv-after', default='AddLv',
                        help='the dropped action (class substring) whose slot the CalcLv action takes')
    parser.add_argument('--selector', choices=('leaf', 'clone'), default='clone')
    parser.add_argument('--allow-no-motion', action='store_true',
                        help='template may have no PlayerPlayMotion2 action (pure branch node)')


def run(args):
    recipe = NodeClonePlan(**{field.name: getattr(args, field.name) for field in fields(NodeClonePlan)})
    return build(args.source.read_bytes(), recipe)
