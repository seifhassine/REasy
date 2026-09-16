"""Show the exact byte layout of a motion's sequence region (v528 payload).

usage: python dump_sequence_layout.py MOTLIST ID [ID ...]
"""
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT)]

from file_handlers.motion.mhr_codec import MHR_MOTION_FORMAT_CODEC as RISE, MHR_PROFILE   # noqa: E402
from file_handlers.motion.binary import ReadContext, align_up                              # noqa: E402
from file_handlers.motion.mot_clip.parser_v43 import CompactClipV43Parser                  # noqa: E402

path, want = Path(sys.argv[1]), [int(x) for x in sys.argv[2:]]
data = path.read_bytes()
doc = RISE.parse(data, label=str(path))
pointers, rows = struct.unpack_from('<QQ', data, 16)
for index, slot in enumerate(doc.slots):
    if slot.payload is None or slot.motion_id not in want:
        continue
    base = struct.unpack_from('<Q', data, pointers + index * 8)[0]
    size = struct.unpack_from('<I', data, base + 12)[0]
    ptr = struct.unpack_from('<10Q', data, base + 16)
    motion = slot.payload.value
    table = struct.unpack_from('<Q', data, base + 48)[0]
    print(f'\n==== motion {slot.motion_id} base=0x{base:X} size={size} sequences=0x{ptr[4]:X} name=0x{ptr[9]:X} '
          f'table=0x{table:X} sequences={len(motion.sequences)}')
    for position, sequence in enumerate(motion.sequences):
        wrapper = struct.unpack_from('<Q', data, base + table + position * 8)[0]
        name, clip, tracks = struct.unpack_from('<3Q', data, base + wrapper)
        parsed = CompactClipV43Parser(MHR_PROFILE).parse_result(
            ReadContext.from_bytes(data), base + clip, base + tracks, pointer_base=base)
        block_end = parsed.physical_end - base   # payload-relative
        track_bytes = len(sequence.tracks) * 28
        track_end = align_up(tracks + track_bytes, 16)
        print(f'  seq[{position}] table[+{position*8}] -> wrapper 0x{wrapper:X} (name={name} clip=0x{clip:X} tracks=0x{tracks:X})')
        print(f'      wrapper span 0x{wrapper:X}..0x{wrapper+64:X}   clip span 0x{clip:X}..0x{block_end:X} '
              f'({block_end-clip} B)   tracks span 0x{tracks:X}..0x{track_end:X} '
              f'({track_bytes} B for {len(sequence.tracks)} tracks)')
    print(f'  region: sequences offset 0x{ptr[4]:X} .. name 0x{ptr[9]:X} ({(ptr[9]-ptr[4])} B), table at +0x{table:X}')
    gap = ptr[9] - ptr[4]
    print(f'  sequences region bytes: {data[base+ptr[4]:base+ptr[9]][:96].hex(" ")} …')
