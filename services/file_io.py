"""Small atomic filesystem primitives shared by REasy workflows."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from contextlib import ExitStack, contextmanager


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


@contextmanager
def _staging_file(destination):
    descriptor, name = tempfile.mkstemp(prefix=f'.{destination.name}.reasy-', dir=destination.parent)
    os.close(descriptor)
    path = Path(name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


def check_output(destination, inputs):
    destination = Path(destination).resolve()
    for value in inputs:
        if value is None:
            continue
        path = Path(value).resolve()
        if path == destination or destination.exists() and path.exists() and path.samefile(destination):
            raise ValueError('Output must not replace the source or another input')


def publish_files(outputs, *, inputs=()):
    """Stage every verified payload before replacing any candidate file.

    Each replacement is atomic. If a replacement fails, already published files
    are restored; this is not a filesystem-wide transaction across a crash.
    Existing user backups and logs are never used as staging files.
    """
    rows = [(Path(path).resolve(), data) for path, data in outputs.items()]
    paths = [path for path, _ in rows]
    if len(paths) != len(set(paths)):
        raise ValueError('Output paths must be distinct')
    for i, path in enumerate(paths):
        check_output(path, inputs)
        if path.exists() and not path.is_file():
            raise ValueError(f'Output is not a file: {path}')
        if path.exists() and any(other.exists() and path.samefile(other) for other in paths[:i]):
            raise ValueError('Output paths refer to the same file')
    with ExitStack() as stack:
        staged, published = [], []
        for path, data in rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            candidate = stack.enter_context(_staging_file(path))
            with candidate.open('wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if candidate.read_bytes() != data:
                raise OSError('Staged output differs from the verified bytes')
            backup = None
            if path.exists():
                backup = stack.enter_context(_staging_file(path))
                shutil.copy2(path, backup)
            staged.append((path, candidate, backup))
        try:
            for path, candidate, backup in staged:
                os.replace(candidate, path)
                published.append((path, backup))
        except BaseException as failure:
            errors = []
            for path, backup in reversed(published):
                try:
                    if backup is None:
                        path.unlink()
                    else:
                        os.replace(backup, path)
                except OSError as error:
                    error.add_note(f'Original candidate retained at {backup}; destination: {path}')
                    errors.append(error)
            if errors:
                stack.pop_all()  # Retain recovery copies when restoration itself fails.
                raise BaseExceptionGroup('Publication and restoration failed', [failure, *errors])
            raise


def publish_bytes(path, data, *, inputs=()):
    publish_files({path: data}, inputs=inputs)
