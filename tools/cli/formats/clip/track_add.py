"""Clone a four-key BOOL track using the source-preserving compact CLIP writer."""
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from file_handlers.motion.mhr_clip_editing import clone_bool_track


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--template-motion', type=int, required=True)
    parser.add_argument('--category', type=int, default=5)
    parser.add_argument('--track', required=True, help='unique template track name substring')
    parser.add_argument('--flag', default='_Flag', help='BOOL property name')
    parser.add_argument('--true-frame', type=float, required=True)
    parser.add_argument('--false-frame', type=float, required=True)


def run(args):
    document = RISE.parse(args.source.read_bytes(), label=str(args.source))
    return clone_bool_track(document, args.template_motion, args.motion, args.category,
                            args.track, args.flag, args.true_frame, args.false_frame)
