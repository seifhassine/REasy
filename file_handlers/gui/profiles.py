"""See docs/internals/gui-format.md."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from functools import cache
from typing import Any

lazy from file_handlers.build_catalog import catalog_reader
lazy from file_handlers.mmtr.profiles.base import mmtr_profile_for_game
lazy from utils.enum_manager import game_enum_definitions

lazy from .adapter import GuiRuntimeAdapter
from .errors import GuiFormatError, GuiSceneError
lazy from .rules import DATA_DIRECTORY, gui_layout

CATALOG_FORMAT = 1


@dataclass(frozen=True, slots=True)
class GuiRuntimeType:
    classes: tuple[str, ...]
    declared: frozenset[str]
    order: Mapping[str, int]
    defaults: Mapping[str, Any]
    facts: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class GuiRuntimeProfile:
    adapter: GuiRuntimeAdapter
    game: str
    resource_version: int
    config_resource_paths: tuple[str, ...]
    runtime_target: str
    safe_area_ratio: float
    preview_language: str
    degree_factors: tuple[float, float]
    srgb_to_linear: tuple[float, float, float, float, float]
    uv_orientations: tuple[tuple[str, str, str, str], ...]
    draw_order: Mapping[str, Any]
    undeclared: Mapping[str, str]
    control_fields: Mapping[str, str]
    clip_mode: int
    list_input: Mapping[str, Mapping[str, Any]]
    list_default: str
    list_handlers: Mapping[str, Mapping[str, str]]
    list_values: Mapping[str, Mapping[str, Mapping[str, Any]]]
    grid_input: tuple[str, str, str, str]
    key_repeat: tuple[float, float]
    try_pointer: str
    world: Mapping[str, Any]
    world_view_output: tuple[float, float]
    types: Mapping[str, Mapping[str, Any]]
    text: Mapping[str, Any]
    message_tags: Mapping[str, Any]
    icons: Mapping[str, Any]

    def runtime_type(self, type_name: str) -> GuiRuntimeType:
        return _runtime_type(self.game, type_name)

    def language(self, label: str) -> int:
        return _languages(self.game)[label]

    @property
    def languages(self) -> Mapping[str, int]:
        return _languages(self.game)

    def enum(self, name: str) -> Mapping[str, int]:
        return _enums(self.game)[name]

    def enum_value(self, properties: Mapping[str, Any], name: str, enum: str | None = None) -> int:
        enum, value = enum or name, properties[name] if name in properties else self.undeclared[name]
        labels = self.enum(enum)
        if isinstance(value, str):
            return labels.get(value, 0)
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise GuiSceneError(f"invalid {enum} value {value!r}") from exc
        if result not in _labels(self.game, enum):
            raise GuiSceneError(f"unknown {enum} value {result}")
        return result

    def enum_label(self, properties: Mapping[str, Any], name: str, enum: str | None = None) -> str:
        return _labels(self.game, enum or name)[self.enum_value(properties, name, enum)]


def _frozen(value):
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, dict):
        return {key: _frozen(item) for key, item in value.items()}
    return value


def _profile(data: Mapping) -> GuiRuntimeProfile:
    if data.get("format") != CATALOG_FORMAT:
        raise GuiFormatError(f"GUI runtime catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    fields = {key: _frozen(value) for key, value in data.items() if key != "format"}
    return GuiRuntimeProfile(adapter=GUI_ADAPTERS[data["game"]], **fields)


_catalog_for = catalog_reader(DATA_DIRECTORY, _profile, GuiFormatError, "GUI runtime")


@cache
def runtime_for_game(game: str) -> GuiRuntimeProfile:
    profile = _catalog_for(mmtr_profile_for_game(game))
    if profile is None:
        raise GuiFormatError(f"there is no GUI runtime catalog for {game}")
    return profile


def gui_runtime(version: int) -> GuiRuntimeProfile:
    return runtime_for_game(gui_layout(version).runtime)


@cache
def _runtime_type(game: str, type_name: str) -> GuiRuntimeType:
    types = runtime_for_game(game).types
    if type_name not in types:
        raise GuiSceneError(f"the {game} GUI runtime has no class {type_name}")
    names = []
    while type_name is not None:
        names.append(type_name)
        type_name = types[type_name].get("parent")
    names.reverse()
    chain = [types[name] for name in names]
    return GuiRuntimeType(
        tuple(names),
        frozenset(name for kind in chain for name in kind["properties"]),
        {name: place for place, name in enumerate(name for kind in chain for name in kind["properties"])},
        {key: value for kind in chain for key, value in kind.get("defaults", {}).items()},
        {key: value for kind in chain for key, value in kind.items()
         if key not in ("parent", "properties", "defaults")},
    )


@cache
def _languages(game: str) -> dict[str, int]:
    return {item["name"]: item["value"]
            for item in game_enum_definitions(game, namespace="via.Language")["via.Language"]}


@cache
def _enums(game: str) -> dict[str, dict[str, int]]:
    return {name.removeprefix("via.gui."): {item["name"]: item["value"] for item in members}
            for name, members in game_enum_definitions(game, namespace="via.gui.").items()}


@cache
def _labels(game: str, enum: str) -> dict[int, str]:
    return {value: label for label, value in _enums(game)[enum].items()}


from .dmc5.adapter import DMC5_GUI_ADAPTER

GUI_ADAPTERS: dict[str, GuiRuntimeAdapter] = {"DMC5": DMC5_GUI_ADAPTER}
