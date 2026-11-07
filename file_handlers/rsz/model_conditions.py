"""See docs/internals/character-assembly.md, Material conditions."""
from __future__ import annotations

from array import array
from collections.abc import Callable, Iterable, Mapping

lazy from .material_variations import enum_names
lazy from .spawn_rules import _value_type, field_pairs, opened, value_at
lazy from .utils.rsz_instance_defaults import type_table

MODEL_CONDITIONS_RESOURCE = "resources/data/model_conditions.json"


def model_condition_rules(game: str) -> dict:
    return type_table(MODEL_CONDITIONS_RESOURCE).get(str(game).upper(), {})


def holder_infos(components: Iterable[tuple[str, Mapping]], rules: Mapping,
                 slotted: Iterable[tuple[str, int, str]] = ()) -> dict[str, dict[int, str]]:
    found: dict[str, dict[int, str]] = {}
    for type_name, fields in components:
        if (kind := rules["managers"].get(type_name)) is not None:
            for holder in fields.get(rules["holders"]) or ():
                found.setdefault(kind, {})[int(holder[rules["id"]])] = holder[rules["data"]] or ""
    for kind, ident, path in slotted:
        found.setdefault(kind, {})[ident] = path
    return found


def parts_shown(info: Mapping[str, object], resolve: Callable[[int], Mapping], spec: Mapping,
                condition) -> dict[int, bool]:
    parts = [opened(value, place) for value, place in field_pairs(info, [*spec["definitions"], "*"], resolve)]
    shown = {}
    for value, place in field_pairs(info, [spec["conditions"], "*"], resolve):
        rule, inner = opened(value, place)
        if condition is None or value_at(rule, spec["id"], inner) != condition:
            continue
        change = value_at(rule, spec["change"], inner) == 1
        for item, at in field_pairs(rule, [spec["groups"], "*"], inner):
            group, held = opened(item, at)
            hidden, sets = value_at(group, spec["hidden"], held), value_at(group, spec["sets"], held) or []
            for part, here in parts if sets or hidden else ():
                if value_at(part, spec["group"], here) == value_at(group, spec["group"], held):
                    listed = not hidden and value_at(part, spec["set"], here) in sets
                    if listed or hidden or change:
                        shown[int(value_at(part, spec["part"], here))] = listed
        break
    return shown


def condition_values(info: Mapping[str, object], resolve: Callable[[int], Mapping], spec: Mapping, ids,
                     game: str) -> list[tuple[tuple[int, ...], int, str, object, float]]:
    conditions = {}
    for value, place in field_pairs(info, [spec["conditions"], "*"], resolve):
        condition, inner = opened(value, place)
        conditions.setdefault(value_at(condition, spec["condition"], inner), (condition, inner))
    kinds = {value: spec["types"].get(name) for value, name in enum_names(game, spec["enum"]).items()}
    found = []
    for condition, inner in (conditions[ident] for ident in ids if ident in conditions):
        for value, place in field_pairs(condition, [spec["parameters"], "*"], inner):
            parameter, here = opened(value, place)
            kind = kinds.get(value_at(parameter, spec["type"], here))
            written = _value(parameter, spec, here) if kind and value_at(parameter, spec["enable"], here) else None
            if written is None:
                continue
            if kind == "scalar" and (channel := value_at(parameter, spec["channel"], here)) >= 0:
                kind, written = "vector", tuple(written if index == channel else None for index in range(4))
            found.append((tuple(value_at(parameter, spec["materials"], here) or ()),
                          value_at(parameter, spec["variable"], here), kind, written,
                          float(value_at(parameter, spec["frames"], here) or 0.0)))
    return found


def condition_writes(values: Iterable[tuple], names) -> tuple[Mapping, ...]:
    return tuple(frozendict({names.variable: variable, field: value} | (
        {} if material is None else {names.material: material}))
        for materials, variable, kind, value, _frames in values if (field := getattr(names, kind)) is not None
        for material in materials or (None,))


def _value(parameter: Mapping[str, object], spec: Mapping, resolve: Callable[[int], Mapping]):
    found = field_pairs(parameter, [spec["value"]], resolve)
    read = spec["values"].get(_value_type(*found[0])) if found else None
    if read is None:
        return None
    value, there = opened(*found[0])
    if isinstance(read, str):
        return value_at(value, read, there)
    preset = spec["preset"]
    held = field_pairs(value, [preset["resource"]], there)
    resource, inner = opened(*held[0]) if held else ({}, there)
    listed, index = field_pairs(resource, [preset["data"], read["list"], "*"], inner), value_at(
        resource, preset["index"], inner)
    if index is None or not 0 <= index < len(listed):
        return None
    item, place = opened(*listed[index])
    value = value_at(item, preset["value"], place)
    return tuple(array("f", [byte / read["divisor"] for byte in value])) if "divisor" in read else value
