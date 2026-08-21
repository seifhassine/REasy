"""Installation-aware BNK/PCK relationships built without parsing audio data."""

from __future__ import annotations

import copy
import gzip
import hashlib
import io
import json
import os
import re
import struct
import tempfile
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from file_handlers.pak.pakfile import _is_chunked_entry
from file_handlers.pak.utils import filepath_hash
from utils.app_paths import cache_directory

from .bnk_parser import (
    _read_bank_source,
    _read_bank_version,
    _read_didx,
    _read_hirc_objects,
    _read_music_track_sources,
)
from .pck_codec import parse_pck_layout
from .sound_resources import resource_key


_CACHE_SCHEMA = 1
_SOUND_PATH = re.compile(r"\.(?:s?bnk|s?pck)\.\d+\.(?:x64|stm)(?:\.|$)", re.I)
_BANK_PATH = re.compile(r"\.s?bnk\.\d+\.(?:x64|stm)(?:\.|$)", re.I)
_PACKAGE_PATH = re.compile(r"\.s?pck\.\d+\.(?:x64|stm)(?:\.|$)", re.I)
_MEDIA_SUFFIX = re.compile(
    r"\.(?:s?bnk\.\d+|s?pck\.\d+)\.(?:x64|stm)(?=\.|$)", re.I
)
_ROLE_SUFFIX = re.compile(r"_(?:es|ev|m(?:_[a-z0-9]+)?)(?=(?:\.[^/.]+)?$)")
_STRUCTURAL_CHUNKS = frozenset({b"BKHD", b"DIDX", b"HIRC"})
_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sound-index")
_HANDLES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_HANDLES_LOCK = threading.Lock()


def _media_key(path: str) -> str:
    name = resource_key(path).rsplit("/", 1)[-1]
    return _MEDIA_SUFFIX.sub("", name)


def _split_bank_family(path: str) -> str:
    return _ROLE_SUFFIX.sub("", _media_key(path))


def _matching_package_keys(
    bank_path: str, candidates, *, split_roles: bool
) -> set[str]:
    candidates = set(candidates)
    exact = {_media_key(bank_path)} & candidates
    if exact or not split_roles:
        return exact or (candidates if len(candidates) == 1 else set())
    family = _split_bank_family(bank_path)
    matches = {key for key in candidates if _split_bank_family(key) == family}
    return matches or (candidates if len(candidates) == 1 else set())


def _selected_bnk_chunks(data: bytes) -> dict[str, bytes]:
    chunks: dict[str, bytes] = {}
    pos = 0
    while pos + 8 <= len(data):
        chunk_id = bytes(data[pos:pos + 4])
        length = struct.unpack_from("<I", data, pos + 4)[0]
        payload, end = pos + 8, pos + 8 + length
        if end > len(data):
            break
        if chunk_id in _STRUCTURAL_CHUNKS:
            chunks[chunk_id.decode("ascii")] = bytes(data[payload:end])
        pos = end
    return chunks


def _raw_bnk_chunks(pak_path: str, entry) -> dict[str, bytes]:
    """Seek over DATA instead of reading a raw, uncompressed WEM payload."""

    chunks: dict[str, bytes] = {}
    remaining = int(entry.decompressed_size)
    with open(pak_path, "rb") as stream:
        stream.seek(int(entry.offset))
        while remaining >= 8:
            header = stream.read(8)
            if len(header) != 8:
                break
            chunk_id = header[:4]
            length = struct.unpack_from("<I", header, 4)[0]
            remaining -= 8
            if length > remaining:
                break
            if chunk_id in _STRUCTURAL_CHUNKS:
                payload = stream.read(length)
                if len(payload) != length:
                    break
                chunks[chunk_id.decode("ascii")] = payload
            else:
                stream.seek(length, io.SEEK_CUR)
            remaining -= length
    return chunks


def _bank_chunks(reader, path: str) -> dict[str, bytes]:
    hit = (getattr(reader, "_cache", None) or {}).get(filepath_hash(path))
    if hit is None:
        return {}
    pak, entry = hit
    if (
        entry.compression == 0
        and entry.encryption == 0
        and not _is_chunked_entry(entry, pak.chunk_table)
    ):
        return _raw_bnk_chunks(pak.filepath, entry)
    stream = reader.get_file(path)
    return _selected_bnk_chunks(stream.getbuffer()) if stream else {}


