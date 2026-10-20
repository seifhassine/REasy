"""Small atomic filesystem primitives shared by REasy workflows."""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import suppress
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


def _write_synced(stream, data: bytes) -> None:
    stream.write(data)
    stream.flush()
    os.fsync(stream.fileno())


def write_files_atomically(outputs: dict[str, bytes]) -> None:
    keys = set()
    for filename in outputs:
        key = os.path.normcase(os.path.abspath(filename))
        if key in keys:
            raise ValueError(f"Two outputs name the same file: {filename}")
        keys.add(key)
    staged = {}
    snapshots = {}
    replaced = []
    try:
        for filename, data in outputs.items():
            path = Path(filename)
            path.parent.mkdir(parents=True, exist_ok=True)
            snapshots[path] = path.read_bytes() if path.exists() else None
            descriptor, temporary = tempfile.mkstemp(prefix=".reasy-save-", dir=path.parent)
            staged[path] = Path(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                _write_synced(stream, data)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except OSError as exc:
        failures = []
        for path in reversed(replaced):
            try:
                if snapshots[path] is None:
                    path.unlink()
                else:
                    temporary = staged[path]
                    with temporary.open("wb") as stream:
                        _write_synced(stream, snapshots[path])
                    os.replace(temporary, path)
            except OSError as rollback_error:
                failures.append(f"{path}: {rollback_error}")
        if failures:
            raise OSError(f"Save failed ({exc}); rollback failed: {'; '.join(failures)}") from exc
        raise
    finally:
        for temporary in staged.values():
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def copy_file_atomically(
    source: Path,
    destination: Path,
    *,
    overwrite: bool = False,
) -> None:
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
