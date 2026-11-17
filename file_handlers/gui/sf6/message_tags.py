"""See docs/internals/gui-rendering.md, Script tags."""
from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..dmc5.text import TextLayoutGap
from .input_icons import InputSymbols

_INTEGER = re.compile(r"[\t\n\v\f\r ]*([+-]?[0-9]+)[\t\n\v\f\r ]*")
_UINT = (0, 0xFFFFFFFF)
_INT = (-0x80000000, 0x7FFFFFFF)
_FORMAT = re.compile(r"(\{\{|\}\})|\{([0-9]+)\}|[{}]")


@dataclass(frozen=True, slots=True)
class Sf6TagState:
    tags: Mapping[str, Any]
    language: int
    language_index: Callable[[str], int]
    message: Callable[[str], str | None]
    named: Callable[[str], str | None]
    tables: Callable[[], Mapping[str, Mapping[int, Mapping[str, Any]]] | None]
    enum: Callable[[str], Mapping[str, int]]
    keyboard: bool
    inputs: Callable[[], Mapping[str, Mapping[str, Any]] | None]
    key_name: Callable[[int], str]


def _integer(text: str | None, limits: tuple[int, int]) -> int | None:
    found = text is not None and _INTEGER.fullmatch(text)
    return int(found[1]) if found and limits[0] <= int(found[1]) <= limits[1] else None


def _args(name: str, parameter: str, rule: Mapping[str, Any]) -> dict[str, str]:
    for character in rule["strip"]:
        parameter = parameter.replace(character, "")
    parts = parameter.split(rule["quote"])[:-1]
    keys = [part.replace(rule["assign"], "") for part in parts if rule["assign"] in part]
    values = [part for part in parts if rule["assign"] not in part]
    if len(keys) != len(values):
        return {}
    if len(set(keys)) != len(keys):
        raise TextLayoutGap(f"its <{name}> tag names an argument twice: the game's script throws")
    return dict(zip(keys, values))


def _uint(name: str, parameter: str, state: Sf6TagState) -> int | None:
    rule = state.tags["args"]
    return _integer(_args(name, parameter, rule).get(f"{rule['uint_key']}0"), _UINT)


def _table(name: str, state: Sf6TagState, table: str) -> Mapping[int, Mapping[str, Any]]:
    if (tables := state.tables()) is None or table not in tables:
        raise TextLayoutGap(f"its <{name}> tag reads a table the game's scenes load: shown as the game shows it"
                            if tables is None else f"its <{name}> tag reads a table that was not read")
    return tables[table]


def _record(name: str, parameter: str, state: Sf6TagState, rule: Mapping[str, Any]) -> Mapping[str, Any] | None:
    return None if (found := _uint(name, parameter, state)) is None else _table(name, state, rule["table"]).get(found)


