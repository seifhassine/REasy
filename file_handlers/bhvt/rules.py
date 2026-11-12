"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

import functools
from dataclasses import dataclass

lazy from file_handlers.layout_codec import RecordCodec, StreamCodec, game_enums, load_table, merged
lazy from utils.game_keys import game_key

from .errors import BhvtFormatError
from .model import Hfsm, Tree

DATA_DIRECTORY = "resources/data/bhvt"


@dataclass(frozen=True, slots=True)
class Section:
    name: str
    offset: str
    kind: str
    align: int
    item_align: int = 1


@dataclass(frozen=True, slots=True)
class TreeRules:
    version: int
    games: tuple[str, ...]
    header: RecordCodec
    node: StreamCodec
    sections: tuple[Section, ...]
    end_align: int
    new_node: dict
    graph: dict


@dataclass(frozen=True, slots=True)
class MotionFsmRules:
    tree: TreeRules
    header: RecordCodec
    tree_info: RecordCodec
    transition_map: RecordCodec
    transition_data: RecordCodec
    align: int


@dataclass(frozen=True, slots=True)
class TimelineFsmRules:
    tree: TreeRules
    header: RecordCodec
    tree_info: RecordCodec
    timeline_info: RecordCodec
    hold_point: RecordCodec
    c16_align: int
    end_align: int
    new: dict
    timeline: int


@dataclass(frozen=True, slots=True)
class HfsmRules:
    version: int
    games: tuple[str, ...]
    header: RecordCodec
    node: StreamCodec
    selector: StreamCodec
    condition: RecordCodec
    dictionary_entry: RecordCodec
    sections: tuple[Section, ...]
    new_node: dict
    new_selector: dict
    new_condition: dict
    graph: dict


@dataclass(frozen=True, slots=True)
class MotionFsmV1Rules:
    hfsm: HfsmRules
    header: RecordCodec
    fsm_info: RecordCodec
    extended_data: RecordCodec
    transition_data: RecordCodec
    align: int
    fsms_read: int


@functools.cache
def _load(name: str) -> dict:
    return load_table(DATA_DIRECTORY, name, BhvtFormatError)


def _family(name: str) -> dict:
    return merged(_load("layouts")["families"], name, ())


def _record(name: str, spec: dict) -> RecordCodec:
    return RecordCodec(name, spec["size"], spec["fields"], BhvtFormatError)


def file_entry(kind: str, version: int) -> dict:
    entry = _load("versions")["files"].get(kind, {}).get(str(version))
    if entry is None:
        raise BhvtFormatError(f".{kind}.{version} is not supported")
    return entry


@functools.cache
def tree_rules(version: int) -> TreeRules:
    entry = _load("versions")["trees"].get(str(version))
    if entry is None:
        raise BhvtFormatError(f"BHVT tree version {version} is not supported")
    family = _family(entry["layout"])
    return TreeRules(version, tuple(entry["games"]), _record("header", family["header"]),
                     StreamCodec("node", family["node"], BhvtFormatError),
                     tuple(Section(**item) for item in family["sections"]), family["end_align"],
                     family["new_node"], family["graph"])


@functools.cache
def motion_fsm_rules(version: int) -> MotionFsmRules:
    entry = file_entry("motfsm2", version)
    family = _family(entry["layout"])
    return MotionFsmRules(tree_rules(entry["tree"]),
                          *(_record(name, family[name])
                            for name in ("header", "tree_info", "transition_map", "transition_data")),
                          family["align"])


@functools.cache
def timeline_fsm_rules(version: int) -> TimelineFsmRules:
    entry = file_entry("tmlfsm2", version)
    family = _family(entry["layout"])
    return TimelineFsmRules(tree_rules(entry["tree"]),
                            *(_record(name, family[name])
                              for name in ("header", "tree_info", "timeline_info", "hold_point")),
                            family["c16_align"], family["end_align"], family["new"], entry["timeline"])


@functools.cache
def hfsm_rules(version: int) -> HfsmRules:
    entry = _load("versions")["hfsm"].get(str(version))
    if entry is None:
        raise BhvtFormatError(f"HFSM version {version} is not supported")
    family = _family(entry["layout"])
    streams = family["streams"]
    return HfsmRules(version, tuple(entry["games"]), _record("header", family["header"]),
                     StreamCodec("node", streams["node"], BhvtFormatError, streams),
                     StreamCodec("selector", streams["selector"], BhvtFormatError, streams),
                     _record("condition", family["condition"]), _record("dictionary_entry", family["dictionary_entry"]),
                     tuple(Section(**item) for item in family["sections"]), family["new_node"],
                     family["new_selector"], family["new_condition"], family["graph"])


def rules_of(model: Tree | Hfsm) -> TreeRules | HfsmRules:
    return (hfsm_rules if isinstance(model, Hfsm) else tree_rules)(model.version)


@functools.cache
def motion_fsm_v1_rules(version: int) -> MotionFsmV1Rules:
    entry = file_entry("motfsm", version)
    family = _family(entry["layout"])
    return MotionFsmV1Rules(hfsm_rules(entry["hfsm"]),
                            *(_record(name, family[name])
                              for name in ("header", "fsm_info", "extended_data", "transition_data")),
                            family["align"], family["fsms_read"])


@functools.cache
def _game_enums(game: str) -> dict[str, dict[int, str]]:
    return game_enums(_load("versions")["enums"], game)


@functools.cache
def enum_names(enum: str, games: tuple[str, ...]) -> dict[int, str]:
    names: dict[int, str] = {}
    for game in games:
        names.update(_game_enums(game)[enum])
    return dict(sorted(names.items()))


def display_names(enum: str, tree_version: int) -> dict[int, str]:
    return enum_names(enum, tree_rules(tree_version).games)


def field_enums() -> dict[str, str]:
    return _load("versions")["field_enums"]


def attribute_bit(tree_version: int, name: str) -> int:
    return next(value for value, label in display_names("NodeAttribute", tree_version).items() if label == name)


@functools.cache
def node_fields(tree_version: int, attribute: int) -> frozenset[str]:
    return frozenset(name for entry in tree_rules(tree_version).node.layout({"attribute": attribute})
                     for name in (entry if isinstance(entry, tuple) else (entry,)))


@functools.cache
def _per_game(name: str) -> dict:
    return {game_key(game): entry for game, entry in _load(name)["games"].items()}


@functools.cache
def activator_refused(game: str) -> frozenset[str]:
    return frozenset(_per_game("activator_refused")[game_key(game)])


def constructor_values(game: str) -> dict[str, dict]:
    return _per_game("constructors")[game_key(game)]


def authoring(game: str) -> dict:
    return _per_game("authoring")[game_key(game)]
