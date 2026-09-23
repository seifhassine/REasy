"""Copy CLIP sequences through the native structural editor."""
from pathlib import Path
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from tools.cli.runtime import EditResult


def categories(value):
    names = [name.strip().upper() for name in value.split(',') if name.strip()]
    if not names:
        raise ValueError('Specify at least one sequence category')
    return names


def extra_inputs(args):
    return (args.donor,) if args.donor else ()


def configure(parser):
    parser.add_argument('--from', dest='source_id', type=int, required=True)
    parser.add_argument('--to', dest='target_id', type=int, required=True)
    select = parser.add_mutually_exclusive_group()
    select.add_argument('--categories', type=categories, help='comma-separated category names or IDs; default SOUND,VFX')
    select.add_argument('--sequence', dest='positions', type=int, action='append', help='source sequence position, repeatable')
    parser.add_argument('--donor', type=Path, help='optional source MOTLIST; primary input remains the target')
    parser.add_argument('--source-scope', choices=('motion', 'override'), default='motion')
    parser.add_argument('--scope', choices=('motion', 'override'), default='motion', help='target sequence scope')
    parser.add_argument('--shift', type=float, default=0.0)
    parser.add_argument('--total-frame', choices=('keep', 'target'), default='keep')
    parser.add_argument('--replace', action='store_true', help='replace target sequences of the copied categories')
    parser.set_defaults(extra_inputs=extra_inputs)


def run(args):
    from file_handlers.motion.mhr_clip_editing import copy_sequences
    document = RISE.parse(args.source.read_bytes(), label=str(args.source))
    donor = RISE.parse(args.donor.read_bytes(), label=str(args.donor)) if args.donor else None
    selected = args.categories if args.categories is not None else (None if args.positions is not None else ['SOUND', 'VFX'])
    result = copy_sequences(document, args.source_id, args.target_id,
                            categories=selected, positions=args.positions, shift=args.shift,
                            total_frame=args.total_frame, replace=args.replace, donor=donor,
                            source_scope=args.source_scope, target_scope=args.scope)
    return EditResult(RISE.write(result), {'source_motion': args.source_id, 'target_motion': args.target_id,
                      'categories': selected, 'sequences': args.positions, 'shift': args.shift,
                      'replace': args.replace, 'source_scope': args.source_scope, 'scope': args.scope})
