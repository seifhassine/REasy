"""Set the on/off window of a four-key BOOL track, in place.

The Charge Axe chainsaw track is a four-key BOOL leaf whose frame layout is

    (0, False), (on, True), (off, True), (off + 1, False)      property range = 0 .. off + 1

so a window ends implicitly one frame after the last true key (native motion 128:
keys 0/4/52/53 = "4..52 frames of chainsaw").  Only fixed-size frame floats move, so the
file is patched in place - no offset shifts and no pointer rebasing.

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
    parser.add_argument('--node', required=True, help='clip node name substring, e.g. ChargeAxeChainsawRunTrack')
    parser.add_argument('--property', required=True, help='BOOL property name, e.g. _Flag')
    parser.add_argument('--on', dest='on_frame', type=float, required=True,
                        help='first frame the flag is true')
    parser.add_argument('--off', dest='off_frame', type=float, required=True,
                        help='last frame the flag is true')


def run(args):
    if not 0 < args.on_frame < args.off_frame:
        raise ValueError('require 0 < on < off')
    SOURCE = args.source.resolve()
    data = SOURCE.read_bytes()
    output = bytearray(data)
    doc = RISE.parse(data, label=str(SOURCE))
    pointers = struct.unpack_from('<QQ', data, 16)[0]
    wanted = (0.0, args.on_frame, args.off_frame, args.off_frame + 1.0)
    patched, allowed, matched = [], set(), 0

    for index, slot in enumerate(doc.slots):
        if slot.motion_id != args.motion or slot.payload is None:
            continue
        base = struct.unpack_from('<Q', data, pointers + index * 8)[0]
        motion = slot.payload.value
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
                    if prop.name != args.property:
                        continue
                    matched += 1
                    key_index, key_count = key_span(data, item.offset)
                    keys = parsed.keys[key_index: key_index + key_count]
                    if key_count != 4:
                        raise ValueError(f'{node.name}.{prop.name}: expected 4 keys, found {key_count}')
                    before = [k.key.frame for k in keys]
                    struct.pack_into('<2f', output, item.offset, wanted[0], wanted[-1])
                    allowed.update(item.offset + byte for byte in range(8))
                    for key, frame in zip(keys, wanted):
                        struct.pack_into('<f', output, key.offset, frame)
                        allowed.update(key.offset + byte for byte in range(4))
                    patched.append((node.name, prop.name, (prop.start_frame, prop.end_frame), before))
            break

    if not matched:
        raise ValueError(f'No {args.property} on {args.node} in {args.category} of motion {args.motion}')
    if not patched:
        raise ValueError('nothing to patch')
    for name, prop_name, was, before in patched:
        print(f'  {name}.{prop_name}: range {was[0]:g}..{was[1]:g} -> 0..{wanted[-1]:g} '
              f'keys {[round(f, 3) for f in before]} -> {list(wanted)}')

    changed = [i for i, (a, b) in enumerate(zip(data, output)) if a != b]
    print(f'  changed bytes: {len(changed)} in {len(patched)} BOOL windows')
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
                    if item.prop.name != args.property:
                        continue
                    key_index, key_count = key_span(bytes(output), item.offset)
                    keys = parsed.keys[key_index: key_index + key_count]
                    assert key_count == 4
                    assert item.prop.start_frame == 0.0 and item.prop.end_frame == wanted[-1]
                    assert [k.key.frame for k in keys] == list(wanted)
                    print(f'  verify {node.name}.{item.prop.name}: range=({item.prop.start_frame:g},'
                          f'{item.prop.end_frame:g}) keys=' + ','.join(f'{k.key.frame:g}' for k in keys)
                          + ' values=' + ','.join(str(k.key.value) for k in keys))
            break

    return bytes(output)
