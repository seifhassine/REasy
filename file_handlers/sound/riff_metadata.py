"""Read and rewrite Wwise-compatible RIFF loop and marker metadata."""

from __future__ import annotations

import struct
from dataclasses import dataclass


class RiffMetadataInheritanceError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class RiffLoop:
    start_sample: int
    end_sample: int
    play_count: int = 0
    loop_type: int = 0
    cue_id: int = 0


@dataclass(slots=True, frozen=True)
class RiffMarker:
    cue_id: int
    sample_offset: int
    label: str = ""


@dataclass(slots=True, frozen=True)
class RiffMetadata:
    sample_rate: int | None = None
    sample_count: int | None = None
    loops: tuple[RiffLoop, ...] = ()
    markers: tuple[RiffMarker, ...] = ()


@dataclass(slots=True, frozen=True)
class _Chunk:
    chunk_id: bytes
    payload: bytes


def read_riff_metadata(data: bytes) -> RiffMetadata:
    chunks = _read_chunks(data)
    by_id: dict[bytes, list[bytes]] = {}
    for chunk in chunks:
        by_id.setdefault(chunk.chunk_id, []).append(chunk.payload)
    fmt = next(iter(by_id.get(b"fmt ", ())), b"")
    sample_rate = struct.unpack_from("<I", fmt, 4)[0] if len(fmt) >= 8 else None
    sample_count = _sample_count(fmt, by_id)
    labels = _read_labels(by_id.get(b"LIST", ()))
    loops = _read_loops(next(iter(by_id.get(b"smpl", ())), b""))
    markers = _read_cues(next(iter(by_id.get(b"cue ", ())), b""), labels)
    return RiffMetadata(sample_rate, sample_count, loops, markers)


def write_riff_metadata(data: bytes, metadata: RiffMetadata) -> bytes:
    """Replace smpl/cue/adtl metadata and preserve every unrelated RIFF chunk."""

    chunks = _read_chunks(data)
    kept = [
        chunk
        for chunk in chunks
        if chunk.chunk_id not in (b"smpl", b"cue ")
        and not (chunk.chunk_id == b"LIST" and chunk.payload[:4] == b"adtl")
    ]
    inserted = []
    if metadata.loops:
        inserted.append(_Chunk(b"smpl", _pack_smpl(metadata)))
    if metadata.markers:
        inserted.append(_Chunk(b"cue ", _pack_cues(metadata.markers)))
        labels = [marker for marker in metadata.markers if marker.label]
        if labels:
            inserted.append(_Chunk(b"LIST", _pack_labels(labels)))

    out_chunks: list[_Chunk] = []
    added = False
    for chunk in kept:
        if chunk.chunk_id == b"data" and not added:
            out_chunks.extend(inserted)
            added = True
        out_chunks.append(chunk)
    if not added:
        out_chunks.extend(inserted)
    body = bytearray(b"WAVE")
    for chunk in out_chunks:
        body += chunk.chunk_id + struct.pack("<I", len(chunk.payload)) + chunk.payload
        if len(chunk.payload) & 1:
            body.append(0)
    return b"RIFF" + struct.pack("<I", len(body)) + bytes(body)


def inherit_riff_metadata(
    replacement_wav: bytes,
    original_wem: bytes,
) -> tuple[bytes, RiffMetadata]:
    """Inherit original loops/markers when the replacement defines none."""

    replacement = read_riff_metadata(replacement_wav)
    count = replacement.sample_count
    if any(
        item.start_sample < 0
        or item.end_sample < item.start_sample
        or (count is not None and item.end_sample >= count)
        for item in replacement.loops
    ):
        raise ValueError("Replacement WAV contains an invalid or out-of-range sample loop.")
    invalid_markers = any(
        item.sample_offset < 0
        or (count is not None and item.sample_offset > count)
        for item in replacement.markers
    )
    duplicate_markers = len({item.cue_id for item in replacement.markers}) != len(
        replacement.markers
    )
    if invalid_markers or duplicate_markers:
        raise ValueError("Replacement WAV contains invalid or duplicate cue markers.")
    original = read_riff_metadata(original_wem)
    if replacement.loops or replacement.markers:
        return replacement_wav, replacement
    need_loops = bool(original.loops)
    need_markers = bool(original.markers)
    if not (need_loops or need_markers):
        return replacement_wav, replacement
    if not replacement.sample_rate or replacement.sample_count is None:
        raise RiffMetadataInheritanceError("Replacement WAV duration is unavailable; original loop/marker metadata cannot be inherited.")
    if not original.sample_rate:
        raise RiffMetadataInheritanceError("Original WEM sample rate is unavailable; loop/marker metadata cannot be inherited safely.")

    def sample(value: int) -> int:
        return round(int(value) * replacement.sample_rate / original.sample_rate)

    loops = replacement.loops or tuple(
        RiffLoop(
            sample(item.start_sample), sample(item.end_sample), item.play_count,
            item.loop_type, item.cue_id,
        )
        for item in original.loops
    )
    markers = replacement.markers or tuple(
        RiffMarker(item.cue_id, sample(item.sample_offset), item.label)
        for item in original.markers
    )
    if any(item.end_sample >= count for item in loops):
        raise RiffMetadataInheritanceError(
            f"An inherited loop ends beyond the replacement's final sample ({count - 1})."
        )
    if any(item.sample_offset > count for item in markers):
        raise RiffMetadataInheritanceError("An inherited cue marker is beyond the replacement audio duration.")
    inherited = RiffMetadata(replacement.sample_rate, count, loops, markers)
    return write_riff_metadata(replacement_wav, inherited), inherited


