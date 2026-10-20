from __future__ import annotations
import os
import re
from pathlib import Path
from typing import List, NamedTuple
import struct
lazy import numpy as np
lazy from .pakfile import (
    PAK_HEADER_SIZE,
    _decrypt_pak_entry_data,
    read_pak_header,
    PAK_FLAG_ENTRY_TABLE_KEY,
    PAK_FLAG_HASH_REMAP,
    _skip_optional_header_sections,
)
lazy from utils.hash_util import murmur3_hash


def normalize_pak_path(path: str, *, lowercase: bool = False) -> str:
    """PAK indexes ignore case, but some callers keep the display case."""
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
_PAK_MOD_FLAGS_CACHE: dict[tuple[str, int, int], tuple[bool, bool]] = {}


class PakModFlags(NamedTuple):
    is_payload: bool
    has_invalidated: bool


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
    return (os.path.abspath(pak_path), st.st_size, st.st_mtime_ns)


def pak_mod_flags(pak_path: str) -> PakModFlags:
    """Return (is_mod_payload, has_invalidated_entries) in one table walk."""
    _ensure_hashes_initialized()
    cache_key = _mod_pak_cache_key(pak_path)
    if cache_key is not None:
        cached = _PAK_MOD_FLAGS_CACHE.get(cache_key)
        if cached is not None:
            return cached

    hashes = read_pak_entry_hashes(pak_path)
    flags = PakModFlags(
        bool(np.any((hashes == _MANIFEST_HASH) | (hashes == _MODINFO_HASH))),
        bool(np.any(hashes == 0)),
    )
    if cache_key is not None:
        _PAK_MOD_FLAGS_CACHE[cache_key] = flags
    return flags


def is_mod_pak(pak_path: str) -> bool:
    """Return True if the PAK contains a manifest or modinfo.ini."""
    return pak_mod_flags(pak_path)[0]


def pak_has_invalidated_entries(pak_path: str) -> bool:
    """Return True if entries in the PAK had their path hash zeroed."""
    return pak_mod_flags(pak_path)[1]


def pak_is_modded(pak_path: str) -> bool:
    flags = pak_mod_flags(pak_path)
    return flags.is_payload or flags.has_invalidated


def any_pak_modded(pak_paths) -> bool:
    for pak in pak_paths:
        try:
            if pak_is_modded(pak):
                return True
        except Exception:
            continue
    return False


def read_pak_entry_hashes(pak_path: str, *, strict: bool = False):
    empty = np.empty(0, dtype='<u8')

    try:
        with open(pak_path, "rb") as f:
            header = read_pak_header(f, pak_path, supported_only=strict)
            maj, features, file_count = header.major, header.feature_flags, header.file_count
            if not file_count:
                return empty

            entry_size = header.entry_size
            table_size = file_count * entry_size
            table = f.read(table_size)
            if len(table) != table_size:
                raise OSError("Unexpected EOF reading PAK entry table")

            _skip_optional_header_sections(f, features)
            if features & PAK_FLAG_HASH_REMAP:
                remap_header = f.read(8)
                if len(remap_header) != 8:
                    raise OSError("Unexpected EOF reading PAK remap header")
                f.seek(struct.unpack('<Q', remap_header)[0] * 16, os.SEEK_CUR)

            if (features & PAK_FLAG_ENTRY_TABLE_KEY) != 0:
                key = f.read(128)
                if len(key) != 128:
                    raise OSError("Unexpected EOF reading PAK key")
                table = bytearray(table)
                _decrypt_pak_entry_data(table, bytearray(key))

            rows = np.frombuffer(table, dtype='<u4').reshape(file_count, entry_size // 4)
            lower, upper = (rows[:, 0], rows[:, 1]) if maj == 4 else (rows[:, 5], rows[:, 4])
            return (upper.astype('<u8') << 32) | lower
    except OSError:
        if strict:
            raise
        return empty


def _natural_path_key(path: Path, root: Path) -> tuple:
    value = str(path.relative_to(root)).replace("\\", "/").casefold()
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in re.split(r"(\d+)", value)
    )


def _has_valid_pak_header(path: Path) -> bool:
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            header = read_pak_header(stream, str(path), supported_only=False)
        return size >= PAK_HEADER_SIZE + header.file_count * header.entry_size
    except OSError:
        return False


def _scan_pak_candidates(directory: str | os.PathLike) -> List[str]:
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
        results.append(str(pak).replace("\\", "/"))
    return results


def scan_pak_files(directory: str | os.PathLike) -> List[str]:
    return [pak for pak in _scan_pak_candidates(directory) if not is_mod_pak(pak)]


def scan_all_pak_files(directory: str | os.PathLike) -> List[str]:
    return _scan_pak_candidates(directory)

