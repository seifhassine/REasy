"""See docs/internals/gui-rendering.md, Typing."""
from __future__ import annotations

from math import inf

from file_handlers.native_math import f32


class TextTyping:

    def __init__(self) -> None:
        self.typed: dict[str, float] = {}
        self._held: dict[str, list] = {}

    def _entry(self, node) -> list:
        if node.path not in self._held:
            command = node.runtime.enum_label(node.render_properties, "TypingCommand")
            self._held[node.path] = [command if command in ("Pause", "End") else "Update", 0.0]
            self.typed[node.path] = inf if command == "End" else 0
        return self._held[node.path]

    def advance(self, nodes, frames: float) -> None:
        for node in nodes:
            held = node.render_properties
            if not held.get("UseTyping"):
                continue
            entry = self._entry(node)
            if entry[0] != "Update":
                continue
            entry[1] = f32(entry[1] + f32(frames))
            if entry[1] >= f32(held["TypingTime"]):
                entry[1] = 0.0
                self.typed[node.path] += 1

    def assigned(self, node, name: str, value) -> None:
        if name in ("Message", "MessageId"):
            self._held[node.path], self.typed[node.path] = ["Update", 0.0], 0
        elif name == "TypingCommand":
            entry, command = self._entry(node), node.runtime.enum_label({name: value}, name)
            if command == "Restart":
                entry[:], self.typed[node.path] = ["Update", 0.0], 0
            elif command == "End":
                entry[0], self.typed[node.path] = "End", inf
            elif (command, entry[0]) in (("Pause", "Update"), ("Resume", "Pause"), ("Resume", "End")):
                entry[0] = "Pause" if command == "Pause" else "Update"
