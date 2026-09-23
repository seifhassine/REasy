"""Retarget a string-valued CLIP key in place (e.g. a VFXRangeTrack EffectId).

The CLIP stores string key values in its own string section: the key record's ``payload`` is an
offset (index) into the ASCII or UTF-16 pool.  A same-length replacement can therefore be written
straight over the pool entry - nothing moves, the file keeps its size.  Shared storage is detected
and refused so one edit cannot silently retarget another track.

"""
import argparse
import struct
from pathlib import Path


from file_handlers.motion.binary import ReadContext
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE
from file_handlers.motion.mot_clip.model import ASCII_VALUE_PROPERTY_TYPES, UTF16_VALUE_PROPERTY_TYPES
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--category', default='VFX')
    parser.add_argument('--field', default='EffectId')
    parser.add_argument('--old', required=True)
    parser.add_argument('--new', required=True)


def run(args):
    SOURCE = args.source.resolve()
    source_bytes = SOURCE.read_bytes()
    document = RISE.parse(source_bytes, label=str(SOURCE))
    pointers, _rows = struct.unpack_from('<QQ', source_bytes, 16)
    index = [slot.motion_id for slot in document.slots].index(args.motion)
    base = struct.unpack_from('<Q', source_bytes, pointers + index * 8)[0]
    motion = document.slots[index].payload.value
    table = struct.unpack_from('<Q', source_bytes, base + 48)[0]

    hits, owners = [], {}
    for position, sequence in enumerate(motion.sequences):
        selected_category = getattr(sequence.category, 'name', '') == args.category
        wrapper = struct.unpack_from('<Q', source_bytes, base + table + position * 8)[0]
        _name, clip, tracks = struct.unpack_from('<3Q', source_bytes, base + wrapper)
        parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
            ReadContext.from_bytes(source_bytes), base + clip, base + tracks, pointer_base=base)
        records = {id(record.key): record for record in [*parsed.keys, *parsed.last_keys]}
        for prop_record in parsed.properties:
            prop = prop_record.prop
            if prop.property_type in UTF16_VALUE_PROPERTY_TYPES:
                pool, unit, encoding, terminator = parsed.section_absolute_offsets['unicode_strings'], 2, 'utf-16le', b'\0\0'
            elif prop.property_type in ASCII_VALUE_PROPERTY_TYPES:
                pool, unit, encoding, terminator = parsed.section_absolute_offsets['ascii_strings'], 1, 'ascii', b'\0'
            else:
                continue
            for key in [*prop.keys, *([prop.last_key] if prop.last_key else [])]:
                record = records[id(key)]
                offset = pool + record.payload * unit
                owners[offset] = owners.get(offset, 0) + 1
                if not selected_category or prop.name != args.field or key.value != args.old:
                    continue
                old_bytes, new_bytes = args.old.encode(encoding) + terminator, args.new.encode(encoding) + terminator
                if len(old_bytes) != len(new_bytes):
                    raise ValueError('Replacement must occupy the same encoded byte length')
                hits.append((sequence, record, offset, old_bytes, new_bytes))
    if not hits:
        raise SystemExit(f'no {args.field} key equal to {args.old!r} in {args.category}')
    print(f'{SOURCE.name} motion {args.motion}: {len(hits)} matching key(s)')
    shared = {offset: owners[offset] for _, _, offset, _, _ in hits if owners[offset] > 1}
    if shared:
        raise SystemExit(f'refusing to edit shared string storage at {sorted(shared)}')
    if args.old == args.new:
        return source_bytes
    output = bytearray(source_bytes)
    for sequence, record, offset, old_bytes, new_bytes in hits:
        print(f'  frame {record.key.frame:g}  {args.old!r} -> {args.new!r}  @0x{offset:X}  ({len(old_bytes)} B)')
        assert output[offset:offset + len(old_bytes)] == old_bytes, f'unexpected bytes at 0x{offset:X}'
        output[offset:offset + len(new_bytes)] = new_bytes
    output = bytes(output)

    reopened = RISE.parse(output, label='candidate')
    motion_after = reopened.slots[index].payload.value
    table_after = struct.unpack_from('<Q', output, base + 48)[0]
    found = 0
    for position, sequence in enumerate(motion_after.sequences):
        if getattr(sequence.category, 'name', '') != args.category:
            continue
        wrapper = struct.unpack_from('<Q', output, base + table_after + position * 8)[0]
        _name, clip, tracks = struct.unpack_from('<3Q', output, base + wrapper)
        parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
            ReadContext.from_bytes(output), base + clip, base + tracks, pointer_base=base)
        for prop_record in parsed.properties:
            if prop_record.prop.name != args.field:
                continue
            for key in [*prop_record.prop.keys, *([prop_record.prop.last_key] if prop_record.prop.last_key else [])]:
                if key.value == args.new:
                    found += 1
                assert key.value != args.old, f'{args.old!r} still present'
    print(f're-parsed: {found} key(s) now read {args.new!r}')
    assert found == len(hits), (found, len(hits))
    diff = [i for i in range(len(source_bytes)) if source_bytes[i] != output[i]]
    ranges = []
    for position in diff:
        if ranges and position <= ranges[-1][1] + 1:
            ranges[-1][1] = position
        else:
            ranges.append([position, position])
    spans = [(offset, offset + len(new_bytes)) for _sequence, _record, offset, _old_bytes, new_bytes in hits]
    print(f'byte diff: {len(diff)} byte(s) in {len(ranges)} range(s)'
          + (f'  {[hex(low) for low, _high in ranges]}' if diff else ''))
    assert diff, 'nothing changed'
    assert all(any(low <= position < high for low, high in spans) for position in diff), (ranges, spans)
    assert len(output) == len(source_bytes)
    assert RISE.write(reopened) == output, 'not stable'
    for other, slot in enumerate(reopened.slots):
        if other == index:
            continue
        other_base = struct.unpack_from('<Q', source_bytes, pointers + other * 8)[0]
        other_size = struct.unpack_from('<I', source_bytes, other_base + 12)[0]
        assert source_bytes[other_base:other_base + other_size] == output[other_base:other_base + other_size], slot.motion_id
    print(f'other {len(reopened.slots) - 1} payloads byte-identical, round-trip stable')


    return bytes(output)
