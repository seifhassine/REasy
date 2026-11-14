"""See docs/internals/gui-format.md."""

from __future__ import annotations

import functools
from dataclasses import dataclass

lazy from file_handlers.clip.rules import display_names
lazy from file_handlers.layout_codec import RecordCodec, load_table, merged

from .errors import GuiFormatError

DATA_DIRECTORY = "resources/data/gui"
STRINGS = {"c8": ("ascii", b"\0"), "c16": ("utf-16-le", b"\0\0")}


@dataclass(frozen=True, slots=True)
class GuiLayout:
    version: int
    runtime: str
    header: RecordCodec
    records: dict[str, RecordCodec]
    special: dict[str, str]
    pools: tuple[str, ...]
    payloads: dict[str, str]
    values: dict[int, str]
    name_property: tuple[str, int]
    clip_version: int


@functools.cache
def _load(name: str) -> dict:
    return load_table(DATA_DIRECTORY, name, GuiFormatError)


@functools.cache
def _versions() -> dict[int, dict]:
    return {int(version): entry for version, entry in _load("versions")["versions"].items()}


def supported_versions() -> tuple[int, ...]:
    return tuple(sorted(_versions()))


@functools.cache
def gui_layout(version: int) -> GuiLayout:
    entry = _versions().get(version)
    if entry is None:
        raise GuiFormatError(f"GUIR version {version} is not supported "
                             f"(supported: {', '.join(map(str, supported_versions()))})")
    data = merged(_load("layouts")["families"], entry["layout"], ("records", "special", "payloads", "values"))
    codes = {name: code for code, name in display_names("PropertyType").items()}
    return GuiLayout(version, entry["runtime"],
                     RecordCodec("header", data["header"]["size"], data["header"]["fields"], GuiFormatError),
                     {name: RecordCodec(name, spec["size"], spec["fields"], GuiFormatError)
                      for name, spec in data["records"].items()},
                     data["special"], tuple(data["pools"]), data["payloads"],
                     {codes[name]: storage for name, storage in data["values"].items()},
                     (data["name_property"][0], codes[data["name_property"][1]]), data["clip_version"])