def rescale_riff_metadata(metadata: RiffMetadata, sample_rate: int) -> RiffMetadata:
    """Move sample-addressed metadata to an authored output sample rate."""

    source_rate = int(metadata.sample_rate or 0)
    sample_rate = int(sample_rate)
    if source_rate <= 0 or sample_rate <= 0:
        raise ValueError("Loop/marker sample rates must be positive.")
    if source_rate == sample_rate:
        return metadata

    sample = lambda value: round(int(value) * sample_rate / source_rate)
    return RiffMetadata(
        sample_rate,
        sample(metadata.sample_count) if metadata.sample_count is not None else None,
        tuple(
            RiffLoop(
                sample(item.start_sample), sample(item.end_sample),
                item.play_count, item.loop_type, item.cue_id,
            )
            for item in metadata.loops
        ),
        tuple(
            RiffMarker(item.cue_id, sample(item.sample_offset), item.label)
            for item in metadata.markers
        ),
    )


def _read_chunks(data: bytes) -> list[_Chunk]:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Input is not a RIFF/WAVE file")
    declared_end = min(len(data), struct.unpack_from("<I", data, 4)[0] + 8)
    pos, chunks = 12, []
    while pos + 8 <= declared_end:
        chunk_id, size = data[pos : pos + 4], struct.unpack_from("<I", data, pos + 4)[0]
        pos += 8
        if pos + size > declared_end:
            raise ValueError(f"RIFF chunk {chunk_id!r} is truncated")
        chunks.append(_Chunk(chunk_id, data[pos : pos + size]))
        pos += size + (size & 1)
    return chunks


def _sample_count(fmt: bytes, by_id: dict[bytes, list[bytes]]) -> int | None:
    if len(fmt) >= 28 and struct.unpack_from("<H", fmt)[0] == 0xFFFF:
        return struct.unpack_from("<I", fmt, 24)[0]
    if len(fmt) < 16:
        return None
    _tag, channels, _rate, _byte_rate, block_align, bits = struct.unpack_from(
        "<HHIIHH", fmt
    )
    data_size = len(next(iter(by_id.get(b"data", ())), b""))
    if block_align:
        return data_size // block_align
    bytes_per_sample = channels * bits // 8
    return data_size // bytes_per_sample if bytes_per_sample else None


def _read_loops(payload: bytes) -> tuple[RiffLoop, ...]:
    if len(payload) < 36:
        return ()
    count = struct.unpack_from("<I", payload, 28)[0]
    if count > (len(payload) - 36) // 24:
        return ()
    return tuple(
        RiffLoop(start, end, play_count, loop_type, cue_id)
        for index in range(count)
        for cue_id, loop_type, start, end, _fraction, play_count in (
            struct.unpack_from("<IIIIII", payload, 36 + index * 24),
        )
    )


def _read_cues(payload: bytes, labels: dict[int, str]) -> tuple[RiffMarker, ...]:
    if len(payload) < 4:
        return ()
    count = struct.unpack_from("<I", payload)[0]
    if count > (len(payload) - 4) // 24:
        return ()
    return tuple(
        RiffMarker(cue_id, sample_offset or position, labels.get(cue_id, ""))
        for index in range(count)
        for cue_id, position, _data_id, _chunk_start, _block_start, sample_offset in (
            struct.unpack_from("<II4sIII", payload, 4 + index * 24),
        )
    )


def _read_labels(payloads) -> dict[int, str]:
    labels = {}
    for payload in payloads:
        if payload[:4] != b"adtl":
            continue
        pos = 4
        while pos + 8 <= len(payload):
            chunk_id, size = payload[pos : pos + 4], struct.unpack_from("<I", payload, pos + 4)[0]
            pos += 8
            if pos + size > len(payload):
                break
            if chunk_id == b"labl" and size >= 4:
                cue_id = struct.unpack_from("<I", payload, pos)[0]
                labels[cue_id] = payload[pos + 4 : pos + size].rstrip(b"\0").decode("utf-8", "replace")
            pos += size + (size & 1)
    return labels


def _pack_smpl(metadata: RiffMetadata) -> bytes:
    sample_rate = metadata.sample_rate or 0
    period = round(1_000_000_000 / sample_rate) if sample_rate else 0
    out = bytearray(struct.pack("<9I", 0, 0, period, 60, 0, 0, 0, len(metadata.loops), 0))
    for loop in metadata.loops:
        start, end = int(loop.start_sample), int(loop.end_sample)
        if start < 0 or end < start:
            raise ValueError("Loop sample range is invalid")
        out += struct.pack(
            "<6I", int(loop.cue_id) & 0xFFFFFFFF, int(loop.loop_type) & 0xFFFFFFFF,
            start, end, 0, int(loop.play_count) & 0xFFFFFFFF,
        )
    return bytes(out)


def _pack_cues(markers: tuple[RiffMarker, ...]) -> bytes:
    out = bytearray(struct.pack("<I", len(markers)))
    used = set()
    for marker in markers:
        cue_id, offset = int(marker.cue_id) & 0xFFFFFFFF, int(marker.sample_offset)
        if cue_id in used or offset < 0:
            raise ValueError("Cue IDs must be unique and sample offsets non-negative")
        used.add(cue_id)
        out += struct.pack("<II4sIII", cue_id, offset, b"data", 0, 0, offset)
    return bytes(out)


def _pack_labels(markers: list[RiffMarker]) -> bytes:
    out = bytearray(b"adtl")
    for marker in markers:
        payload = struct.pack("<I", int(marker.cue_id) & 0xFFFFFFFF) + str(marker.label).encode("utf-8") + b"\0"
        out += b"labl" + struct.pack("<I", len(payload)) + payload
        if len(payload) & 1:
            out.append(0)
    return bytes(out)
