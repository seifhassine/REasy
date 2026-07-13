from __future__ import annotations

import ctypes
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


GDEFLATE_MAGIC = 0xFB04
_GDEFLATE_ID = 4
_TILE_SIZE = 0x10000
_MIN_PARALLEL_TILES = 16
_MAX_WORKERS = 16
_LOCK = threading.RLock()
_DLL = _DECOMPRESSOR = _DLL_DIR = None


class _Page(ctypes.Structure):
    _fields_ = [("data", ctypes.c_void_p), ("size", ctypes.c_int)]


def is_gdeflate_payload(data: bytes | bytearray | memoryview) -> bool:
    return len(data) >= 2 and int.from_bytes(data[:2], "little") == GDEFLATE_MAGIC


def _candidate_paths() -> list[Path]:
    root = Path(__file__).resolve().parents[2]
    return [
        root / "tools" / "runtimes" / "win-x64" / "native" / "libGDeflate.dll",
        root / ".cache" / "gdeflate" / "libGDeflate.dll",
        root / "GDeflateNet" / "GDeflateNet" / "runtimes" / "win-x64" / "native" / "libGDeflate.dll",
    ]


def _load_library():
    global _DLL, _DLL_DIR
    if _DLL is not None:
        return _DLL
    errors: list[str] = []
    for path in _candidate_paths():
        if not path.is_file():
            continue
        try:
            if os.name == "nt" and hasattr(os, "add_dll_directory"):
                _DLL_DIR = os.add_dll_directory(str(path.parent))
            dll = ctypes.CDLL(str(path))
            dll.libdeflate_alloc_gdeflate_decompressor.restype = ctypes.c_void_p
            dll.libdeflate_gdeflate_decompress.argtypes = [
                ctypes.c_void_p,
                ctypes.POINTER(_Page),
                ctypes.c_size_t,
                ctypes.c_void_p,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_size_t),
            ]
            dll.libdeflate_gdeflate_decompress.restype = ctypes.c_int
            dll.libdeflate_free_gdeflate_decompressor.argtypes = [ctypes.c_void_p]
            dll.libdeflate_free_gdeflate_decompressor.restype = None
            _DLL = dll
            return dll
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    raise RuntimeError("Unable to load libGDeflate: " + ("; ".join(errors) or "no binary found"))


def _decompressor():
    global _DECOMPRESSOR
    if _DECOMPRESSOR is None:
        _DECOMPRESSOR = _load_library().libdeflate_alloc_gdeflate_decompressor()
        if not _DECOMPRESSOR:
            raise RuntimeError("Unable to allocate GDeflate decompressor")
    return _DECOMPRESSOR


def _parallel_ranges(tile_count: int) -> list[tuple[int, int]]:
    """Split large streams into balanced ranges without oversubscribing small ones."""
    if tile_count < _MIN_PARALLEL_TILES:
        return [(0, tile_count)]
    available_workers = min(_MAX_WORKERS, os.cpu_count() or 1, tile_count // 4)
    if available_workers < 2:
        return [(0, tile_count)]

    range_size, extra = divmod(tile_count, available_workers)
    ranges: list[tuple[int, int]] = []
    start = 0
    for worker_index in range(available_workers):
        count = range_size + (worker_index < extra)
        ranges.append((start, count))
        start += count
    return ranges


def _decompress_range(
    dll,
    pages,
    start: int,
    count: int,
    output_address: int,
    output_size: int,
) -> None:
    decompressor = dll.libdeflate_alloc_gdeflate_decompressor()
    if not decompressor:
        raise RuntimeError("Unable to allocate GDeflate decompressor")

    page_pointer = ctypes.cast(
        ctypes.byref(pages, start * ctypes.sizeof(_Page)), ctypes.POINTER(_Page)
    )
    written = ctypes.c_size_t()
    try:
        result = dll.libdeflate_gdeflate_decompress(
            decompressor,
            page_pointer,
            count,
            output_address,
            output_size,
            ctypes.byref(written),
        )
    finally:
        dll.libdeflate_free_gdeflate_decompressor(decompressor)

    if result != 0:
        raise RuntimeError(f"GDeflate decompression failed with code {result}")


def _header(data: bytes | bytearray | memoryview):
    view = memoryview(data)
    if len(view) < 8 or view[0] != _GDEFLATE_ID or view[1] != (0xFF ^ _GDEFLATE_ID):
        return None
    return view, int.from_bytes(view[2:4], "little"), int.from_bytes(view[4:8], "little")


def gdeflate_uncompressed_size(data: bytes | bytearray | memoryview, fallback_size: int) -> int:
    parsed = _header(data)
    if parsed is None:
        return fallback_size
    _view, tile_count, flags = parsed
    last_tile_size = (flags >> 2) & 0x3FFFF
    size = tile_count * _TILE_SIZE - (0 if last_tile_size == 0 else _TILE_SIZE - last_tile_size)
    return size if size > 0 else fallback_size


def decompress_gdeflate(data: bytes | bytearray | memoryview, expected_size: int) -> bytes:
    parsed = _header(data)
    if parsed is None:
        raise RuntimeError("Invalid GDeflate tile stream header")
    view, tile_count, _flags = parsed
    payload_start = 8 + tile_count * 4
    if tile_count <= 0 or payload_start > len(view):
        raise RuntimeError("Invalid GDeflate tile table")

    offsets = [int.from_bytes(view[8 + i * 4 : 12 + i * 4], "little") for i in range(tile_count)]
    compressed = ctypes.create_string_buffer(bytes(view))
    pages = (_Page * tile_count)()
    cursor = payload_start
    base = ctypes.addressof(compressed)
    for i in range(tile_count):
        tile_offset = offsets[i] if i else 0
        tile_size = (offsets[i + 1] - tile_offset) if i + 1 < tile_count else offsets[0]
        if tile_size < 0 or cursor + tile_size > len(view):
            raise RuntimeError("GDeflate tile payload is truncated")
        pages[i] = _Page(base + cursor, tile_size)
        cursor += tile_size

    output = ctypes.create_string_buffer(expected_size)
    output_address = ctypes.addressof(output)
    dll = _load_library()
    is_complete_tile_stream = (tile_count - 1) * _TILE_SIZE < expected_size <= tile_count * _TILE_SIZE
    ranges = _parallel_ranges(tile_count) if is_complete_tile_stream else [(0, tile_count)]
    if len(ranges) == 1:
        written = ctypes.c_size_t()
        with _LOCK:
            result = dll.libdeflate_gdeflate_decompress(
                _decompressor(), pages, tile_count, output_address, expected_size, ctypes.byref(written)
            )
        if result != 0:
            raise RuntimeError(f"GDeflate decompression failed with code {result}")
    else:
        with ThreadPoolExecutor(max_workers=len(ranges), thread_name_prefix="gdeflate") as executor:
            futures = []
            for start, count in ranges:
                range_output_offset = start * _TILE_SIZE
                range_output_size = min(count * _TILE_SIZE, expected_size - range_output_offset)
                futures.append(
                    executor.submit(
                        _decompress_range,
                        dll,
                        pages,
                        start,
                        count,
                        output_address + range_output_offset,
                        range_output_size,
                    )
                )
            for future in futures:
                future.result()
    return output.raw[:expected_size]
