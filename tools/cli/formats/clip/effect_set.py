"""Command adapter for native CLIP event editing."""
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_events import set_effect


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--category', default='VFX')
    parser.add_argument('--field', default='EffectId')
    parser.add_argument('--old', required=True)
    parser.add_argument('--new', required=True)


def run(args):
    document = CODEC.parse(args.source.read_bytes(), label=str(args.source))
    return set_effect(document, args.motion, old=args.old, new=args.new, field=args.field, category=args.category)
