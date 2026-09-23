"""Read the native EFX header and its printable strings without assuming semantic IDs."""
import re
import struct


def query(args):
    if args.game_dir:
        from file_handlers.motion.motlist_handler import MotListHandler
        from file_handlers.motion.preview.mhr_assets import MhrPreviewAssets, preview_context
        assets = MhrPreviewAssets(preview_context(MotListHandler(), args.game_dir))
        source, data = assets.resource(str(args.source).replace('\\', '/'))
        if not data:
            raise ValueError(f'Game resource not found: {args.source}')
    else:
        source, data = str(args.source.resolve()), args.source.read_bytes()
    if len(data) < 40 or data[:4] != b'efxr':
        raise ValueError('Expected an efxr resource with a complete header')
    header = struct.unpack_from('<10I', data)
    rows = []
    for match in re.finditer(rb'[ -~]{3,}', data):
        text = match.group().decode('ascii')
        if text == 'efxr':
            continue
        start, end = match.start(), match.end()
        rows.append({'offset': start, 'text': text,
                     'before_u32': list(struct.unpack_from('<4I', data, start - 16)) if start >= 16 else [],
                     'after_u32': list(struct.unpack_from('<4I', data, end + 1)) if end + 17 <= len(data) else []})
    return {'source': source, 'size': len(data), 'version': header[1],
            'header_u32': list(header), 'strings': rows}
