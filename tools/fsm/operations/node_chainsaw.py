"""Command adapter for the MHR node chainsaw recipe."""
from dataclasses import fields
from services.mhr.fsm.node_chainsaw import NodeChainsawPlan, build


def configure(parser):
    parser.add_argument('--allow-uvar-drift', action='store_true',
                        help='skip the UVAR snapshot equality check (heuristic relation reads)')
    parser.add_argument('--node', action='append', required=True, help='target node (repeatable)')
    parser.add_argument('--template-node', default='atk_axe_rush_strike_main',
                        help='attack node that already owns a chainsaw branch')
    parser.add_argument('--command', dest='pad_command', type=int, default=None, help='ChainsawHit._Command (default: template)')
    parser.add_argument('--hit-id', type=int, default=None, help='ChainsawHit._HitId (default: template)')
    parser.add_argument('--ref-work-id', type=int, default=None, help='ChainsawHit._RefWorkId (default: template)')
    parser.add_argument('--work-id', type=int, default=None, help='ChainsawHit._WorkId (default: template)')
    parser.add_argument('--pad-type', type=int, default=None, help='ChainsawHit._PadType (default: template)')
    parser.add_argument('--motion-id', type=int, default=None,
                        help="also retarget each node's PlayerPlayMotion2.v4_MotionID (e.g. to a freshly "
                             'duplicated slot that carries the shield chainsaw track)')


def run(args):
    recipe = NodeChainsawPlan(**{field.name: getattr(args, field.name) for field in fields(NodeChainsawPlan)})
    return build(args.source.read_bytes(), recipe)
