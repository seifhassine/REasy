"""See docs/internals/meshlets.md, LODs."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import struct

lazy import numpy as np

lazy from file_handlers.engine_math import half_bits
lazy from utils.resource_file_utils import resource_path_with_version

MAGIC = b"LOD\0"
# Mask bits 0-6: the entries for LOD counts 2-8; bit 31: isMeshletLodParameter.
_ENTRIES = 7
MESHLET_FLAG = 1 << 31
LANES = 8
THRESHOLD_FAMILIES = ("inverse_size", "half_reciprocal")
# "record_flag": only records flagged for meshlets are written, others get T_k = k (MH Wilds); "mesh_flag": a mesh
# whose header byte 0x19 has bit 1 set keeps its file's (DD2).
THRESHOLD_GATES = ("record_flag", "mesh_flag")


def lod_settings_file(path: str, version: int) -> str:
    return resource_path_with_version(path, "lod", version)


class LodSettingsError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class LodRecord:
    mask: int
    # By LOD count: e0, e1, then count - 3 weights (float32).
    entries: Mapping[int, tuple]

    @property
    def meshlet(self) -> bool:
        return bool(self.mask & MESHLET_FLAG)


@dataclass(frozen=True, slots=True)
class LodSettings:
    # By key, a mesh header's lod_hash (0: the default record, which no meshlet writer reads).
    records: Mapping[int, LodRecord]


def read_lod_settings(data: bytes) -> LodSettings:
    if len(data) < 12 or data[:4] != MAGIC:
        raise LodSettingsError("the file is not a LodSettings file")
    records, at = {}, 12
    for _record in range(struct.unpack_from("<I", data, 8)[0] + 1):
        if at + 12 > len(data):
            raise LodSettingsError("the LodSettings file ends inside a record")
        mask, _count, key = struct.unpack_from("<3I", data, at)
        at += 12
        entries = {}
        for bit in range(_ENTRIES):
            if mask >> bit & 1:
                floats = 2 + max(0, bit - 1)
                if at + 4 * floats > len(data):
                    raise LodSettingsError("the LodSettings file ends inside a record")
                entries[bit + 2] = tuple(np.frombuffer(data, "<f4", floats, at))
                at += 4 * floats
        if key in records:
            raise LodSettingsError(f"the LodSettings file holds key {key:#x} twice")
        records[key] = LodRecord(mask, frozendict(entries))
    if at != len(data):
        raise LodSettingsError(f"the LodSettings file's records take {at} of its {len(data)} bytes")
    return LodSettings(frozendict(records))


def _thresholds(family: str, entry, count: int) -> list:
    f32 = np.float32
    middle = range(2, count - 1)
    if family == "inverse_size":
        # Sizes 1, e1, weights between them toward e0, e0; T = max(1 / size - 1, 0).
        sizes = [f32(1)] + [entry[1]] * (count > 1) + [(entry[1] - entry[0]) * entry[k] + entry[0] for k in middle]
        if count >= 3:
            sizes.append(entry[0])
        return [max(f32(1) / size + f32(-1), f32(0)) for size in sizes[:count]]
    # 0, b, weights between b and a, a; a = 0.5 / e0 and b = 0.5 / e1 unless not over 0.
    a, b = f32(0.5) / entry[0], f32(0.5) / entry[1]
    a, b = a if a > 0 else f32(0), b if b > 0 else f32(0)
    values = [f32(0)] + [b] * (count > 1) + [(b - a) * entry[k] + a for k in middle]
    if count >= 3:
        values.append(a)
    return values[:count]


def meshlet_thresholds(settings: LodSettings, family: str, gate: str | None, lod_hash: int, count: int,
                       mesh_flags: int, halves: np.ndarray) -> np.ndarray:
    """The 8 half-float thresholds (bits) a plain meshlet mesh's blob holds on the GPU, as the build's writer sets them
    when the mesh loads; ``halves`` are its file's, kept where the writer writes none. ``mesh_flags`` is the mesh
    header's byte 0x19."""
    if family not in THRESHOLD_FAMILIES or gate is not None and gate not in THRESHOLD_GATES:
        raise ValueError(f"no meshlet threshold writer {family!r} gated by {gate!r}")
    if not 1 <= count <= LANES:
        raise ValueError(f"a meshlet mesh of {count} LODs")
    record = settings.records.get(lod_hash) if lod_hash else None
    entry = None if record is None else record.entries.get(max(count, 2))
    written = np.zeros(LANES, "<u2")
    if gate == "record_flag" and (entry is None or not record.meshlet):
        written[:count] = [half_bits(lane) for lane in range(count)]
        return written
    if entry is None or gate == "mesh_flag" and mesh_flags & 2:
        return np.asarray(halves, "<u2").copy()
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        written[:count] = [half_bits(value) for value in _thresholds(family, entry, count)]
    return written


@dataclass(frozen=True, slots=True)
class ClassicLevels:
    centre: tuple[float, float, float]
    count: int
    scale: float
    rules: Mapping[str, tuple[object, tuple[float, ...]]]


def classic_table(settings: LodSettings, lod_hash: int, count: int, radius: float, rate: float, rate2: float,
                  size: float, exponent: float) -> tuple[float, tuple[float, ...]]:
    f32 = np.float32
    record = settings.records.get(lod_hash) if lod_hash else None
    if record is None or max(count, 2) not in record.entries:
        record = settings.records.get(0)
    if record is None or max(count, 2) not in record.entries:
        raise LodSettingsError(f"the LodSettings file has no default record for {count} LODs")
    entry = [f32(value) for value in record.entries[max(count, 2)]]
    scale = f32(np.power(f32(radius) * f32(size), f32(rate) * f32(exponent), dtype=f32)) * f32(rate2)
    near, far = scale * entry[0], scale * entry[1]
    thresholds = [f32(0)] * max(count - 1, 0)
    if count > 2:
        thresholds[count - 2] = f32(1) / near
    for k in range(count - 2, 1, -1):
        thresholds[k - 1] = f32(1) / ((far - near) * entry[k] + near)
    if count > 1:
        thresholds[0] = f32(1) / far
    return float(f32(-1) / f32(radius)), tuple(float(value) for value in thresholds)


def classic_picks(metrics: np.ndarray, thresholds: np.ndarray) -> np.ndarray:
    passed = thresholds < metrics.astype(np.float32)[:, None]
    return np.where(passed.any(axis=1), passed.shape[1] - np.argmax(passed[:, ::-1], axis=1), 0).astype(np.int64)


def classic_fractions(metrics: np.ndarray, thresholds: np.ndarray) -> np.ndarray:
    f = np.float32
    metrics, thresholds = metrics.astype(f), np.asarray(thresholds, f)
    last = thresholds.shape[1]
    picked = classic_picks(metrics, thresholds)
    rows = np.arange(len(metrics))
    low = thresholds[rows, np.maximum(picked - 1, 0)]
    high = thresholds[rows, np.minimum(picked, last - 1)]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = ((metrics - low) / (high - low)).astype(f) + picked.astype(f)
        below = (metrics / thresholds[:, 0]).astype(f)
    return np.where(picked == last, f(last), np.where(picked > 0, between, below)).astype(f)


__all__ = ["ClassicLevels", "LANES", "LodRecord", "LodSettings", "LodSettingsError", "MAGIC", "MESHLET_FLAG",
           "THRESHOLD_FAMILIES", "THRESHOLD_GATES", "classic_fractions", "classic_picks", "classic_table",
           "lod_settings_file", "meshlet_thresholds", "read_lod_settings"]
