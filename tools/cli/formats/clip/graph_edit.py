"""CLI selectors for native node, property, and key copy/delete operations."""
from pathlib import Path

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE
from tools.cli.runtime import EditResult
from .sequence_copy import extra_inputs


def configure(parser):
    operation = parser.prog.rsplit(' ', 1)[-1]
    level, action = operation.split('-')
    parser.set_defaults(operation=operation)
    parser.add_argument('--scope', choices=('motion', 'override'), default='motion')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--sequence', type=int, help='target sequence position')
    selection.add_argument('--category', help='unique target sequence category name or ID')
    if action == 'copy':
        parser.add_argument('--from', dest='source_id', type=int, required=True)
        parser.add_argument('--to', dest='motion', type=int, required=True)
        parser.add_argument('--donor', type=Path)
        parser.add_argument('--source-scope', choices=('motion', 'override'), default='motion')
        source = parser.add_mutually_exclusive_group()
        source.add_argument('--source-sequence', type=int)
        source.add_argument('--source-category')
        parser.add_argument('--shift', type=float, default=0.0)
        if level == 'node':
            parser.add_argument('--target-node-index', type=int, required=True)
        elif level == 'property':
            target = parser.add_mutually_exclusive_group(required=True)
            target.add_argument('--target-node-index', type=int)
            target.add_argument('--target-property-index', type=int, help='destination container property')
        else:
            parser.add_argument('--target-property-index', type=int, required=True)
            parser.add_argument('--frame', type=float, help='explicit copied key frame')
        parser.set_defaults(extra_inputs=extra_inputs)
    else:
        parser.add_argument('--motion', type=int, required=True)
    if level == 'node':
        parser.add_argument('--node-index', type=int, required=True, help='parsed node table index')
    else:
        parser.add_argument('--property-index', type=int, required=True, help='parsed property table index')
    if level == 'key':
        parser.add_argument('--key-index', type=int, required=True, help='key position within the selected property')


def run(args):
    from file_handlers.motion.mhr_clip_editing import edit_clip
    document = RISE.parse(args.source.read_bytes(), label=str(args.source))
    donor_path = getattr(args, 'donor', None)
    donor = RISE.parse(donor_path.read_bytes(), label=str(donor_path)) if donor_path else None
    names = ('sequence', 'category', 'scope', 'operation', 'source_id', 'source_sequence',
             'source_category', 'source_scope', 'node_index', 'property_index', 'key_index',
             'target_node_index', 'target_property_index', 'shift', 'frame')
    options = {name: getattr(args, name) for name in names if hasattr(args, name)}
    if options['sequence'] is None and options['category'] is None and options.get('source_category') is not None:
        options['category'] = options['source_category']
    result = edit_clip(document, args.motion, donor=donor, **options)
    return EditResult(RISE.write(result), {'motion': args.motion, **options})
