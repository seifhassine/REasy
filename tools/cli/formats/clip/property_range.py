"""Stretch a CLIP property to a motion's full length, in place.

`WeaponHold` keys do **not** hold their value past the last key: the property range itself must
cover the motion's frames.  A block copied from a shorter move (e.g. an axe-mode `WeaponHold`
taken from motion 131, 114 frames) therefore has to be stretched to the target motion's length.

Only fixed-size frame floats change (property `start_frame`/`end_frame`, key frames), so the file
is patched in place - no offset moves and no pointer needs rebasing.

"""
import argparse
import struct
from pathlib import Path


from file_handlers.motion.binary import ReadContext
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser

def key_span(data, record_offset):
    """Property record layout (compact CLIP v43): key_index = u64 @+32, key_count = u16 @+40."""
    return (struct.unpack_from('<Q', data, record_offset + 32)[0],
            struct.unpack_from('<H', data, record_offset + 40)[0])


def configure(parser):
    parser.add_argument('--motion', type=int, required=True)
    parser.add_argument('--category', default='EXTRA_0')
    parser.add_argument('--node', required=True, help='clip node name substring, e.g. WeaponHold')
    parser.add_argument('--property', action='append', default=[], help='limit to these property names')
    parser.add_argument('--end-frame', type=float, help='defaults to the motion end frame')


def run(args):
    SOURCE = args.source.resolve()
    data = SOURCE.read_bytes()
    output = bytearray(data)
    doc = RISE.parse(data, label=str(SOURCE))
    pointers = struct.unpack_from('<QQ', data, 16)[0]
    patched, matched, allowed, expected = [], 0, set(), {}

    for index, slot in enumerate(doc.slots):
        if slot.motion_id != args.motion or slot.payload is None:
            continue
        base = struct.unpack_from('<Q', data, pointers + index * 8)[0]
        motion = slot.payload.value
        end_frame = args.end_frame if args.end_frame is not None else motion.end_frame
        table = base + struct.unpack_from('<Q', data, base + 48)[0]
        for position, sequence in enumerate(motion.sequences):
            category = getattr(sequence.category, 'name', sequence.category)
            if category != args.category:
                continue
            wrapper = base + struct.unpack_from('<Q', data, table + position * 8)[0]
            _name, clip, tracks = struct.unpack_from('<3Q', data, wrapper)
            parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
                ReadContext.from_bytes(data), base + clip, base + tracks, pointer_base=base)
            stack = [parsed.clip.root]
            while stack:
                node = stack.pop()
                stack.extend(node.children)
                if args.node not in node.name:
                    continue
                record = next((r for r in parsed.nodes if r.node is node), None)
                if record is None:
                    continue
                for item in parsed.properties[record.property_index: record.property_index + record.property_count]:
                    prop = item.prop
                    if args.property and prop.name not in args.property:
                        continue
                    matched += 1
                    if prop.end_frame >= end_frame - 1e-4:
                        continue
                    key_index, key_count = key_span(data, item.offset)
                    keys = parsed.keys[key_index: key_index + key_count]
                    assert keys, f'{node.name}.{prop.name}: no key to stretch'
                    struct.pack_into('<f', output, item.offset + 4, end_frame)
                    struct.pack_into('<f', output, keys[-1].offset, end_frame)
                    allowed.update(offset + byte for offset in (item.offset + 4, keys[-1].offset) for byte in range(4))
                    expected[item.offset] = (keys[-1].offset, struct.unpack('<f', struct.pack('<f', end_frame))[0])
                    patched.append((node.name, prop.name, prop.end_frame, end_frame, keys[-1].offset))
            break

    if not matched:
        raise ValueError(f'No matching property on {args.node} in {args.category} of motion {args.motion}')
    if not patched:
        return data
    for name, prop_name, was, now, key_offset in patched:
        print(f'  {name}.{prop_name}: range end {was:g} -> {now:g} (last key @0x{key_offset:X})')

    changed = [i for i, (a, b) in enumerate(zip(data, output)) if a != b]
    print(f'  changed bytes: {len(changed)} in {len(patched)} property/key frame pairs')
    assert set(changed) <= allowed, 'Bytes outside the selected frame fields changed'
    assert len(output) == len(data), 'size changed'

    verified = RISE.parse(bytes(output), label='verify')
    for index, slot in enumerate(verified.slots):
        if slot.motion_id != args.motion or slot.payload is None:
            continue
        motion = slot.payload.value
        base = struct.unpack_from('<Q', output, pointers + index * 8)[0]
        table = base + struct.unpack_from('<Q', output, base + 48)[0]
        for position, sequence in enumerate(motion.sequences):
            if getattr(sequence.category, 'name', sequence.category) != args.category:
                continue
            wrapper = base + struct.unpack_from('<Q', output, table + position * 8)[0]
            _n, clip, tracks = struct.unpack_from('<3Q', output, wrapper)
            parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
                ReadContext.from_bytes(bytes(output)), base + clip, base + tracks, pointer_base=base)
            stack = [parsed.clip.root]
            while stack:
                node = stack.pop()
                stack.extend(node.children)
                if args.node not in node.name:
                    continue
                record = next((r for r in parsed.nodes if r.node is node), None)
                if record is None:
                    continue
                for item in parsed.properties[record.property_index: record.property_index + record.property_count]:
                    if args.property and item.prop.name not in args.property:
                        continue
                    key_index, key_count = key_span(bytes(output), item.offset)
                    keys = parsed.keys[key_index: key_index + key_count]
                    if item.offset in expected:
                        last_offset, value = expected[item.offset]
                        assert keys[-1].offset == last_offset and item.prop.end_frame == value
                        assert keys[-1].key.frame == value
                    print(f'  verify {node.name}.{item.prop.name}: frames=({item.prop.start_frame:g},'
                          f'{item.prop.end_frame:g}) keys=' + ','.join(f'{k.key.frame:g}' for k in keys))
            break


    return bytes(output)
