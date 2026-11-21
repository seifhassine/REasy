"""See docs/internals/maba-format.md, Editing."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cache

lazy from utils.app_paths import resource_path
lazy from utils.enum_manager import game_enum_definitions
from utils.hash_util import murmur3_hash_utf16le


@dataclass(frozen=True, slots=True)
class MabaRules:
    game: str
    commands: dict[int, tuple[str, str, str]]
    default_class: str
    joint_classes: frozenset[str]
    tags: dict[int, str]
    sampled: dict[str, str]


@cache
def maba_rules(version: int) -> MabaRules:
    with open(resource_path("resources/data/maba/rules.json"), encoding="utf-8") as handle:
        rules = json.load(handle)[str(version)]
    enums = game_enum_definitions(rules["game"])
    return MabaRules(rules["game"], {murmur3_hash_utf16le(name): (name, held["class"], held.get("components", ""))
                                     for name, held in rules["commands"].items()},
                     rules["default_class"], frozenset(rules["joint_classes"]),
                     {member["value"]: member["name"] for enum in rules["motion_tags"] for member in enums[enum]},
                     rules["sampled"])
