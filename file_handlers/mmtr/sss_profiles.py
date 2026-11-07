"""See docs/internals/light-passes.md, Subsurface profiles."""
from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from dataclasses import dataclass

lazy import numpy as np

from file_handlers.build_catalog import catalog_reader
lazy from file_handlers.engine_math import crt, flushed_denormals

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/sss_profiles"
# Magic, version, record count.
_HEADER = struct.Struct("<3I")
HEADER_SIZE = 16
# A render config entry: four-character key, value.
_ENTRY = struct.Struct("<4sI")


class SssProfileCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SssClamp:
    words: tuple[int, ...]
    least: float
    most: float
    unordered: float


@dataclass(frozen=True, slots=True)
class SssFill:
    record: str
    start: int | str
    end: int | str


@dataclass(frozen=True, slots=True)
class SssConfig:
    path: str
    section: bytes
    key: bytes
    least: int


@dataclass(frozen=True, slots=True)
class SssPacking:
    slots: int
    words: tuple[int | tuple[int, int] | None, ...]
    fills: tuple[SssFill, ...]


@dataclass(frozen=True, slots=True)
class SssBake:
    slots: int
    slot: int
    half: int
    fills: tuple[SssFill, ...]
    falloff: tuple[int, int, int]
    mix: tuple[int, int, int]
    width: int
    ratio: int
    falloff_min: float | None
    range: float
    exponent: float
    area: float
    variance: float
    norm: float


@dataclass(frozen=True, slots=True)
class SssProfileCatalog:
    game: str
    resource_version: int
    path: str
    magic: int
    version: int
    record: int
    most: int
    clamp: SssClamp | None
    records: Mapping[str, tuple[float, ...]]
    config: SssConfig | None
    packing: SssPacking | None
    bake: SssBake | None
    flush_denormals: bool


def _word(value) -> int | tuple[int, int] | None:
    if isinstance(value, list):
        first, second = value
        return int(first), int(second)
    return None if value is None else int(value)


def _floats(values: Sequence) -> tuple[float, ...]:
    return tuple(float(value) for value in values)


def _end(value) -> int | str:
    return value if value in ("records", "config") else int(value)


def _fills(data: Sequence[Mapping]) -> tuple[SssFill, ...]:
    return tuple(SssFill(str(fill["record"]), _end(fill["from"]), _end(fill["to"])) for fill in data)


def _bake_from_dict(data: Mapping) -> SssBake:
    least = data.get("falloff_min")
    return SssBake(int(data["slots"]), int(data["slot"]), int(data["half"]), _fills(data["fills"]),
                   tuple(int(word) for word in data["falloff"]), tuple(int(word) for word in data["mix"]),
                   int(data["width"]), int(data["ratio"]), None if least is None else float(least),
                   float(data["range"]), float(data["exponent"]), float(data["area"]), float(data["variance"]),
                   float(data["norm"]))


