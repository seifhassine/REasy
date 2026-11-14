"""See docs/internals/gui-editor.md, Document and edits."""

from __future__ import annotations

import uuid

from .model import GuiAnimation, GuiBinding, GuiDocument, GuiObject, GuiSymbol


def join(path: str, name: str) -> str:
    return f"{path.rstrip('/')}/{name}"


class Structure:
    def __init__(self, gui: GuiDocument):
        self.gui = gui
        self.symbols: dict[uuid.UUID, GuiSymbol] = {symbol.guid: symbol for symbol in gui.symbols}
        self.owner: dict[int, GuiSymbol] = {id(obj): symbol for symbol in gui.symbols for obj in symbol.objects}
        self.home: dict[int, GuiSymbol] = {id(item): symbol for symbol in gui.symbols for item in symbol.animations}
        self.uses: dict[uuid.UUID, list[GuiObject]] = {}
        for obj in (gui.root_object, *(item for symbol in gui.symbols for item in symbol.objects)):
            if obj is not None and obj.prototype_guid in self.symbols:
                self.uses.setdefault(obj.prototype_guid, []).append(obj)
        self._paths: dict[int, list[str]] = {}

    def symbol_of(self, obj: GuiObject) -> GuiSymbol | None:
        return self.symbols.get(obj.prototype_guid)

    def children(self, obj: GuiObject) -> list[GuiObject]:
        symbol = self.symbol_of(obj)
        return symbol.objects if symbol is not None else []

    def paths(self, obj: GuiObject, seen: frozenset[int] = frozenset()) -> list[str]:
        if obj is self.gui.root_object:
            return ["/"]
        if id(obj) not in self._paths or seen:
            owner = self.owner.get(id(obj))
            found = [] if owner is None or id(owner) in seen else [
                join(path, obj.name) for instance in self.uses.get(owner.guid, ())
                for path in self.paths(instance, seen | {id(owner)})]
            if seen:
                return found
            self._paths[id(obj)] = found
        return self._paths[id(obj)]

    def chain(self, path: str) -> list[GuiObject]:
        found = [self.gui.root_object]
        for name in filter(None, path.split("/")):
            child = next((item for item in self.children(found[-1]) if item.name == name), None)
            if child is None:
                break
            found.append(child)
        return found

    def target(self, binding: GuiBinding) -> GuiObject | None:
        chain = self.chain(binding.target_path)
        return chain[-1] if len(chain) == len(list(filter(None, binding.target_path.split("/")))) + 1 else None

    def through(self, obj: GuiObject) -> list[tuple[GuiBinding, str]]:
        paths = self.paths(obj)
        return [(binding, path) for binding in self.gui.bindings for path in paths
                if binding.target_path == path or binding.target_path.startswith(path + "/")]

    def contains(self, symbol: GuiSymbol, other: GuiSymbol, seen: frozenset[int] = frozenset()) -> bool:
        if symbol is other:
            return True
        return id(symbol) not in seen and any(
            self.contains(inner, other, seen | {id(symbol)})
            for inner in map(self.symbol_of, symbol.objects) if inner is not None)

    def state_symbol(self, animation: GuiAnimation) -> GuiSymbol | None:
        return self.home.get(id(animation))
