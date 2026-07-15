from __future__ import annotations

import struct

import numpy as np

from .data import LprbData


_LEGACY_HEADER_SIZE = 16
_V8_HEADER_SIZE = 32
_PACKED_TERM_SIZE = 4
_V3_TERM_COUNT = 6
_ICOSAHEDRAL_TERM_COUNT = 12


def parse_lprb(data: bytes, *, version: int | None = None) -> LprbData:
    if version is None:
        version = _detect_version(data)
    if version not in (3, 4, 6, 8):
        raise ValueError(f"Unsupported LPRB version: {version}")
    header = _read_header(data, version=version)
    if version == 3:
        return _parse_direct_payload(data, header, version=3, term_count=_V3_TERM_COUNT)
    if version == 4:
        return _parse_direct_payload(
            data,
            header,
            version=4,
            term_count=_ICOSAHEDRAL_TERM_COUNT,
        )
    range_compression_ev = struct.unpack_from("<i", data, 0x1C)[0] if version == 8 else 0
    return _parse_offset_payload(
        data,
        header,
        version=version,
        range_compression_ev=range_compression_ev,
    )


def parse_lprb_v3(data: bytes) -> LprbData:
    """Parse direct 24-byte records ordered +X, +Z, +Y, -X, -Z, -Y."""
    return parse_lprb(data, version=3)


def parse_lprb_v4(data: bytes) -> LprbData:
    """Parse direct 48-byte records containing 12 icosahedral terms."""
    return parse_lprb(data, version=4)


def parse_lprb_v6(data: bytes) -> LprbData:
    return parse_lprb(data, version=6)


def parse_lprb_v8(data: bytes) -> LprbData:
    return parse_lprb(data, version=8)


def _detect_version(data: bytes) -> int:
    probe_count, probe_data_size = _read_header_prefix(data)
    if len(data) == _V8_HEADER_SIZE + probe_data_size:
        return 8
    if probe_data_size == probe_count * _V3_TERM_COUNT * _PACKED_TERM_SIZE:
        return 3
    if probe_data_size == probe_count * _ICOSAHEDRAL_TERM_COUNT * _PACKED_TERM_SIZE:
        return 4
    return 6


def _read_header_prefix(data: bytes) -> tuple[int, int]:
    if len(data) < 12 or data[:4] != b"NPRB":
        raise ValueError("LPRB data is not an NPRB payload")
    probe_count, probe_data_size = struct.unpack_from("<II", data, 4)
    return int(probe_count), int(probe_data_size)


def _read_header(data: bytes, *, version: int) -> tuple[int, int, int, int]:
    probe_count, probe_data_size = _read_header_prefix(data)
    payload_start = _V8_HEADER_SIZE if version == 8 else _LEGACY_HEADER_SIZE
    payload_end = payload_start + probe_data_size
    if payload_end > len(data):
        raise ValueError("LPRB payload is truncated")
    return probe_count, probe_data_size, payload_start, payload_end


def _parse_direct_payload(
    data: bytes,
    header: tuple[int, int, int, int],
    *,
    version: int,
    term_count: int,
) -> LprbData:
    probe_count, probe_data_size, payload_start, _payload_end = header
    expected_size = probe_count * term_count * _PACKED_TERM_SIZE
    if probe_data_size != expected_size:
        raise ValueError(
            f"LPRB v{version} payload size is {probe_data_size}, expected {expected_size} "
            f"for {probe_count} probes"
        )
    words = np.frombuffer(
        data,
        dtype="<u4",
        count=probe_count * term_count,
        offset=payload_start,
    ).reshape(probe_count, term_count)
    return LprbData(probe_count=probe_count, terms_rgb=_decode_packed_probe_words(words))


def _parse_offset_payload(
    data: bytes,
    header: tuple[int, int, int, int],
    *,
    version: int,
    range_compression_ev: int = 0,
) -> LprbData:
    probe_count, probe_data_size, payload_start, payload_end = header
    offset_table_bytes = probe_count * 4
    if probe_data_size < offset_table_bytes:
        raise ValueError("LPRB offset table is truncated")

    offsets = np.frombuffer(data, dtype="<u4", count=probe_count, offset=payload_start)
    words = np.empty((probe_count, _ICOSAHEDRAL_TERM_COUNT), dtype=np.uint32)
    record_size = _ICOSAHEDRAL_TERM_COUNT * _PACKED_TERM_SIZE
    for probe_index, offset in enumerate(offsets):
        absolute = payload_start + int(offset)
        if absolute < payload_start + offset_table_bytes or absolute + record_size > payload_end:
            raise ValueError(f"LPRB v{version} probe {probe_index} record is outside the payload")
        words[probe_index] = np.frombuffer(
            data,
            dtype="<u4",
            count=_ICOSAHEDRAL_TERM_COUNT,
            offset=absolute,
        )
    terms_rgb = _decode_packed_probe_words(words)
    if range_compression_ev:
        # Match the runtime decoding scale: each EV stop is a power of two.
        terms_rgb = np.ldexp(terms_rgb, range_compression_ev)
    return LprbData(
        probe_count=probe_count,
        terms_rgb=terms_rgb,
        range_compression_ev=range_compression_ev,
    )


def _decode_packed_probe_words(words: np.ndarray) -> np.ndarray:
    packed = np.asarray(words, dtype=np.uint32)
    half_bits = np.empty((*packed.shape, 3), dtype=np.uint16)
    half_bits[..., 0] = ((packed << 4) & 0x7FFF).astype(np.uint16)
    half_bits[..., 1] = ((packed >> 7) & 0x7FFF).astype(np.uint16)
    half_bits[..., 2] = ((packed >> 17) & 0x7FFF).astype(np.uint16)
    return half_bits.view(np.float16).astype(np.float32)


def _decode_packed_probe_rgb(packed: int) -> tuple[float, float, float]:
    decoded = _decode_packed_probe_words(np.asarray(packed, dtype=np.uint32))
    return tuple(float(component) for component in decoded)
