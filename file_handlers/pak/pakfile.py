from __future__ import annotations
import io
import struct
import threading
import zlib
from dataclasses import dataclass
import zstandard as zstd
import numpy as np
from typing import TYPE_CHECKING, Callable, Optional, BinaryIO, List, Sequence

if TYPE_CHECKING:
    from .resolution import PakReaderInfo


MAGIC = 0x414B504B
PAK_HEADER_SIZE = 16
PAK_FLAG_HEADER_PADDING = 0x04
PAK_FLAG_ENTRY_TABLE_KEY = 0x08
PAK_FLAG_HEADER_MARKER = 0x10
PAK_FLAG_CHUNK_TABLE = 0x20
PAK_FLAG_HASH_REMAP = 0x40
PAK_SUPPORTED_FLAGS = (
    PAK_FLAG_HEADER_PADDING
    | PAK_FLAG_ENTRY_TABLE_KEY
    | PAK_FLAG_HEADER_MARKER
    | PAK_FLAG_CHUNK_TABLE
    | PAK_FLAG_HASH_REMAP
)
_HEADER_SKIP_BY_FLAG = (
    (PAK_FLAG_HEADER_MARKER, 4),
    (PAK_FLAG_HEADER_PADDING, 9),
)
CHUNK_SIZE_SHIFT = 10
CHUNK_UNCOMPRESSED_SIZE = 512 * 1024
CHUNKED_ATTRIBS = {0x1000000, 0x1000400}
EMBEDDED_CHILD_MAGIC = 0x555DE17A
_ZSTD_THREAD_STATE = threading.local()


def _get_thread_zstd_decompressor():
    decompressor = getattr(_ZSTD_THREAD_STATE, "decompressor", None)
    if decompressor is None:
        decompressor = zstd.ZstdDecompressor()
        _ZSTD_THREAD_STATE.decompressor = decompressor
    return decompressor


def _skip_optional_header_sections(f: BinaryIO, features: int) -> None:
    for flag, skip_len in _HEADER_SKIP_BY_FLAG:
        if (features & flag) != 0:
            f.seek(skip_len, io.SEEK_CUR)


def _has_embedded_child_footer(f: BinaryIO) -> bool:
    position = f.tell()
    try:
        f.seek(0, io.SEEK_END)
        if f.tell() < 32:
            return False
        f.seek(-32, io.SEEK_END)
        footer = f.read(8)
        return len(footer) == 8 and struct.unpack("<II", footer) == (
            EMBEDDED_CHILD_MAGIC,
            1,
        )
    finally:
        f.seek(position)


@dataclass(frozen=True)
class PakChunkEntry:
    offset: int
    size: int


@dataclass
class PakHeader:
    magic: int
    major: int
    minor: int
    feature_flags: int
    file_count: int
    fingerprint: int

    @property
    def entry_size(self) -> int:
        return 48 if self.major == 4 else 24


def read_pak_header(f: BinaryIO, label: str, *, supported_only: bool = True) -> PakHeader:
    """An embedded child PAK's index may not name every entry."""
    data = f.read(PAK_HEADER_SIZE)
    if len(data) != PAK_HEADER_SIZE:
        raise IOError("Invalid PAK header size")
    header = PakHeader(*struct.unpack("<IBBHII", data))
    if header.magic != MAGIC:
        raise IOError("File is not a valid PAK file")
    if (header.major, header.minor) not in {(4, 0), (4, 1), (4, 2), (2, 0)}:
        raise IOError(f"Unsupported PAK version {header.major}.{header.minor}")
    if supported_only and header.feature_flags & ~PAK_SUPPORTED_FLAGS:
        raise IOError(f"Unsupported PAK flags {header.feature_flags} for file {label}")
    if supported_only and _has_embedded_child_footer(f):
        raise IOError(f"Embedded child PAK readers are not yet supported: {label}")
    return header