def _bank_sources(
    chunks: dict[str, bytes], bank_versions: frozenset[int]
) -> tuple[dict[int, int], set[int]]:
    version = _read_bank_version(chunks.get("BKHD"))
    if version not in bank_versions:
        raise ValueError(f"unexpected Wwise bank version {version}")
    hirc = _read_hirc_objects(chunks.get("HIRC"), [], version)
    if chunks.get("HIRC") and not hirc.complete:
        raise ValueError("truncated HIRC chunk")
    sources: dict[int, int] = {}
    for obj in hirc.objects:
        records = ()
        if obj.type_id == 0x02:
            source, _end = _read_bank_source(obj.payload, 4, obj.object_id, version)
            records = (source,) if source else ()
        elif obj.type_id == 0x0B:
            records = _read_music_track_sources(obj.payload, obj.object_id, version)
        for source in records:
            if source.source_id:
                previous = sources.setdefault(source.source_id, source.stream_type)
                if previous != source.stream_type:
                    raise ValueError(
                        f"Source ID {source.source_id} has conflicting stream types"
                    )
    return sources, {entry.source_id for entry in _read_didx(chunks.get("DIDX"))}


def _known_sound_paths(reader, profile) -> tuple[str, ...]:
    searched = getattr(reader, "_searched_paths", {})
    values = searched.values() if searched else reader.cached_paths(include_unknown=False)
    return tuple(sorted({
        resource_key(path)
        for path in values
        if _SOUND_PATH.search(str(path)) and profile.matches_path(str(path))
    }))


