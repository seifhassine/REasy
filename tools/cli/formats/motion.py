"""Motion catalog/CLIP inspection and native slot duplication."""
from dataclasses import asdict
import struct

from file_handlers.motion.motlist_file import MotListFile
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE
from file_handlers.motion.mhr_editing import duplicate_slot, next_motion_id
from file_handlers.motion.binary import ReadContext, align_up
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser
from file_handlers.motion.errors import MotionCodecError
from tools.fsm.common import integer
from tools.cli.runtime import EditResult


def configure_query(parser):
    parser.add_argument('--motion', type=integer, action='append', help='MotionID, repeatable')
    parser.add_argument('--name', help='motion name substring')
    parser.add_argument('--limit', type=integer, default=40)


def node_record(node, parsed=None):
    node_indices = {id(record.node): index for index, record in enumerate(parsed.nodes)} if parsed else {}
    prop_indices = {id(record.prop): index for index, record in enumerate(parsed.properties)} if parsed else {}
    def property_record(prop):
        return {**asdict(prop), 'property_index': prop_indices.get(id(prop)),
                'keys': [{**asdict(key), 'key_index': index} for index, key in enumerate(prop.keys)],
                'children': [property_record(child) for child in prop.children]}
    def walk(item):
        return {'name': item.name, 'start_frame': item.start_frame, 'end_frame': item.end_frame,
                'node_index': node_indices.get(id(item)),
                'root_guid': item.root_guid.hex(), 'extra_guid': item.extra_guid.hex(),
                'properties': [property_record(prop) for prop in item.properties],
                'children': [walk(child) for child in item.children]}
    return walk(node)


def sequence_records(raw, sequences, base, table, command, scope):
    rows = []
    for position, sequence in enumerate(sequences):
        row = {'position': position, 'scope': scope,
               'category': getattr(sequence.category, 'name', str(sequence.category)),
               'category_id': int(sequence.category), 'tracks': len(sequence.tracks)}
        if command in ('dump', 'layout') and sequence.clip is not None:
            parsed = None
            if table is not None:
                wrapper = base + struct.unpack_from('<Q', raw, table + position * 8)[0]
                _, clip, tracks = struct.unpack_from('<3Q', raw, wrapper)
                parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
                    ReadContext.from_bytes(raw), base + clip, base + tracks, pointer_base=base)
                if command == 'layout':
                    row.update(wrapper=[wrapper, wrapper + 64], clip=[base + clip, parsed.physical_end],
                               track_span=[base + tracks, base + align_up(tracks + len(sequence.tracks) * 28, 16)])
            if command == 'dump':
                # Use the same parsed objects as the native index tables.
                clip = parsed.clip if parsed else sequence.clip
                row.update(total_frame=clip.total_frame, tree=node_record(clip.root, parsed))
        rows.append(row)
    return rows


def query(args):
    if args.limit < 0:
        raise ValueError('--limit must be nonnegative')
    raw = args.source.read_bytes()
    doc = MotListFile()
    doc.read(raw, label=str(args.source))
    rows = []
    for index, slot in enumerate(doc.model.slots):
        if args.motion and slot.motion_id not in args.motion:
            continue
        row = {'index': index, 'motion_id': slot.motion_id, 'embedded': slot.payload is not None}
        if slot.payload is not None:
            try:
                motion = slot.payload.value
            except MotionCodecError as exc:
                row.update(supported=False, error=str(exc))
                rows.append(row)
                continue
            if args.name and args.name.casefold() not in motion.name.casefold():
                continue
            row.update(supported=True, name=motion.name, end_frame=motion.end_frame,
                       fps=motion.frames_per_second, sequences=[])
            base, table = 0, None
            if struct.unpack_from('<I', raw)[0] == 528:
                pointers = struct.unpack_from('<Q', raw, 16)[0]
                base = struct.unpack_from('<Q', raw, pointers + index * 8)[0]
                if struct.unpack_from('<I', raw, base)[0] == 495:
                    table = base + struct.unpack_from('<Q', raw, base + 48)[0]
            row['sequences'] = sequence_records(raw, motion.sequences, base, table, args.command, 'motion')
            if args.command == 'layout':
                if table is None:
                    raise ValueError('Sequence layout inspection requires a Rise MOT 495 payload')
                row.update(payload_offset=base, payload_size=struct.unpack_from('<I', raw, base + 12)[0])
        overrides = slot.overrides
        override_table = None
        if struct.unpack_from('<I', raw)[0] == 528 and overrides:
            slots_table = struct.unpack_from('<Q', raw, 24)[0]
            override_table = struct.unpack_from('<Q', raw, slots_table + index * 72)[0]
        row['overrides'] = sequence_records(raw, overrides, 0, override_table, args.command, 'override')
        rows.append(row)
    return {'source': str(args.source.resolve()), 'slot_count': len(doc.model.slots),
            'matched': len(rows), 'motions': rows[:args.limit]}


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
