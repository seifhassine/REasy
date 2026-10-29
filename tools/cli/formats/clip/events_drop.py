"""Command adapter for native CLIP event editing."""
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_events import silence_events


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--category', default='SOUND')
    parser.add_argument('--node-class', default='snow.wwise.SoundBakeTriggerTracks')
    parser.add_argument('--from-frame', type=float, default=100.0)
    parser.add_argument('--frames', default='', help='explicit comma separated frames to drop (overrides --from-frame)')


def run(args):
    document = CODEC.parse(args.source.read_bytes(), label=str(args.source))
    return silence_events(document, args.motion, category=args.category, node_classes=[s.strip() for s in args.node_class.split(',') if s.strip()], from_frame=args.from_frame, frames=[float(s) for s in args.frames.split(',') if s.strip()])
