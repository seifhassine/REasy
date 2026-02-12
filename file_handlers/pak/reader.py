from __future__ import annotations
import io
import os
import re
import struct
import threading
import zstandard as zstd
import zlib
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, wait, as_completed

from .utils import filepath_hash, guess_extension_from_header
from utils.native_build import ensure_fast_pakresolve
from .pakfile import (
    PakFile,
    PakEntry,
    _read_entry_raw,
    _decrypt_pak_entry_data,
    _decrypt_resource,
    _read_chunk_table,
    FEATURE_EXTRA_DATA,
    FEATURE_CHUNKED_RESOURCES,
    _is_chunked_entry,
)

def _normalize_for_hash(path: str) -> str:
    s = path.strip().replace("\\", "/").lower()
    while "//" in s:
        s = s.replace("//", "/")
    return s


@dataclass
class _ChunkBase:
    file: PakFile
    start: int
    end: int
    file_count: int = 0
    finished: bool = False
    found_hashes: Optional[List[int]] = None

    def __post_init__(self) -> None:
        self.found_hashes = []


class PakReader:
    def __init__(self) -> None:
        self.pak_file_priority: List[str] = []
        self.max_threads: int = 32
        self.filter: Optional[re.Pattern[str]] = None
        self.enable_console_logging: bool = False
        self._searched_paths: Dict[int, str] = {}

        self._path_to_hashes: Dict[str, List[int]] = {}

    def reset_file_list(self) -> None:
        self._searched_paths.clear()
        self._path_to_hashes.clear()

    def add_files(self, *files: str) -> None:
        for p in files:
            if self.filter and not self.filter.search(p):
                continue
            h = filepath_hash(p)
            self._searched_paths[h] = p
            self._path_to_hashes.setdefault(p, []).append(h)

    def add_files_from_list_stream(self, fp: io.TextIOBase) -> None:
        for line in fp:
            line = line.strip()
            if not line:
                continue
            if self.filter and not self.filter.search(line):
                continue
            h = filepath_hash(line)
            self._searched_paths[h] = line
            self._path_to_hashes.setdefault(line, []).append(h)

    def find_files(self) -> Iterator[Tuple[str, io.BytesIO]]:
        pak = PakFile()
        for pakpath in self._enumerate_temp_paks_with_searched_files(pak):
            ctx = _ChunkBase(pak, 0, len(pak.entries))
            for entry, data in self._read_entries_in_chunk_to_memory(ctx):
                data.seek(0)
                yield entry.path or f"__Unknown/{entry.combined_hash:016X}", data

            for h in ctx.found_hashes:
                self._searched_paths.pop(h, None)
            if self.enable_console_logging:
                print(f"Finished searching {pakpath}")
            if not self._searched_paths:
                break

    def unpack_files_to(self, output_directory: str, missing_files: Optional[List[str]] = None) -> int:


        def _calc_threads(entry_count: int) -> int:
            if self.max_threads <= 1:
                return 1

            return max(min(self.max_threads, max(1, entry_count // 64)), 1)

        def _partition_chunks(entry_count: int, chunks: int) -> List[tuple[int, int]]:
            parts: List[tuple[int, int]] = []
            for k in range(chunks):
                start = int(entry_count * (k / chunks))
                end = int(entry_count * ((k + 1) / chunks))
                if end > start:
                    parts.append((start, end))
            return parts

        def _extract_chunk(ctx: _ChunkBase, outdir: str, indices: List[int], created_dirs: set[str]) -> None:

            with open(ctx.file.filepath, "rb") as fs:
                for i in indices:
                    e = ctx.file.entries[i]
                    h = e.combined_hash

                    if h not in self._searched_paths:
                        continue
                    rel = self._searched_paths.get(h) or f"__Unknown/{h:016X}"
                    dest = Path(outdir) / rel
                    parent = str(dest.parent)
                    if parent not in created_dirs:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        created_dirs.add(parent)
                    with open(dest, "wb") as f:
                        _read_entry_raw(e, fs, f, chunk_table=ctx.file.chunk_table)
                    ctx.found_hashes.append(h)
                    ctx.file_count += 1

        unpacked = 0
        pak = PakFile()
        for pakpath in self._enumerate_temp_paks_with_searched_files(pak):
            entries = pak.entries
            entry_count = len(entries)
            matched: List[tuple[int, int]] = []
            sp = self._searched_paths
            for idx, e in enumerate(entries):
                if e.combined_hash in sp:
                    matched.append((int(e.offset), idx))
            if not matched:
                if self.enable_console_logging:
                    print(f"Finished unpacking {pakpath}")
                continue

            matched.sort(key=lambda t: t[0])
            matched_indices = [i for _, i in matched]

            threads = _calc_threads(len(matched_indices))
            created_dirs: set[str] = set()
            if threads == 1:
                ctx = _ChunkBase(pak, 0, len(matched_indices))
                _extract_chunk(ctx, output_directory, matched_indices, created_dirs)
                unpacked += ctx.file_count

                for h in ctx.found_hashes:
                    p = self._searched_paths.get(h)
                    if p is None:
                        continue
                    for k in self._path_to_hashes.get(p, []):
                        self._searched_paths.pop(k, None)
                    self._path_to_hashes.pop(p, None)
            else:

                parts = _partition_chunks(len(matched_indices), threads)
                contexts = [_ChunkBase(pak, s, e) for (s, e) in parts]
                with ThreadPoolExecutor(max_workers=threads) as ex:
                    futures = [
                        ex.submit(
                            _extract_chunk,
                            ctx,
                            output_directory,
                            matched_indices[ctx.start : ctx.end],
                            created_dirs,
                        )
                        for ctx in contexts
                    ]
                    wait(futures)
                for ctx in contexts:
                    unpacked += ctx.file_count
                    for h in ctx.found_hashes:
                        p = self._searched_paths.get(h)
                        if p is None:
                            continue
                        for k in self._path_to_hashes.get(p, []):
                            self._searched_paths.pop(k, None)
                        self._path_to_hashes.pop(p, None)

            if self.enable_console_logging:
                print(f"Finished unpacking {pakpath}")
            if not self._searched_paths:
                break

        if missing_files is not None:

            missing_files.extend(self._searched_paths.values())
        return unpacked


    def _enumerate_temp_paks_with_searched_files(self, pak: PakFile) -> Iterable[str]:
        for i in range(len(self.pak_file_priority) - 1, -1, -1):
            pakfile = self.pak_file_priority[i]
            try:
                if os.path.getsize(pakfile) <= 16:
                    continue
            except FileNotFoundError:
                continue
            pak.filepath = pakfile
            with open(pakfile, "rb") as f:
                pak.read_contents(f, self._searched_paths)
            if pak.entries:
                yield pakfile

    def _read_entries_in_chunk_to_memory(self, ctx: _ChunkBase) -> Iterator[Tuple[PakEntry, io.BytesIO]]:
        with open(ctx.file.filepath, "rb") as fs:
            for i in range(ctx.start, ctx.end):
                e = ctx.file.entries[i]
                h = e.combined_hash
                if h not in self._searched_paths:
                    continue

                if e.compressed_size > (1 << 31) or e.decompressed_size > (1 << 31):
                    raise RuntimeError("PAK entry size exceeds int range")
                buf = io.BytesIO()

                _read_entry_raw(e, fs, buf, chunk_table=ctx.file.chunk_table)

                ctx.found_hashes.append(h)
                ctx.file_count += 1
                yield e, buf


class CachedPakReader(PakReader):
    def __init__(self) -> None:
        super().__init__()
        self._cache: Optional[Dict[int, tuple[PakFile, PakEntry]]] = None
        self._cache_keys_set: Optional[set[int]] = None

    def cache_entries(self, assign_paths: bool = False) -> None:
        if self._cache:
            return
        self._cache = {}

        for pak in self._enumerate_paks(assign_paths=False):
            for e in pak.entries:
                if e.path is None and self._searched_paths:
                    name = self._searched_paths.get(e.combined_hash)
                    if name:
                        e.path = name
                h = e.combined_hash
                if h not in self._cache:
                    self._cache[h] = (pak, e)

        self._cache_keys_set = set(self._cache.keys())

    def assign_paths(self, paths: Iterable[str]) -> int:
        """Fast path: assign known names into the existing cache without rebuilding.

        Returns number of entries newly named.
        """
        if self._cache is None:
            self.cache_entries(assign_paths=False)
            if self._cache is None:
                return 0


        norm_paths = list({_normalize_for_hash(p) for p in paths})
        fast = ensure_fast_pakresolve()
        if fast is None:
            raise RuntimeError("fast_pakresolve native module is required but not available")
        
        _rem, updated = fast.resolve_paths_utf16le(self._cache, norm_paths)
        return int(updated)


    def _enumerate_paks(self, assign_paths: bool) -> Iterable[PakFile]:
        for i in range(len(self.pak_file_priority) - 1, -1, -1):
            pakfile = self.pak_file_priority[i]
            try:
                if os.path.getsize(pakfile) <= 16:
                    continue
            except FileNotFoundError:
                continue
            pak = PakFile()
            pak.filepath = pakfile
            with open(pakfile, "rb") as f:
                pak.read_contents(f, self._searched_paths if assign_paths else None)
            if pak.entries:
                yield pak

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
                if not os.path.exists(pak_path) or os.path.getsize(pak_path) <= 16:
                    continue
                
                with open(pak_path, "rb") as f:

                    header_data = f.read(16)
                    if len(header_data) != 16:
                        continue
                    
                    magic, maj, minr, features, file_count, fingerprint = struct.unpack("<IBBhII", header_data)
                    if magic != 0x414B504B:
                        continue
                    

                    if (maj, minr) not in {(4, 0), (4, 1), (4, 2), (2, 0)}:
                        continue
                    

                    entry_table_size = file_count * (48 if maj == 4 else 24)
                    entry_table = bytearray(f.read(entry_table_size))
                    
                    if (features & FEATURE_EXTRA_DATA) != 0:
                        f.seek(4, 1)
                    
                    if features != 0:
                        key = bytearray(f.read(128))
                        _decrypt_pak_entry_data(entry_table, key)

                    chunk_table = _read_chunk_table(f) if (features & FEATURE_CHUNKED_RESOURCES) != 0 else ()

                    off = 0
                    for _ in range(file_count):
                        if maj == 4:
                            hash_lower, hash_upper = struct.unpack_from("<II", entry_table, off)
                            combined = ((hash_upper & 0xFFFFFFFF) << 32) | (hash_lower & 0xFFFFFFFF)
                            
                            if combined == manifest_hash:

                                hash_lower, hash_upper, offset, csize, dsize, attrib, checksum = struct.unpack_from(
                                    "<IIqqqqq", entry_table, off
                                )
                                compression = attrib & 0xF
                                encryption = (attrib & 0x00FF0000) >> 16
                                

                                f.seek(offset)
                                e = PakEntry(
                                    hash_lower=hash_lower,
                                    hash_upper=hash_upper,
                                    offset=offset,
                                    compressed_size=csize,
                                    decompressed_size=dsize,
                                    compression=compression,
                                    encryption=encryption,
                                    checksum=checksum,
                                    attributes=attrib,
                                    path=manifest_path
                                )
                                
                                stream = io.BytesIO()
                                _read_entry_raw(e, f, stream, chunk_table=chunk_table)
                                stream.seek(0)
                                content = stream.read().decode('utf-8')
                                
                                paths = []
                                for line in content.splitlines():
                                    line = line.strip()
                                    if line and not line.startswith('#'):
                                        paths.append(line.replace('\\', '/'))
                                
                                return paths
                            
                            off += 48
                        else:
                            offset, csize, hash_upper, hash_lower = struct.unpack_from("<qqII", entry_table, off)
                            combined = ((hash_upper & 0xFFFFFFFF) << 32) | (hash_lower & 0xFFFFFFFF)
                            
                            if combined == manifest_hash:

                                e = PakEntry(
                                    hash_lower=hash_lower,
                                    hash_upper=hash_upper,
                                    offset=offset,
                                    compressed_size=csize,
                                    decompressed_size=csize,
                                    path=manifest_path
                                )
                                
                                stream = io.BytesIO()
                                _read_entry_raw(e, f, stream, chunk_table=chunk_table)
                                stream.seek(0)
                                content = stream.read().decode('utf-8')
                                
                                paths = []
                                for line in content.splitlines():
                                    line = line.strip()
                                    if line and not line.startswith('#'):
                                        paths.append(line.replace('\\', '/'))
                                
                                return paths
                            
                            off += 24
                        
            except (IOError, OSError, struct.error):

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
                            size = int(e.decompressed_size)
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
                                    data = zlib.decompress(data, -zlib.MAX_WBITS)
                            elif e.compression == 2 and resources.zstd_decompressor:
                                data = resources.zstd_decompressor.decompress(data)
                            
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

