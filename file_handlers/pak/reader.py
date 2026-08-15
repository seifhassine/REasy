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
from typing import Dict, Iterable, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from .utils import filepath_hash, guess_extension_from_header
from utils.native_build import ensure_fast_pakresolve
from .pakfile import (
    PakFile,
    PakEntry,
    _read_entry_raw,
    _decrypt_resource,
    _decrypt_pak_entry_data,
    _read_chunk_table,
    PAK_FLAG_ENTRY_TABLE_KEY,
    PAK_FLAG_CHUNK_TABLE,
    _is_chunked_entry,
    _skip_optional_header_sections,
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


def _normalize_for_hash(path: str) -> str:
    s = path.strip().replace("\\", "/").lower()
    while "//" in s:
        s = s.replace("//", "/")
    return s


@dataclass
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




class CachedPakReader(PakReader):
    def __init__(self) -> None:
        super().__init__()
        self._cache: Optional[Dict[int, tuple[PakFile, PakEntry]]] = None
        self._cache_keys_set: Optional[set[int]] = None
        self._cache_complete: bool = True

    def cache_entries(self, assign_paths: bool = False) -> None:
        if self._cache is not None and self._cache_complete:
            return
        self._cache = {}

        for pak in self._enumerate_paks(assign_paths=assign_paths):
            for e in pak.entries:
                if e.path is None and self._searched_paths:
                    name = self._searched_paths.get(e.combined_hash)
                    if name:
                        e.path = name
                h = e.combined_hash
                if h not in self._cache:
                    self._cache[h] = (pak, e)

        self._cache_keys_set = set(self._cache.keys())
        self._cache_complete = True

    def cache_entries_for_paths(self, paths: Iterable[str]) -> None:
        """Build a lightweight cache containing only known list paths."""
        if self._cache is not None:
            return

        self.reset_file_list()
        self.add_files(*paths)
        self._cache = {}

        for pak in self._enumerate_paks(assign_paths=True):
            for e in pak.entries:
                h = e.combined_hash
                if h not in self._cache:
                    self._cache[h] = (pak, e)

        self._cache_keys_set = set(self._cache.keys())
        self._cache_complete = False

    def assign_paths(self, paths: Iterable[str], replace_existing: bool = False) -> int:
        """Fast path: assign known names into the existing cache without rebuilding.

        Returns number of entries newly named.
        """
        norm_paths = list({_normalize_for_hash(p) for p in paths})
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
                if not os.path.exists(pak_path) or os.path.getsize(pak_path) <= 16:
                    continue
                
                with open(pak_path, "rb") as f:

                    header_data = f.read(16)
                    if len(header_data) != 16:
                        continue
                    
                    magic, maj, minr, features, file_count, _ = struct.unpack("<IBBhII", header_data)
                    if magic != 0x414B504B:
                        continue
                    

                    if (maj, minr) not in {(4, 0), (4, 1), (4, 2), (2, 0)}:
                        continue
                    

                    entry_table_size = file_count * (48 if maj == 4 else 24)
                    entry_table = bytearray(f.read(entry_table_size))
                    
                    _skip_optional_header_sections(f, features)

                    if (features & PAK_FLAG_ENTRY_TABLE_KEY) != 0:
                        key = bytearray(f.read(128))
                        _decrypt_pak_entry_data(entry_table, key)

                    chunk_table = _read_chunk_table(f) if (features & PAK_FLAG_CHUNK_TABLE) != 0 else ()

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

