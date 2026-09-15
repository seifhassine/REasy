"""Select RSZ definitions by the file's type IDs and CRCs."""
from functools import lru_cache
import mmap
from pathlib import Path
import struct

from file_handlers.rsz.rsz_file import RszRSZHeader
from utils.app_paths import resource_path
from utils.type_registry import TypeRegistry


@lru_cache(maxsize=4)
def _registry(path, modified_ns, size):
    return TypeRegistry(path)


def _snapshot(path):
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_mtime_ns, stat.st_size


@lru_cache(maxsize=32)
def _candidates(probes, snapshots):
    matches = []
    for path, modified_ns, size in snapshots:
        if not size:
            continue
        with open(path, 'rb') as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            # Presence is only a fast filter. JSON/type/CRC validation decides
            # whether a candidate can actually interpret this file.
            if all(any(data.find(f'"{key}"'.encode('ascii')) >= 0
                       for key in (f'{type_id:x}', f'{type_id:08x}')) for type_id in probes):
                matches.append((path, modified_ns, size))
    return tuple(matches)


def _matches(registry, required):
    for type_id, crc in required:
        info = registry.get_type_info(type_id)
        if info is None or int(info.get('crc', '0'), 16) != crc:
            return False
    return True


def _score(registry, required):
    exact, known = 0, 0
    for type_id, crc in required:
        info = registry.get_type_info(type_id)
        if info is not None:
            known += 1
            exact += int(info.get('crc', '0'), 16) == crc
    return exact, known


def _layout_signature(registry, required):
    return tuple(
        (type_id, info['name'], tuple(tuple(fd.get(key) for key in
         ('name', 'type', 'size', 'align', 'array', 'native', 'original_type'))
         for fd in info.get('fields', [])))
        for type_id, crc in required
        for info in [registry.get_type_info(type_id)]
        if info is not None
    )


def detect_registry(source, blocks, hint=''):
    required = set()
    for block in set(blocks.blocks.values()):
        header = RszRSZHeader()
        header.parse(source, block.offset)
        start = block.offset + header.instance_offset
        stride = 16 if header.version < 4 else 8
        if start < block.offset or start + header.instance_count * stride > block.end:
            raise ValueError(f'Invalid RSZ instance table in {block.name}')
        for index in range(1, header.instance_count):
            type_id, crc = struct.unpack_from('<II', source, start + index * stride)
            if type_id:
                required.add((type_id, crc))
    if not required:
        return None
    required = tuple(sorted(required))
    ranked = []
    if hint and Path(hint).is_file():
        registry = _registry(*_snapshot(hint))
        if _matches(registry, required):
            return registry
        ranked.append((_score(registry, required), registry))
    root = Path(resource_path('resources/data/dumps', required=True))
    snapshots = tuple(_snapshot(path) for path in sorted(root.glob('rsz*.json')))
    probes = tuple(type_id for type_id, crc in required[:8])
    for snapshot in _candidates(probes, snapshots):
        registry = _registry(*snapshot)
        ranked.append((_score(registry, required), registry))
    if not ranked:
        types = ', '.join(f'0x{type_id:08X}/{crc:08X}' for type_id, crc in required[:5])
        raise ValueError(f'No RSZ type library matches the file type IDs/CRCs ({types})')
    best = max(score for score, registry in ranked)
    matches = [registry for score, registry in ranked if score == best]
    # Identification and decoding validation are separate. A uniquely identified
    # dump can still report missing/changed types when the shared reader runs.
    if len(matches) == 1:
        return matches[0]
    signature = _layout_signature(matches[0], required)
    if any(_layout_signature(registry, required) != signature for registry in matches[1:]):
        raise ValueError('Ambiguous RSZ type libraries: ' + ', '.join(r.json_path for r in matches))
    return matches[0]
