"""Command adapter for native CLIP event editing."""
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_events import set_bool_window


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--category', default='EXTRA_0')
    parser.add_argument('--node', required=True, help='clip node name substring, e.g. ChargeAxeChainsawRunTrack')
    parser.add_argument('--property', required=True, help='BOOL property name, e.g. _Flag')
    parser.add_argument('--on', dest='on_frame', type=float, required=True,
                        help='first frame the flag is true')
    parser.add_argument('--off', dest='off_frame', type=float, required=True,
                        help='last frame the flag is true')


def run(args):
    document = CODEC.parse(args.source.read_bytes(), label=str(args.source))
    return set_bool_window(document, args.motion, node=args.node, property_name=args.property, on=args.on_frame, off=args.off_frame, category=args.category)
