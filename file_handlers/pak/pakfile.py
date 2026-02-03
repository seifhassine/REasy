from __future__ import annotations
import io
import struct
from dataclasses import dataclass
from typing import Optional, BinaryIO, List


MAGIC = 0x414B504B


@dataclass
class PakHeader:
    magic: int
    major: int
    minor: int
    feature_flags: int
    file_count: int
    fingerprint: int


@dataclass
class PakEntry:
    hash_lower: int = 0
    hash_upper: int = 0
    offset: int = 0
    compressed_size: int = 0
    decompressed_size: int = 0
    checksum: int = 0
    compression: int = 0
    encryption: int = 0
    path: Optional[str] = None

    @property
    def combined_hash(self) -> int:
        return ((self.hash_upper & 0xFFFFFFFF) << 32) | (self.hash_lower & 0xFFFFFFFF)


class PakFile:
    def __init__(self) -> None:
        self.header: Optional[PakHeader] = None
        self.entries: List[PakEntry] = []
        self.filepath: str = ""
        self._fs: Optional[BinaryIO] = None


    def read_contents(self, f: BinaryIO, expected_paths: Optional[dict[int, str]] = None) -> None:
        data = f.read(4 + 1 + 1 + 2 + 4 + 4)
        if len(data) != 16:
            raise IOError("Invalid PAK header size")
        magic, maj, minr, features, file_count, fingerprint = struct.unpack("<IBBhII", data)
        if magic != MAGIC:
            raise IOError("File is not a valid PAK file")

        if (maj, minr) not in {(4, 0), (4, 1), (4, 2), (2, 0)}:
            raise IOError(f"Unsupported PAK version {maj}.{minr}")

        if features not in (0, 8, 24, 16):

            pass

        self.header = PakHeader(magic, maj, minr, features, file_count, fingerprint)
        self.entries.clear()
        if file_count == 0:
            return

        entry_size = 48 if maj == 4 else 24
        entry_table = bytearray(f.read(file_count * entry_size))
        if len(entry_table) != file_count * entry_size:
            raise IOError("Unexpected EOF reading PAK entry table")


        if (features & 16) != 0:
            f.seek(4, io.SEEK_CUR)


        if features != 0:
            key = f.read(128)
            if len(key) != 128:
                raise IOError("Unexpected EOF reading PAK key")
            _decrypt_pak_entry_data(entry_table, bytearray(key))

        buf = memoryview(entry_table)
        off = 0
        if maj == 4:
            for _ in range(file_count):

                hash_lower, hash_upper, offset, csize, dsize, attrib, checksum = struct.unpack_from(
                    "<IIqqqqq", buf, off
                )
                off += 48
                compression = attrib & 0xF
                encryption = (attrib & 0x00FF0000) >> 16
                e = PakEntry(
                    hash_lower=hash_lower,
                    hash_upper=hash_upper,
                    offset=offset,
                    compressed_size=csize,
                    decompressed_size=dsize,
                    checksum=checksum,
                    compression=compression,
                    encryption=encryption,
                )
                if expected_paths is not None:
                    p = expected_paths.get(e.combined_hash)
                    if p is None:
                        continue
                    e.path = p
                self.entries.append(e)
        else:
            for _ in range(file_count):
                offset, csize, hash_upper, hash_lower = struct.unpack_from("<qqII", buf, off)
                off += 24
                e = PakEntry(
                    hash_lower=hash_lower,
                    hash_upper=hash_upper,
                    offset=offset,
                    compressed_size=csize,
                    decompressed_size=csize,
                )
                if expected_paths is not None:
                    p = expected_paths.get(e.combined_hash)
                    if p is None:
                        continue
                    e.path = p
                self.entries.append(e)

    def read_entry(self, entry: PakEntry, out_stream: BinaryIO) -> None:
        if self._fs is None:
            self._fs = open(self.filepath, "rb")
        _read_entry_raw(entry, self._fs, out_stream)




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


def _read_entry_raw(entry: PakEntry, read_stream: BinaryIO, out_stream: BinaryIO) -> None:
    read_stream.seek(entry.offset)
    if entry.compression == 0:
        size = int(entry.decompressed_size)
        
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
        return

    size = int(entry.compressed_size)
    data = bytearray(read_stream.read(size))
    if entry.encryption != 0:
        sr: List[int] = [size]
        data = _decrypt_resource(data, sr)
        size = sr[0]
    
    comp_view = memoryview(data)[:size]
    if entry.compression == 1:  # Deflate
        import zlib

        d = zlib.decompressobj() 
        try:
            # Feed in moderate chunks to limit peak memory
            mv = comp_view
            step = 256 * 1024
            pos = 0
            while pos < len(mv):
                out = d.decompress(mv[pos : pos + step])
                if out:
                    out_stream.write(out)
                pos += step
            tail = d.flush()
            if tail:
                out_stream.write(tail)
        except zlib.error:
            # Fallback to raw DEFLATE stream
            d = zlib.decompressobj(-zlib.MAX_WBITS)
            mv = comp_view
            step = 256 * 1024
            pos = 0
            while pos < len(mv):
                out = d.decompress(mv[pos : pos + step])
                if out:
                    out_stream.write(out)
                pos += step
            tail = d.flush()
            if tail:
                out_stream.write(tail)
    elif entry.compression == 2:  # Zstd
        import zstandard as zstd

        dctx = _ZSTD_CTX or zstd.ZstdDecompressor()
        bio = io.BytesIO(bytes(comp_view))
        dctx.copy_stream(bio, out_stream)
    else:
        out_stream.write(comp_view.tobytes())

    comp_view.release()

try:
    import zstandard as _zstd_mod
    _ZSTD_CTX = _zstd_mod.ZstdDecompressor()
except Exception:
    _ZSTD_CTX = None

