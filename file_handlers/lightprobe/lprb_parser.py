from __future__ import annotations

import struct

import numpy as np

from .data import LprbData


def parse_lprb_v6(data: bytes) -> LprbData:
    if len(data) < 16 or data[:4] != b"NPRB":
        raise ValueError("LPRB data is not an NPRB v6 payload")
    probe_count = struct.unpack_from("<I", data, 4)[0]
    probe_data_size = struct.unpack_from("<I", data, 8)[0]
    payload_start = 16
    payload_end = payload_start + probe_data_size
    if payload_end > len(data):
        raise ValueError("LPRB payload is truncated")

    offset_table_bytes = probe_count * 4
    if probe_data_size < offset_table_bytes:
        raise ValueError("LPRB offset table is truncated")
    offsets = struct.unpack_from(f"<{probe_count}I", data, payload_start)
    terms = np.zeros((probe_count, 12, 3), dtype=np.float32)
    for probe_index, offset in enumerate(offsets):
        absolute = payload_start + offset
        if absolute + 48 > payload_end:
            raise ValueError(f"LPRB probe {probe_index} record is outside the payload")
        words = struct.unpack_from("<12I", data, absolute)
        for term_index, packed in enumerate(words):
            terms[probe_index, term_index] = _decode_packed_probe_rgb(packed)
    return LprbData(probe_count=int(probe_count), terms_rgb=terms)


def _decode_packed_probe_rgb(packed: int) -> tuple[float, float, float]:
    r_bits = ((packed << 4) & 0xFFFF) & 0x7FFF
    g_bits = (packed >> 7) & 0x7FFF
    b_bits = (packed >> 17) & 0x7FFF
    return _half_to_float(r_bits), _half_to_float(g_bits), _half_to_float(b_bits)


def _half_to_float(bits: int) -> float:
    sign = -1.0 if bits & 0x8000 else 1.0
    exp = (bits >> 10) & 0x1F
    mant = bits & 0x03FF
    if exp == 0:
        if mant == 0:
            return -0.0 if sign < 0.0 else 0.0
        return sign * (mant / 1024.0) * (2.0 ** -14)
    if exp == 0x1F:
        return sign * float("inf") if mant == 0 else float("nan")
    return sign * (1.0 + (mant / 1024.0)) * (2.0 ** (exp - 15))
