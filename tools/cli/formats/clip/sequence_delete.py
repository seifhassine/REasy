"""Delete selected native CLIP sequences."""
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from tools.cli.runtime import EditResult
from .sequence_copy import categories


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    select = parser.add_mutually_exclusive_group(required=True)
    select.add_argument('--categories', type=categories)
    select.add_argument('--sequence', dest='positions', type=int, action='append')
    parser.add_argument('--scope', choices=('motion', 'override'), default='motion')


def run(args):
    from file_handlers.motion.mhr_clip_editing import delete_sequences
    document = RISE.parse(args.source.read_bytes(), label=str(args.source))
    result = delete_sequences(document, args.motion, categories=args.categories,
                              positions=args.positions, scope=args.scope)
    return EditResult(RISE.write(result), {'motion': args.motion, 'categories': args.categories,
                      'sequences': args.positions, 'scope': args.scope})