def catalog_from_dict(data: Mapping) -> SssProfileCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise SssProfileCatalogError(f"subsurface profile catalog format {data.get('format')!r} is not "
                                     f"{CATALOG_FORMAT}")
    file, config, packing, bake = data["file"], data.get("config"), data.get("packing"), data.get("bake")
    clamp = file.get("clamp")
    catalog = SssProfileCatalog(
        str(data["game"]), int(data["resource_version"]), str(data["path"]), int(file["magic"]), int(file["version"]),
        int(file["record"]), int(file["most"]), None if clamp is None else SssClamp(
            tuple(int(word) for word in clamp["words"]), float(clamp["least"]), float(clamp["most"]),
            float(clamp["unordered"])), {str(name): _floats(record) for name, record in data["records"].items()},
        None if config is None else SssConfig(str(config["path"]), str(config["section"]).encode("ascii"),
                                              str(config["key"]).encode("ascii"), int(config["least"])),
        None if packing is None else SssPacking(int(packing["slots"]), tuple(_word(word) for word in packing["words"]),
                                                _fills(packing["fills"])),
        None if bake is None else _bake_from_dict(bake), bool(data.get("flush_denormals", False)))
    words = catalog.record // 4
    used = [] if catalog.clamp is None else [*catalog.clamp.words]
    fills = []
    if catalog.packing is not None:
        used += [index for word in catalog.packing.words if word is not None
                 for index in (word if isinstance(word, tuple) else (word,))]
        fills += [(fill, catalog.packing.slots) for fill in catalog.packing.fills]
    if bake is not None:
        bake = catalog.bake
        used += [*bake.falloff, *bake.mix, bake.width, bake.ratio]
        fills += [(fill, bake.slots) for fill in bake.fills]
        if (2 * bake.half + 1) * 16 > bake.slot or bake.slot % 4:
            raise SssProfileCatalogError("the subsurface profile catalog's kernels do not fit their slots")
    if catalog.record % 4 or any(len(record) != words for record in catalog.records.values()) \
            or any(not 0 <= word < words for word in used) or any(
            fill.record not in catalog.records or "config" in (fill.start, fill.end) and catalog.config is None
            or any(isinstance(end, int) and not 0 <= end <= slots for end in (fill.start, fill.end))
            for fill, slots in fills):
        raise SssProfileCatalogError("the subsurface profile catalog's words or fills fall outside its records or "
                                     "slots")
    return catalog


sss_profile_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, SssProfileCatalogError,
                                         "subsurface profile")


