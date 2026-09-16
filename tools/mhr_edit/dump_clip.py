"""Dump the CLIP (sequence) tree of motions in a motlist, with properties and keys.

usage: python dump_clip.py MOTLIST OUT.TXT ID [ID ...]
"""
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE   # noqa: E402
from file_handlers.motion.binary import ReadContext                                        # noqa: E402
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser                  # noqa: E402

path, target, want = Path(sys.argv[1]), Path(sys.argv[2]), [int(x) for x in sys.argv[3:]]
data = path.read_bytes()
doc = RISE.parse(data, label=str(path))
pointers, _rows = struct.unpack_from('<QQ', data, 16)
out = []


def describe(parsed, label, indent='     '):
    by_node = {id(record.node): record for record in parsed.nodes}
    out.append(f'{indent}{label}: nodes={len(parsed.nodes)} properties={len(parsed.properties)} '
               f'keys={len(parsed.keys)} speed_points={len(parsed.speed_points)} last_keys={len(parsed.last_keys)} '
               f'extra_ranges={len(parsed.clip.extra_ranges)}')

    def walk(clip_node, depth):
        pad = indent + '  ' * depth
        record = by_node.get(id(clip_node))
        props = (parsed.properties[record.property_index:record.property_index + record.property_count]
                 if record else [])
        out.append(f'{pad}{clip_node.name!r} props={len(props)} children={len(clip_node.children)}')
        for item in props:
            prop = item.prop
            out.append(f'{pad}  {prop.name!r} type={getattr(prop.property_type, "name", prop.property_type)} '
                       f'frames=({prop.start_frame:g},{prop.end_frame:g}) keys={len(prop.keys)} '
                       + ', '.join(f'f{k.frame:g}={k.value!r}' for k in prop.keys[:12]))
        for child in clip_node.children:
            walk(child, depth + 1)

    walk(parsed.clip.root, 0)


for index, slot in enumerate(doc.slots):
    if slot.payload is None or slot.motion_id not in want:
        continue
    base = struct.unpack_from('<Q', data, pointers + index * 8)[0]
    motion = slot.payload.value
    table = base + struct.unpack_from('<Q', data, base + 48)[0]
    out.append(f'\n==== motion {slot.motion_id} name={motion.name!r} end={motion.end_frame} '
               f'fps={motion.frames_per_second} sequences={len(motion.sequences)}')
    for position, sequence in enumerate(motion.sequences):
        wrapper = base + struct.unpack_from('<Q', data, table + position * 8)[0]
        name, clip, tracks = struct.unpack_from('<3Q', data, wrapper)
        category = getattr(sequence.category, 'name', sequence.category)
        out.append(f'  sequence[{position}] category={category}({sequence.category}) '
                   f'clip=0x{clip:X} tracks=0x{tracks:X} tracks_count={len(sequence.tracks)}')
        try:
            parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
                ReadContext.from_bytes(data), base + clip, base + tracks, pointer_base=base)
        except Exception as exc:
            out.append(f'     parse failed: {exc!r}')
            continue
        describe(parsed, 'parsed')

target.write_text('\n'.join(out), encoding='utf-8')
print('lines', len(out))
