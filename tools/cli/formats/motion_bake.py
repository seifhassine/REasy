"""Bake inclusive source ranges into a single MOT 495 animation."""
from file_handlers.motion.mhr_bake import MotionSegment, bake_segments
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from tools.fsm.common import integer
from tools.cli.runtime import EditResult
from .wilds_import import configure_donor, load_inputs
from file_handlers.motion.motion_root import ROOT_TRANSFORM_MODES
from file_handlers.motion.motion_bake_blend import seam_windows


def configure(parser):
    configure_donor(parser, required=False)
    parser.add_argument('--segment', nargs=4, action='append', required=True,
                        metavar=('MOTION', 'START', 'END', 'SPEED'),
                        help='inclusive source frame range and playback multiplier; repeat in output order')
    parser.add_argument('--target', required=True, type=integer, help='output MotionID')
    parser.add_argument('--replace', action='store_true', help='replace an existing target animation')
    parser.add_argument('--name', help='optional output animation name')
    parser.add_argument('--blend-frames', type=integer, default=3, help='frames on each side of a cut to blend; 0 disables')
    parser.add_argument('--waist-rotation', choices=('align', 'authored'), default='align',
                        help='align cut starts to the first Waist_00 rotation; never adjusts its translation or scale')
    parser.add_argument('--root-transform', choices=ROOT_TRANSFORM_MODES, default='relative',
                        help='relative: normalize each cut start and continue its transform; identity: clear Root TRS; authored: keep source transforms')
    parser.add_argument('--segment-root-transform', nargs='+', choices=ROOT_TRANSFORM_MODES,
                        help='one root policy per --segment, in the same order; overrides --root-transform')
    parser.add_argument('--segment-root-translation-scale', nargs='+', type=float,
                        help='one displacement multiplier per segment; defaults to 1 and preserves position continuity')


def run(args):
    modes = args.segment_root_transform or [None] * len(args.segment)
    if len(modes) != len(args.segment):
        raise ValueError('--segment-root-transform must supply one policy per --segment')
    scales = args.segment_root_translation_scale or [1.0] * len(args.segment)
    if len(scales) != len(args.segment):
        raise ValueError('--segment-root-translation-scale must supply one multiplier per --segment')
    segments = [MotionSegment(integer(motion), float(start), None if end.lower() == 'end' else float(end), float(speed), mode, scale)
                for (motion, start, end, speed), mode, scale in zip(args.segment, modes, scales, strict=True)]
    if args.donor is not None:
        from file_handlers.motion.wilds_bake import bake_wilds_segments
        donor, document, rig, hold, family, hold_path = load_inputs(args)
        result, details = bake_wilds_segments(document, donor, segments, rig, hold, args.target,
                                             replace_existing=args.replace, name=args.name, family=family,
                                             root_transform=args.root_transform, align_waist=args.waist_rotation == 'align',
                                             blend_frames=args.blend_frames)
        details.update(donor=str(args.donor.resolve()), hold_template=hold_path, attach_family=family)
        return EditResult(CODEC.write(result), details)
    if args.game_dir or args.hold_template is not None:
        raise ValueError('--game-dir and --hold-template require --donor')
    document = CODEC.parse(args.source.read_bytes(), label=str(args.source))
    result, ranges = bake_segments(document, segments, args.target,
                                  replace_existing=args.replace, name=args.name, root_transform=args.root_transform,
                                  align_waist=args.waist_rotation == 'align', blend_frames=args.blend_frames)
    motion = next(s.payload.value for s in result.slots if s.motion_id == args.target)
    return EditResult(CODEC.write(result), {'motion_id': args.target, 'end_frame': motion.end_frame, 'fps': motion.frames_per_second,
                                          'segments': ranges, 'events': 'target preserved', 'root_transform': args.root_transform,
                                          'blend_frames': args.blend_frames, 'waist_alignment': args.waist_rotation,
                                          'blend_windows': seam_windows(ranges, args.blend_frames)})
