from __future__ import annotations

import struct

import numpy as np

from .data import LprbData


_HEADER_SIZE = 16
_PACKED_TERM_SIZE = 4
_V3_TERM_COUNT = 6
_V6_TERM_COUNT = 12


def parse_lprb(data: bytes, *, version: int | None = None) -> LprbData:
    header = _read_header(data)
    if version is None:
        probe_count, probe_data_size, _payload_start, _payload_end = header
        version = 3 if probe_data_size == probe_count * _V3_TERM_COUNT * _PACKED_TERM_SIZE else 6
    if version == 3:
        return _parse_v3_payload(data, header)
    if version == 6:
        return _parse_v6_payload(data, header)
    raise ValueError(f"Unsupported LPRB version: {version}")


def parse_lprb_v3(data: bytes) -> LprbData:
    """Parse direct 24-byte records ordered +X, +Z, +Y, -X, -Z, -Y."""
    return _parse_v3_payload(data, _read_header(data))


def parse_lprb_v6(data: bytes) -> LprbData:
    return _parse_v6_payload(data, _read_header(data))


def _read_header(data: bytes) -> tuple[int, int, int, int]:
    if len(data) < _HEADER_SIZE or data[:4] != b"NPRB":
        raise ValueError("LPRB data is not an NPRB payload")
    probe_count, probe_data_size = struct.unpack_from("<II", data, 4)
    payload_end = _HEADER_SIZE + probe_data_size
    if payload_end > len(data):
        raise ValueError("LPRB payload is truncated")
    return int(probe_count), int(probe_data_size), _HEADER_SIZE, payload_end


def _parse_v3_payload(
    data: bytes,
    header: tuple[int, int, int, int],
) -> LprbData:
    probe_count, probe_data_size, payload_start, _payload_end = header
    expected_size = probe_count * _V3_TERM_COUNT * _PACKED_TERM_SIZE
    if probe_data_size != expected_size:
        raise ValueError(
            f"LPRB v3 payload size is {probe_data_size}, expected {expected_size} "
            f"for {probe_count} probes"
        )
    words = np.frombuffer(
        data,
        dtype="<u4",
        count=probe_count * _V3_TERM_COUNT,
        offset=payload_start,
    ).reshape(probe_count, _V3_TERM_COUNT)
    return LprbData(probe_count=probe_count, terms_rgb=_decode_packed_probe_words(words))


def _parse_v6_payload(
    data: bytes,
    header: tuple[int, int, int, int],
) -> LprbData:
    probe_count, probe_data_size, payload_start, payload_end = header
    offset_table_bytes = probe_count * 4
    if probe_data_size < offset_table_bytes:
        raise ValueError("LPRB offset table is truncated")

    offsets = np.frombuffer(data, dtype="<u4", count=probe_count, offset=payload_start)
    words = np.empty((probe_count, _V6_TERM_COUNT), dtype=np.uint32)
    record_size = _V6_TERM_COUNT * _PACKED_TERM_SIZE
    for probe_index, offset in enumerate(offsets):
        absolute = payload_start + int(offset)
        if absolute < payload_start + offset_table_bytes or absolute + record_size > payload_end:
            raise ValueError(f"LPRB probe {probe_index} record is outside the payload")
        words[probe_index] = np.frombuffer(
            data,
            dtype="<u4",
            count=_V6_TERM_COUNT,
            offset=absolute,
        )
    return LprbData(probe_count=probe_count, terms_rgb=_decode_packed_probe_words(words))


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
