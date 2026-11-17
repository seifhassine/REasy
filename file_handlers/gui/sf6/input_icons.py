"""See docs/internals/gui-rendering.md, Script tags."""
from __future__ import annotations

import ctypes
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any


def key_name(scan: int) -> str:
    buffer = ctypes.create_unicode_buffer(0x20)
    length = ctypes.windll.user32.GetKeyNameTextW(scan << 16, buffer, 0x20)
    return buffer.value[:length]


def _filled(held: dict[str, Any]) -> bool:
    return any(held["split"]) or any(value for part, value in held.items() if part != "split")


@dataclass(frozen=True, slots=True)
class InputSymbols:
    rule: Mapping[str, Any]
    files: Mapping[str, Mapping[str, Any]]
    enum: Callable[[str], Mapping[str, int]]
    key_name: Callable[[int], str]
    keyboard: bool
    mode: int

    def text(self, kind: str, value: int) -> str:
        if kind == "guide":
            return self._guide(value)
        if (param := self._param(kind, value)) is None:
            return ""
        held = self._collect(self._held(), self.rule["params"][kind], param)
        return self._drawn(held, False) if _filled(held) else self.rule["blank"]

    def _record(self, kind: str, value: int) -> Mapping[str, Any] | None:
        file, field, limit = self.rule["lists"][kind]
        return next((row for row in self.files[file][self.rule["records"]] if row[field] == value),
                    None) if 0 < value <= limit else None

    def _param(self, kind: str, value: int) -> Mapping[str, Any] | None:
        found, target = self._record(kind, value), self.rule["params"][kind]
        return found if target == kind else found and self._record(target, found[self.rule["config"]])

    def _held(self) -> dict[str, Any]:
        return {"pad": [], "split": [0] * len(self.rule["split"]), "axes": 0, "keys": [], "mouse": 0, "mouse_axes": 0}

    def _add(self, held: dict, part: str, value: int) -> None:
        if not value:
            return
        if part == "pad" and (at := next((at for at, mask in enumerate(self.rule["split"]) if value & mask),
                                         None)) is not None:
            held["split"][at] |= value
        elif part in ("pad", "keys"):
            held[part].append(value)
        else:
            held[part] |= value

    def _key(self, row: Mapping[str, Any], fields: list[str]) -> int:
        if row[fields[0]] or len(fields) == 1 or not row[fields[1]]:
            return row[fields[0]]
        return next((key for button, key in self.rule["mouse_keys"] if button == row[fields[1]]), 0)

    def _preset(self, kind: str, config: int) -> list[int] | None:
        for preset in self.rule["presets"]:
            side = preset.get("keys" if self.keyboard else "pad")
            if preset["kind"] == kind and preset["range"][0] <= config <= preset["range"][1] and side:
                if config not in side["ids"]:
                    return None
                row = next((row for row in self.files[preset["file"]][side["records"][self.mode]]
                            if row[self.rule["config"]] == config), None)
                return [0 if row is None else self._key(row, fields) for fields in side["fields"]]
        return None

    def _collect(self, held: dict, kind: str, param: Mapping[str, Any]) -> dict:
        if (preset := self._preset(kind, param[self.rule["config"]])) is not None:
            for value in preset:
                self._add(held, "keys" if self.keyboard else "pad", value)
            return held
        fields = self.rule["fields"][kind]
        flags = self.enum(fields["flags"][1])
        for part in ("keys", "mouse", "mouse_axes") if self.keyboard else ("pad", "axes"):
            for field in fields.get(part, ()):
                if not param[fields["flags"][0]] & flags[field]:
                    self._add(held, part, param[field])
        return held

    def _guide(self, value: int) -> str:
        rule = self.rule["guide"]
        if (record := self._record("guide", value)) is None:
            return ""
        held, plus = self._held(), record[rule["plus"]]
        for field, kind in rule["ids"]:
            for item in record[field]:
                if (param := self._param(kind, item)) is not None:
                    self._collect(held, self.rule["params"][kind], param)
        text = self._drawn(held, plus) if _filled(held) else ""
        for child in record[rule["children"]]:
            text = (text + self.rule["plus"] if plus and text else text) + self._guide(child)
        return text

    def _link(self, text: str, part: str, value: int, plus: bool) -> str:
        links, field = self.rule["links"][part]
        if part == "pad":
            value = next((raw for button, raw in self.rule["raw"] if button == value), value)
        found = next((link for link in self.files["symbols"][links] if link[field] == value), None)
        if found is None:
            return text
        return (text + self.rule["plus"] if plus and text else text) + found[self.rule["links"]["text"]]

    def _display(self, key: int) -> int:
        layout = self.rule["layout"]
        if any(vk == key for _button, vk in self.rule["mouse_keys"]):
            return key
        if (flags := next((flags for found, flags in layout["keys"] if found == key), None)) is None:
            return 0
        if not flags & layout["flag"]:
            return key
        name = self.key_name(scan) if (scan := layout["scan"].get(str(key))) else ""
        index = layout["names"].get(name, 0) if len(name) == 1 else 0
        return layout["display"][index] if index else 0

    def _drawn(self, held: dict, plus: bool) -> str:
        text, orders = "", self.rule["orders"]
        for key in held["keys"]:
            text = self._link(text, "keys", self._display(key), plus)
        for part, flags in ((("mouse_axes", held["mouse_axes"]), ("mouse", held["mouse"])) if self.keyboard else (
                *(("pad", flags) for flags in held["split"]), ("axes", held["axes"]))):
            for value in orders[part] if flags else ():
                if value & flags:
                    text = self._link(text, part, value & flags, plus)
        for button in held["pad"]:
            text = self._link(text, "pad", button, plus)
        return text
