"""Bake inclusive source ranges into a single MOT 495 animation."""
from pathlib import Path
import re
from file_handlers.motion.mhr_bake import MotionSegment, PoseTransition, bake_segments
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as CODEC
from tools.fsm.common import integer
from tools.cli.runtime import EditResult
from .wilds_import import configure_donor, load_inputs, load_target
from file_handlers.motion.motion_root import ROOT_TRANSFORM_MODES
from file_handlers.motion.motion_bake_blend import seam_windows
from file_handlers.motion.mhr_editing import next_motion_id


def parse_target(value):
    return None if value.casefold() == 'next' else integer(value)


def target_id(args, document):
    return next_motion_id(document) if args.target is None else args.target


def configure(parser):
    configure_donor(parser, required=False)
    parser.add_argument('--lmt-source', nargs=2, action='append', metavar=('NAME', 'PATH'),
                        help='named LMT source; repeat for multiple files and select with NAME:MOTION in --segment')
    parser.add_argument('--segment', nargs=4, action='append', required=True,
                        metavar=('MOTION', 'START', 'END', 'SPEED'),
                        help='MOTION or NAME:MOTION, inclusive frame range and speed; repeat in output order')
    parser.add_argument('--target', required=True, type=parse_target, help='output MotionID, or next to append with a free ID')
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
    parser.add_argument('--transition-to', nargs=3, metavar=('MOTION', 'FRAME', 'FRAMES'),
                        help='append synthetic transition frames to a donor pose (NAME:MOTION for named LMT sources); hold final Root')


def extra_inputs(args):
    return (args.donor, args.hold_template, args.skeleton,
            *(path for _, path in (args.lmt_source or ())))


def lmt_sources(args):
    """Resolve aliases first; one physical file is one source identity."""
    if args.donor is not None:
        raise ValueError('--lmt-source and --donor cannot be combined')
    aliases = {}
    for name, path in args.lmt_source:
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*', name):
            raise ValueError('LMT source names must start with a letter and contain only letters, digits, _ or -')
        if name in aliases:
            raise ValueError(f'Duplicate LMT source name: {name}')
        aliases[name] = str(Path(path).resolve())
    references = [row[0] for row in args.segment]
    if args.transition_to is not None:
        references.append(args.transition_to[0])
    for motion in references:
        name, separator, motion_id = motion.partition(':')
        if not separator or name not in aliases:
            raise ValueError(f'Use a declared NAME:MOTION for every LMT segment: {motion}')
        integer(motion_id)
    from file_handlers.motion.lmt_codec import LMT_MOTION_FORMAT_CODEC
    skeleton = args.skeleton.read_bytes() if args.skeleton is not None else None
    sources = {path: LMT_MOTION_FORMAT_CODEC.parse(Path(path).read_bytes(), label=path, skeleton_data=skeleton)
               for path in dict.fromkeys(aliases.values())}
    return aliases, sources


def run(args):
    if args.replace and args.target is None:
        raise ValueError('--target next cannot be used with --replace')
    if args.transition_to is not None and not args.lmt_source and args.donor is None:
        raise ValueError('--transition-to requires --donor or --lmt-source')
    modes = args.segment_root_transform or [None] * len(args.segment)
    if len(modes) != len(args.segment):
        raise ValueError('--segment-root-transform must supply one policy per --segment')
    scales = args.segment_root_translation_scale or [1.0] * len(args.segment)
    if len(scales) != len(args.segment):
        raise ValueError('--segment-root-translation-scale must supply one multiplier per --segment')
    aliases, sources = lmt_sources(args) if args.lmt_source else ({}, None)
    transition = None
    if args.transition_to is not None:
        motion, frame, count = args.transition_to
        source = None
        if sources is not None:
            name, motion = motion.split(':', 1)
            source = aliases[name]
        transition = PoseTransition(integer(motion), float(frame), integer(count), source)
    segments = []
    for (motion, start, end, speed), mode, scale in zip(args.segment, modes, scales, strict=True):
        source = None
        if sources is not None:
            name, motion = motion.split(':', 1)
            source = aliases[name]
        segments.append(MotionSegment(integer(motion), float(start), None if end.lower() == 'end' else float(end),
                                      float(speed), mode, scale, source))
    if sources is not None:
        from file_handlers.motion.wilds_bake import bake_lmt_segments
        document, rig, hold, family, hold_path = load_target(args)
        result, details = bake_lmt_segments(document, sources, segments, rig, hold, target_id(args, document),
                                            replace_existing=args.replace, name=args.name, family=family,
                                            root_transform=args.root_transform, align_waist=args.waist_rotation == 'align',
                                            blend_frames=args.blend_frames, transition=transition)
        details.update(lmt_sources=aliases, hold_template=hold_path, attach_family=family,
                       mode='replace' if args.replace else 'append')
        return EditResult(CODEC.write(result), details)
    if args.donor is not None:
        from file_handlers.motion.wilds_bake import bake_wilds_segments
        donor, document, rig, hold, family, hold_path = load_inputs(args)
        result, details = bake_wilds_segments(document, donor, segments, rig, hold, target_id(args, document),
                                             replace_existing=args.replace, name=args.name, family=family,
                                             root_transform=args.root_transform, align_waist=args.waist_rotation == 'align',
                                             blend_frames=args.blend_frames, transition=transition)
        details.update(donor=str(args.donor.resolve()), hold_template=hold_path, attach_family=family)
        return EditResult(CODEC.write(result), details)
    if args.game_dir or args.hold_template is not None or args.skeleton is not None:
        raise ValueError('--game-dir, --hold-template and --skeleton require --donor')
    document = CODEC.parse(args.source.read_bytes(), label=str(args.source))
    motion_id = target_id(args, document)
    result, ranges = bake_segments(document, segments, motion_id,
                                  replace_existing=args.replace, name=args.name, root_transform=args.root_transform,
                                  align_waist=args.waist_rotation == 'align', blend_frames=args.blend_frames)
    motion = next(s.payload.value for s in result.slots if s.motion_id == motion_id)
    return EditResult(CODEC.write(result), {'motion_id': motion_id, 'end_frame': motion.end_frame, 'fps': motion.frames_per_second,
                                          'segments': ranges, 'events': 'target preserved', 'root_transform': args.root_transform,
                                          'blend_frames': args.blend_frames, 'waist_alignment': args.waist_rotation,
                                          'blend_windows': seam_windows(ranges, args.blend_frames)})
