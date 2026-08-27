from __future__ import annotations
import io
import os
import re
import threading
import zstandard as zstd
import zlib
import traceback
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed

from .utils import filepath_hash, guess_extension_from_header, normalize_pak_path
from utils.native_build import ensure_fast_pakresolve
from .pakfile import (
    PakFile,
    PakEntry,
    _read_entry_raw,
    _decrypt_resource,
    _is_chunked_entry,
)
from .resolution import (
    PakReaderInfo,
    PakResolutionProfile,
    classify_reader,
    entry_is_gated,
    order_readers,
    select_profile,
)

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def _decompress_with_alternate_codec(e: PakEntry, data: bytes, zstd_decompressor=None) -> bytes:
    if e.compression == 1:
        dctx = zstd_decompressor or zstd.ZstdDecompressor()
        return dctx.decompress(data)
    if e.compression == 2:
        try:
            return zlib.decompress(data)
        except zlib.error:
            try:
                return zlib.decompress(data, -zlib.MAX_WBITS)
            except zlib.error:
                dctx = zstd_decompressor or zstd.ZstdDecompressor()
                return dctx.decompress(data)
    return data


class PakReader:
    def __init__(self, game: str | None = None) -> None:
        self.game = game
        self.pak_file_priority: List[str] = []
        self.max_threads: int = 32
        self.filter: Optional[re.Pattern[str]] = None
        self.enable_console_logging: bool = False
        self._searched_paths: Dict[int, str] = {}
        self._registered_hashes: Dict[str, int] = {}

    def reset_file_list(self) -> None:
        self._searched_paths.clear()
        self._registered_hashes.clear()

    def add_files(self, *files: str) -> None:
        for p in files:
            if self.filter and not self.filter.search(p):
                continue
            h = filepath_hash(p)
            self._searched_paths[h] = p
            self._registered_hashes[p] = h




