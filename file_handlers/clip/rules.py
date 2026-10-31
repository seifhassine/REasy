"""See docs/internals/clip-format.md."""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass
from enum import IntEnum

lazy from utils.app_paths import resource_path
lazy from utils.enum_manager import game_enum_definitions

from .layout import ClipFormatError, RecordCodec

DATA_DIRECTORY = "resources/data/clip"


@dataclass(frozen=True, slots=True)
class ValueCodec:
    name: str
    kind: str
    bits: int = 64
    signed: bool = False
    pool: str = ""
    table: str = ""
    raw: int = 0
    numeric: bool = False


@dataclass(frozen=True, slots=True)
class PropertyTypeInfo:
    code: int
    name: str
    keys_since: int | None
    value: ValueCodec | None
    key_storage: str
    speed_points: bool
    children: str
    members: tuple[tuple[str, int], ...]

    def owns_keys(self, version: int) -> bool:
        return self.keys_since is not None and version >= self.keys_since


@dataclass(frozen=True, slots=True)
class InterpolationInfo:
    code: int
    name: str
    numeric: bool
    control: str


@dataclass(frozen=True, slots=True)
class Section:
    table: str
    pointer: str
    record: RecordCodec | None
    count: str
    pool: str
    align: int
    empty: str
    user_curve: bool


@dataclass(frozen=True, slots=True)
class FormatRules:
    family: str
    string_dedupe: bool
    extra_key_slots: int
    last_key_table: bool
    compact_keys: bool
    clip_roots: bool
    width_default: int
    key_tables: tuple[str, ...]
    container_key_table: str
    compact_key_table: str
    header_size: int
    header: RecordCodec
    sections: tuple[Section, ...]
    records: dict[str, RecordCodec]
    tables: dict[str, RecordCodec]

    def key_table(self, info: PropertyTypeInfo | None, version: int, compact: bool) -> str:
        if info is None or not info.owns_keys(version):
            return self.container_key_table
        if self.compact_keys and info.key_storage:
            return info.key_storage
        return self.compact_key_table if compact else self.key_tables[0]

    def key_record(self, info: PropertyTypeInfo | None, version: int, compact: bool, last: bool = False) -> RecordCodec:
        return self.tables["last_keys" if last else self.key_table(info, version, compact)]


@dataclass(frozen=True, slots=True)
class VersionProfile:
    version: int
    games: tuple[str, ...]
    checked: bool
    rules: FormatRules


@functools.cache
def _load(name: str) -> dict:
    with open(resource_path(f"{DATA_DIRECTORY}/{name}.json", required=True), encoding="utf-8") as stream:
        table = json.load(stream)
    if table.get("format") != 1:
        raise ClipFormatError(f"{name}.json has format {table.get('format')}, not 1")
    return table


def _member_name(name: str) -> str:
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z][a-z])", "_", name).upper()


@functools.cache
def _family(name: str) -> dict:
    family = dict(_load("layouts")["families"][name])
    if parent := family.pop("extends", None):
        base = _family(parent)
        family = {**base, **family, "records": {**base["records"], **family.get("records", {})},
                  "rules": {**base["rules"], **family.get("rules", {})}}
    return family


@functools.cache
def format_rules(family: str) -> FormatRules:
    data = _family(family)
    records = {name: RecordCodec(name, spec["size"], spec["fields"]) for name, spec in data["records"].items()}
    sections = tuple(Section(item["table"], item["pointer"], records.get(item.get("record", "")),
                             item.get("count", ""), item.get("pool", ""), item.get("align", 8),
                             item.get("empty", ""), item.get("user_curve", False))
                     for item in data["sections"])
    rules = {**data["rules"], "key_tables": tuple(data["rules"]["key_tables"])}
    return FormatRules(family, header_size=data["header_size"],
                       header=RecordCodec("header", data["header_size"], data["header"]),
                       sections=sections, records=records,
                       tables={section.table: section.record for section in sections if section.record}, **rules)


@functools.cache
def _versions() -> dict[int, dict]:
    return {int(version): entry for version, entry in _load("versions")["versions"].items()}


def supported_versions() -> tuple[int, ...]:
    return tuple(sorted(_versions()))


@functools.cache
def profile(version: int) -> VersionProfile:
    entry = _versions().get(version)
    if entry is None:
        raise ClipFormatError(f"CLIP version {version} is not supported "
                              f"(supported: {', '.join(map(str, supported_versions()))})")
    return VersionProfile(version, tuple(entry.get("games", ())), entry.get("checked", True),
                          format_rules(entry["layout"]))


@functools.cache
def _game_enums(game: str) -> dict[str, dict[int, str]]:
    engine = _load("versions")["enums"]
    namespace = min((name.rpartition(".")[0] for name in engine.values()), key=len) + "."
    definitions = game_enum_definitions(game, namespace=namespace)
    return {enum: {member["value"]: member["name"] for member in definitions.get(name, ())}
            for enum, name in engine.items()}


@functools.cache
def display_names(enum: str, version: int | None = None) -> dict[int, str]:
    """Without a version: the newest version's games, which know every value."""
    names: dict[int, str] = {}
    for game in _versions()[supported_versions()[-1] if version is None else version]["games"]:
        names.update(_game_enums(game)[enum])
    return dict(sorted(names.items()))


@functools.cache
def _property_types() -> dict[int, PropertyTypeInfo]:
    data = _load("property_types")
    codes = {name: code for code, name in display_names("PropertyType").items()}
    codecs = {name: ValueCodec(name, **spec) for name, spec in data["values"].items()}
    return {codes[name]: PropertyTypeInfo(
        codes[name], name, spec.get("keys"), codecs.get(spec.get("value", "")), spec.get("key_storage", ""),
        spec.get("speed_points", False), "members" if "members" in spec else spec.get("children", "fields"),
        tuple((member, codes[kind]) for member, kind in spec.get("members", ())))
        for name, spec in data["types"].items()}


def property_type(code: int) -> PropertyTypeInfo | None:
    return _property_types().get(code)


def owns_keys(code: int, version: int) -> bool:
    info = property_type(code)
    return info is not None and info.owns_keys(version)


@functools.cache
def _interpolations() -> dict[int, InterpolationInfo]:
    facts = _load("interpolations")["types"]
    return {code: InterpolationInfo(code, name, facts.get(name, {}).get("numeric", False),
                                    facts.get(name, {}).get("control", ""))
            for code, name in display_names("InterpolationType").items()}


def interpolation(code: int) -> InterpolationInfo | None:
    return _interpolations().get(code)


@functools.cache
def control_width(table: str) -> int:
    return _load("interpolations")["control_tables"][table]


@functools.cache
def control_tables(owner: str) -> frozenset[str]:
    return frozenset(_load("interpolations")["control_owners"][owner])


def _enum(name: str) -> type[IntEnum]:
    return IntEnum(name, {_member_name(text): value for value, text in display_names(name).items()})


PropertyType = _enum("PropertyType")
InterpolationType = _enum("InterpolationType")
NodeType = _enum("NodeType")
