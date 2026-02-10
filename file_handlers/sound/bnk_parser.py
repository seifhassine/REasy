from __future__ import annotations

import struct
from dataclasses import dataclass

_PCK_MAGIC = b"AKPK"
_RIFF_MAGIC = b"RIFF"
_WAVE_MAGIC = b"WAVE"
_BNK = "bnk"
_PCK = "pck"
_PCK_HDR_FMT = "<IIIIII"
_PCK_ENTRY_FMT = "<IIIII"
_DIDX_ENTRY_FMT = "<III"
_HIRC_HDR_SZ = 5
_HIRC_SRC_OFF = 17


@dataclass(slots=True)
class ChunkRecord:
    chunk_id: bytes
    payload: bytes

@dataclass(slots=True)
class BnkEmbeddedAudio:
    source_id: int
    offset: int
    length: int

@dataclass(slots=True)
class BnkTrack:
    index: int
    source_id: int
    offset: int
    length: int
    absolute_offset: bool = False

@dataclass(slots=True)
class BnkParseResult:
    bank_version: int | None
    tracks: list[BnkTrack]
    container_type: str = _BNK

@dataclass(slots=True)
class WemMetadata:
    codec: str
    channels: int | None
    sample_rate: int | None
    duration_seconds: float | None

@dataclass(slots=True)
class _PckTable:
    count_pos: int
    count: int
    entries: list[tuple[int, int, int, int, int]]

@dataclass(slots=True)
class _PckLayout:
    version: int
    header_size: int
    tables_start: int
    tables_end: int
    blob_base: int
    tables: list[_PckTable]


def rewrite_soundbank(data: bytes, replacements: dict[int, bytes]) -> bytes:
    if not replacements:
        return bytes(data)
    return _rewrite_pck(data, replacements) if data[:4] == _PCK_MAGIC else _rewrite_bnk(data, replacements)

def export_non_streaming_pck(data: bytes) -> bytes:
    layout = _parse_pck_layout(data)
    if layout is None:
        return bytes(data)
    return bytes(data[: min(layout.blob_base, len(data))])

def parse_soundbank(data: bytes) -> BnkParseResult:
    return parse_pck(data) if data[:4] == _PCK_MAGIC else parse_bnk(data)

def parse_bnk(data: bytes) -> BnkParseResult:
    chunks = _read_chunks(data)
    media = _read_didx(chunks.get("DIDX"))
    indexed = {e.source_id: e for e in media}
    tracks = [
        BnkTrack(index=i, source_id=sid, offset=m.offset, length=m.length)
        for i, sid in enumerate(_read_hirc_tracks(chunks.get("HIRC")), 1)
        if (m := indexed.get(sid))
    ]
    if not tracks:
        tracks = [BnkTrack(index=i, source_id=e.source_id, offset=e.offset, length=e.length)
                  for i, e in enumerate(media, 1)]
    return BnkParseResult(bank_version=_read_bank_version(chunks.get("BKHD")), tracks=tracks)

def parse_pck(data: bytes) -> BnkParseResult:
    layout = _parse_pck_layout(data)
    if layout is None:
        return BnkParseResult(bank_version=None, tracks=[], container_type=_PCK)
    bnk_t, wem_t, ext_t = layout.tables
    entries = wem_t.entries + ext_t.entries or bnk_t.entries
    tracks = [BnkTrack(index=i, source_id=e[0], offset=e[3], length=e[2], absolute_offset=True)
              for i, e in enumerate(entries, 1)]
    if layout.header_size:
        for t in tracks:
            if t.offset + t.length > len(data) and layout.header_size + t.offset + t.length <= len(data):
                t.offset += layout.header_size
    return BnkParseResult(bank_version=layout.version, tracks=tracks, container_type=_PCK)

def get_data_chunk(data: bytes) -> bytes | None:
    return _read_chunks(data).get("DATA")

def extract_embedded_wem(data: bytes, track: BnkTrack) -> bytes:
    if track.absolute_offset:
        return _safe_slice(data, track.offset, track.length)
    chunk = get_data_chunk(data)
    return _safe_slice(chunk, track.offset, track.length) if chunk else b""

