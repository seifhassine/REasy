"""See docs/internals/gui-editor.md, Data."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

lazy from file_handlers.build_catalog import catalog_reader
lazy from file_handlers.clip.rules import PropertyType, display_names
lazy from file_handlers.mmtr.profiles.base import mmtr_profile_for_game
lazy from utils.enum_manager import game_enum_definitions

from .errors import GuiFormatError
lazy from .model import GuiProperty
lazy from .profiles import runtime_for_game
lazy from .rules import gui_layout

DATA_DIRECTORY = "resources/data/gui_authoring"
CATALOG_FORMAT = 1


@dataclass(frozen=True, slots=True)
class GuiField:
    name: str
    owner: str
    type: PropertyType
    enum: str | None = None
    labels: tuple[str, ...] = ()
    asset: tuple[str, ...] = ()
    when: tuple[str, Any] | None = None
    unread: bool = False
    default: Any = None
    keys: int | None = None
    lines: bool = False


@dataclass(frozen=True, slots=True)
class GuiAuthoring:
    game: str
    resource_version: int
    names: Mapping[str, str]
    control_class: str
    payloads: Mapping[str, Mapping[str, Any]]
    palette: tuple[str, ...]
    track_types: frozenset[PropertyType]
    interpolations: Mapping[str, frozenset[int]]
    classes: Mapping[str, Mapping[str, Mapping[str, Any]]]

    def fields(self, type_name: str) -> tuple[GuiField, ...]:
        return _fields(self.game, type_name)

    def field(self, type_name: str, name: str) -> GuiField | None:
        return _by_name(self.game, type_name).get(name)

    def labels(self, field: GuiField) -> tuple[str, ...]:
        return field.labels or _enum(self.game, field.enum)

    def stored(self, type_name: str, values: Mapping[str, Any]) -> list[GuiField]:
        fields = _by_name(self.game, type_name)
        return [field for field in fields.values() if field.when is None
                or values.get(field.when[0], fields[field.when[0]].default) == field.when[1]]

    def new_properties(self, type_name: str) -> list[GuiProperty]:
        return [GuiProperty(field.name, field.type, copied(field.default))
                for field in self.stored(type_name, {}) if not field.unread]

    def is_control(self, type_name: str) -> bool:
        runtime = runtime_for_game(self.game)
        return type_name in runtime.types and self.control_class in runtime.runtime_type(type_name).classes

    def free_name(self, pattern: str, taken, **parts) -> str:
        number = 0
        while (name := self.names[pattern].format(number=number, **parts)) in taken:
            number += 1
        return name


def copied(value):
    return list(value) if isinstance(value, (list, tuple)) else value


def _catalog(data: Mapping) -> GuiAuthoring:
    if data.get("format") != CATALOG_FORMAT:
        raise GuiFormatError(f"GUI authoring catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    codes = {name: PropertyType(code) for code, name in display_names("PropertyType").items()}
    modes = {name: code for code, name in display_names("InterpolationType").items()}
    return GuiAuthoring(data["game"], data["resource_version"], data["names"], data["control_class"],
                        data["payloads"], tuple(data["palette"]),
                        frozenset(codes[name] for name in data["track_types"]),
                        {family: frozenset(modes[name] for name in names)
                         for family, names in data["interpolations"].items()}, data["classes"])


_catalog_for = catalog_reader(DATA_DIRECTORY, _catalog, GuiFormatError, "GUI authoring")


@cache
def authoring_for_game(game: str) -> GuiAuthoring:
    catalog = _catalog_for(mmtr_profile_for_game(game))
    if catalog is None:
        raise GuiFormatError(f"there is no GUI authoring catalog for {game}")
    return catalog


def gui_authoring(version: int) -> GuiAuthoring:
    return authoring_for_game(gui_layout(version).runtime)


@cache
def _fields(game: str, type_name: str) -> tuple[GuiField, ...]:
    catalog, runtime = authoring_for_game(game), runtime_for_game(game)
    kind = runtime.runtime_type(type_name)
    codes = {name: PropertyType(code) for code, name in display_names("PropertyType").items()}
    modes = {name: code for code, name in display_names("InterpolationType").items()}
    owners = {name: owner for owner in kind.classes for name in runtime.types[owner]["properties"]}
    owners.update((name, owner) for owner in kind.classes for name, spec in catalog.classes.get(owner, {}).items()
                  if spec.get("unread"))
    found = []
    for name, owner in owners.items():
        spec = catalog.classes.get(owner, {}).get(name)
        if spec is None:
            continue
        default = spec["default"] if "default" in spec else kind.defaults.get(name)
        if codes[spec["type"]] == PropertyType.GUID and default is not None:
            default = uuid.UUID(default)
        found.append((kind.order.get(name, float("inf")), GuiField(
            name, owner, codes[spec["type"]], spec.get("enum"), tuple(spec.get("labels", ())),
            tuple(spec.get("asset", ())), next(iter(spec["when"].items())) if "when" in spec else None,
            spec.get("unread", False), default, modes.get(spec.get("keys")), spec.get("lines", False))))
    return tuple(field for _rank, field in sorted(found, key=lambda item: item[0]))


@cache
def _by_name(game: str, type_name: str) -> dict[str, GuiField]:
    return {field.name: field for field in _fields(game, type_name)}


@cache
def _enum(game: str, name: str) -> tuple[str, ...]:
    members = game_enum_definitions(game, namespace=name.rsplit(".", 1)[0] + ".")[name]
    return tuple(item["name"] for item in sorted(members, key=lambda item: item["value"]))
