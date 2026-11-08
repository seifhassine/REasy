"""Motion catalog/CLIP inspection and native slot duplication."""
from file_handlers.motion.inspection import describe_motion_list
from file_handlers.motion.mhr.codec import MHR_MOTION_FORMAT_CODEC as RISE
from file_handlers.motion.mhr.editing import duplicate_slot, next_motion_id
from tools.cli.runtime import integer
from tools.cli.runtime import EditResult


def configure_query(parser):
    parser.add_argument('--motion', type=integer, action='append', help='MotionID, repeatable')
    parser.add_argument('--name', help='motion name substring')
    parser.add_argument('--limit', type=integer, default=40)


def query(args):
    if args.limit < 0:
        raise ValueError('--limit must be nonnegative')
    result = describe_motion_list(args.source.read_bytes(), str(args.source), motion_ids=args.motion,
                                  name_contains=args.name, detail=args.command)
    return {'source': str(args.source.resolve()), **result, 'motions': result['motions'][:args.limit]}


def configure_duplicate(parser):
    parser.add_argument('--motion', required=True, type=integer, help='source MotionID')
    parser.add_argument('--new-id', type=integer, help='defaults to the native next free MotionID')
    parser.add_argument('--name', help='optional name for the copied animation')


def duplicate(args):
    document = RISE.parse(args.source.read_bytes(), label=str(args.source))
    matches = [i for i, slot in enumerate(document.slots) if slot.motion_id == args.motion]
    if len(matches) != 1:
        raise ValueError(f'MotionID {args.motion} resolved to {len(matches)} slots')
    motion_id = args.new_id if args.new_id is not None else next_motion_id(document)
    cloned = duplicate_slot(document, matches[0], motion_id, name=args.name)
    output = RISE.write(cloned)
    verified = RISE.parse(output, label='verified duplicate')
    if [slot.motion_id for slot in verified.slots] != sorted([slot.motion_id for slot in document.slots] + [motion_id]):
        raise ValueError('Unexpected slot changes after duplication')
    if RISE.write(verified) != output:
        raise ValueError('Duplicated motion does not roundtrip stably')
    return EditResult(output, {'source_motion': args.motion, 'new_motion': motion_id,
                               'name': next(slot.payload.value.name for slot in verified.slots if slot.motion_id == motion_id)})