def extract_embedded_wem_from_data_chunk(data_chunk: bytes, track: BnkTrack) -> bytes:
    return _safe_slice(data_chunk, track.offset, track.length)

def parse_wem_metadata(data: bytes) -> WemMetadata:
    unknown = WemMetadata(codec="Unknown", channels=None, sample_rate=None, duration_seconds=None)
    if len(data) < 12 or data[:4] != _RIFF_MAGIC or data[8:12] != _WAVE_MAGIC:
        return unknown
    pos, fmt_chunk, data_size = 12, None, None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        csz = struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8
        end = pos + csz
        if end > len(data):
            break
        if cid == b"fmt ":
            fmt_chunk = data[pos:end]
        elif cid == b"data":
            data_size = csz
        pos = end + (csz & 1)
    if not fmt_chunk or len(fmt_chunk) < 16:
        return unknown
    tag, ch, sr, avg_bps = struct.unpack_from("<HHII", fmt_chunk, 0)
    dur = (data_size / avg_bps) if data_size and avg_bps else None
    return WemMetadata(codec=f"0x{tag:04X}", channels=ch or None, sample_rate=sr or None, duration_seconds=dur)


def _safe_slice(data: bytes, offset: int, length: int) -> bytes:
    end = offset + length
    return b"" if offset < 0 or length < 0 or end > len(data) else data[offset:end]

def _read_chunk_records(data: bytes) -> list[ChunkRecord]:
    chunks, pos, size = [], 0, len(data)
    while pos + 8 <= size:
        cid = data[pos:pos + 4]
        length = struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8
        end = pos + length
        if end > size:
            break
        chunks.append(ChunkRecord(chunk_id=cid, payload=data[pos:end]))
        pos = end
    return chunks

def _read_chunks(data: bytes) -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    for rec in _read_chunk_records(data):
        try:
            result[rec.chunk_id.decode("ascii")] = rec.payload
        except UnicodeDecodeError:
            pass
    return result

def _pack_chunk_records(chunks: list[ChunkRecord]) -> bytes:
    out = bytearray()
    for c in chunks:
        out += c.chunk_id + struct.pack("<I", len(c.payload)) + c.payload
    return bytes(out)

def _read_bank_version(chunk: bytes | None) -> int | None:
    return struct.unpack_from("<I", chunk, 0)[0] if chunk and len(chunk) >= 4 else None

def _read_didx(chunk: bytes | None) -> list[BnkEmbeddedAudio]:
    if not chunk:
        return []
    sz = struct.calcsize(_DIDX_ENTRY_FMT)
    return [BnkEmbeddedAudio(*struct.unpack_from(_DIDX_ENTRY_FMT, chunk, p))
            for p in range(0, len(chunk) - len(chunk) % sz, sz)]

def _read_hirc_tracks(chunk: bytes | None) -> list[int]:
    if not chunk or len(chunk) < 4:
        return []
    count, out, pos = struct.unpack_from("<I", chunk, 0)[0], [], 4
    for _ in range(count):
        if pos + _HIRC_HDR_SZ > len(chunk):
            break
        etype, length = chunk[pos], struct.unpack_from("<I", chunk, pos + 1)[0]
        pos += _HIRC_HDR_SZ
        end = pos + length
        if end > len(chunk):
            break
        if etype == 2 and length >= _HIRC_SRC_OFF + 4:
            out.append(struct.unpack_from("<I", chunk, pos + _HIRC_SRC_OFF)[0])
        pos = end
    return out