def _index_signature(reader, profile, paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    digest.update(
        f"{_CACHE_SCHEMA}\0{profile.game}\0{int(profile.split_sbnk_roles)}\0"
        f"{','.join(map(str, sorted(profile.bank_versions)))}\0".encode()
    )
    for pak_path in getattr(reader, "pak_file_priority", ()):
        try:
            stat = os.stat(pak_path)
            record = (
                os.path.normcase(os.path.abspath(pak_path)),
                stat.st_size,
                stat.st_mtime_ns,
            )
        except OSError:
            record = (os.path.normcase(os.path.abspath(pak_path)), -1, -1)
        digest.update(repr(record).encode("utf-8", "surrogatepass"))
    cache = getattr(reader, "_cache", None) or {}
    for path in paths:
        digest.update(path.encode("utf-8", "surrogatepass") + b"\0")
        hit = cache.get(filepath_hash(path))
        if hit:
            pak, entry = hit
            digest.update(repr((
                os.path.normcase(str(pak.filepath)), entry.offset,
                entry.compressed_size, entry.decompressed_size,
                entry.compression, entry.encryption, entry.checksum,
            )).encode())
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class RuntimeSoundIndex:
    """Normalized operational relationships for one installed game build."""

    game: str
    split_roles: bool
    banks_by_source: dict[int, tuple[tuple[str, int], ...]]
    embedded_by_source: dict[int, tuple[str, ...]]
    packages_by_source: dict[int, tuple[str, ...]]
    package_records: dict[str, dict[str, str]]

    def media_packages(self, source_id: int, bank_path: str) -> tuple[dict, ...]:
        keys = _matching_package_keys(
            resource_key(bank_path),
            self.packages_by_source.get(int(source_id) & 0xFFFFFFFF, ()),
            split_roles=self.split_roles,
        )
        return tuple(self.package_records[key] for key in sorted(keys))

    def banks_for_package(self, package_path: str, source_id: int) -> tuple[str, ...]:
        source_id = int(source_id) & 0xFFFFFFFF
        package = _media_key(package_path)
        packages = self.packages_by_source.get(source_id, ())
        if package not in packages:
            return ()
        return tuple(dict.fromkeys(
            bank
            for bank, stream_type in self.banks_by_source.get(source_id, ())
            if stream_type in {1, 2}
            and package in _matching_package_keys(
                bank, packages, split_roles=self.split_roles
            )
        ))

    def embedded_media_banks(self, source_id: int, bank_path: str) -> tuple[str, ...]:
        source_id = int(source_id) & 0xFFFFFFFF
        embedded = self.embedded_by_source.get(source_id, ())
        declarations = self.banks_by_source.get(source_id, ())
        current = resource_key(bank_path)
        if not embedded or not any(
            stream_type == 0 and bank not in embedded
            for bank, stream_type in declarations
        ):
            return ()
        current_types = {
            stream_type for bank, stream_type in declarations if bank == current
        }
        return (
            embedded
            if not current_types or (0 in current_types and current not in embedded)
            else ()
        )

    def prefetch_media_banks(self, source_id: int, bank_path: str) -> tuple[str, ...]:
        if not self.split_roles:
            return ()
        source_id = int(source_id) & 0xFFFFFFFF
        current = resource_key(bank_path)
        if not any(
            bank == current and stream_type == 1
            for bank, stream_type in self.banks_by_source.get(source_id, ())
        ):
            return ()
        family = _split_bank_family(current)
        return tuple(
            path for path in self.embedded_by_source.get(source_id, ())
            if path != current and _split_bank_family(path) == family
        )

    def prefetch_event_banks(self, source_id: int, media_path: str) -> tuple[str, ...]:
        if not self.split_roles:
            return ()
        current = resource_key(media_path)
        family = _split_bank_family(current)
        return tuple(dict.fromkeys(
            bank
            for bank, stream_type in self.banks_by_source.get(
                int(source_id) & 0xFFFFFFFF, ()
            )
            if stream_type == 1 and bank != current
            and _split_bank_family(bank) == family
        ))

    def source_event_banks(self, source_id: int, media_path: str) -> tuple[str, ...]:
        source_id = int(source_id) & 0xFFFFFFFF
        current = resource_key(media_path)
        embedded = set(self.embedded_by_source.get(source_id, ()))
        package = _media_key(current) if _PACKAGE_PATH.search(current) else ""
        packages = self.packages_by_source.get(source_id, ())
        family = _split_bank_family(current)
        return tuple(dict.fromkeys(
            bank
            for bank, stream_type in self.banks_by_source.get(source_id, ())
            if bank != current and bank not in embedded
            and (
                not package
                or stream_type in {1, 2}
                and package in _matching_package_keys(
                    bank, packages, split_roles=self.split_roles
                )
            )
            and (
                package or not self.split_roles
                or _split_bank_family(bank) == family
            )
        ))

    def to_json(self, signature: str) -> dict:
        paths = sorted({
            path
            for values in self.banks_by_source.values()
            for path, _stream_type in values
        } | {
            path for values in self.embedded_by_source.values() for path in values
        } | {
            path for record in self.package_records.values() for path in record.values()
        })
        path_indexes = {path: index for index, path in enumerate(paths)}
        package_keys = sorted(self.package_records)
        package_indexes = {key: index for index, key in enumerate(package_keys)}
        source_ids = sorted(
            set(self.banks_by_source)
            | set(self.embedded_by_source)
            | set(self.packages_by_source)
        )
        return {
            "schema": _CACHE_SCHEMA,
            "signature": signature,
            "game": self.game,
            "split_roles": self.split_roles,
            "paths": paths,
            "packages": [
                [
                    key,
                    path_indexes.get(self.package_records[key].get("index", ""), -1),
                    path_indexes.get(self.package_records[key].get("streaming", ""), -1),
                ]
                for key in package_keys
            ],
            "sources": [
                [
                    source_id,
                    [
                        [path_indexes[path], stream_type]
                        for path, stream_type in self.banks_by_source.get(source_id, ())
                    ],
                    [
                        path_indexes[path]
                        for path in self.embedded_by_source.get(source_id, ())
                    ],
                    [
                        package_indexes[key]
                        for key in self.packages_by_source.get(source_id, ())
                    ],
                ]
                for source_id in source_ids
            ],
        }

    @classmethod
    def from_json(cls, data: dict, signature: str) -> "RuntimeSoundIndex":
        if (
            data.get("schema") != _CACHE_SCHEMA
            or data.get("signature") != signature
            or not isinstance(data.get("paths"), list)
            or not isinstance(data.get("packages"), list)
            or not isinstance(data.get("sources"), list)
        ):
            raise ValueError("stale sound index cache")
        paths = tuple(map(str, data["paths"]))
        package_keys: list[str] = []
        records: dict[str, dict[str, str]] = {}
        for key, index_path, streaming_path in data["packages"]:
            key = str(key)
            package_keys.append(key)
            record = {}
            for field, index in (("index", index_path), ("streaming", streaming_path)):
                if isinstance(index, int) and 0 <= index < len(paths):
                    record[field] = paths[index]
            records[key] = record
        banks, embedded, packages = {}, {}, {}
        for raw_id, bank_values, embedded_values, package_values in data["sources"]:
            source_id = int(raw_id) & 0xFFFFFFFF
            bank_rows = tuple(
                (paths[path_index], int(stream_type))
                for path_index, stream_type in bank_values
                if isinstance(path_index, int) and 0 <= path_index < len(paths)
            )
            embedded_rows = tuple(
                paths[index] for index in embedded_values
                if isinstance(index, int) and 0 <= index < len(paths)
            )
            package_rows = tuple(
                package_keys[index] for index in package_values
                if isinstance(index, int) and 0 <= index < len(package_keys)
            )
            if bank_rows:
                banks[source_id] = bank_rows
            if embedded_rows:
                embedded[source_id] = embedded_rows
            if package_rows:
                packages[source_id] = package_rows
        return cls(
            str(data.get("game", "")), bool(data.get("split_roles")),
            banks, embedded, packages, records,
        )


def build_runtime_sound_index(reader, profile, paths: tuple[str, ...]) -> RuntimeSoundIndex:
    """Build only source-location facts; never decode HIRC layouts or BNK DATA."""

    banks_by_source: dict[int, list[tuple[str, int]]] = {}
    embedded_by_source: dict[int, list[str]] = {}
    package_paths: dict[str, dict[str, str]] = {}
    packages_by_source: dict[int, set[str]] = {}
    cache = getattr(reader, "_cache", None) or {}
    failures = []

    for path in (path for path in paths if _BANK_PATH.search(path)):
        if filepath_hash(path) not in cache:
            continue
        try:
            sources, embedded = _bank_sources(
                _bank_chunks(reader, path), profile.bank_versions
            )
        except (OSError, ValueError, struct.error) as exc:
            failures.append(f"{path}: {exc}")
            continue
        for source_id, stream_type in sources.items():
            banks_by_source.setdefault(source_id, []).append((path, stream_type))
        for source_id in embedded:
            embedded_by_source.setdefault(source_id, []).append(path)

    pck_paths = [path for path in paths if _PACKAGE_PATH.search(path)]
    for path in pck_paths:
        if filepath_hash(path) not in cache:
            continue
        key = _media_key(path)
        field = "streaming" if "/streaming/" in path else "index"
        record = package_paths.setdefault(key, {})
        if field in record and record[field] != path:
            failures.append(f"conflicting {field} PCK paths for {key}")
        record[field] = path
    for path in (path for path in pck_paths if "/streaming/" not in path):
        if filepath_hash(path) not in cache:
            continue
        try:
            stream = reader.get_file(path)
            layout = parse_pck_layout(stream.getbuffer()) if stream else None
            if layout is None:
                raise ValueError("invalid AKPK tables")
        except (OSError, ValueError, struct.error) as exc:
            failures.append(f"{path}: {exc}")
            continue
        key = _media_key(path)
        for table in layout.tables:
            if table.kind == "banks":
                continue
            for entry in table.entries:
                if 0 < entry.entry_id <= 0xFFFFFFFF:
                    packages_by_source.setdefault(entry.entry_id, set()).add(key)

    if failures:
        sample = "; ".join(failures[:3])
        raise ValueError(
            f"Could not safely build {profile.game} sound index "
            f"({len(failures)} malformed file(s)): {sample}"
        )
    return RuntimeSoundIndex(
        profile.game,
        bool(profile.split_sbnk_roles),
        {
            source_id: tuple(values)
            for source_id, values in banks_by_source.items()
        },
        {
            source_id: tuple(values)
            for source_id, values in embedded_by_source.items()
        },
        {
            source_id: tuple(sorted(values))
            for source_id, values in packages_by_source.items()
        },
        package_paths,
    )


def _cache_path(root: Path, game: str) -> Path:
    name = re.sub(r"[^a-z0-9]+", "_", game.casefold()).strip("_") or "game"
    return root / "sound_indexes" / f"{name}.json.gz"


def _load_cached(path: Path, signature: str) -> RuntimeSoundIndex | None:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return RuntimeSoundIndex.from_json(json.load(stream), signature)
    except (OSError, EOFError, ValueError, TypeError, KeyError, IndexError):
        return None


def _save_cached(path: Path, index: RuntimeSoundIndex, signature: str) -> None:
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "wb", prefix=path.name, suffix=".tmp", dir=path.parent, delete=False
        ) as raw:
            temporary = Path(raw.name)
        with gzip.open(
            temporary, "wt", encoding="utf-8", compresslevel=1
        ) as stream:
            json.dump(index.to_json(signature), stream, separators=(",", ":"))
        os.replace(temporary, path)
    except (OSError, ValueError, TypeError):
        try:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _load_or_build(reader, profile, paths, signature, root) -> RuntimeSoundIndex:
    path = _cache_path(root, profile.game)
    cached = _load_cached(path, signature)
    if (
        cached
        and cached.game == profile.game
        and cached.split_roles == bool(profile.split_sbnk_roles)
    ):
        print(f"Loaded {profile.game} runtime sound index from cache.")
        return cached
    started = time.perf_counter()
    index = build_runtime_sound_index(reader, profile, paths)
    _save_cached(path, index, signature)
    print(
        f"Built {profile.game} runtime sound index in "
        f"{time.perf_counter() - started:.2f}s."
    )
    return index


