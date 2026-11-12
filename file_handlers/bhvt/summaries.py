"""See docs/internals/bhvt-editor.md, Summaries."""

from __future__ import annotations

import uuid

lazy from file_handlers.rsz.rsz_data_types import (ArrayData, BoolData, GameObjectRefData, GuidData, ObjectData,
                                                   RawBytesData, ResourceData, StringData, StructData, UserDataData,
                                                   _DataValue)
lazy from file_handlers.rsz.utils.rsz_field_utils import is_type_assignable
lazy from utils.enum_manager import FlagMembers, flag_names, game_enum_definitions
lazy from utils.number_format import format_full_float

from .model import Hfsm, Tree
from .references import asset, base_name, field_value, own_variables, variables
from .rules import constructor_values, rules_of


def short_name(type_name: str) -> str:
    head = type_name.split("`", 1)[0].split("[[", 1)[0].split("<", 1)[0]
    return head.rsplit(".", 1)[-1]


def label(key: str) -> str:
    return base_name(key).lstrip("_")


def type_name(model: Tree | Hfsm, block: str, place: int) -> str:
    rsz = model.blocks[block]
    return rsz.type_registry.get_type_info(rsz.instance_infos[rsz.object_table[place]].type_id)["name"]


class Summaries:
    def __init__(self, model: Tree | Hfsm, owners=(), timelines=frozendict(), holds=frozendict()):
        self.model = model
        rules = rules_of(model)
        self.slots, self.variable = rules.graph["slots"], rules.graph["variables"].get("condition")
        self.timelines, self.timeline = timelines, rules.graph["timeline"] if timelines else None
        self.holds, self.hold_point = holds, rules.graph["hold_point"] if holds else None
        self.identity, self.negation = set(rules.graph["identity"]), rules.graph["negation"]
        self.user_path = rules.graph["user_path"]
        block = next(iter(model.blocks.values()))
        self.registry, self.game = block.type_registry, block.game_version
        self.enums = game_enum_definitions(self.game)
        self.defaults = constructor_values(self.game)
        self.found = variables(model, owners)
        self.own = {id(uvar) for uvar in own_variables(model)}

    def type_name(self, block: str, place: int) -> str:
        return type_name(self.model, block, place)

    def fields(self, block: str, place: int) -> dict:
        rsz = self.model.blocks[block]
        return rsz.parsed_elements[rsz.object_table[place]]

    def variable_name(self, guid: bytes) -> str:
        found = self.found.get(guid)
        return "" if found is None else found[1].name or str(uuid.UUID(bytes_le=guid))

    def named(self, guid: bytes) -> str:
        return self.variable_name(guid) or self.timelines.get(guid, "") or self.holds.get(guid, "")

    def variable_of(self, block: str, place: int) -> bytes | None:
        if self.variable is None or not is_type_assignable(self.registry, self.type_name(block, place),
                                                           self.variable[0]):
            return None
        return field_value(self.fields(block, place), self.variable[1])

    def value_text(self, field, rsz) -> str:
        if isinstance(field, (ArrayData, StructData)):
            return f"[{len(field.values)}]"
        if isinstance(field, UserDataData) and not rsz.has_embedded_rsz:
            return field.string.rstrip("\0").rsplit("/", 1)[-1]
        if isinstance(field, (ObjectData, UserDataData)):
            if not field.value:
                return "null"
            name = short_name(self.registry.get_type_info(rsz.instance_infos[field.value].type_id)["name"])
            if isinstance(field, ObjectData):
                return name
            held = asset(rsz._rsz_userdata_dict[field.value], self.user_path)
            return f"{name}  {held[1].rsplit('/', 1)[-1]}" if held else name
        if isinstance(field, (GuidData, GameObjectRefData)):
            return self.named(field.raw_bytes) or (
                str(uuid.UUID(bytes_le=field.raw_bytes))[:8] if any(field.raw_bytes) else "none")
        if isinstance(field, (StringData, ResourceData)):
            return field.value.rstrip("\0").rsplit("/", 1)[-1]
        if isinstance(field, BoolData):
            return "true" if field.value else "false"
        if isinstance(field, RawBytesData):
            return field.raw_bytes.hex(" ")
        if not hasattr(field, "value"):
            return "(" + ", ".join(format_full_float(part, 4) if isinstance(part, float) else str(part)
                                   for part in _numbers(field)) + ")"
        if isinstance(field.value, float):
            return format_full_float(field.value, 4)
        members = self.enums.get(field.orig_type)
        if members and isinstance(field.value, int):
            names, rest = flag_names(members, field.value, 64) if isinstance(members, FlagMembers) else \
                ([member["name"] for member in members if member["value"] == field.value][:1], 0)
            if names and not rest:
                return " | ".join(names)
        return str(field.value)

    def changed(self, block: str, place: int) -> list[tuple[str, str]]:
        fields, defaults = self.fields(block, place), self.defaults.get(self.type_name(block, place), {})
        rows = []
        for key, field in fields.items():
            if base_name(key) in self.identity or isinstance(field, (ObjectData, ArrayData, StructData)) \
                    and not _filled(field):
                continue
            default = defaults.get(key)
            if _differs(field, default):
                rows.append((label(key), self.value_text(field, self.model.blocks[block])))
        return rows

    def detail(self, block: str, place: int, limit: int = 2) -> str:
        guid = self.variable_of(block, place)
        if guid is not None:
            return self.variable_name(guid) or (str(uuid.UUID(bytes_le=guid))[:8] if any(guid) else "?")
        return ", ".join(f"{key} {value}" for key, value in [
            row for row in self.changed(block, place) if row[0] not in ("Enabled", self.negation)][:limit])

    def object_text(self, block: str, place: int, limit: int = 2) -> str:
        name, detail = short_name(self.type_name(block, place)), self.detail(block, place, limit)
        guid = self.variable_of(block, place)
        return detail if guid in self.found else f"{name}  {detail}" if detail else name

    def condition_text(self, ref: tuple[str, int] | None, record=None) -> str:
        if ref is None:
            return "" if record is None else short_name(record.name or "")
        text = self.object_text(*ref)
        holds = record.condition if record is not None else field_value(self.fields(*ref), self.negation)
        return text if holds else f"not {text}"


def _filled(field) -> bool:
    return bool(field.values) if isinstance(field, (ArrayData, StructData)) else bool(field.value)


def _numbers(field) -> list:
    return [number for name, part in vars(field).items() if not name.startswith("_") and name != "orig_type"
            for number in (part if isinstance(part, list) else _numbers(part) if isinstance(part, _DataValue)
                           else [part])]


def _differs(field, default) -> bool:
    if isinstance(field, (ArrayData, StructData)):
        return len(field.values) != len(default or ())
    if isinstance(field, (ObjectData, UserDataData)):
        return bool(field.value) != bool(default)
    if isinstance(field, (GuidData, GameObjectRefData)):
        return field.raw_bytes != (uuid.UUID(default).bytes_le if default else bytes(16))
    if isinstance(field, (StringData, ResourceData)):
        return field.value.rstrip("\0") != (default or "")
    if isinstance(field, RawBytesData):
        return any(field.raw_bytes)
    if not hasattr(field, "value"):
        parts = _numbers(field)
        return parts != list(default) if isinstance(default, list) else any(parts)
    if isinstance(default, str):
        return True
    return field.value != (default or 0) if not isinstance(field.value, float) else \
        abs(field.value - float(default or 0)) > 1e-6 * max(1.0, abs(field.value))