def _rewrite_bnk(data: bytes, replacements: dict[int, bytes]) -> bytes:
    chunks = _read_chunk_records(data)
    di = next((i for i, c in enumerate(chunks) if c.chunk_id == b"DIDX"), None)
    da = next((i for i, c in enumerate(chunks) if c.chunk_id == b"DATA"), None)
    if di is None or da is None:
        return bytes(data)
    entries = _read_didx(chunks[di].payload)
    if not entries:
        return bytes(data)
    old = chunks[da].payload
    new_didx, new_data = bytearray(), bytearray()
    for e in entries:
        payload = replacements.get(e.source_id, _safe_slice(old, e.offset, e.length))
        new_didx += struct.pack(_DIDX_ENTRY_FMT, e.source_id, len(new_data), len(payload))
        new_data += payload
    chunks[di] = ChunkRecord(chunk_id=b"DIDX", payload=bytes(new_didx))
    chunks[da] = ChunkRecord(chunk_id=b"DATA", payload=bytes(new_data))
    return _pack_chunk_records(chunks)

def _parse_pck_layout(data: bytes) -> _PckLayout | None:
    if len(data) < 28 or data[:4] != _PCK_MAGIC:
        return None
    hsz, ver, lang_sz, *_ = struct.unpack_from(_PCK_HDR_FMT, data, 4)
    pos = 28
    if pos + 4 > len(data):
        return None
    sc = struct.unpack_from("<I", data, pos)[0]
    pos = max(pos + 4 + sc * 8, min(28 + lang_sz, len(data)))
    if pos > len(data):
        return None
    ts = pos
    tables: list[_PckTable] = []
    for _ in range(3):
        tbl, pos = _read_pck_table(data, pos)
        tables.append(tbl)
    bb = max(hsz, pos)
    return _PckLayout(version=ver, header_size=hsz, tables_start=ts, tables_end=pos, blob_base=bb, tables=tables)

def _read_pck_table(data: bytes, pos: int) -> tuple[_PckTable, int]:
    if pos + 4 > len(data):
        return _PckTable(count_pos=pos, count=0, entries=[]), pos
    cp = pos
    cnt = struct.unpack_from("<I", data, pos)[0]
    entries, pos = _read_pck_entries(data, pos)
    return _PckTable(count_pos=cp, count=cnt, entries=entries), pos

def _read_pck_entries(data: bytes, pos: int) -> tuple[list[tuple[int, int, int, int, int]], int]:
    if pos + 4 > len(data):
        return [], pos
    count = struct.unpack_from("<I", data, pos)[0]
    pos += 4
    esz = struct.calcsize(_PCK_ENTRY_FMT)
    n = min(count, max(0, (len(data) - pos) // esz))
    entries = [struct.unpack_from(_PCK_ENTRY_FMT, data, pos + i * esz) for i in range(n)]
    return entries, pos + n * esz

def _extract_pck_payload(data: bytes, hsz: int, offset: int, length: int) -> bytes:
    return _safe_slice(data, offset, length) or _safe_slice(data, hsz + offset, length)

def _rewrite_pck(data: bytes, replacements: dict[int, bytes]) -> bytes:
    layout = _parse_pck_layout(data)
    if layout is None:
        return bytes(data)
    new_blob = bytearray()
    packed: list[_PckTable] = []
    for tbl in layout.tables:
        updated = []
        for sid, f1, length, offset, f4 in tbl.entries:
            payload = replacements.get(sid)
            if payload is None:
                payload = _extract_pck_payload(data, layout.blob_base, offset, length)
            updated.append((sid, f1, len(payload), layout.blob_base + len(new_blob), f4))
            new_blob += payload
        packed.append(_PckTable(count_pos=tbl.count_pos, count=len(updated), entries=updated))
    out = bytearray(data[:layout.tables_start])
    out += _pack_pck_tables(packed)
    if layout.blob_base > len(out):
        gs, ge = layout.tables_end, min(layout.blob_base, len(data))
        if ge > gs:
            out += data[gs:ge]
        out += b"\x00" * max(0, layout.blob_base - len(out))
    out += new_blob
    return bytes(out)

def _pack_pck_tables(tables: list[_PckTable]) -> bytes:
    out = bytearray()
    for t in tables:
        out += struct.pack("<I", len(t.entries))
        for e in t.entries:
            out += struct.pack(_PCK_ENTRY_FMT, *e)
    return bytes(out)
