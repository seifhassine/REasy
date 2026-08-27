from __future__ import annotations
import os
import re
from pathlib import Path
from typing import List
import struct
from .pakfile import _decrypt_pak_entry_data
from utils.hash_util import murmur3_hash


def normalize_pak_path(path: str, *, lowercase: bool = False) -> str:
    """
    PAK indexes are case-insensitive but some callers retain the display case,
    so lowercasing is explicit rather than an unconditional side effect.
    """
    p = path.strip().replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p.lower() if lowercase else p


def filepath_hash(filepath: str) -> int:
    p = normalize_pak_path(filepath)
    lower = murmur3_hash(p.lower().encode("utf-16le")) & 0xFFFFFFFF
    upper = murmur3_hash(p.upper().encode("utf-16le")) & 0xFFFFFFFF
    return ((upper << 32) | lower) & 0xFFFFFFFFFFFFFFFF


def guess_extension_from_header(header: bytes) -> str | None:
    if not header:
        return None
    h = header
    if len(h) >= 8:
        versioned_extension = {
            b"GCFG": "gcf",
            b"IFNT": "ift",
        }.get(h[4:8])
        if versioned_extension:
            return versioned_extension
    if h.startswith(b"FBFO"):
        return "oft"
    try:
        ascii_bytes = []
        for b in h[:8]:
            if 48 <= b <= 57 or 65 <= b <= 90 or 97 <= b <= 122 or b == 95:
                ascii_bytes.append(b)
            else:
                break
        if len(ascii_bytes) >= 3:
            return bytes(ascii_bytes).decode('ascii', errors='ignore').upper()
    except Exception:
        return None
    return None


_MANIFEST_PATH = "__MANIFEST/MANIFEST.TXT"
_MODINFO_PATH = "modinfo.ini"
_MANIFEST_HASH = None
_MODINFO_HASH = None
_MOD_PAK_CACHE: dict[tuple[str, int, int], bool] = {}


def _ensure_hashes_initialized() -> None:
    global _MANIFEST_HASH, _MODINFO_HASH
    if _MANIFEST_HASH is None:
        _MANIFEST_HASH = filepath_hash(_MANIFEST_PATH)
    if _MODINFO_HASH is None:
        _MODINFO_HASH = filepath_hash(_MODINFO_PATH)


def _mod_pak_cache_key(pak_path: str) -> tuple[str, int, int] | None:
    try:
        st = os.stat(pak_path)
    except OSError:
        return None
    mtime_ns = getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000))
    return (os.path.abspath(pak_path), int(st.st_size), int(mtime_ns))


def is_mod_pak(pak_path: str) -> bool:
    """Return True if the PAK contains a manifest or modinfo.ini."""
    from .pakfile import PAK_FLAG_ENTRY_TABLE_KEY, _skip_optional_header_sections

    _ensure_hashes_initialized()
    cache_key = _mod_pak_cache_key(pak_path)
    if cache_key is not None:
        cached = _MOD_PAK_CACHE.get(cache_key)
        if cached is not None:
            return cached
        size = cache_key[1]
    else:
        size = os.path.getsize(pak_path)

    result = False
    if size <= 16:
        if cache_key is not None:
            _MOD_PAK_CACHE[cache_key] = result
        return result

    try:
        with open(pak_path, "rb") as f:
            header = f.read(16)
            if len(header) != 16:
                return False
            magic, maj, minr, features, file_count, _ = struct.unpack("<IBBHII", header)
            if magic != 0x414B504B:
                return False
            if (maj, minr) not in {(4, 0), (4, 1), (4, 2), (2, 0)}:
                return False

            entry_size = 48 if maj == 4 else 24
            table_size = file_count * entry_size
            table = bytearray(f.read(table_size))
            if len(table) != table_size:
                return False

            _skip_optional_header_sections(f, features)

            if (features & PAK_FLAG_ENTRY_TABLE_KEY) != 0:
                key = bytearray(f.read(128))
                if len(key) != 128:
                    return False
                _decrypt_pak_entry_data(table, key)

            off = 0
            if maj == 4:
                while off < table_size:
                    hash_lower, hash_upper = struct.unpack_from("<II", table, off)
                    combined = ((hash_upper & 0xFFFFFFFF) << 32) | (hash_lower & 0xFFFFFFFF)
                    if combined == _MANIFEST_HASH or combined == _MODINFO_HASH:
                        result = True
                        break
                    off += 48
            else:
                while off < table_size:
                    _, _, hash_upper, hash_lower = struct.unpack_from("<qqII", table, off)
                    combined = ((hash_upper & 0xFFFFFFFF) << 32) | (hash_lower & 0xFFFFFFFF)
                    if combined == _MANIFEST_HASH or combined == _MODINFO_HASH:
                        result = True
                        break
                    off += 24
    finally:
        if cache_key is not None:
            _MOD_PAK_CACHE[cache_key] = result
    return result


def _natural_path_key(path: Path, root: Path) -> tuple:
    value = str(path.relative_to(root)).replace("\\", "/").casefold()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value)
    )


def _has_valid_pak_header(path: Path) -> bool:
    try:
        size = path.stat().st_size
        if size < 16:
            return False
        with path.open("rb") as stream:
            header = stream.read(16)
        magic, major, minor, _features, file_count, _fingerprint = struct.unpack(
            "<IBBHII", header
        )
        if magic != 0x414B504B or (major, minor) not in {
            (4, 0),
            (4, 1),
            (4, 2),
            (2, 0),
        }:
            return False
        entry_size = 48 if major == 4 else 24
        return size >= 16 + file_count * entry_size
    except (OSError, struct.error):
        return False


def scan_pak_files(directory: str | os.PathLike, ignore_mod_paks: bool = True) -> List[str]:
    """Discover official-layout PAKs without treating discovery order as priority."""
    root = Path(directory)
    if not root.is_dir():
        return []

    candidates = list(root.glob("*.pak"))
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.casefold() in {"dlc", "pdlc"} or child.name.isdigit():
            candidates.extend(child.glob("*.pak"))

    results: List[str] = []
    seen: set[str] = set()
    for pak in sorted(candidates, key=lambda path: _natural_path_key(path, root)):
        path_key = os.path.normcase(os.path.abspath(pak))
        if path_key in seen or not _has_valid_pak_header(pak):
            continue
        seen.add(path_key)
        normalized = str(pak).replace("\\", "/")
        if ignore_mod_paks and is_mod_pak(normalized):
            continue
        results.append(normalized)
    return results

