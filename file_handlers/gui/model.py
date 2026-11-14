"""See docs/internals/gui-format.md."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from typing import Any

lazy from file_handlers.clip.model import Timeline
lazy from file_handlers.clip.rules import PropertyType


GUI_MAGIC = b"GUIR"
ZERO_GUID = uuid.UUID(int=0)

GuiValue = bool | int | float | str | uuid.UUID | list[Any] | None


@dataclass(slots=True, eq=False)
class GuiProperty:
    name: str
    type: PropertyType
    value: GuiValue


@dataclass(slots=True)
class GuiTextureEntry:
    sequence: int
    pattern: int
    bounds: tuple[float, float, float, float]
    tagged: bool = False


@dataclass(slots=True)
class GuiTextureSet:
    entries: list[GuiTextureEntry] = field(default_factory=list)


NINE_SLICE_CELL_NAMES = (
    "top_left", "top_center", "top_right",
    "middle_left", "middle_center", "middle_right",
    "bottom_left", "bottom_center", "bottom_right",
)


@dataclass(slots=True)
class GuiNineSliceCell:
    name: str
    sequence: int
    pattern: int
    repeat_size: tuple[float, float]


@dataclass(slots=True)
class GuiNineSlice:
    borders: tuple[float, float, float, float]
    cells: list[GuiNineSliceCell] = field(default_factory=list)


GuiSpecialData = GuiTextureSet | GuiNineSlice | None


@dataclass(slots=True, eq=False)
class GuiObject:
    instance_guid: uuid.UUID
    prototype_guid: uuid.UUID
    name: str
    type_name: str
    properties: list[GuiProperty] = field(default_factory=list)
    special_data: GuiSpecialData = None

    def property(self, name: str) -> GuiProperty | None:
        return next((item for item in self.properties if item.name == name), None)

    def values(self) -> dict[str, GuiValue]:
        return {item.name: copy.deepcopy(item.value) for item in self.properties}


@dataclass(slots=True, eq=False)
class GuiAnimation:
    guid: uuid.UUID
    name: str
    loop: bool
    clip: Timeline
    transition: "GuiAnimation | None" = None


@dataclass(slots=True, eq=False)
class GuiSymbol:
    guid: uuid.UUID
    name: str
    type_name: str
    objects: list[GuiObject] = field(default_factory=list)
    animations: list[GuiAnimation] = field(default_factory=list)

    def animation_states(self) -> dict[str, list[GuiAnimation]]:
        result: dict[str, list[GuiAnimation]] = {}
        for animation in self.animations:
            result.setdefault(animation.name, []).append(animation)
        return result


@dataclass(slots=True, eq=False)
class GuiBinding:
    target_path: str
    target_type: str
    property: GuiProperty


@dataclass(slots=True, eq=False)
class GuiDocument:
    version: int
    root_object: GuiObject | None
    symbols: list[GuiSymbol]
    bindings: list[GuiBinding] = field(default_factory=list)
    imported_gui_paths: list[str] = field(default_factory=list)
    source: str = "<bytes>"
    objects: list[GuiObject] = field(default_factory=list, repr=False)
    animations: list[GuiAnimation] = field(default_factory=list, repr=False)

    def symbol(self, guid: uuid.UUID) -> GuiSymbol | None:
        return next((item for item in self.symbols if item.guid == guid), None)

    def validate_relationships(self) -> None:
        objects, animations = self.objects, self.animations
        owned = [self.root_object, *(item for symbol in self.symbols for item in symbol.objects)]
        states = [item for symbol in self.symbols for item in symbol.animations]
        if len({id(item) for item in objects}) != len(objects):
            raise ValueError("GUI object declarations must not contain duplicates")
        if len({id(item) for item in animations}) != len(animations):
            raise ValueError("GUI animation declarations must not contain duplicates")
        if len({id(item) for item in owned}) != len(owned):
            raise ValueError("a GUI object belongs to two symbols")
        if len({id(item) for item in states}) != len(states):
            raise ValueError("a GUI animation belongs to two symbols")
        if any(item is not None and item not in objects for item in owned):
            raise ValueError("every owned GUI object must have a declaration")
        if {id(item) for item in states} != {id(item) for item in animations}:
            raise ValueError("the GUI animation declarations must be the symbols' animations")
        if len({item.instance_guid for item in objects}) != len(objects):
            raise ValueError("GUI object instance GUIDs must be unique")
        if len({item.guid for item in self.symbols}) != len(self.symbols):
            raise ValueError("GUI symbol GUIDs must be unique")
        if len({item.guid for item in animations}) != len(animations):
            raise ValueError("GUI animation GUIDs must be unique")
        for symbol in self.symbols:
            if any(item.transition is not None and item.transition not in symbol.animations
                   for item in symbol.animations):
                raise ValueError(f"an animation of symbol {symbol.name!r} continues in another symbol")
