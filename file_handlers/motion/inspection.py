"""JSON views of MOTLIST slots, sequences and CLIP trees, including native index positions."""
from dataclasses import asdict
import struct

from file_handlers.motion.motlist_file import MotListFile
from file_handlers.motion.mhr.codec import MHR_PROFILE
from file_handlers.motion.binary import ReadContext, align_up
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser
from file_handlers.motion.errors import MotionCodecError


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


def describe_motion_list(raw: bytes, label: str, *, motion_ids=None, name_contains=None, detail='query') -> dict:
    """List slots with names, frames and sequences; ``detail`` is query, dump (CLIP trees) or layout (offsets)."""
    doc = MotListFile()
    doc.read(raw, label=label)
    rows = []
    for index, slot in enumerate(doc.model.slots):
        if motion_ids and slot.motion_id not in motion_ids:
            continue
        row = {'index': index, 'motion_id': slot.motion_id, 'embedded': slot.payload is not None}
        if slot.payload is not None:
            try:
                motion = slot.payload.value
            except MotionCodecError as exc:
                row.update(supported=False, error=str(exc))
                rows.append(row)
                continue
            if name_contains and name_contains.casefold() not in motion.name.casefold():
                continue
            row.update(supported=True, name=motion.name, end_frame=motion.end_frame,
                       fps=motion.frames_per_second, sequences=[])
            base, table = 0, None
            if struct.unpack_from('<I', raw)[0] == 528:
                pointers = struct.unpack_from('<Q', raw, 16)[0]
                base = struct.unpack_from('<Q', raw, pointers + index * 8)[0]
                if struct.unpack_from('<I', raw, base)[0] == 495:
                    table = base + struct.unpack_from('<Q', raw, base + 48)[0]
            row['sequences'] = sequence_records(raw, motion.sequences, base, table, detail, 'motion')
            if detail == 'layout':
                if table is None:
                    raise ValueError('Sequence layout inspection requires a Rise MOT 495 payload')
                row.update(payload_offset=base, payload_size=struct.unpack_from('<I', raw, base + 12)[0])
        overrides = slot.overrides
        override_table = None
        if struct.unpack_from('<I', raw)[0] == 528 and overrides:
            slots_table = struct.unpack_from('<Q', raw, 24)[0]
            override_table = struct.unpack_from('<Q', raw, slots_table + index * 72)[0]
        row['overrides'] = sequence_records(raw, overrides, 0, override_table, detail, 'override')
        rows.append(row)
    return {'slot_count': len(doc.model.slots), 'matched': len(rows), 'motions': rows}
