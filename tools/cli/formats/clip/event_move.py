"""Command adapter for native CLIP event editing."""
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from file_handlers.motion.mhr_events import move_event
from tools.fsm.common import integer


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument('--effect', help='EffectId string of the VFXRangeTrack')
    target.add_argument('--trigger', help='Wwise trigger ID, decimal or 0x-prefixed')
    target.add_argument('--track-index', type=int, help='VFXRangeTrack position (0-based)')
    timing = parser.add_mutually_exclusive_group(required=True)
    timing.add_argument('--to', type=float, help='absolute frame for the track (first key)')
    timing.add_argument('--delta', type=float, help='frames added to the track')


def run(args):
    document = CODEC.parse(args.source.read_bytes(), label=str(args.source))
    return move_event(document, args.motion, effect=args.effect, trigger=integer(args.trigger) if args.trigger is not None else None, track_index=args.track_index, to=args.to, delta=args.delta)
