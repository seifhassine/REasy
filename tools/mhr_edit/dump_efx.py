"""Dump the effect ids/names declared inside an MHR .efx file (efxr v1).

usage: python dump_efx.py PATH [PATH ...]
"""
import os
import re
import struct
import sys
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]

from file_handlers.motion.motlist_handler import MotListHandler                   # noqa: E402
from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context  # noqa: E402

assets = MhrPreviewAssets(preview_context(MotListHandler()))
paths = sys.argv[1:]
if not paths:
    paths = ['vfx/editor/efd_pl/efd_l-swd/efd_0004_l-swd_00_0010.efx',
             'vfx/editor/efd_pl/efd_l-swd/efd_0004_l-swd_00_0005.efx',
             'vfx/editor/efd_pl/efd_l-swd/efd_0004_l-swd_00_0000.efx',
             'vfx/editor/efd_pl/efd_l-swd/efd_0004_l-swd_00_0011.efx']
for path in paths:
    found, data = assets.resource(path)
    if not data:
        print(f'{path}: not found')
        continue
    header = struct.unpack_from('<10I', data, 0)
    print(f'\n--- {path.split("/")[-1]}  {len(data):,} B  magic={data[:4]!r} v={header[1]} '
          f'u32[2]={header[2]} u32[3]={header[3]} u32[7]={header[7]}')
    # strings with their offsets, plus the words around them
    for match in re.finditer(rb'[ -~]{3,}', data):
        text = match.group().decode('ascii')
        if text in ('efxr',):
            continue
        offset = match.start()
        before = struct.unpack_from('<4I', data, max(0, offset - 16)) if offset >= 16 else ()
        after = struct.unpack_from('<4I', data, offset + len(match.group()) + 1) \
            if offset + len(match.group()) + 17 <= len(data) else ()
        print(f'    @0x{offset:04X} {text!r:<22} before={before} after={after}')
