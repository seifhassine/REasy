"""Silence CLIP sound events from a given frame on, without restructuring the clip.

Scalar CLIP key values live inline in the key record (the u64 at record+16; the low
bytes carry the value for U8/U16/U32/...), so an event is removed by zeroing that
field: the property, its range and its key stay in place, the file keeps its size and
no pointer moves.  A zeroed wwise trigger id posts no event.

"""
import argparse
import struct
from pathlib import Path


from file_handlers.motion.binary import ReadContext
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE
from file_handlers.motion.mot_clip.model import ClipPropertyType
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser

INLINE_TYPES = {ClipPropertyType.BOOL, ClipPropertyType.U8, ClipPropertyType.U16, ClipPropertyType.U32,
                ClipPropertyType.U64, ClipPropertyType.S8, ClipPropertyType.S16, ClipPropertyType.S32,
                ClipPropertyType.S64, ClipPropertyType.F32, ClipPropertyType.F64}


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--category', default='SOUND')
    parser.add_argument('--node-class', default='snow.wwise.SoundBakeTriggerTracks')
    parser.add_argument('--from-frame', type=float, default=100.0)
    parser.add_argument('--frames', default='', help='explicit comma separated frames to drop (overrides --from-frame)')


def run(args):
    SOURCE = args.source.resolve()
    source_bytes = SOURCE.read_bytes()
    document = RISE.parse(source_bytes, label=str(SOURCE))
    pointers, slot_table = struct.unpack_from('<QQ', source_bytes, 16)
    index = [slot.motion_id for slot in document.slots].index(args.motion)
    base = struct.unpack_from('<Q', source_bytes, pointers + index * 8)[0]
    motion = document.slots[index].payload.value
    table = struct.unpack_from('<Q', source_bytes, base + 48)[0]
    matches = [i for i, sequence in enumerate(motion.sequences)
               if getattr(sequence.category, 'name', '') == args.category]
    if len(matches) != 1:
        raise ValueError(f'Sequence category {args.category} resolved to {len(matches)} entries')
    wrapper = struct.unpack_from('<Q', source_bytes, base + table + matches[0] * 8)[0]
    _name, clip, tracks = struct.unpack_from('<3Q', source_bytes, base + wrapper)
    parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
        ReadContext.from_bytes(source_bytes), base + clip, base + tracks, pointer_base=base)
    records = {id(record.key): record for record in parsed.keys}
    classes = {name.strip() for name in args.node_class.split(',') if name.strip()}
    explicit = {round(float(value)) for value in args.frames.split(',') if value.strip()}
    if explicit:
        print(f'dropping exactly these frames: {sorted(explicit)}')

    removed, kept = [], []
    for record in parsed.nodes:
        if record.node.name not in classes:
            continue
        for item in parsed.properties[record.property_index:record.property_index + record.property_count]:
            if item.prop.property_type not in INLINE_TYPES:
                continue
            for key in item.prop.keys:
                entry = records[id(key)]
                row = (round(key.frame, 3), item.prop.name, record.node.name, key.value, entry.offset)
                wanted = round(key.frame) in explicit if explicit else key.frame >= args.from_frame
                (removed if wanted else kept).append(row)
    print(f'motion {args.motion} sequence {args.category}: {len(classes)} node class(es) matched, '
          f'{len(removed)} event(s) at or after frame {args.from_frame:g}, {len(kept)} earlier event(s) kept')
    for frame, prop, node, value, offset in removed:
        print(f'   remove f{frame:g} {prop}={value} ({node.rsplit(".", 1)[-1]}) @0x{offset:X}+16')
    for frame, prop, node, value, offset in sorted(kept):
        mark = '  (<- also after 100? no)' if frame >= args.from_frame else ''
        if frame >= 90:
            print(f'   keep   f{frame:g} {prop}={value} ({node.rsplit(".", 1)[-1]}){mark}')
    if not removed:
        return source_bytes

    output = bytearray(source_bytes)
    for _frame, _prop, _node, _value, offset in removed:
        struct.pack_into('<Q', output, offset + 16, 0)
    output = bytes(output)

    reopened = RISE.parse(output, label='candidate')
    motion_after = reopened.slots[index].payload.value
    parsed_after = CompactClipV43Parser(MHR_PROFILE).parse_result(
        ReadContext.from_bytes(output), base + clip, base + tracks, pointer_base=base)
    records_after = {id(record.key): record for record in parsed_after.keys}
    removed_offsets = {offset for _f, _p, _n, _v, offset in removed}
    for frame, prop, node, value, offset in removed:
        stored = struct.unpack_from('<Q', output, offset + 16)[0]
        assert stored == 0, (frame, prop, stored)
        assert struct.unpack_from('<f', output, offset)[0] == struct.unpack_from('<f', source_bytes, offset)[0]
    print(f'silenced {len(removed)} event(s) at frames {[frame for frame, *_ in removed]}')
    # every other key record must keep its value and frame exactly
    checked = 0
    for record in parsed_after.keys:
        if record.offset - base in removed_offsets or record.offset in removed_offsets:
            continue
        assert output[record.offset:record.offset + 32] == source_bytes[record.offset:record.offset + 32], hex(record.offset)
        checked += 1
    for record in parsed_after.properties:
        assert output[record.offset:record.offset + 8] == source_bytes[record.offset:record.offset + 8], hex(record.offset)
    print(f'all other key records byte-identical: {checked}; every property range unchanged')
    still = [(round(key.frame, 3), round(key.value, 6) if isinstance(key.value, (int, float)) else key.value)
             for record in parsed_after.nodes
             for item in parsed_after.properties[record.property_index:record.property_index + record.property_count]
             for key in item.prop.keys if isinstance(key.value, (int, float)) and key.value]
    print(f'remaining numeric sound events: {sorted(value for _f, value in still)}')
    diff = [i for i in range(len(source_bytes)) if source_bytes[i] != output[i]]
    ranges = []
    for position in diff:
        if ranges and position <= ranges[-1][1] + 1:
            ranges[-1][1] = position
        else:
            ranges.append([position, position])
    print(f'byte diff: {len(diff)} byte(s) in {len(ranges)} range(s): '
          + ', '.join(f'0x{low:X}-0x{high:X}' for low, high in ranges[:8]))
    allowed = {offset + byte for _f, _p, _n, _v, offset in removed for byte in range(16, 24)}
    assert set(diff) <= allowed, 'Bytes outside selected inline key values changed'
    for record in parsed_after.keys:
        if record.offset in removed_offsets:
            assert record.key.value == 0, 'Reopened event was not silenced'
    assert len(output) == len(source_bytes)
    assert RISE.write(reopened) == output, 'not stable'
    assert [slot.motion_id for slot in reopened.slots] == [slot.motion_id for slot in document.slots]
    for other, slot in enumerate(reopened.slots):
        if other == index:
            continue
        other_base = struct.unpack_from('<Q', source_bytes, pointers + other * 8)[0]
        other_size = struct.unpack_from('<I', source_bytes, other_base + 12)[0]
        assert source_bytes[other_base:other_base + other_size] == output[other_base:other_base + other_size], slot.motion_id
    print(f'other {len(reopened.slots) - 1} payloads byte-identical, animation channels untouched, round-trip stable')


    return bytes(output)
