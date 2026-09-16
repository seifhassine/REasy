"""Copy CLIP sequences (SOUND / VFX) from one motion into another, retimed.

The v528 payload lays its sequence region out as

    table[align_up(count*8, 16)] then, per sequence: wrapper(64) + clip(align 16) + tracks(28*N, align 16)

with every stored pointer payload-relative.  REasy ships no v43 CLIP writer, so this
copies the source blocks verbatim, rebases the pointers listed in the model's own
relocation table, and patches only the fixed-size frame floats (property ranges and
key frames).  Everything is then re-parsed and compared with the source graph.

usage: python copy_clip_sequences.py MOTLIST OUTDIR --from 109 --to 620 \
           [--categories SOUND,VFX] [--shift 0]
"""
import argparse
import hashlib
import json
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

from file_handlers.motion.binary import ReadContext, align_up                        # noqa: E402
from file_handlers.motion.errors import MotionWriteError                             # noqa: E402
from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE   # noqa: E402
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser            # noqa: E402


def parse_clip(data, base, wrapper_rel):
    _name, clip_rel, tracks_rel = struct.unpack_from('<3Q', data, base + wrapper_rel)
    parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
        ReadContext.from_bytes(data), base + clip_rel, base + tracks_rel, pointer_base=base)
    return clip_rel, tracks_rel, parsed


def sequence_blocks(data, motion, base):
    table_rel = struct.unpack_from('<Q', data, base + 48)[0]
    blocks = []
    for position, sequence in enumerate(motion.sequences):
        wrapper_rel = struct.unpack_from('<Q', data, base + table_rel + position * 8)[0]
        clip_rel, tracks_rel, parsed = parse_clip(data, base, wrapper_rel)
        clip_end_rel = parsed.physical_end - base
        tracks_end_rel = align_up(tracks_rel + len(sequence.tracks) * 28, 16)
        assert clip_rel < clip_end_rel <= tracks_rel < tracks_end_rel, (clip_rel, clip_end_rel, tracks_rel, tracks_end_rel)
        blocks.append(dict(position=position, category=sequence.category, tracks=len(sequence.tracks),
                           wrapper=wrapper_rel, clip=clip_rel, clip_end=clip_end_rel,
                           tracks_rel=tracks_rel, tracks_end=tracks_end_rel, parsed=parsed,
                           size=tracks_end_rel - wrapper_rel))
    return blocks


def clip_graph(parsed):
    """Decoded, order-stable description of a clip graph for comparisons."""
    by_node = {id(record.node): record for record in parsed.nodes}
    props = {id(record.prop): record for record in parsed.properties}

    def walk(node):
        record = by_node.get(id(node))
        entries = []
        if record:
            for item in parsed.properties[record.property_index:record.property_index + record.property_count]:
                entries.append((item.prop.name, getattr(item.prop.property_type, 'name', item.prop.property_type),
                                round(item.prop.start_frame, 3), round(item.prop.end_frame, 3),
                                tuple((round(k.frame, 3), k.value) for k in item.prop.keys)))
        return (node.name, tuple(entries), tuple(walk(child) for child in node.children))

    return walk(parsed.clip.root)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('motlist', type=Path)
parser.add_argument('output_directory', type=Path)
parser.add_argument('--from', dest='source_id', type=int, default=109)
parser.add_argument('--to', dest='target_id', type=int, default=620)
parser.add_argument('--categories', default='SOUND,VFX')
parser.add_argument('--shift', type=float, default=0.0, help='frames added to every copied property range and key')
parser.add_argument('--total-frame', choices=('keep', 'target'), default='keep',
                    help='clip total_frame: keep the source value or use the target motion duration')
args = parser.parse_args()
SOURCE = args.motlist.resolve()
source_bytes = SOURCE.read_bytes()
document = RISE.parse(source_bytes, label=str(SOURCE))
pointers, slot_table = struct.unpack_from('<QQ', source_bytes, 16)
ids = [slot.motion_id for slot in document.slots]
assert len(document.slots) - 1 == ids.index(args.target_id), 'the target motion must be the last payload'
source_index, target_index = ids.index(args.source_id), ids.index(args.target_id)
source_base = struct.unpack_from('<Q', source_bytes, pointers + source_index * 8)[0]
target_base = struct.unpack_from('<Q', source_bytes, pointers + target_index * 8)[0]
source_size = struct.unpack_from('<I', source_bytes, source_base + 12)[0]
target_size = struct.unpack_from('<I', source_bytes, target_base + 12)[0]
source_motion = document.slots[source_index].payload.value
target_motion = document.slots[target_index].payload.value
wanted = {name.strip().upper() for name in args.categories.split(',') if name.strip()}