def _records(data: bytes, catalog: SssProfileCatalog) -> np.ndarray | None:
    if len(data) < HEADER_SIZE:
        return None
    magic, version, count = _HEADER.unpack_from(data)
    count = min(count, catalog.most)
    if magic != catalog.magic or version != catalog.version or len(data) < HEADER_SIZE + count * catalog.record:
        return None
    records = np.frombuffer(data, "<f4", count * catalog.record // 4, HEADER_SIZE).reshape(
        count, catalog.record // 4).copy()
    clamp = catalog.clamp
    for word in () if clamp is None else clamp.words:
        values = records[:, word]
        records[:, word] = np.where(np.isnan(values), np.float32(clamp.unordered),
                                    np.clip(values, np.float32(clamp.least), np.float32(clamp.most)))
    return records


def config_count(data: bytes | None, config: SssConfig) -> int:
    entries = [] if data is None else [_ENTRY.unpack_from(data, at) for at in range(0, len(data) - 7, 8)]
    sections = [index for index, (key, _value) in enumerate(entries) if key == config.section]
    found = 0
    if sections:
        first = sections[-1] + 1
        found = next((value for key, value in entries[first:first + entries[first - 1][1]] if key == config.key), 0)
    return max(config.least, found)


def _placed(records: np.ndarray, fills: Sequence[SssFill], catalog: SssProfileCatalog, slots: int,
            config: bytes | None) -> list[np.ndarray | None]:
    count = (config_count(config, catalog.config) if catalog.config is not None else None)
    ends = {"records": len(records), "config": count}
    placed: list[np.ndarray | None] = [*records[:slots], *[None] * max(0, slots - len(records))]
    for fill in fills:
        record = np.array(catalog.records[fill.record], np.float32)
        start, end = (min(slots, ends.get(at, at)) for at in (fill.start, fill.end))
        for slot in range(start, end):
            placed[slot] = record
    return placed


def _float_mode(catalog: SssProfileCatalog):
    return flushed_denormals() if catalog.flush_denormals else nullcontext()


def packed_profiles(data: bytes, catalog: SssProfileCatalog, config: bytes | None = None) -> bytes | None:
    records, packing = _records(data, catalog), catalog.packing
    if records is None or packing is None:
        return None
    slots = np.zeros((packing.slots, len(packing.words)), "<f4")
    with _float_mode(catalog):
        for slot, record in enumerate(_placed(records, packing.fills, catalog, packing.slots, config)):
            if record is not None:
                slots[slot] = [0.0 if word is None else record[word] if isinstance(word, int)
                               else record[word[0]] * record[word[1]] for word in packing.words]
    return slots.tobytes()


def _gaussian(variances: np.ndarray, r: np.float32, bake: SssBake) -> np.ndarray:
    f = np.float32
    return np.array([(f(1.0) / np.sqrt(variance * f(bake.norm)))
                     * f(crt("expf")(((-r) * r) / (variance * f(bake.variance)))) for variance in variances], f)


def _kernel(record: np.ndarray, bake: SssBake) -> np.ndarray:
    f = np.float32
    width, ratio, exponent = record[bake.width], record[bake.ratio], f(bake.exponent)
    count = 2 * bake.half + 1
    step = (width * f(bake.range)) / f(2 * bake.half)
    kernel = np.zeros((count, 4), f)
    for index in range(count):
        offset = f(index) * step - width
        sign = f(-1.0) if f(0.0) > offset else f(1.0)
        kernel[index, 3] = (np.abs(f(crt("powf")(offset, exponent))) * (width * sign)) / f(
            crt("powf")(width, exponent))
    colour = record[list(bake.falloff)]
    falloff = width * (colour if bake.falloff_min is None else np.maximum(f(bake.falloff_min), colour))
    mix = record[list(bake.mix)]
    narrow, wide = falloff * falloff, (falloff * ratio) * (falloff * ratio)
    total = np.zeros(3, f)
    for index in range(count):
        before = np.abs(kernel[index, 3] - kernel[index - 1, 3]) if index else f(0.0)
        after = np.abs(kernel[index, 3] - kernel[index + 1, 3]) if index < count - 1 else f(0.0)
        r = kernel[index, 3]
        weights = (mix * _gaussian(wide, r, bake) + (f(1.0) - mix) * _gaussian(narrow, r, bake)) * (
            (after + before) * f(bake.area))
        total = total + weights
        kernel[index, :3] = weights
    kernel[:, :3] = kernel[:, :3] * (f(1.0) / total)
    return np.concatenate([kernel[bake.half:bake.half + 1], kernel[:bake.half], kernel[bake.half + 1:]])


def baked_kernels(data: bytes, catalog: SssProfileCatalog, config: bytes | None = None) -> bytes | None:
    records, bake = _records(data, catalog), catalog.bake
    if records is None or bake is None:
        return None
    slots = np.zeros((bake.slots, bake.slot // 4), "<f4")
    size = (2 * bake.half + 1) * 4
    # A profile without falloff divides zero by zero, as the engine does.
    with np.errstate(divide="ignore", invalid="ignore"), _float_mode(catalog):
        for slot, record in enumerate(_placed(records, bake.fills, catalog, bake.slots, config)):
            if record is not None:
                slots[slot, :size] = _kernel(record, bake).reshape(-1)
    return slots.tobytes()


def written_bytes(data: bytes, catalog: SssProfileCatalog, kernels: bool, config: bytes | None = None) -> bytes | None:
    records, made = _records(data, catalog), catalog.bake if kernels else catalog.packing
    if records is None or made is None:
        return None
    if kernels:
        stride, floats = made.slot // 4, range((2 * made.half + 1) * 4)
    else:
        stride, floats = len(made.words), [index for index, word in enumerate(made.words) if word is not None]
    mask = np.zeros((made.slots, stride, 4), np.uint8)
    for slot, record in enumerate(_placed(records, made.fills, catalog, made.slots, config)):
        if record is not None:
            mask[slot, list(floats)] = 0xFF
    return mask.tobytes()


__all__ = ["SssBake", "SssClamp", "SssConfig", "SssFill", "SssPacking", "SssProfileCatalog", "SssProfileCatalogError",
           "baked_kernels", "catalog_from_dict", "config_count", "packed_profiles", "sss_profile_catalog_for",
           "written_bytes"]
