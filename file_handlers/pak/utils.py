from __future__ import annotations
import os
from pathlib import Path
from typing import List
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


def scan_pak_files(directory: str | os.PathLike, ignore_mod_paks: bool = True) -> List[str]:
    dir_path = Path(directory)
    results: List[str] = []

    # Top-level .pak
    for pak in sorted(dir_path.glob("*.pak")):
        size = pak.stat().st_size
        if size <= 16:
            if ignore_mod_paks:
                break
            else:
                continue
        results.append(str(pak).replace("\\", "/"))

    dlc = dir_path / "dlc"
    if dlc.is_dir():
        for pak in sorted(dlc.glob("*.pak")):
            results.append(str(pak).replace("\\", "/"))

    for sub in sorted(dir_path.iterdir()):
        if sub.is_dir() and sub.name.isdigit():
            p = sub / "re_dlc_000.pak"
            if p.exists():
                results.append(str(p).replace("\\", "/"))

    return results

