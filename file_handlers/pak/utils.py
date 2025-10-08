from __future__ import annotations
import os
from pathlib import Path
from typing import List
import struct
from .pakfile import _decrypt_pak_entry_data
import fast_pakresolve

murmur3_hash = fast_pakresolve.murmur3_hash


def _normalize_path_for_hash(path: str) -> str:
    p = path.strip().replace("\\", "/")
    while "//" in p:
        p = p.replace("//", "/")
    return p


def filepath_hash(filepath: str) -> int:
    p = _normalize_path_for_hash(filepath)
    lower = murmur3_hash(p.lower().encode("utf-16le")) & 0xFFFFFFFF
    upper = murmur3_hash(p.upper().encode("utf-16le")) & 0xFFFFFFFF
    return ((upper << 32) | lower) & 0xFFFFFFFFFFFFFFFF


def guess_extension_from_header(header: bytes) -> str | None:
    if not header:
        return None
    h = header
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


def _ensure_hashes_initialized() -> None:
    global _MANIFEST_HASH, _MODINFO_HASH
    if _MANIFEST_HASH is None:
        _MANIFEST_HASH = filepath_hash(_MANIFEST_PATH)
    if _MODINFO_HASH is None:
        _MODINFO_HASH = filepath_hash(_MODINFO_PATH)


def is_mod_pak(pak_path: str) -> bool:
    """Return True if the PAK contains a manifest or modinfo.ini."""
    _ensure_hashes_initialized()
    size = os.path.getsize(pak_path)
    if size <= 16:
        return False
    with open(pak_path, "rb") as f:
        header = f.read(16)
        if len(header) != 16:
            return False
        magic, maj, minr, features, file_count, fingerprint = struct.unpack("<IBBhII", header)
        if magic != 0x414B504B:
            return False
        if (maj, minr) not in {(4, 0), (4, 1), (2, 0)}:
            return False

        entry_size = 48 if maj == 4 else 24
        table_size = file_count * entry_size
        table = bytearray(f.read(table_size))
        if len(table) != table_size:
            return False

        if (features & 16) != 0:
            f.seek(4, 1)

        if features != 0:
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
                    return True
                off += 48
        else:
            while off < table_size:
                _, _, hash_upper, hash_lower = struct.unpack_from("<qqII", table, off)
                combined = ((hash_upper & 0xFFFFFFFF) << 32) | (hash_lower & 0xFFFFFFFF)
                if combined == _MANIFEST_HASH or combined == _MODINFO_HASH:
                    return True
                off += 24
        return False


def scan_pak_files(directory: str | os.PathLike, ignore_mod_paks: bool = True) -> List[str]:
    dir_path = Path(directory)
    results: List[str] = []

    # Top-level .pak
    for pak in sorted(dir_path.glob("*.pak")):
        try:
            if pak.stat().st_size <= 16:
                continue
        except OSError:
            continue
        sp = str(pak).replace("\\", "/")
        if ignore_mod_paks and is_mod_pak(sp):
            continue
        results.append(sp)

    dlc = dir_path / "dlc"
    if dlc.is_dir():
        for pak in sorted(dlc.glob("*.pak")):
            sp = str(pak).replace("\\", "/")
            if ignore_mod_paks and is_mod_pak(sp):
                continue
            results.append(sp)

    for sub in sorted(dir_path.iterdir()):
        if sub.is_dir() and sub.name.isdigit():
            p = sub / "re_dlc_000.pak"
            if p.exists():
                sp = str(p).replace("\\", "/")
                if ignore_mod_paks and is_mod_pak(sp):
                    continue
                results.append(sp)

    return results