source_blocks = sequence_blocks(source_bytes, source_motion, source_base)
target_blocks = sequence_blocks(source_bytes, target_motion, target_base)
copied = [b for b in source_blocks if getattr(b['category'], 'name', '') in wanted]
assert len(copied) == len(wanted), [b['category'] for b in source_blocks]
print(f'source motion {args.source_id} sequences: ' + ', '.join(
    f"{b['position']}:{getattr(b['category'], 'name', b['category'])}({b['size']}B)" for b in source_blocks))
print(f'target motion {args.target_id} sequences: ' + ', '.join(
    f"{b['position']}:{getattr(b['category'], 'name', b['category'])}({b['size']}B)" for b in target_blocks))
print(f'copying {[getattr(b["category"], "name", "") for b in copied]} with shift {args.shift:g} frames')

# --- assemble the new region: table, then the copied sequences, then the target's own
new_table_size = align_up((len(copied) + len(target_blocks)) * 8, 16)
region_rel = struct.unpack_from('<Q', source_bytes, target_base + 48)[0]
region = bytearray(new_table_size)
entries = []
cursor = region_rel + new_table_size
for block, origin in [(b, source_base) for b in copied] + [(b, target_base) for b in target_blocks]:
    start = cursor
    wrapper = bytearray(source_bytes[origin + block['wrapper']:origin + block['wrapper'] + 64])
    clip = bytearray(source_bytes[origin + block['clip']:origin + block['clip_end']])
    tracks = bytearray(source_bytes[origin + block['tracks_rel']:origin + block['tracks_end']])
    delta = start - block['wrapper']
    assert delta % 1 == 0
    struct.pack_into('<3Q', wrapper, 0, 0, block['clip'] - block['wrapper'] + start, block['tracks_rel'] - block['wrapper'] + start)
    entries.append((block['wrapper'] + delta, len(clip), delta))
    region.extend(wrapper)
    cursor += 64
    assert cursor == start + 64
    region.extend(clip)
    cursor += len(clip)
    region.extend(tracks)
    cursor += len(tracks)
    # rebase every pointer the model itself declared inside this block
    rebased = 0
    for relocation in document.relocations.values():
        if relocation.base != origin:
            continue
        source_rel = relocation.offset - origin
        if not block['wrapper'] <= source_rel < block['tracks_end']:
            continue
        if not block['wrapper'] <= relocation.target - origin < block['tracks_end']:
            raise MotionWriteError(f'sequence pointer escapes its block: 0x{relocation.target:X}')
        if delta % relocation.unit:
            raise MotionWriteError('sequence rebase breaks pointer alignment')
        stored = int.from_bytes(source_bytes[relocation.offset:relocation.offset + struct.calcsize('<' + relocation.format)], 'little')
        offset_in_region = (start - region_rel) + (source_rel - block['wrapper'])
        struct.pack_into('<' + relocation.format, region, offset_in_region, stored + delta // relocation.unit)
        rebased += 1
    # retime the fixed-size frame fields
    for record in block['parsed'].properties:
        position = (start - region_rel) + (record.offset - origin - block['wrapper'])
        if not 0 <= position <= len(region) - 72:
            continue
        start_frame, end_frame = struct.unpack_from('<2f', region, position)
        struct.pack_into('<f', region, position, max(0.0, start_frame + args.shift))
        if end_frame >= 0:
            struct.pack_into('<f', region, position + 4, end_frame + args.shift)
    for record in block['parsed'].keys:
        position = (start - region_rel) + (record.offset - origin - block['wrapper'])
        assert 0 <= position <= len(region) - 4, position
        frame = struct.unpack_from('<f', region, position)[0]
        struct.pack_into('<f', region, position, max(0.0, frame + args.shift))
    if args.total_frame == 'target':
        struct.pack_into('<f', region, (start - region_rel) + 64 + 8, target_motion.end_frame)
    block['new_start'] = start
    print(f"  {getattr(block['category'], 'name', '')}: blocks 0x{block['wrapper']:X} -> 0x{start:X} "
          f"(delta {delta:+d}), clip {len(clip)}B, tracks {len(tracks)}B, pointers rebased {rebased}")
for position, (wrapper_rel, _clip_len, _delta) in enumerate(entries):
    struct.pack_into('<Q', region, position * 8, wrapper_rel)

name_rel = struct.unpack_from('<Q', source_bytes, target_base + 16 + 9 * 8)[0]
name_end = name_rel
while source_bytes[target_base + name_end:target_base + name_end + 2] != b'\x00\x00':
    name_end += 2
name_block = source_bytes[target_base + name_rel:target_base + name_end + 2]
size = region_rel + len(region)
size += (-size) % 16
body = bytearray(source_bytes[target_base:target_base + region_rel])
body.extend(region)
name_offset = len(body)
body.extend(name_block)
body.extend(bytes((-len(body)) % 16))
size = len(body)
assert size % 16 == 0
struct.pack_into('<I', body, 12, size)
struct.pack_into('<Q', body, 16, size)
struct.pack_into('<Q', body, 16 + 4 * 8, region_rel)
struct.pack_into('<Q', body, 16 + 9 * 8, name_offset)
struct.pack_into('<H', body, 112, len(target_motion.skeleton.joints))
struct.pack_into('<B', body, 116, len(copied) + len(target_blocks))
struct.pack_into('<f', body, 96, target_motion.end_frame)
output = bytearray(source_bytes[:target_base])
output.extend(body)
output.extend(bytes((-len(output)) % 16))
new_slot_table = len(output)
output.extend(source_bytes[slot_table:])
struct.pack_into('<Q', output, 24, new_slot_table)
output = bytes(output)
print(f'payload {target_size:,} -> {size:,} B, file {len(source_bytes):,} -> {len(output):,} B, '
      f'slot table 0x{slot_table:X} -> 0x{new_slot_table:X}')

# --- verify
folder = args.output_directory.resolve()
folder.mkdir(parents=True, exist_ok=True)
candidate = folder / SOURCE.name
candidate.write_bytes(output)
reopened = RISE.parse(output, label='candidate')
assert [slot.motion_id for slot in reopened.slots] == ids
motion = reopened.slots[target_index].payload.value
categories = [getattr(sequence.category, 'name', str(sequence.category)) for sequence in motion.sequences]
print(f're-parsed target sequences: {categories}')
assert categories == [getattr(b['category'], 'name', '') for b in copied] + \
                     [getattr(b['category'], 'name', '') for b in target_blocks]
for block in copied:
    parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
        ReadContext.from_bytes(output), target_base + block['new_start'] + 64,
        target_base + block['new_start'] + 64 + (block['clip_end'] - block['clip']), pointer_base=target_base)
    source_graph = clip_graph(block['parsed'])
    candidate_graph = clip_graph(parsed)
    if args.shift == 0 and args.total_frame == 'keep':
        assert candidate_graph == source_graph, getattr(block['category'], 'name')
    else:
        def shifted(graph, amount, is_root=True):
            name, props, children = graph
            fixed = tuple((p, t, round(max(0.0, s + amount), 3),
                           round(e + amount, 3) if e >= 0 else e,
                           tuple((round(max(0.0, f + amount), 3), v) for f, v in keys))
                          for p, t, s, e, keys in props)
            return (name, fixed, tuple(shifted(child, amount, False) for child in children))
        assert candidate_graph == shifted(source_graph, args.shift), getattr(block['category'], 'name')
    print(f"  verified {getattr(block['category'], 'name', '')}: {len(parsed.nodes)} nodes, "
          f"{len(parsed.properties)} properties, {len(parsed.keys)} keys match the source"
          + (f' (shifted {args.shift:g})' if args.shift else ''))
