"""Command adapter for native CLIP event editing."""
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_events import extend_property_range


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--category', default='EXTRA_0')
    parser.add_argument('--node', required=True, help='clip node name substring, e.g. WeaponHold')
    parser.add_argument('--property', action='append', default=[], help='limit to these property names')
    parser.add_argument('--end-frame', type=float, help='defaults to the motion end frame')


def run(args):
    document = CODEC.parse(args.source.read_bytes(), label=str(args.source))
    return extend_property_range(document, args.motion, node=args.node, properties=args.property, end_frame=args.end_frame, category=args.category)
