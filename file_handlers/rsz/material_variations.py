"""See docs/internals/material-variations.md."""
from __future__ import annotations

from array import array
from collections.abc import Callable, Mapping
from dataclasses import dataclass

lazy from file_handlers.engine_math import crt
lazy from file_handlers.rsz.rsz_data_types import ArrayData, ObjectData
lazy from file_handlers.rsz.utils.rsz_instance_defaults import type_table
lazy from utils.enum_manager import game_enum_definitions

MATERIAL_VARIATIONS_RESOURCE = "resources/data/material_variations.json"


@dataclass(frozen=True, slots=True)
class VariationEntry:
    material: str
    parameter: str
    kind: str
    fields: tuple[str, ...]
    raw: tuple
    values: tuple[float, ...]
    file: str
    path: tuple[str | int, ...]
    enable: str
    base: bool
    applied: bool


def variation_rules(game: str) -> dict:
    return type_table(MATERIAL_VARIATIONS_RESOURCE).get(str(game).upper(), {})


def chosen(rule: Mapping, fields: Mapping[str, object], picked: int | None = None) -> int:
    if fields.get(rule["fixed"]):
        return int(fields[rule["fixed choice"]])
    return int(fields[rule["choice"]] if picked is None else picked)


def enum_names(game: str, enum: str) -> dict[int, str]:
    return {member["value"]: member["name"] for member in game_enum_definitions(game, namespace=enum).get(enum, ())}


def variation_choices(rule: Mapping, table: Mapping, game: str) -> tuple[tuple[int, str], ...]:
    names, labels = rule["choices"], rule["labels"]
    members = enum_names(game, labels["enum"])
    starts = (0, *labels["borders"])
    found = []
    for entry in table.get(names["list"]) or ():
        if entry and entry.get(names["data"]):
            ident = int(entry[names["id"]])
            kind = sum(ident >= border for border in labels["borders"])
            found.append((ident, f"{members.get(kind, kind)} {ident - starts[kind] + 1}"))
    return tuple(found)


def variation_entries(rule: Mapping, table: Mapping, choice: int,
                      read: Callable[[str, bool], Mapping]) -> dict[int, list[VariationEntry]]:
    names, entries = rule["choices"], {}
    data = next((entry.get(names["data"]) for entry in table.get(names["list"]) or ()
                 if entry and entry.get(names["id"]) == choice), None)
    for path, base in ((table[rule["base"]], True), (data, False)) if table.get(rule["base"]) and data else ():
        for part, found in read(path, base).items():
            entries.setdefault(part, []).extend(found)
    return entries


def file_entries(rule: Mapping, root: Mapping | None, path: str, base: bool) -> dict[int, list[VariationEntry]]:
    parts, materials, entries = rule["parts"], rule["materials"], {}
    for part_index, part in enumerate((root or {}).get(parts["list"]) or ()):
        for cluster_index, cluster in enumerate((part or {}).get(materials["list"]) or ()):
            for write in rule["writes"] if cluster else ():
                for item, place, steps in _reached(cluster, write["path"], write.get("limit")):
                    raw = tuple(item[name] for name in write["value"])
                    values = linear_colour(raw[0], rule["colour"]) if write["kind"] == "colour" \
                        else tuple(map(float, raw))
                    entries.setdefault(part[parts["key"]], []).append(VariationEntry(
                        cluster[materials["name"]], write["parameter"].format(place), write["kind"],
                        tuple(write["value"]), raw, values, path,
                        (parts["list"], part_index, materials["list"], cluster_index, *steps), write["enable"],
                        base, base or bool(item.get(write["enable"]))))
    return entries


def set_entry(rsz, entry: VariationEntry, raw: tuple, applied: bool) -> None:
    fields = rsz.parsed_elements[entry_instance(rsz, entry.path)]
    for name, value in zip(entry.fields, raw):
        if entry.kind == "colour":
            fields[name].r, fields[name].g, fields[name].b, fields[name].a = value
        else:
            fields[name].value = value
    if not entry.base:
        fields[entry.enable].value = applied


def entry_instance(rsz, path: tuple[str | int, ...]) -> int | None:
    instance, value = rsz.object_table[0], None
    for step in path:
        if isinstance(step, int):
            listed = value.values if isinstance(value, ArrayData) else ()
            value = listed[step] if step < len(listed) else None
        else:
            value = rsz.parsed_elements.get(instance, {}).get(step)
        if isinstance(value, ObjectData):
            instance = value.value
        elif not isinstance(value, ArrayData):
            return None
    return instance


def linear_colour(rgba, constants: Mapping) -> tuple[float, ...]:
    threshold, slope, offset, scale, exponent = array("f", [constants[name] for name in (
        "threshold", "slope", "offset", "scale", "exponent")])
    *channels, alpha = array("f", [byte / constants["divisor"] for byte in rgba])
    power = crt("powf")
    return (*array("f", [value / slope if not value > threshold else power((value + offset) / scale, exponent)
                         for value in channels]), alpha)


def _reached(node: Mapping, path, limit: int | None) -> list[tuple[Mapping, int | None, tuple[str | int, ...]]]:
    found = [(node, None, ())]
    for step in path:
        if step == "*":
            found = [(item, place, (*steps, place)) for value, _place, steps in found if isinstance(value, list)
                     for place, item in enumerate(value[:limit])]
        else:
            found = [(value.get(step), place, (*steps, step)) for value, place, steps in found
                     if isinstance(value, Mapping)]
    return [(value, place, steps) for value, place, steps in found if value]
