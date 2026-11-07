"""See docs/internals/jcns-format.md."""

from __future__ import annotations

import functools
import struct
from dataclasses import dataclass

lazy from file_handlers.layout_codec import RecordCodec, load_table, merged
lazy from utils.enum_manager import game_enum_definitions
lazy from utils.game_keys import game_key

from .errors import JcnsFormatError
from .model import Record

DATA_DIRECTORY = "resources/data/jcns"


@dataclass(frozen=True, slots=True)
class Link:
    name: str
    at: int
    record: str = ""
    count: str = ""
    fixed: int = 0
    add: int = 0
    align: int = 0


@dataclass(frozen=True, slots=True)
class RecordLayout:
    name: str
    codec: RecordCodec
    links: tuple[Link, ...]
    order: tuple[Link, ...]
    counts: frozenset[str]
    block_align: int
    defaults: dict[str, object]
    enums: dict[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class FormatRules:
    family: str
    depth_first: bool
    pooled: frozenset[tuple[str, str]]
    root_order: tuple[str, ...]
    expressions: dict[int, str]
    records: dict[str, RecordLayout]
    derived: dict[str, object]


class Enums:
    def __init__(self, definitions: dict[str, dict[str, int]], game: str = ""):
        self.definitions, self.game = definitions, game

    @classmethod
    def of_game(cls, game: str) -> Enums:
        return cls({name: {member["name"]: member["value"] for member in members} for name, members in
                    game_enum_definitions(game, namespace="via.motion.").items() if isinstance(members, list)}, game)

    def members(self, names: tuple[str, ...]) -> dict[str, int]:
        return next((self.definitions[name] for name in names if name in self.definitions), {})

    def of_field(self, layout: RecordLayout, name: str) -> dict[str, int]:
        return self.members(layout.enums.get(name, ()))

    def __call__(self, names: tuple[str, ...], member: str) -> int | None:
        return self.members(names).get(member)


@functools.cache
def _load(name: str) -> dict:
    return load_table(DATA_DIRECTORY, name, JcnsFormatError)


def joint_view() -> dict:
    return _load("joint_view")


@functools.cache
def header_codec() -> RecordCodec:
    spec = _load("layouts")["header"]
    return RecordCodec("header", spec["size"], spec["fields"], JcnsFormatError)


def _record(name: str, spec: dict) -> RecordLayout:
    links = tuple(Link(item["name"], item["at"], item.get("record", ""), item.get("count", ""), item.get("fixed", 0),
                       item.get("add", 0), item.get("align", 0)) for item in spec.get("links", ()))
    fields = [item if isinstance(item, dict) else {"at": item[0], "type": item[1], "name": item[2]}
              for item in spec.get("fields", ())]
    enums = {item[2]: tuple(item[3]) if isinstance(item[3], list) else (item[3],)
             for item in spec.get("fields", ()) if isinstance(item, list) and len(item) > 3}
    fields += [{"at": link.at, "type": "u64", "name": link.name} for link in links]
    by_name = {link.name: link for link in links}
    counts = frozenset(link.count for link in links if link.count)
    defaults = {}
    for item in fields:
        for label in [bit[2] for bit in item["bits"]] if "bits" in item else [item["name"]]:
            if label not in by_name and label not in counts:
                defaults[label] = _zero(item)
    return RecordLayout(name, RecordCodec(name, spec["size"], fields, JcnsFormatError), links,
                        tuple(by_name[label] for label in spec.get("order", by_name)), counts,
                        spec.get("block_align", 8), defaults, enums)


def _zero(item: dict) -> object:
    width = {"f32x3": 3, "f32x4": 4}.get(item["type"])
    return (0.0,) * width if width else 0.0 if item["type"] == "f32" and "bits" not in item else 0


@functools.cache
def format_rules(family: str) -> FormatRules:
    families = _load("layouts")["families"]
    data = merged(families, family, ("records", "rules", "expressions", "derived"))
    rules = data["rules"]
    return FormatRules(family, rules.get("depth_first", False), frozenset(map(tuple, rules.get("pooled", ()))),
                       tuple(rules.get("root_order", ())),
                       {int(code): kind for code, kind in data["expressions"].items()},
                       {kind: _record(kind, spec) for kind, spec in data["records"].items()}, data["derived"])


@functools.cache
def _versions() -> dict[int, dict]:
    return {int(version): entry for version, entry in _load("versions")["versions"].items()}


def supported_versions() -> tuple[int, ...]:
    return tuple(sorted(_versions()))


def evaluated(version: int) -> bool:
    return bool(_versions().get(version, {}).get("evaluated"))


def version_game(version: int, game: str) -> str:
    games = _versions()[version]["games"]
    return game if game_key(game) in map(game_key, games) else games[0]


def evaluation_rules(version: int, game: str) -> dict:
    entry = _versions()[version]
    return {**entry["evaluation"], **entry.get("game_evaluation", {}).get(game_key(game), {})}


def rules_for(version: int) -> FormatRules:
    entry = _versions().get(version)
    if entry is None:
        raise JcnsFormatError(f".jcns version {version} is not supported (supported: {supported_versions()})")
    return format_rules(entry["layout"])


def stored(layout: RecordLayout, name: str, value):
    if not layout.codec.fields.get(name, "").startswith("f32"):
        return value
    lanes = value if isinstance(value, tuple) else (value,)
    rounded = struct.unpack(f"<{len(lanes)}f", struct.pack(f"<{len(lanes)}f", *lanes))
    return rounded if isinstance(value, tuple) else rounded[0]


def new_record(version: int, kind: str) -> Record:
    layout = rules_for(version).records[kind]
    return Record(kind, dict(layout.defaults), {link.name: None for link in layout.links})