class CachedPakReader(PakReader):
    def __init__(self, game: str | None = None) -> None:
        super().__init__(game=game)
        self._cache: Optional[Dict[int, tuple[PakFile, PakEntry]]] = None
        self._cache_complete: bool = True
        self.resolution_profile: PakResolutionProfile | None = None
        self.resolution_profile_source: str = ""
        self.resolved_game: str | None = None
        self.ordered_pak_paths: tuple[str, ...] = ()
        self.resolution_stats: dict[str, int] = {}

    @classmethod
    def from_paks(
        cls,
        pak_paths: Iterable[str],
        *,
        game: str | None = None,
    ) -> "CachedPakReader":
        """Create a reader with an explicit PAK set and game identity."""
        reader = cls(game=game)
        reader.pak_file_priority = list(pak_paths)
        return reader

    def matches_source(
        self,
        pak_paths: Sequence[str],
        *,
        game: str | None,
    ) -> bool:
        return self.game == game and self.pak_file_priority == list(pak_paths)

    @property
    def cache_ready(self) -> bool:
        return self._cache is not None

    @property
    def cache_complete(self) -> bool:
        return self._cache is not None and self._cache_complete

    @property
    def registered_paths(self) -> tuple[str, ...]:
        return tuple(self._searched_paths.values())

    def fork(self) -> "CachedPakReader":
        """Create a cache-free reader with the same source settings."""
        reader = type(self).from_paks(self.pak_file_priority, game=self.game)
        reader.max_threads = self.max_threads
        reader.filter = self.filter
        reader.enable_console_logging = self.enable_console_logging
        return reader

    def snapshot(self) -> "CachedPakReader":
        """Copy mutable lookup state for a worker without sharing dictionaries."""
        reader = self.fork()
        reader._cache = dict(self._cache) if self._cache is not None else None
        reader._cache_complete = self._cache_complete
        reader._searched_paths = dict(self._searched_paths)
        reader._registered_hashes = dict(self._registered_hashes)
        reader.resolution_profile = self.resolution_profile
        reader.resolution_profile_source = self.resolution_profile_source
        reader.resolved_game = self.resolved_game
        reader.ordered_pak_paths = self.ordered_pak_paths
        reader.resolution_stats = dict(self.resolution_stats)
        return reader

    def prepare(
        self,
        paths: Iterable[str] = (),
        *,
        full: bool = False,
    ) -> "CachedPakReader":
        """Prepare either a targeted or complete winner index.

        Targeted indexes keep bulk list validation cheap. A later full request
        upgrades the same reader while retaining every registered path name.
        """
        requested = (
            normalize_pak_path(path, lowercase=True)
            for path in paths
            if path and path.strip()
        )
        known = tuple(dict.fromkeys((*self.registered_paths, *requested)))

        if full:
            if known:
                self.add_files(*known)
            if not self.cache_complete:
                self.cache_entries(assign_paths=False)
            elif known:
                self.assign_paths(known)
            return self

        if self._cache is None:
            if known:
                self.cache_entries_for_paths(known)
            else:
                self.cache_entries(assign_paths=False)
            return self

        if self._cache_complete:
            if known:
                self.assign_paths(known)
            return self

        missing = [path for path in known if filepath_hash(path) not in self._searched_paths]
        if missing:
            all_paths = (*self.registered_paths, *missing)
            self._cache = None
            self.reset_file_list()
            self.cache_entries_for_paths(all_paths)
        return self

    def contains_cached(self, path_or_hash: str | int) -> bool:
        """Return whether the current index contains a winner, without expanding it."""
        if self._cache is None:
            return False
        if not isinstance(path_or_hash, str):
            return path_or_hash in self._cache
        registered = self._registered_hashes.get(path_or_hash)
        if registered is not None:
            return registered in self._cache
        return filepath_hash(path_or_hash) in self._cache

    def cached_path_for_hash(self, value: int) -> str | None:
        if self._cache is None:
            return None
        hit = self._cache.get(value)
        return hit[1].path if hit else None

    def cached_known_paths(self) -> List[str]:
        """Return named winners, including registered names for a targeted index."""
        paths = self.cached_paths(include_unknown=False)
        seen = set(paths)
        for path in self.registered_paths:
            if path not in seen and self.contains_cached(path):
                paths.append(path)
                seen.add(path)
        return paths

    def cached_unknown_paths(self) -> List[str]:
        """Return unknown names only when the current index is already complete."""
        if not self.cache_complete or self._cache is None:
            return []
        return [
            f"__Unknown/{value:016X}"
            for value, (_pak, entry) in self._cache.items()
            if entry.path is None
        ]

    def cache_entries(self, assign_paths: bool = False) -> None:
        if self._cache is not None and self._cache_complete:
            return
        self._cache = self._build_cache(assign_paths=assign_paths)
        self._cache_complete = True

    def cache_entries_for_paths(self, paths: Iterable[str]) -> None:
        """Build a lightweight cache containing only known list paths."""
        if self._cache is not None:
            return

        self.reset_file_list()
        self.add_files(*paths)
        self._cache = self._build_cache(assign_paths=True)
        self._cache_complete = False

    def assign_paths(self, paths: Iterable[str], replace_existing: bool = False) -> int:
        """Fast path: assign known names into the existing cache without rebuilding.

        Returns number of entries newly named.
        """
        norm_paths = list({normalize_pak_path(p, lowercase=True) for p in paths})
        if replace_existing:
            self.reset_file_list()
        if norm_paths:
            self.add_files(*norm_paths)

        if self._cache is None:
            self.cache_entries(assign_paths=False)
            if self._cache is None:
                return 0

        if replace_existing:
            for _pak, entry in self._cache.values():
                entry.path = None
            if not norm_paths:
                return 0

        fast = ensure_fast_pakresolve()
        if fast is None:
            raise RuntimeError("fast_pakresolve native module is required but not available")
        
        _rem, updated = fast.resolve_paths_utf16le(self._cache, norm_paths)
        return int(updated)


    @staticmethod
    def _path_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path))

    def _load_resolved_paks(self, assign_paths: bool) -> list[tuple[PakReaderInfo, PakFile]]:
        parsed: dict[str, PakFile] = {}
        infos: list[PakReaderInfo] = []
        expected_paths = self._searched_paths if assign_paths else None

        for pakfile in self.pak_file_priority:
            path_key = self._path_key(pakfile)
            if path_key in parsed:
                continue
            try:
                if os.path.getsize(pakfile) < 16:
                    continue
            except OSError:
                continue
            pak = PakFile()
            pak.filepath = pakfile
            with open(pakfile, "rb") as f:
                pak.read_contents(f, expected_paths)
            if pak.header is None:
                continue
            info = classify_reader(
                pakfile,
                pak.header.feature_flags,
                (pak.header.major, pak.header.minor),
            )
            parsed[path_key] = pak
            infos.append(info)

        profile, resolved_game, source = select_profile(self.game)
        ordered_infos = order_readers(infos, profile)
        ordered: list[tuple[PakReaderInfo, PakFile]] = []
        for info in ordered_infos:
            pak = parsed[self._path_key(info.path)]
            pak.resolution_info = info
            ordered.append((info, pak))

        self.resolution_profile = profile
        self.resolution_profile_source = source
        self.resolved_game = resolved_game
        self.ordered_pak_paths = tuple(info.path for info in ordered_infos)
        return ordered

    def _build_cache(self, assign_paths: bool) -> Dict[int, tuple[PakFile, PakEntry]]:
        ordered = self._load_resolved_paks(assign_paths)
        profile = self.resolution_profile
        if profile is None:
            return {}

        gated_hashes: set[int] = set()
        for _info, pak in ordered:
            for entry in pak.availability_entries:
                if entry_is_gated(entry.attributes, profile.gate, pak.has_attr_bit20):
                    gated_hashes.add(entry.combined_hash)

        authorized: set[int] = set()
        if gated_hashes:
            for witness_type in profile.witness_types:
                for info, pak in ordered:
                    if info.reader_type != witness_type:
                        continue
                    for entry in pak.entries:
                        entry_hash = entry.combined_hash
                        if (
                            entry_hash in gated_hashes
                            and entry_hash not in authorized
                            and not entry_is_gated(
                                entry.attributes, profile.gate, pak.has_attr_bit20
                            )
                        ):
                            authorized.add(entry_hash)
                    if len(authorized) == len(gated_hashes):
                        break
                if len(authorized) == len(gated_hashes):
                    break

        cache: Dict[int, tuple[PakFile, PakEntry]] = {}
        rejected = 0
        for _info, pak in ordered:
            for entry in pak.entries:
                entry_hash = entry.combined_hash
                if entry_hash in cache:
                    continue
                if (
                    (entry.attributes & 0x70) != 0
                    and entry_is_gated(entry.attributes, profile.gate, pak.has_attr_bit20)
                    and entry_hash not in authorized
                ):
                    rejected += 1
                    continue
                if entry.path is None and self._searched_paths:
                    entry.path = self._searched_paths.get(entry_hash)
                cache[entry_hash] = (pak, entry)

        self.resolution_stats = {
            "readers": len(ordered),
            "gated_hashes": len(gated_hashes),
            "authorized_hashes": len(authorized),
            "rejected_entries": rejected,
            "winners": len(cache),
        }
        return cache

    def get_file(self, path_or_hash: str | int) -> Optional[io.BytesIO]:
        if isinstance(path_or_hash, str):
            h = filepath_hash(path_or_hash)
        else:
            h = path_or_hash
        if self._cache is None:
            self.cache_entries()
            if self._cache is None:
                raise RuntimeError("Failed to build cache")
        hit = self._cache.get(h)
        if not hit and not self._cache_complete:
            self.cache_entries(assign_paths=False)
            hit = self._cache.get(h) if self._cache else None
        if not hit:
            return None
        pak, e = hit
        with open(pak.filepath, "rb") as fs:
            buf = io.BytesIO()

            _read_entry_raw(e, fs, buf, chunk_table=pak.chunk_table)
            buf.seek(0)
            return buf

    def cached_paths(self, include_unknown: bool = True) -> List[str]:
        if self._cache is None:
            self.cache_entries()
            if self._cache is None:
                return []
        elif include_unknown and not self._cache_complete:
            self.cache_entries()

        named = []
        unknown = []
        for h, (_pak, e) in self._cache.items():
            if e.path:
                named.append(e.path)
            elif include_unknown:
                unknown.append(f"__Unknown/{h:016X}")
        if include_unknown:
            return named + unknown
        return named
    

    @staticmethod
    def read_manifest(pak_files: List[str]) -> List[str]:
        manifest_path = "__MANIFEST/MANIFEST.TXT"
        manifest_hash = filepath_hash(manifest_path)

        for pak_path in pak_files:
            try:
                if os.path.getsize(pak_path) < 16:
                    continue

                pak = PakFile()
                pak.filepath = pak_path
                with open(pak_path, "rb") as f:
                    pak.read_contents(f, {manifest_hash: manifest_path})
                    if not pak.entries:
                        continue
                    stream = io.BytesIO()
                    _read_entry_raw(pak.entries[0], f, stream, chunk_table=pak.chunk_table)
                content = stream.getvalue().decode("utf-8")
                return [
                    line.replace("\\", "/")
                    for raw_line in content.splitlines()
                    if (line := raw_line.strip()) and not line.startswith("#")
                ]
            except (IOError, OSError, UnicodeDecodeError):
                continue

        return []

    def extract_files_to(self, output_directory: str, paths: Iterable[str], missing_files: Optional[List[str]] = None, progress_dialog=None) -> int:
        if self._cache is None:
            self.cache_entries(assign_paths=False)
            if self._cache is None:
                raise RuntimeError("Failed to build cache")
        
        out_base = Path(output_directory)


        groups: Dict[str, tuple[PakFile, List[tuple[PakEntry, Path]]]] = {}
        missing_local: List[str] = []
        
        for p in paths:
            try:
                h = int(p.split("/",1)[1], 16) if p.startswith("__Unknown/") else filepath_hash(p)
            except Exception:
                missing_local.append(p)
                continue
            hit = self._cache.get(h)
            if not hit and not self._cache_complete:
                self.cache_entries(assign_paths=False)
                hit = self._cache.get(h) if self._cache else None
            if not hit:
                missing_local.append(p)
                continue
            pak, e = hit
            out_name = e.path or p
            outp = out_base / out_name
            bucket = groups.get(pak.filepath)
            if bucket is None:
                bucket = (pak, [])
                groups[pak.filepath] = bucket
            bucket[1].append((e, outp))

        thread_local = threading.local()
        
        def get_thread_resources():
            if not hasattr(thread_local, 'initialized'):
                thread_local.initialized = True
                thread_local.created_dirs = set()
                thread_local.buffer = bytearray(8 * 1024 * 1024)
                try:
                    thread_local.zstd_decompressor = zstd.ZstdDecompressor()
                except ImportError:
                    thread_local.zstd_decompressor = None
            return thread_local
        
        def extract_from_pak(pak: PakFile, entries: List[tuple[PakEntry, Path]]) -> int:
            pak_path = pak.filepath
            count = 0
            resources = get_thread_resources()
            
            with open(pak_path, "rb") as pak_file:
                for e, outp in entries:
                    try:
                        pak_file.seek(int(e.offset))
                        
                        is_unknown = (e.path is None)
                        target_outp = outp
                        
                        if _is_chunked_entry(e, pak.chunk_table):
                            parent = target_outp.parent
                            if parent not in resources.created_dirs:
                                parent.mkdir(parents=True, exist_ok=True)
                                resources.created_dirs.add(parent)
                            if is_unknown:
                                stream = io.BytesIO()
                                _read_entry_raw(e, pak_file, stream, chunk_table=pak.chunk_table)
                                data = stream.getvalue()
                                if data:
                                    ext = guess_extension_from_header(data[:64])
                                    if ext and not target_outp.suffix:
                                        target_outp = target_outp.with_suffix("." + ext)
                                with open(target_outp, "wb") as out_file:
                                    out_file.write(data)
                            else:
                                with open(target_outp, "wb") as out_file:
                                    _read_entry_raw(e, pak_file, out_file, chunk_table=pak.chunk_table)
                        elif e.compression == 0 and e.encryption == 0:
                            size = e.stored_size
                            header = b""
                            if is_unknown:
                                peek = min(64, size)
                                header = pak_file.read(peek)
                                if header:
                                    ext = guess_extension_from_header(header)
                                    if ext and not target_outp.suffix:
                                        target_outp = target_outp.with_suffix("." + ext)
                            parent = target_outp.parent
                            if parent not in resources.created_dirs:
                                parent.mkdir(parents=True, exist_ok=True)
                                resources.created_dirs.add(parent)
                            with open(target_outp, "wb") as out_file:
                                if header:
                                    out_file.write(header)
                                remaining = size - len(header)
                                buffer = resources.buffer
                                buffer_size = len(buffer)
                                while remaining > 0:
                                    chunk_size = min(remaining, buffer_size)
                                    bytes_read = pak_file.readinto(memoryview(buffer)[:chunk_size])
                                    if bytes_read == 0:
                                        break
                                    out_file.write(memoryview(buffer)[:bytes_read])
                                    remaining -= bytes_read
                        else:
                            comp_size = int(e.compressed_size) if e.compressed_size else int(e.decompressed_size)
                            data = pak_file.read(comp_size)

                            if e.encryption != 0:
                                sr = [len(data)]
                                data = _decrypt_resource(data, sr)

                            if e.compression == 1:
                                try:
                                    data = zlib.decompress(data)
                                except zlib.error:
                                    try:
                                        data = zlib.decompress(data, -zlib.MAX_WBITS)
                                    except zlib.error:
                                        data = _decompress_with_alternate_codec(e, data, resources.zstd_decompressor)
                            elif e.compression == 2:
                                if data.startswith(_ZSTD_MAGIC):
                                    data = (resources.zstd_decompressor or zstd.ZstdDecompressor()).decompress(data)
                                else:
                                    data = _decompress_with_alternate_codec(e, data, resources.zstd_decompressor)

                            if is_unknown and data:
                                ext = guess_extension_from_header(data[:64])
                                if ext and not target_outp.suffix:
                                    target_outp = target_outp.with_suffix("." + ext)

                            parent = target_outp.parent
                            if parent not in resources.created_dirs:
                                parent.mkdir(parents=True, exist_ok=True)
                                resources.created_dirs.add(parent)

                            with open(target_outp, "wb") as out_file:
                                out_file.write(data)
                        
                        count += 1
                    except Exception as ex:
                        print(f"Failed to extract {outp}: {ex}")
            return count

        extracted = 0
        
        total_files = sum(len(entries) for _pak, entries in groups.values())
        num_cores = os.cpu_count() or 4
        
        if total_files < 10:
            for _pak_path, (pak, entries) in groups.items():
                count = extract_from_pak(pak, entries)
                extracted += count
        else:
            work_items = []
            
            for pak_path, (pak, entries) in groups.items():
                entries.sort(key=lambda t: int(t[0].offset))
                
                if len(entries) <= 50:
                    work_items.append((pak, entries))
                else:
                    if len(groups) > 1:
                        batch_size = max(20, len(entries) // (num_cores * 2))
                    else:
                        batch_size = max(50, len(entries) // num_cores)
                    
                    for i in range(0, len(entries), batch_size):
                        batch = entries[i:i + batch_size]
                        work_items.append((pak, batch))
            
            max_workers = min(num_cores, len(work_items), 16)
            if max_workers > 8 and total_files < 1000:
                max_workers = 8
            
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_info = {}
                for pak, batch in work_items:
                    future = executor.submit(extract_from_pak, pak, batch)
                    future_to_info[future] = (pak.filepath, len(batch))
                
                for future in as_completed(future_to_info):
                    pak_path, batch_size = future_to_info[future]
                    try:
                        count = future.result()
                        extracted += count
                        
                        if progress_dialog:
                            try:
                                progress_dialog.signals.progress_update.emit(batch_size)
                                
                                if progress_dialog.cancelled:
                                    for f in future_to_info:
                                        f.cancel()
                                    break
                            except Exception as e:
                                print(f"Error updating progress: {e}")
                                traceback.print_exc()
                    except Exception as e:
                        print(f"Error extracting from {pak_path}: {e}")

        if missing_files is not None and missing_local:
            missing_files.extend(missing_local)
        
        return extracted