def _prepare_runtime_sound_index(reader, profile, root) -> RuntimeSoundIndex | None:
    reader = copy.copy(reader)
    reader._cache = dict(reader._cache)
    reader._searched_paths = dict(getattr(reader, "_searched_paths", {}))
    reader.pak_file_priority = tuple(getattr(reader, "pak_file_priority", ()))
    paths = _known_sound_paths(reader, profile)
    if not paths:
        return None
    return _load_or_build(
        reader, profile, paths, _index_signature(reader, profile, paths), root
    )


class RuntimeSoundIndexHandle:
    """Shared future whose non-blocking reads never delay tab creation."""

    def __init__(self, future: Future):
        self._future = future
        self._reported_error = False

    def get(self, *, wait: bool = False) -> RuntimeSoundIndex | None:
        if not wait and not self._future.done():
            return None
        try:
            return self._future.result()
        except Exception as exc:
            if not self._reported_error:
                print(f"Runtime sound index unavailable: {exc}")
                self._reported_error = True
            return None


def request_runtime_sound_index(
    reader,
    profile,
    *,
    cache_root: Path | None = None,
) -> RuntimeSoundIndexHandle | None:
    """Start or reuse one background index for a project PAK reader."""

    if reader is None or getattr(reader, "_cache", None) is None:
        return None
    root = Path(cache_root) if cache_root is not None else cache_directory()
    key = profile.game, os.path.normcase(str(root))
    with _HANDLES_LOCK:
        handles = _HANDLES.setdefault(reader, {})
        if handle := handles.get(key):
            return handle
        handle = RuntimeSoundIndexHandle(_EXECUTOR.submit(
            _prepare_runtime_sound_index, reader, profile, root
        ))
        handles[key] = handle
        return handle


def clear_runtime_sound_index_handles() -> None:
    """Drop completed in-memory handles; primarily useful after project changes."""

    with _HANDLES_LOCK:
        _HANDLES.clear()


__all__ = [
    "RuntimeSoundIndex",
    "RuntimeSoundIndexHandle",
    "build_runtime_sound_index",
    "clear_runtime_sound_index_handles",
    "request_runtime_sound_index",
]
