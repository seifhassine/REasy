from __future__ import annotations
import json
import os
import struct
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from functools import cache, partial
from pathlib import Path

lazy import zstandard
lazy from xxhash import xxh32_intdigest, xxh64_intdigest
lazy from utils.app_paths import resource_path
lazy from utils.native_build import ensure_fast_pakresolve

from .pakfile import MAGIC
from .utils import filepath_hash

# Video (ASF, .mov, MP4) and Wwise banks and packages must be stored uncompressed.
_STORED_MAGICS = (b"\x30\x26\xb2\x75", b"REMV", b"BKHD", b"AKPK")
_ROW = struct.Struct("<QqqqqQ")


def _deflate(data: bytes) -> bytes:
    # libdeflate's level 10 is smaller than zlib's best at the same speed; 11 and 12 are much slower for little.
    return ensure_fast_pakresolve().deflate(data, 10)


def _zstd(data: bytes) -> bytes:
    # The games' own zstd entries use windows up to 4 MiB; levels above 18 make them no smaller.
    params = zstandard.ZstdCompressionParameters(compression_level=18, window_log=22)
    return zstandard.ZstdCompressor(compression_params=params).compress(data)


_COMPRESSIONS = {"deflate": (1, _deflate), "zstd": (2, _zstd), "none": (0, None)}


@cache
def pak_compressions(game: str) -> tuple[str, ...]:
    catalog = json.loads(resource_path("resources/data/pak_compression.json", required=True).read_text("utf-8"))
    return tuple(name for name in _COMPRESSIONS if name != "zstd" or game in catalog["zstd"])


def _packed(compression: str, path: Path) -> tuple[bytes, int, int, int]:
    data = path.read_bytes()
    kind, compress = _COMPRESSIONS[compression]
    stored = (data if not kind or len(data) < 8 or data[:4] in _STORED_MAGICS or data[4:8] == b"ftyp"
              else compress(data))
    digest = xxh64_intdigest(data, 0xFFFFFFFF)
    checksum = (digest & 0xFFFFFFFF) << 32 | xxh32_intdigest(digest.to_bytes(8, "little"), 0xFFFFFFFF)
    return stored, len(data), kind if stored is not data else 0, checksum


def write_pak(files: Iterable[tuple[str, Path]], destination: str | Path, compression: str = "deflate",
              progress: Callable[[int], None] = lambda percent: None) -> None:
    # Largest first, so big files compress alongside the rest instead of finishing alone.
    entries = sorted(((int(Path(name).stem, 16) if name.startswith("__Unknown/") else filepath_hash(name), path)
                      for name, path in files), key=lambda entry: entry[1].stat().st_size, reverse=True)
    rows = []
    with open(destination, "wb") as out, ThreadPoolExecutor() as pool:
        out.write(struct.pack("<4I", MAGIC, 4, len(entries), 0xDEC0ADDE) + bytes(_ROW.size * len(entries)))
        packed = pool.map(partial(_packed, compression), [path for _, path in entries],
                          buffersize=os.process_cpu_count())
        for done, ((value, _), (stored, *fields)) in enumerate(zip(entries, packed), 1):
            rows.append((value, out.tell(), len(stored), *fields))
            out.write(stored)
            progress(100 * done // len(entries))
        out.seek(16)
        out.write(b"".join(_ROW.pack(*row) for row in sorted(rows)))