def _highlight(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["highlight"]
    record = _record(name, parameter, state, rule)
    return "" if record is None else record[rule["fields"][name]]


def _platform(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["platform_message"]
    listed = (_record(name, parameter, state, rule) or {}).get(rule["list"])
    if not listed:
        return ""
    if (text := state.message(listed[0][rule["guid"]])) is None:
        raise TextLayoutGap(f"its <{name}> tag names a message that is not loaded: the game's script throws")
    return text


def _reference(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["input_reference"]
    found = _args(name, parameter, state.tags["args"]).get(rule["key"])
    return "" if found is None or state.keyboard != rule["keyboard"][name] else rule["head"] + found + rule["tail"]


def _command(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["command"]
    value = state.enum(rule["enum"]).get(parameter, -1)
    return rule["texts"][value] if 0 <= value < len(rule["texts"]) else rule["invalid"]


def _cased(text: str, state: Sf6TagState, upper: bool) -> str:
    lower, capital = state.tags["case"]
    return text.translate(str.maketrans(lower, capital) if upper else str.maketrans(capital, lower))


def _title(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["title"]
    if parameter == rule["kept"] or not parameter:
        return parameter
    return _cased(parameter[0], state, True) + "".join(
        _cased(character, state, before in rule["after"]) for before, character in zip(parameter, parameter[1:]))


def _type_icon(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["type_icon"]
    if parameter not in rule["modes"]:
        return ""
    suffix = next((held for key, held in rule["suffixes"].items() if state.language_index(key) == state.language), None)
    return (rule["head"] + rule["names"][rule["modes"].index(parameter)] + (
        "" if suffix is None else rule["separator"] + suffix) + rule["tail"])


def _formatted(value: str, rule: Mapping[str, Any], group: str, threshold: int, point: str, groups: list[int] | None,
               precision: int, pad: int, fill: str) -> str:
    if any(character not in rule["digits"] and (character != rule["sign"] or at) for at, character in enumerate(value)):
        return value
    whole, *rest = value.split(rule["point"])
    fraction, point = (rest[0], point) if rest else ("", point if precision >= 2 else "")
    while len(whole) < pad and fill:
        whole = fill + whole
    if groups and (len(whole) >= threshold or threshold == -1):
        parts, at, index = [], len(whole), len(groups) - 1
        while at > 0:
            if at < groups[index]:
                parts.append(whole[:at])
                break
            at -= groups[index]
            parts.append(whole[at:at + groups[index]])
            index -= index > 0
        whole = group.join(reversed(parts))
    if precision >= 0:
        fraction = (fraction + rule["zero"] * precision)[:precision]
    return whole + point + fraction


def _number(name: str, parameter: str, state: Sf6TagState) -> str:
    rule, found = state.tags["number"], _args(name, parameter, state.tags["args"])
    keys = rule["keys"]
    if (value := found.get(keys["value"])) is None:
        return parameter
    if (preset := rule["regions"].get(_cased(found.get(keys["region"], rule["region"]), state, True))) is None:
        return value
    group, point, threshold, groups = preset
    numbers = {key: _integer(found.get(keys[key]), _INT) for key in ("threshold", "precision", "pad")}
    listed = found.get(keys["groups"])
    return _formatted(value, rule, found.get(keys["group"], group),
                      threshold if numbers["threshold"] is None or numbers["threshold"] < 0 else numbers["threshold"],
                      found.get(keys["decimal"], point), groups if listed is None else [
                          size for part in listed.split(rule["list"]) if (size := _integer(part, _INT) or 0) > 0],
                      -1 if numbers["precision"] is None else numbers["precision"], numbers["pad"] or 0,
                      found.get(keys["fill"], rule["zero"]))


def _plunum_tag(name: str, tag: str, rule: Mapping[str, Any]) -> dict[str, int]:
    found = {rule["index"]: -1, rule["digit"]: -1}
    while True:
        index, digit = tag.find(rule["index"]), tag.find(rule["digit"])
        key, at = (rule["digit"], digit) if index < 0 or 0 <= digit <= index else (rule["index"], index)
        if at < 0:
            return found
        start = tag.find(rule["quote"], at + 1)
        if (end := tag.find(rule["quote"], start + 1)) < 0:
            raise TextLayoutGap(f"its <{name}> tag's message leaves a PLUNUM value unquoted: the game's script throws")
        value = _integer(tag[start + 1:end], _INT)
        found[key], tag = -1 if value is None else value, tag[end + 1:]


def _plunum(name: str, text: str, numbers: list[int], rule: Mapping[str, Any]) -> str:
    tags, start, held = [], 0, text.replace(rule["lower"], rule["open"])
    while (at := held.find(rule["open"], start)) >= 0 and len(tags) < len(numbers):
        if (end := held.find(rule["close"], at)) < 0 < at:
            raise TextLayoutGap(f"its <{name}> tag's message leaves a PLUNUM tag open: the game's script throws")
        tags.append(_plunum_tag(name, held[at:end + 1], rule))
        held, start = held[:at] + rule["placeholder"] + held[end + 1:], at + 1
    if len(tags) != len(numbers):
        return text
    places: list[dict | None] = [None] * len(tags)
    for tag in tags:
        if (index := tag[rule["index"]]) == -1:
            continue
        if index < 0:
            raise TextLayoutGap(f"its <{name}> tag's message gives a PLUNUM index below 0: the game's script throws")
        if index >= len(tags) or places[index] is not None:
            return text
        places[index] = tag
    for tag in tags:
        if tag[rule["index"]] == -1:
            places[places.index(None)] = tag
    if len(parts := held.split(rule["placeholder"])) != len(tags) + 1:
        return text
    if any(tag[rule["digit"]] < -1 for tag in tags):
        raise TextLayoutGap(f"its <{name}> tag's message gives a PLUNUM digit below 0: the game's script throws")
    shown = {id(tag): str(number) for tag, number in zip(places, numbers)}
    return "".join(part + ("" if at == len(tags) else shown[id(tags[at])].rjust(tags[at][rule["digit"]], rule["fill"]))
                   for at, part in enumerate(parts))


def _format(name: str, text: str, state: Sf6TagState, numbers: Sequence[int] = (), strings: Sequence[str] = ()) -> str:
    rule = state.tags["message_format"]
    if any(item in text for item in rule["speaker"]):
        raise TextLayoutGap(f"its <{name}> tag's message names a speaker or listener, which REasy does not fill")
    if numbers and any(item in text for item in rule["numbers"]):
        text = _plunum(name, text, numbers, state.tags["plunum"])
        if any(item in text for item in rule["plural"]):
            raise TextLayoutGap(f"its <{name}> tag's message holds PLURAL tags, which REasy does not fill")
    if not strings:
        return text
    parts = _FORMAT.split(text)
    items = [escape[0] if escape else strings[int(index)] if index and int(index) < len(strings) else None
             for escape, index in zip(parts[1::3], parts[2::3])]
    if None in items:
        raise TextLayoutGap(f"its <{name}> tag's message holds a format item REasy does not fill")
    return "".join(plain + item for plain, item in zip(parts[::3], [*items, ""]))


def _trimmed(name: str, parameter: str, quote: str) -> str | None:
    if len(parameter) < 2:
        return None
    if not (text := parameter.strip(quote)):
        raise TextLayoutGap(f"its <{name}> tag's parameter is only quotes: the game's script throws")
    return text


def _local(name: str, parts: list[int]) -> datetime:
    try:
        return datetime(*parts) + datetime.now().astimezone().utcoffset()
    except (ValueError, OverflowError) as error:
        raise TextLayoutGap(f"its <{name}> tag holds a date the game's script refuses: it throws") from error


def _common(name: str, number: int, numbers: list[int], state: Sf6TagState) -> str:
    rule = state.tags["common_message"]
    record = _table(name, state, rule["table"]).get(number)
    if record is None or (text := state.message(record[rule["guid"]])) is None:
        raise TextLayoutGap(f"its <{name}> tag names a message that is not loaded: the game's script throws")
    return _format(name, text, state, numbers)


def _time(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["time_text"]
    parts = [] if (text := _trimmed(name, parameter, rule["quote"])) is None else text.split(rule["separator"])
    if len(parts) != 2 or None in (found := [_integer(part, _INT) for part in parts]):
        return ""
    shown = _local(name, [*rule["date"], *found])
    return _common(name, rule["message"], [shown.hour, shown.minute], state)


def _date(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["date_time"]
    if (text := _trimmed(name, parameter, rule["quote"])) is None:
        return ""
    parts = re.split(f"[{re.escape(rule['separators'])}]", text)
    found = [_integer(part, _INT) for part in parts[:rule["fields"] + 1]]
    if len(parts) < rule["fields"] or None in found[:rule["fields"]]:
        return ""
    shown = _local(name, [*found[:rule["fields"]], *(value or 0 for value in found[rule["fields"]:])])
    if shown == datetime.min:
        return ""
    return _common(name, rule["message"], [shown.year, shown.month, shown.day, shown.hour, shown.minute], state)


def _span(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["date_span"]
    if len(parts := parameter.split(rule["separator"])) != 2:
        return ""
    dates = [_date(name, part, state) for part in parts]
    if (text := state.named(rule["message"])) is None:
        raise TextLayoutGap(f"its <{name}> tag names a message that is not loaded: the game's script throws")
    return _format(name, text, state, strings=dates)


def _item(name: str, parameter: str, state: Sf6TagState) -> str:
    rule = state.tags["item"]
    parts = parameter.split(rule["separator"])
    found = [_integer(part, _UINT) for part in parts] if len(parts) == 2 else [None]
    if None in found or found[0] not in state.enum(rule["enum"]).values():
        return ""
    raise TextLayoutGap(f"its <{name}> tag names an item from the player's inventory")


def _shop_item(name: str, parameter: str, state: Sf6TagState) -> str:
    if _integer(parameter, _UINT) is None:
        return ""
    raise TextLayoutGap(f"its <{name}> tag names a product of the game's online shop")


def _word_list(name: str, parameter: str, state: Sf6TagState) -> str:
    if _integer(_args(name, parameter, state.tags["args"]).get(state.tags["word_list"]["key"]), _UINT) is None:
        return ""
    raise TextLayoutGap(f"its <{name}> tag runs a command the game's script registers while it runs")


def _input_icon(name: str, parameter: str, state: Sf6TagState) -> str:
    rule, found = state.tags["input_icon"], _args(name, parameter, state.tags["args"])
    keys, kind = rule["keys"], rule["types"].get(found.get(rule["keys"]["type"]))
    value = None if keys["id"] not in found or kind is None else state.enum(rule["enums"][kind]).get(found[keys["id"]])
    if value is None:
        return ""
    if (files := state.inputs()) is None or any(file not in files for file in rule["files"]):
        raise TextLayoutGap(f"its <{name}> tag reads input assignments the game's scenes load: shown as the game shows "
                            f"it" if files is None else f"its <{name}> tag reads input assignments that were not read")
    mode = found.get(keys["mode"])
    return InputSymbols(rule, files, state.enum, state.key_name, state.keyboard, rule["modes"].index(mode)
                        if mode in rule["modes"] else rule["control"]).text(kind, value)


_HANDLERS = {"highlight": _highlight, "platform_message": _platform, "input_reference": _reference,
             "command": _command, "type_icon": _type_icon, "title": _title, "number": _number, "time_text": _time,
             "date_time": _date, "date_span": _span, "item": _item, "shop_item": _shop_item, "word_list": _word_list,
             "input_icon": _input_icon, "upper": lambda _name, parameter, state: _cased(parameter, state, True),
             "lower": lambda _name, parameter, state: _cased(parameter, state, False)}


def resolve_sf6_text_tag(name: str, parameter: str, state: Sf6TagState) -> str:
    kind = state.tags["handlers"].get(name)
    return "" if kind is None else _HANDLERS[kind](name, parameter, state)
