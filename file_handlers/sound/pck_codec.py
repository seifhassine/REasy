"""Low-level Audiokinetic AKPK package parsing and rewriting."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, replace


_PCK_MAGIC = b"AKPK"


def _safe_slice(data: bytes, offset: int, length: int) -> bytes:
    end = offset + length
    return b"" if offset < 0 or length < 0 or end > len(data) else data[offset:end]


def _source_id(value: int) -> int:
    value = int(value)
    if not 0 < value <= 0xFFFFFFFF:
        raise ValueError(f"Invalid Wwise Source ID: {value}")
    return value


@dataclass(slots=True)
class PckEntry:
    entry_id: int
    block_size: int
    length: int
    start_block: int
    language_id: int
    offset: int
    table_kind: str
    entry_size: int
    available: bool = False
    id_is_64_bit: bool = False
    size_is_64_bit: bool = False
    record_offset: int = 0


@dataclass(slots=True)
class _PckTable:
    kind: str
    offset: int
    size: int
    entry_size: int
    entries: list[PckEntry]


@dataclass(slots=True)
class _PckLayout:
    version: int
    header_size: int
    fixed_header_size: int
    language_size: int
    tables_start: int
    tables_end: int
    blob_base: int
    tables: list[_PckTable]


def export_non_streaming_pck(data: bytes) -> bytes:
    layout = parse_pck_layout(data)
    if layout is None:
        return bytes(data)
    return bytes(data[: min(layout.blob_base, len(data))])


def parse_pck_layout(data: bytes) -> _PckLayout | None:
    if len(data) < 24 or data[:4] != _PCK_MAGIC:
        return None
    header_size, version, language_size, bank_size, sound_size = struct.unpack_from("<IIIII", data, 4)
    fixed_header_size = 24
    external_size = 0
    # The fourth table was added later and is present when the declared header has room for it.
    if language_size + bank_size + sound_size + 0x10 < header_size:
        if len(data) < 28:
            return None
        external_size = struct.unpack_from("<I", data, 24)[0]
        fixed_header_size = 28
    tables_start = fixed_header_size + language_size
    section_sizes = (bank_size, sound_size, external_size)
    kinds = ("banks", "sounds", "externals")
    pos = tables_start
    tables: list[_PckTable] = []
    for kind, section_size in zip(kinds, section_sizes):
        table = _read_pck_table(data, pos, section_size, kind)
        if table is None:
            return None
        tables.append(table)
        pos += section_size
    tables_end = pos
    blob_base = max(tables_end, header_size + 8)
    for table in tables:
        for entry in table.entries:
            entry.available = bool(_safe_slice(data, entry.offset, entry.length))
    return _PckLayout(
        version=version,
        header_size=header_size,
        fixed_header_size=fixed_header_size,
        language_size=language_size,
        tables_start=tables_start,
        tables_end=tables_end,
        blob_base=blob_base,
        tables=tables,
    )


def _read_pck_table(data: bytes, offset: int, size: int, kind: str) -> _PckTable | None:
    if size == 0:
        return _PckTable(kind=kind, offset=offset, size=0, entry_size=0, entries=[])
    if size < 4 or offset < 0 or offset + size > len(data):
        return None
    count = struct.unpack_from("<I", data, offset)[0]
    if count == 0:
        return _PckTable(kind=kind, offset=offset, size=size, entry_size=0, entries=[])
    body_size = size - 4
    if body_size % count:
        return None
    entry_size = body_size // count
    if entry_size not in (20, 24):
        return None
    entries: list[PckEntry] = []
    record_offset = offset + 4
    for index in range(count):
        pos = record_offset + index * entry_size
        entry = _decode_pck_entry(data, pos, entry_size, kind)
        if entry is None:
            return None
        entries.append(entry)
    return _PckTable(kind=kind, offset=offset, size=size, entry_size=entry_size, entries=entries)


def _decode_pck_entry(data: bytes, pos: int, entry_size: int, kind: str) -> PckEntry | None:
    if pos + entry_size > len(data):
        return None
    id_is_64_bit = entry_size == 24 and kind == "externals"
    size_is_64_bit = entry_size == 24 and kind != "externals"
    if id_is_64_bit:
        entry_id, block_size, length, start_block, language_id = struct.unpack_from("<QIIII", data, pos)
    elif size_is_64_bit:
        entry_id, block_size, length, start_block, language_id = struct.unpack_from("<IIQII", data, pos)
    else:
        entry_id, block_size, length, start_block, language_id = struct.unpack_from("<IIIII", data, pos)
    absolute_offset = start_block * block_size if block_size else start_block
    return PckEntry(
        entry_id=entry_id,
        block_size=block_size,
        length=length,
        start_block=start_block,
        language_id=language_id,
        offset=absolute_offset,
        table_kind=kind,
        entry_size=entry_size,
        id_is_64_bit=id_is_64_bit,
        size_is_64_bit=size_is_64_bit,
        record_offset=pos,
    )


def _encode_pck_entry(out: bytearray, entry: PckEntry) -> None:
    values = (entry.entry_id, entry.block_size, entry.length, entry.start_block, entry.language_id)
    if entry.id_is_64_bit:
        struct.pack_into("<QIIII", out, entry.record_offset, *values)
    elif entry.size_is_64_bit:
        struct.pack_into("<IIQII", out, entry.record_offset, *values)
    else:
        struct.pack_into("<IIIII", out, entry.record_offset, *values)


def rewrite_pck(data: bytes, replacements: dict[int, bytes]) -> bytes:
    layout = parse_pck_layout(data)
    if layout is None:
        return bytes(data)
    existing_media_ids = {
        entry.entry_id
        for table in layout.tables
        if table.kind != "banks"
        for entry in table.entries
    }
    out = bytes(data)
    for source_id in sorted(set(replacements) - existing_media_ids):
        out = _add_pck_sound_entry(out, source_id, replacements[source_id])
    data = out
    layout = parse_pck_layout(data)
    if layout is None:
        raise ValueError("Added PCK media produced an invalid package layout")
    replaceable = [
        entry
        for table in layout.tables
        if table.kind != "banks"
        for entry in table.entries
        if entry.entry_id in replacements
    ]
    if not replaceable:
        return bytes(data)

    out = bytearray(data)
    all_entries = [entry for table in layout.tables for entry in table.entries]
    replacing_ids = {id(entry) for entry in replaceable}
    original_offsets = sorted(
        {entry.offset for entry in all_entries if entry.length > 0}
    )
    reserved = sorted(
        (entry.offset, entry.offset + entry.length)
        for entry in all_entries
        if id(entry) not in replacing_ids and entry.length > 0
    )
    append_cursor = max(
        [layout.blob_base, len(out)]
        + [entry.offset + entry.length for entry in all_entries if entry.length > 0]
    )
    allocated: list[tuple[int, int]] = list(reserved)

    for entry in sorted(replaceable, key=lambda item: item.offset):
        payload = bytes(replacements[entry.entry_id])
        start = entry.offset
        slot_limit = min(
            (offset for offset in original_offsets if offset > start),
            default=start + entry.length,
        )
        candidate = (start, start + len(payload))
        overlaps = any(
            candidate[0] < other_end and other_start < candidate[1]
            for other_start, other_end in allocated
        )
        fits_original_slot = candidate[1] <= slot_limit and not overlaps
        if start < layout.blob_base or not fits_original_slot:
            alignment = entry.block_size or 1
            start = _align_up(append_cursor, alignment)
        end = start + len(payload)
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[start:end] = payload
        entry.offset = start
        entry.length = len(payload)
        entry.start_block = start // entry.block_size if entry.block_size else start
        _encode_pck_entry(out, entry)
        allocated.append((start, end))
        allocated.sort()
        append_cursor = max(append_cursor, end)
    return bytes(out)


def _add_pck_sound_entry(data: bytes, source_id: int, payload: bytes) -> bytes:
    source_id, payload = _source_id(source_id), bytes(payload)
    layout = parse_pck_layout(data)
    if layout is None:
        raise ValueError("Cannot add media to an invalid PCK")
    sound_table = next(table for table in layout.tables if table.kind == "sounds")
    if sound_table.size < 4 or sound_table.entry_size not in (0, 20):
        raise ValueError("This PCK sound table cannot accept a 32-bit media entry")
    if any(entry.entry_id == source_id for table in layout.tables for entry in table.entries):
        raise ValueError(f"PCK entry {source_id} already exists")

    entry_size = sound_table.entry_size or 20
    insert_at = sound_table.offset + sound_table.size
    block_sizes = [entry.block_size or 1 for table in layout.tables for entry in table.entries]
    alignment = math.lcm(*block_sizes) if block_sizes else 1
    if alignment > 0x100000:
        raise ValueError(f"PCK media alignment {alignment} is unreasonably large")
    shift = _align_up(entry_size + len(payload), alignment)
    media_span = shift - entry_size
    new_blob_base = layout.blob_base + entry_size

    header = bytearray(data[: layout.blob_base])
    header[insert_at:insert_at] = b"\0" * entry_size
    struct.pack_into("<I", header, 4, layout.header_size + entry_size)
    struct.pack_into("<I", header, 20, sound_table.size + entry_size)
    struct.pack_into("<I", header, sound_table.offset, len(sound_table.entries) + 1)

    for table in layout.tables:
        for original in table.entries:
            entry = replace(original)
            if entry.record_offset >= insert_at:
                entry.record_offset += entry_size
            if entry.offset >= layout.blob_base:
                entry.offset += shift
                if entry.block_size and entry.offset % entry.block_size:
                    raise ValueError(f"PCK entry {entry.entry_id} lost block alignment")
                entry.start_block = entry.offset // entry.block_size if entry.block_size else entry.offset
            _encode_pck_entry(header, entry)

    new_entry = PckEntry(
        entry_id=source_id,
        block_size=1,
        length=len(payload),
        start_block=new_blob_base,
        language_id=0,
        offset=new_blob_base,
        table_kind="sounds",
        entry_size=entry_size,
        record_offset=insert_at,
    )
    _encode_pck_entry(header, new_entry)
    return bytes(header) + payload + b"\0" * (media_span - len(payload)) + data[layout.blob_base :]


def _align_up(value: int, alignment: int) -> int:
    if alignment <= 1:
        return value
    return (value + alignment - 1) // alignment * alignment

__all__ = [
    "PckEntry",
    "export_non_streaming_pck",
    "parse_pck_layout",
    "rewrite_pck",
]