# the target's own animation and the other payloads must be untouched
kept = 0
for index, slot in enumerate(reopened.slots):
    if index == target_index:
        continue
    old_base = struct.unpack_from('<Q', source_bytes, pointers + index * 8)[0]
    new_base = struct.unpack_from('<Q', output, pointers + index * 8)[0]
    old_size = struct.unpack_from('<I', source_bytes, old_base + 12)[0]
    assert source_bytes[old_base:old_base + old_size] == output[new_base:new_base + old_size], slot.motion_id
    kept += 1
assert [n.joint.name for n in motion.animation_nodes] == [n.joint.name for n in target_motion.animation_nodes]
for old_node, new_node in zip(target_motion.animation_nodes, motion.animation_nodes):
    for attribute in ('translation', 'rotation', 'scale'):
        old_track, new_track = getattr(old_node, attribute), getattr(new_node, attribute)
        if old_track is None:
            assert new_track is None
            continue
        assert (old_track.frames, old_track.values) == (new_track.frames, new_track.values), old_node.joint.name
print(f'unchanged payloads byte-identical: {kept}; target animation channels identical; round-trip '
      f'{"stable" if RISE.write(reopened) == output else "UNSTABLE"}')

(folder / 'manifest.json').write_text(json.dumps(dict(
    source=str(SOURCE), source_sha256=hashlib.sha256(source_bytes).hexdigest(), source_size=len(source_bytes),
    candidate=str(candidate), candidate_sha256=hashlib.sha256(output).hexdigest(), candidate_size=len(output),
    from_motion=args.source_id, to_motion=args.target_id, categories=sorted(wanted), shift=args.shift,
    total_frame=args.total_frame, target_payload_size=size, size_delta=len(output) - len(source_bytes),
    copied=[dict(category=getattr(b['category'], 'name', ''), old=hex(b['wrapper']), new=hex(b['new_start'])) for b in copied]),
    indent=2), encoding='utf-8')
print(f'wrote {candidate}')
