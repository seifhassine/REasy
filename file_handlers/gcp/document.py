"""See docs/internals/gcp-format.md, Editing."""
from __future__ import annotations

import json
import re
import uuid
from collections import defaultdict
from functools import cache

from PySide6.QtCore import QObject

from file_handlers.document_commands import Change, EditDocument, ReplaceContents, SetFields
lazy from utils.app_paths import resource_path
from utils.hash_util import murmur3_hash_utf16le

from .gcp_file import PATTERNS, ColorPreset, GcpFile, read_gcp, write_gcp

_HASH = re.compile(r"0x([0-9a-fA-F]{1,8})")


@cache
def known_names() -> dict[int, str]:
    with open(resource_path("resources/data/gcp/names.json"), encoding="utf-8") as handle:
        return {int(name_hash, 16): name for name_hash, name in json.load(handle).items()}


class GcpDocument(EditDocument):
    def __init__(self, gcp: GcpFile, parent: QObject | None = None):
        super().__init__(parent)
        self.gcp, self.names = gcp, dict(known_names())

    @classmethod
    def read(cls, data: bytes, parent: QObject | None = None) -> GcpDocument:
        return cls(read_gcp(data), parent)

    def write(self) -> bytes:
        if problems := self.problems():
            raise ValueError("\n".join(problems))
        return write_gcp(self.gcp)

    @property
    def presets(self) -> list[ColorPreset]:
        return self.gcp.presets

    def name(self, preset: ColorPreset) -> str:
        return self.names.get(preset.name_hash, f"0x{preset.name_hash:08X}")

    def clashes(self, field: str) -> dict[object, list[int]]:
        rows = defaultdict(list)
        for row, preset in enumerate(self.presets):
            rows[getattr(preset, field)].append(row)
        return {value: held for value, held in rows.items() if len(held) > 1}

    def problems(self) -> list[str]:
        shared = self.tr("Presets {0} share a {1}: the game finds only the last.")
        return [shared.format(", ".join(map(str, rows)), what)
                for field, what in (("guid", "GUID"), ("name_hash", self.tr("name")))
                for rows in self.clashes(field).values()]

    def set_values(self, preset: ColorPreset, text: str, **values) -> None:
        for field in values.keys() & {"guid", "name_hash"}:
            if (row := next((row for row, held in enumerate(self.presets) if held is not preset and
                             getattr(held, field) == values[field]), None)) is not None:
                raise ValueError(self.tr("Preset {0} already has this {1}.").format(
                    row, "GUID" if field == "guid" else self.tr("name")))
        if any(getattr(preset, field) != value for field, value in values.items()):
            self.push(SetFields(self, [(preset, values)], text))

    def rename(self, preset: ColorPreset, text: str) -> None:
        if not (text := text.strip()):
            raise ValueError(self.tr("Type a name, or 0x and the name's hash."))
        if found := _HASH.fullmatch(text):
            name_hash = int(found.group(1), 16)
        else:
            self.names[name_hash := murmur3_hash_utf16le(text)] = text
        if name_hash != preset.name_hash:
            self.set_values(preset, self.tr("Rename preset"), name_hash=name_hash)
        else:
            self.changed.emit(Change(preset, "name_hash"))

    def new_presets(self, sources: list[ColorPreset | None]) -> list[ColorPreset]:
        taken, made = {preset.name_hash for preset in self.presets}, []
        for source in sources:
            name = "Preset" if source is None else self.names.get(source.name_hash, "Preset")
            stem, digits = re.fullmatch(r"(.*?)(\d*)", name).groups()
            number = int(digits or 0) + 1
            while (name_hash := murmur3_hash_utf16le(name := f"{stem}{number:0{len(digits)}d}")) in taken:
                number += 1
            taken.add(name_hash)
            self.names[name_hash] = name
            colors = [(255, 255, 255, 255)] * PATTERNS[self.gcp.version] if source is None else list(source.colors)
            made.append(ColorPreset(uuid.uuid4(), name_hash, colors, source is not None and source.enable_alpha))
        return made

    def insert(self, row: int, presets: list[ColorPreset], text: str) -> None:
        self.push(ReplaceContents(self, self.gcp, "presets", [*self.presets[:row], *presets, *self.presets[row:]],
                                  text))

    def remove(self, presets: list[ColorPreset]) -> None:
        gone = set(map(id, presets))
        self.push(ReplaceContents(self, self.gcp, "presets", [held for held in self.presets if id(held) not in gone],
                                  self.tr("Delete presets")))

    def move(self, presets: list[ColorPreset], step: int) -> None:
        order, moving = list(self.presets), set(map(id, presets))
        rows = [row for row, held in enumerate(order) if id(held) in moving]
        if not rows or not (0 <= rows[0] + step and rows[-1] + step < len(order)):
            return
        for row in rows if step < 0 else reversed(rows):
            order[row], order[row + step] = order[row + step], order[row]
        self.push(ReplaceContents(self, self.gcp, "presets", order, self.tr("Move presets")))