@dataclass
class PakEntry:
    hash_lower: int = 0
    hash_upper: int = 0
    offset: int = 0
    compressed_size: int = 0
    decompressed_size: int = 0
    checksum: int = 0
    attributes: int = 0
    compression: int = 0
    encryption: int = 0
    path: Optional[str] = None

    @property
    def combined_hash(self) -> int:
        return ((self.hash_upper & 0xFFFFFFFF) << 32) | (self.hash_lower & 0xFFFFFFFF)

    @property
    def stored_size(self) -> int:
        return int(self.compressed_size or self.decompressed_size)


class PakFile:
    def __init__(self) -> None:
        self.header: Optional[PakHeader] = None
        self.entries: List[PakEntry] = []
        self.availability_entries: List[PakEntry] = []
        self.has_attr_bit20: bool = False
        self.filepath: str = ""
        self._fs: Optional[BinaryIO] = None
        self.chunk_table: tuple[PakChunkEntry, ...] = ()
        self.resolution_info: Optional["PakReaderInfo"] = None
        self.remap_pairs: List[tuple[int, int]] = []
        self.remap_stored_to_public: dict[int, int] = {}


    def read_contents(self, f: BinaryIO, expected_paths: Optional[dict[int, str]] = None) -> None:
        self.header = read_pak_header(f, self.filepath)
        maj, features, file_count = self.header.major, self.header.feature_flags, self.header.file_count
        self.entries.clear()
        self.availability_entries.clear()
        self.has_attr_bit20 = False
        self.chunk_table = ()
        self.remap_pairs.clear()
        self.remap_stored_to_public.clear()
        if file_count == 0:
            return

        entry_size = self.header.entry_size
        entry_table = bytearray(f.read(file_count * entry_size))
        if len(entry_table) != file_count * entry_size:
            raise IOError("Unexpected EOF reading PAK entry table")


        _skip_optional_header_sections(f, features)

        if (features & PAK_FLAG_HASH_REMAP) != 0:
            remap_header = f.read(8)
            if len(remap_header) != 8:
                raise IOError("Unexpected EOF reading PAK remap header")
            remap_count = struct.unpack("<Q", remap_header)[0]
            remap_raw = f.read(remap_count * 16)
            if len(remap_raw) != remap_count * 16:
                raise IOError("Unexpected EOF reading PAK remap pairs")
            for i in range(remap_count):
                stored_hash, public_hash = struct.unpack_from(
                    "<QQ", remap_raw, i * 16
                )
                self.remap_pairs.append((stored_hash, public_hash))
                self.remap_stored_to_public.setdefault(stored_hash, public_hash)

        if (features & PAK_FLAG_ENTRY_TABLE_KEY) != 0:
            key = f.read(128)
            if len(key) != 128:
                raise IOError("Unexpected EOF reading PAK key")
            _decrypt_pak_entry_data(entry_table, bytearray(key))

        self.chunk_table = _read_chunk_table(f) if (features & PAK_FLAG_CHUNK_TABLE) != 0 else ()

        buf = memoryview(entry_table)
        indices = range(file_count)
        named_hashes = expected_paths
        if expected_paths is not None:
            # bit20 is checked over all rows, unmatched ones included: it affects RE4 availability.
            rows = np.frombuffer(buf, dtype='<u4').reshape(file_count, entry_size // 4)
            lower, upper = (rows[:, 0], rows[:, 1]) if maj == 4 else (rows[:, 5], rows[:, 4])
            hashes = (upper.astype('<u8') << 32) | lower
            if self.remap_pairs:
                # Match the public hash aliases exactly as _resolve_expected_path.
                named_hashes = dict(expected_paths)
                for stored, public in self.remap_stored_to_public.items():
                    if public in expected_paths:
                        named_hashes.setdefault(stored, expected_paths[public])
            wanted = np.fromiter(named_hashes, dtype='<u8')
            # Vanilla tables are sorted: binary-search the requested hashes instead of ``np.isin`` over a million rows.
            if hashes.size < 2 or bool(np.all(hashes[:-1] <= hashes[1:])):
                starts = np.searchsorted(hashes, wanted, side='left')
                ends = np.searchsorted(hashes, wanted, side='right')
                indices = sorted(index for start, end in zip(starts, ends) for index in range(int(start), int(end)))
            else:
                indices = np.flatnonzero(np.isin(hashes, wanted))
            if maj == 4:
                self.has_attr_bit20 = bool(np.any(rows[:, 8] & 0x20))
        if maj == 4:
            for index in indices:
                hash_lower, hash_upper, offset, csize, dsize, attrib, checksum = struct.unpack_from(
                    "<IIqqqqq", buf, int(index) * 48
                )
                compression = attrib & 0xF
                encryption = (attrib & 0x00FF0000) >> 16
                self.has_attr_bit20 = self.has_attr_bit20 or (attrib & 0x20) != 0
                path = None
                if expected_paths is not None:
                    path = named_hashes.get((hash_upper << 32) | hash_lower)
                    if path is None:
                        continue
                e = PakEntry(
                    hash_lower=hash_lower,
                    hash_upper=hash_upper,
                    offset=offset,
                    compressed_size=csize,
                    decompressed_size=dsize,
                    checksum=checksum,
                    attributes=attrib,
                    compression=compression,
                    encryption=encryption,
                    path=path,
                )
                self.entries.append(e)
                if (attrib & 0x70) != 0:
                    self.availability_entries.append(e)
        else:
            for index in indices:
                offset, csize, hash_upper, hash_lower = struct.unpack_from("<qqII", buf, int(index) * 24)
                path = None
                if expected_paths is not None:
                    path = named_hashes.get((hash_upper << 32) | hash_lower)
                    if path is None:
                        continue
                e = PakEntry(
                    hash_lower=hash_lower,
                    hash_upper=hash_upper,
                    offset=offset,
                    compressed_size=csize,
                    decompressed_size=csize,
                    path=path,
                )
                self.entries.append(e)



    def _resolve_expected_path(
        self, expected_paths: dict[int, str], combined_hash: int
    ) -> Optional[str]:
        path = expected_paths.get(combined_hash)
        if path is not None:
            return path
        if self.remap_stored_to_public:
            public_hash = self.remap_stored_to_public.get(combined_hash)
            if public_hash is not None:
                path = expected_paths.get(public_hash)
        return path


def _read_chunk_table(f: BinaryIO) -> tuple[PakChunkEntry, ...]:
    header = f.read(8)
    if len(header) != 8:
        raise IOError("Unexpected EOF reading PAK chunk table header")

    _, chunk_count = struct.unpack("<ii", header)
    if chunk_count <= 0:
        return ()

    table_raw = f.read(chunk_count * 8)
    if len(table_raw) != chunk_count * 8:
        raise IOError("Unexpected EOF reading PAK chunk table")

    chunks: List[PakChunkEntry] = []
    for i in range(chunk_count):
        offset32, size_word = struct.unpack_from("<II", table_raw, i * 8)
        offset = offset32 | ((size_word & 0x3FF) << 32)
        size = size_word >> CHUNK_SIZE_SHIFT
        chunks.append(PakChunkEntry(offset=offset, size=size))
    return tuple(chunks)

def _is_chunked_entry(entry: PakEntry, chunk_table: Sequence[PakChunkEntry]) -> bool:
    if not chunk_table or entry.compression != 0 or entry.encryption != 0:
        return False

    chunk_id = int(entry.offset)
    if chunk_id < 0 or chunk_id >= len(chunk_table) or int(entry.compressed_size) <= 0:
        return False

    attrib = int(entry.attributes)
    if attrib in CHUNKED_ATTRIBS:
        return True

    # 4.2 chunked entries may carry additional high-bit flags; keep known marker bit check.
    return (attrib & 0x1000000) == 0x1000000


def _decrypt_key(key: bytearray) -> None:

    key_modulus_bytes = bytes([
        0x7D, 0x0B, 0xF8, 0xC1, 0x7C, 0x23, 0xFD, 0x3B, 0xD4, 0x75, 0x16, 0xD2, 0x33, 0x21, 0xD8, 0x10,
        0x71, 0xF9, 0x7C, 0xD1, 0x34, 0x93, 0xBA, 0x77, 0x26, 0xFC, 0xAB, 0x2C, 0xEE, 0xDA, 0xD9, 0x1C,
        0x89, 0xE7, 0x29, 0x7B, 0xDD, 0x8A, 0xAE, 0x50, 0x39, 0xB6, 0x01, 0x6D, 0x21, 0x89, 0x5D, 0xA5,
        0xA1, 0x3E, 0xA2, 0xC0, 0x8C, 0x93, 0x13, 0x36, 0x65, 0xEB, 0xE8, 0xDF, 0x06, 0x17, 0x67, 0x96,
        0x06, 0x2B, 0xAC, 0x23, 0xED, 0x8C, 0xB7, 0x8B, 0x90, 0xAD, 0xEA, 0x71, 0xC4, 0x40, 0x44, 0x9D,
        0x1C, 0x7B, 0xBA, 0xC4, 0xB6, 0x2D, 0xD6, 0xD2, 0x4B, 0x62, 0xD6, 0x26, 0xFC, 0x74, 0x20, 0x07,
        0xEC, 0xE3, 0x59, 0x9A, 0xE6, 0xAF, 0xB9, 0xA8, 0x35, 0x8B, 0xE0, 0xE8, 0xD3, 0xCD, 0x45, 0x65,
        0xB0, 0x91, 0xC4, 0x95, 0x1B, 0xF3, 0x23, 0x1E, 0xC6, 0x71, 0xCF, 0x3E, 0x35, 0x2D, 0x6B, 0xE3,
    ])
    key_exponent_bytes = bytes([0x01, 0x00, 0x01, 0x00])

    key_modulus = int.from_bytes(key_modulus_bytes, "little", signed=False)
    key_exponent = int.from_bytes(key_exponent_bytes, "little", signed=False)

    m_encrypted = int.from_bytes(key, "little", signed=False)
    result = pow(m_encrypted, key_exponent, key_modulus)
    out = result.to_bytes(len(key), "little", signed=False)
    key[:] = out


def _decrypt_resource(buf: bytearray, size_ref: list[int]) -> bytearray:

    view = memoryview(buf)
    if len(view) < 8:
        return buf
    out_size = int.from_bytes(view[:8], "little", signed=False)
    size_ref[0] = out_size


    resource_modulus_bytes = bytes([
        0x13, 0xD7, 0x9C, 0x89, 0x88, 0x91, 0x48, 0x10, 0xD7, 0xAA, 0x78, 0xAE, 0xF8, 0x59, 0xDF, 0x7D,
        0x3C, 0x43, 0xA0, 0xD0, 0xBB, 0x36, 0x77, 0xB5, 0xF0, 0x5C, 0x02, 0xAF, 0x65, 0xD8, 0x77, 0x03,
    ])
    resource_exponent_bytes = bytes([
        0xC0, 0xC2, 0x77, 0x1F, 0x5B, 0x34, 0x6A, 0x01, 0xC7, 0xD4, 0xD7, 0x85, 0x2E, 0x42, 0x2B, 0x3B,
        0x16, 0x3A, 0x17, 0x13, 0x16, 0xEA, 0x83, 0x30, 0x30, 0xDF, 0x3F, 0xF4, 0x25, 0x93, 0x20, 0x01,
    ])

    resource_modulus = int.from_bytes(resource_modulus_bytes, "little", signed=False)
    resource_exponent = int.from_bytes(resource_exponent_bytes, "little", signed=False)


    block_count = (len(view) - 8) // 128
    byte_size = block_count * 8
    

    out = bytearray(out_size + 1)
    

    in_pos = 8
    for offset in range(0, byte_size, 8):

        b1_bytes = bytes(view[in_pos : in_pos + 64])
        b2_bytes = bytes(view[in_pos + 64 : in_pos + 128])
        

        b1 = int.from_bytes(b1_bytes, "little", signed=False)
        b2 = int.from_bytes(b2_bytes, "little", signed=False)


        mod = pow(b1, resource_exponent, resource_modulus)
        
        if mod != 0:
            result = b2 // mod
        else:
            # This shouldn't happen with valid data
            result = 0
        
        result_bytes = (result & 0xFFFFFFFFFFFFFFFF).to_bytes(8, "little", signed=False)
        out[offset : offset + 8] = result_bytes
        in_pos += 128

    return out


def _decrypt_pak_entry_data(entry_table: bytearray, key: bytearray) -> None:
    _decrypt_key(key)
    if not key:
        return
    size = len(entry_table)
    for i in range(size):
        entry_table[i] ^= (i + key[i % 32] * key[i % 29]) & 0xFF


def _inflate(data, size: int) -> bytes:
    return zlib.decompress(data, zlib.MAX_WBITS, max(size, 1))


def _inflate_raw(data, size: int) -> bytes:
    return zlib.decompress(data, -zlib.MAX_WBITS, max(size, 1))


def _unzstd(data, size: int) -> bytes:
    return _get_thread_zstd_decompressor().decompress(data, max_output_size=size)


_CODECS = {1: (_inflate, _inflate_raw, _unzstd), 2: (_unzstd, _inflate, _inflate_raw)}


def _decompress(data, compression: int, size: int) -> bytes:
    *tried, last = _CODECS[compression]
    for codec in tried:
        try:
            return codec(data, size)
        except (zlib.error, zstd.ZstdError):
            pass
    return last(data, size)


def stream_reads(stream: BinaryIO) -> Callable[[int, int], bytes]:
    def read(offset: int, size: int) -> bytes:
        stream.seek(offset)
        return stream.read(size)
    return read


def read_entry(entry: PakEntry, read: Callable[[int, int], bytes], *,
               chunk_table: Sequence[PakChunkEntry] = ()) -> bytes:
    """Reads and decompression are single calls that release the GIL, so threaded reads barely block each other."""
    if _is_chunked_entry(entry, chunk_table):
        chunks = []
        remaining = int(entry.compressed_size)
        chunk_id = int(entry.offset)
        while remaining > 0:
            chunks.append(chunk_table[chunk_id])
            remaining -= chunks[-1].size
            chunk_id += 1
        start = min(chunk.offset for chunk in chunks)
        end = max(chunk.offset + chunk.size for chunk in chunks)
        stored = memoryview(read(start, end - start))
        if len(stored) != end - start:
            raise IOError("Unexpected EOF reading chunk payload")
        payloads = (stored[chunk.offset - start:chunk.offset - start + chunk.size] for chunk in chunks)
        data = b"".join(payload if len(payload) == CHUNK_UNCOMPRESSED_SIZE
                        else _unzstd(payload, CHUNK_UNCOMPRESSED_SIZE * 2) for payload in payloads)
        if len(data) < int(entry.decompressed_size):
            raise IOError(
                "Chunked entry uses an unsupported tail codec: "
                f"hash {entry.combined_hash:016X}"
            )
        return data[:int(entry.decompressed_size)]

    if entry.compression == 0:
        return read(entry.offset, entry.stored_size)
    data = read(entry.offset, int(entry.compressed_size))
    if entry.encryption != 0:
        size = [len(data)]
        data = memoryview(_decrypt_resource(bytearray(data), size))[:size[0]]
    if entry.compression not in _CODECS:
        return bytes(data)
    return _decompress(data, entry.compression, int(entry.decompressed_size))


def _read_entry_raw(
    entry: PakEntry,
    read_stream: BinaryIO,
    out_stream: BinaryIO,
    *,
    chunk_table: Sequence[PakChunkEntry] = (),
) -> None:
    if entry.compression != 0 or _is_chunked_entry(entry, chunk_table):
        out_stream.write(read_entry(entry, stream_reads(read_stream), chunk_table=chunk_table))
        return

    read_stream.seek(entry.offset)
    size = entry.stored_size
    buffer_size = min(size, 64 * 1024 * 1024)
    buffer = bytearray(buffer_size)
    view = memoryview(buffer)
    remaining = size
    while remaining > 0:
        chunk_size = min(remaining, buffer_size)
        n = read_stream.readinto(view[:chunk_size])
        if n is None or n == 0:
            break
        out_stream.write(view[:n])
        remaining -= n
