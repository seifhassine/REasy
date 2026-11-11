"""See docs/internals/engine-buffers.md, The frame clock."""
from __future__ import annotations

import struct
from collections.abc import Mapping
from dataclasses import dataclass

lazy import numpy as np

from file_handlers.build_catalog import catalog_reader
lazy from file_handlers.engine_math import crt
lazy from file_handlers.sdf.dxc import enrich_dxil_shader

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/frame_clock"
_WORD = 0xFFFFFFFF


class FrameClockCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Timer:
    word: int
    per_second: float | None
    period: float
    subtract: bool = False
    fraction: bool = False


@dataclass(frozen=True, slots=True)
class FrameClockCatalog:
    game: str
    resource_version: int
    buffer: str
    time: str
    day: int
    scale: float
    frame: str
    odd: str
    parameters: str
    timers: tuple[Timer, ...]
    mirrors: Mapping[str, int]

    @property
    def fields(self) -> tuple[str, ...]:
        return self.time, self.frame, self.odd, self.parameters, *self.mirrors


def catalog_from_dict(data: Mapping) -> FrameClockCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise FrameClockCatalogError(f"frame clock catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    timers = tuple(Timer(int(item["word"]), None if item.get("per_second") is None else float(item["per_second"]),
                         float(item["period"]), bool(item.get("subtract", False)), bool(item.get("fraction", False)))
                   for item in data["timers"])
    catalog = FrameClockCatalog(
        str(data["game"]), int(data["resource_version"]), str(data["buffer"]), str(data["time"]), int(data["day"]),
        float(data["scale"]), str(data["frame"]), str(data["odd"]), str(data["parameters"]), timers,
        {str(name): int(word) for name, word in data.get("mirrors", {}).items()})
    words = [timer.word for timer in timers]
    if len(set(words)) != len(words) or any(word < 0 or timer.period <= 0 for word, timer in zip(words, timers)) \
            or any(timer.per_second is None and not timer.period.is_integer() for timer in timers) \
            or catalog.day <= 0 or len(set(catalog.fields)) != len(catalog.fields):
        raise FrameClockCatalogError("the frame clock's timers must each have a word of their own and a period above "
                                     "zero (whole for frame counts), its day must be above zero and its fields apart")
    return catalog


frame_clock_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, FrameClockCatalogError,
                                         "frame clock")


class FrameClock:
    def __init__(self, catalog: FrameClockCatalog):
        self.catalog = catalog
        self.time = 0
        self.frame = 0
        self._begun = False
        self.values: list[float | int] = [0 if timer.per_second is None else 0.0 for timer in catalog.timers]

    def drawn(self, time: int, frame: int, values) -> None:
        self.time, self.frame, self.values, self._begun = time, frame, list(values), True

    def step(self, seconds: float) -> None:
        catalog = self.catalog
        elapsed = float(np.float32(seconds))
        milliseconds = float(np.float32(seconds * 1000.0))
        self.time = (self.time + int(float(np.float32(catalog.scale)) * milliseconds)) & _WORD
        if self.time >= catalog.day:
            self.time = (self.time - catalog.day) & _WORD
        if self._begun:
            self.frame = (self.frame + 1) & _WORD
        self._begun = True
        for index, timer in enumerate(catalog.timers):
            if timer.per_second is None:
                self.values[index] = (self.values[index] + 1) % int(timer.period)
            else:
                total, period = float(np.float32(elapsed * timer.per_second + self.values[index])), float(
                    np.float32(timer.period))
                self.values[index] = (float(np.float32(total - period)) if total > period else total) if (
                    timer.subtract) else crt("fmodf")(total, period)

    def parameter_words(self, count: int) -> tuple[int, ...]:
        words = [0] * count
        for timer, value in zip(self.catalog.timers, self.values):
            if timer.word < count:
                words[timer.word] = struct.unpack("<I", struct.pack("<f", float(value) / float(np.float32(
                    timer.period)) if timer.fraction else float(value)))[0]
        return tuple(words)

    def words(self, count: int) -> dict[str, dict[str, tuple[int, ...]]]:
        catalog = self.catalog
        parameters = self.parameter_words(count)
        fields = {catalog.time: (self.time,), catalog.frame: (self.frame,), catalog.odd: (self.frame & 1,),
                  catalog.parameters: parameters}
        fields.update((name, (parameters[word],)) for name, word in catalog.mirrors.items() if word < count)
        return {catalog.buffer: fields}

    def key(self) -> tuple:
        return self.time, self.frame, *self.values


def clock_words(clock: FrameClock | None, name: str, layout) -> Mapping[str, tuple[int, ...]]:
    if clock is None or name != clock.catalog.buffer:
        return {}
    held = next((variable for variable in layout.variables if variable.name == clock.catalog.parameters), None)
    return clock.words(0 if held is None else held.byte_size // 4)[name]


def follows_clock(clock: FrameClock | None, name: str) -> bool:
    return clock is not None and name == clock.catalog.buffer


def reads_running(catalog: FrameClockCatalog, program) -> bool:
    running = {catalog.time, catalog.parameters}
    for shader in program.shaders:
        if not shader.present or shader.binary_format != "dxbc-container" or shader.reflection is None:
            continue
        dxbc = shader.reflection
        if dxbc.reflection is None:
            dxbc = enrich_dxil_shader(dxbc, bytes(shader.payload))
        if dxbc.reflection is not None and any(
                buffer.name == catalog.buffer and any(variable.flags & 2 and variable.name in running
                                                      for variable in buffer.variables)
                for buffer in dxbc.reflection.constant_buffers):
            return True
    return False


__all__ = ["FrameClock", "FrameClockCatalog", "FrameClockCatalogError", "Timer", "catalog_from_dict", "clock_words",
           "follows_clock", "frame_clock_catalog_for", "reads_running"]
