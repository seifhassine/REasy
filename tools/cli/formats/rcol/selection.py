"""Keep request table positions, authoring IDs, and hit references distinct."""
from tools.fsm.common import integer


def configure(parser, *, required=False):
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument('--id', dest='request_id', type=integer, help='RCOL request authoring ID')
    group.add_argument('--field0', type=integer, help='hit reference used by PlayerHitAction2 and ChainsawHit')
    group.add_argument('--index', type=integer, help='zero-based request table position')


def select(rcol, args):
    return [i for i, request in enumerate(rcol.request_sets)
            if (args.request_id is None or request.info.id == args.request_id)
            and (args.field0 is None or request.info.field0 == args.field0)
            and (args.index is None or i == args.index)]


def resolve(rcol, args):
    matches = select(rcol, args)
    if len(matches) != 1:
        raise ValueError(f'RCOL request selector resolved to {len(matches)} entries')
    return matches[0]
