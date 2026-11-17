"""See docs/internals/gui-rendering.md, Text animation."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from file_handlers.native_math import fadd, fdiv, f32, fsub

from ..dmc5.text import TextLayoutGap
from ..errors import GuiAssetError

_MASK = 0xFFFFFFFF


def seeded(seed: int, rule: Mapping[str, Any]) -> list[int]:
    first, second, third = rule["shifts"]
    held, w, made = (seed ^ rule["seed"]) & _MASK, rule["start"], []
    for _ in range(rule["rounds"]):
        held ^= rule["mix"] >> (held & 3)
        held ^= ((held >> 1 ^ held) >> 3) ^ ((held * 2 ^ held) << 3 & _MASK)
        mixed = (held ^ held << first) & _MASK
        w = w ^ w >> second ^ mixed ^ mixed >> third
        made.append(w)
    return made[-4:]


class SharedRandom:
    def __init__(self, state: Iterable[int], shifts) -> None:
        self.state, self._shifts = list(state), tuple(shifts)

    def draw(self) -> int:
        (x, y, z, w), (first, second, third) = self.state, self._shifts
        mixed = (x ^ x << first) & _MASK
        self.state = [y, z, w, w ^ w >> second ^ mixed ^ mixed >> third]
        return self.state[3]


class Flicker:
    def __init__(self, fields: Mapping[str, Any], rule: Mapping[str, Any]) -> None:
        self.rule, self.time, self.count, self.ended, self.shown = rule, 0.0, 0.0, False, None
        self.max_time, self.interval = f32(fields[rule["max_time"]]), f32(fields[rule["interval"]])
        self.blink, self.blink_value = bool(fields[rule["blink"]]), int(fields[rule["blink_value"]])

    def start(self, words: int) -> None:
        self.shown, self.ended, self.time = [False] * words, False, 0.0

    def update(self, frames: float) -> None:
        if not self.ended:
            self.time, self.count = fadd(self.time, frames), fadd(self.count, frames)

    def _chance(self, random: SharedRandom) -> float:
        return f32(f32(random.draw()) * self.rule["scale"])

    def draw(self, sprites: int, random: SharedRandom) -> list[int | None]:
        if self.ended and not self.blink:
            return [None] * sprites
        if hit := self.interval <= self.count:
            self.count = fsub(self.count, self.interval)
        threshold, alphas = self.rule["threshold"], []
        for at in range(sprites):
            if at >= len(self.shown):
                alphas.append(None)
            elif self.ended:
                alphas.append(self.blink_value if self._chance(random) < threshold else None)
            else:
                if not self.max_time:
                    raise TextLayoutGap("its text animation's maximum time is 0: the game's script throws")
                progress = fdiv(self.time, self.max_time)
                if hit:
                    self.shown[at] = (not self.shown[at] or self._chance(random) < progress if progress >= threshold
                                      else not self.shown[at] and self._chance(random) < progress)
                alphas.append(None if self.shown[at] else 0)
        if not self.ended and self.max_time <= self.time:
            self.time, self.ended = self.max_time, True
        return alphas


@dataclass(slots=True)
class _Entry:
    flicker: Flicker | None = None
    frames: float = 0.0
    before: float = 0.0
    restart: bool = True
    alphas: tuple[int | None, ...] = ()
    drawn: int | None = None


class TextAnimations:
    def __init__(self, profile, load: Callable[[str], tuple[str, Mapping[str, Any]]], seed: int | None = None) -> None:
        self.profile, self.rule, self._load, self.serial = profile, profile.text_animation, load, 0
        self.random = SharedRandom(seeded(time.perf_counter_ns() if seed is None else seed, self.rule["random"]),
                                   self.rule["random"]["shifts"])
        self._held: dict[str, _Entry] = {}

    def active(self, node) -> bool:
        properties = node.render_properties
        return bool(properties.get(self.rule["enable"]) and properties.get(self.rule["resource"]))

    def gap(self, node) -> str | None:
        try:
            kind, _fields = self._load(node.render_properties[self.rule["resource"]])
        except GuiAssetError as error:
            return str(error)
        if kind != self.rule["flicker"]["class"]:
            return f"its text animation {kind} follows a game script's state: not drawn"
        command = self.profile.enum_label(node.render_properties, *self.rule["command"])
        return None if command in self.rule["starts"] else f"its text animation command {command} is not ported"

    def begin(self, nodes) -> None:
        self._held.update({node.path: _Entry() for node in nodes if self.active(node)})

    def advance(self, frames: float) -> None:
        for entry in self._held.values():
            entry.frames += frames
        self.serial += 1

    def assigned(self, assignments) -> None:
        name, enum = self.rule["command"]
        for item in assignments:
            if item.field_name == name and self.profile.enum_label(
                    {name: item.value}, name, enum) == self.rule["restart"]:
                entry = self._held.setdefault(item.path, _Entry())
                entry.before, entry.frames, entry.restart = entry.before + entry.frames, 0.0, True
                self.serial += 1

    def alphas(self, node, sprites: int, words: int) -> tuple[int | None, ...]:
        entry = self._held.setdefault(node.path, _Entry())
        if entry.flicker is None:
            entry.flicker = Flicker(self._load(node.render_properties[self.rule["resource"]])[1], self.rule["flicker"])
        if entry.drawn != self.serial:
            if entry.restart:
                entry.flicker.update(entry.before)
                entry.flicker.start(words)
                entry.restart, entry.before = False, 0.0
            entry.flicker.update(entry.frames)
            entry.frames, entry.drawn = 0.0, self.serial
            entry.alphas = tuple(entry.flicker.draw(sprites, self.random))
        return entry.alphas
