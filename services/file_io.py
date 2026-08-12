"""Small atomic filesystem primitives shared by REasy workflows."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    modified_ns: int
    device: int
    inode: int


def file_fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat(follow_symlinks=False)
    return FileFingerprint(
        size=int(stat.st_size),
        modified_ns=int(stat.st_mtime_ns),
        device=int(stat.st_dev),
        inode=int(stat.st_ino),
    )


def reserve_new_file(path: Path) -> None:
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.close(descriptor)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def copy_file_atomically(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> None:
    """Copy through a sibling staging file, optionally replacing destination."""

    temporary: Path | None = None
    reserved = False
    try:
        descriptor, raw_temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.reasy-",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(raw_temporary)
        shutil.copy2(source, temporary)
        with temporary.open("r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        if not overwrite:
            reserve_new_file(destination)
            reserved = True
        os.replace(temporary, destination)
        temporary = None
        reserved = False
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        if reserved:
            destination.unlink(missing_ok=True)
