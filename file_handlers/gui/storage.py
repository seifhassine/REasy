"""See docs/internals/gui-format.md."""

from __future__ import annotations

import copy
import functools
from collections.abc import Iterable, Mapping
from typing import NamedTuple

lazy from file_handlers.clip.model import walk_nodes, walk_properties
lazy from file_handlers.clip.rules import PropertyType, property_type

from .model import GuiAnimation, GuiDocument, GuiObject, GuiProperty, GuiSymbol, GuiValue


class Stored(NamedTuple):
    name: str
    type: PropertyType
    value: GuiValue
    mask: int = -1


def nodes(animation: GuiAnimation) -> list:
    return animation.clip.roots[0].children if animation.clip.roots else []


@functools.cache
def places(kind: int) -> dict[str, int]:
    return {name: place for place, (name, _kind) in enumerate(property_type(kind).members)}


def track_mask(track) -> int:
    if not track.children:
        return -1
    mask = 0
    for member in track.children:
        mask |= 1 << places(track.type)[member.name]
    return mask


def animated(symbol: GuiSymbol) -> dict[int, dict[str, int]]:
    targets: dict[str, GuiObject] = {}
    for obj in symbol.objects:
        targets.setdefault(obj.name, obj)
    result: dict[int, dict[str, int]] = {}
    for animation in symbol.animations:
        for node in nodes(animation):
            if node.name not in targets:
                continue
            masks = result.setdefault(id(targets[node.name]), {})
            for track in node.properties:
                held, mask = masks.get(track.name, 0), track_mask(track)
                masks[track.name] = -1 if -1 in (held, mask) else held | mask
    return result


def stored(obj: GuiObject, masks: Mapping[str, int]) -> tuple[list[Stored], list[Stored]]:
    properties, defaults = [], []
    for prop in obj.properties:
        mask = masks.get(prop.name)
        if mask is None:
            properties.append(Stored(prop.name, prop.type, prop.value))
        elif mask == -1:
            defaults.append(Stored(prop.name, prop.type, prop.value))
        else:
            everything = (1 << len(places(prop.type))) - 1
            properties.append(Stored(prop.name, prop.type, prop.value, everything & ~mask))
            defaults.append(Stored(prop.name, prop.type, prop.value, mask))
    return properties, defaults


def baselines(symbol: GuiSymbol) -> dict[int, list[Stored]]:
    masks = animated(symbol)
    return {id(obj): stored(obj, masks[id(obj)])[1] for obj in symbol.objects if id(obj) in masks}


def masked(current: GuiValue, record: Stored) -> GuiValue:
    if record.mask == -1 or current is None or not isinstance(record.value, list):
        return copy.deepcopy(record.value)
    return [new if record.mask >> place & 1 else old for place, (old, new) in enumerate(zip(current, record.value))]


def merged(properties: list[Stored], defaults: list[Stored], rank: Mapping[str, int]) -> list[GuiProperty]:
    held: dict[str, GuiProperty] = {}
    written: dict[str, int] = {}
    for record in (*properties, *defaults):
        prop = held.get(record.name)
        if prop is None:
            prop = held[record.name] = GuiProperty(record.name, record.type, None)
        elif prop.type != record.type:
            raise ValueError(f"{record.name} is stored as {prop.type.name} and as {record.type.name}")
        count = len(places(record.type)) if isinstance(record.value, list) else 0
        everything = (1 << count) - 1 if count else -1
        mask = everything if record.mask == -1 or not count else record.mask & everything
        if written.get(record.name, 0) & mask:
            raise ValueError(f"{record.name} is stored twice")
        written[record.name] = written.get(record.name, 0) | mask
        prop.value = masked(prop.value, record._replace(mask=mask if count else -1))
    for name, mask in written.items():
        value = held[name].value
        if isinstance(value, list) and mask != (1 << len(value)) - 1:
            raise ValueError(f"{name} is stored without some of its components")
    return [held[name] for name in _order([item.name for item in properties], [item.name for item in defaults], rank)]


def _order(first: list[str], second: list[str], rank: Mapping[str, int]) -> list[str]:
    if len(set(first)) != len(first) or len(set(second)) != len(second):
        raise ValueError("a property is stored twice")
    result, in_first, in_second, at, other, last = [], set(first), set(second), 0, 0, len(rank)
    while at < len(first) and other < len(second):
        one, two = first[at], second[other]
        if one == two:
            other += 1
        elif one in in_second:
            if two in in_first:
                raise ValueError(f"{one} and {two} are stored in a different order in the two property lists")
            result.append(two)
            other += 1
            continue
        elif two not in in_first and rank.get(two, last) < rank.get(one, last):
            result.append(two)
            other += 1
            continue
        result.append(one)
        at += 1
    return [*result, *first[at:], *second[other:]]


def asset_paths(document: GuiDocument, lists: Iterable[tuple[list[Stored], list[Stored]]]) -> list[str]:
    used = [record.value for pair in lists for records in pair for record in records
            if record.type == PropertyType.ASSET and record.value]
    used += [key.value for animation in document.animations for node in walk_nodes(animation.clip.roots)
             for track in walk_properties(node.properties) if track.type == PropertyType.ASSET
             for key in track.keys if key.value]
    used += [binding.property.value for binding in document.bindings
             if binding.property.type == PropertyType.ASSET and binding.property.value]
    return list(dict.fromkeys(used))
