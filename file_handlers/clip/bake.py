"""See docs/internals/clip-format.md (.dlgtml, Bake bits)."""

from __future__ import annotations

import functools
import operator
import re
from collections.abc import Iterator

from .model import FLAG_NAMES, Key, Property, Timeline, walk_nodes
from .rules import InterpolationType as Mode
from .rules import dialogue_tracks, display_names, interpolation, property_type, rules_for
from .sequences import key_role, sequence_leaves

_ELEMENT = re.compile(r"\[\d+\](?=/|$)")
_FLAGS = operator.attrgetter(*FLAG_NAMES)
_KEY_FIELDS = ("width", "rate", "instance_value")
_KEY_BITS = operator.attrgetter(*_KEY_FIELDS)
_HOLDS = (True, *(False,) * (len(FLAG_NAMES) - 1))
_CLEAR = (False,) * len(FLAG_NAMES)


def bake_changes(timeline: Timeline, leaves: list | None = None) -> Iterator[tuple[Property, dict, list]]:
    rules, version = rules_for(timeline.version, timeline.kind), timeline.version
    sets, controlled, types, records = dialogue_tracks(version, "set_defaults"), _control_codes(), {}, {}
    found = {id(hit): (boxed, _ELEMENT.sub("[*]", path))
             for _info, hits, path, boxed in (sequence_leaves(timeline) if leaves is None else leaves) for hit in hits}
    for node in walk_nodes(timeline.roots):
        stack = [(prop, None, "") for prop in reversed(node.properties)]
        while stack:
            prop, boxed, field = stack.pop()
            boxed, field = found.get(id(prop), (boxed, field))
            if (kind := types.get(prop.type)) is None:
                info = property_type(prop.type)
                kind = types[prop.type] = info, rules.compact_default(info, version), \
                    info is not None and info.children == "members"
            info, default, members = kind
            if prop.children:
                stack += [(child, boxed, field if not boxed or members else
                           f"{field}/{_ELEMENT.sub('[*]', child.name) if '[' in child.name else child.name}")
                          for child in reversed(prop.children)]
            if keys := prop.keys:
                codes = {key.interpolation for key in keys}
                last = next((key.interpolation for key in reversed(keys) if key.interpolation != Mode.DISCRETE), None)
                flags = _HOLDS if last == Mode.DISCRETE_TO_END else _CLEAR
                compact = False if not controlled.isdisjoint(codes) else \
                    _keyed(keys, codes, boxed, field, sets, default)
            else:
                flags = _CLEAR
                compact = (False if boxed else default) if prop.children else None if boxed else default
            changes = {} if _FLAGS(prop) == flags else {
                name: value for name, value in zip(FLAG_NAMES, flags) if getattr(prop, name) != value}
            if compact is not None and compact != prop.compact_keys:
                changes["compact_keys"] = compact
            fits = []
            if keys or prop.extra_keys:
                table = prop.compact_keys if compact is None else compact
                if (wanted := records.get((prop.type, table))) is None:
                    names = rules.key_record(info, version, table).names
                    wanted = records[prop.type, table] = (rules.width_default if "width" in names else 0, 0.0, False)
                fits = [(key, {name: value for name, value in zip(_KEY_FIELDS, wanted) if getattr(key, name) != value})
                        for key in (*keys, *prop.extra_keys.values()) if _KEY_BITS(key) != wanted]
            if changes or fits:
                yield prop, changes, fits


def _keyed(keys: list[Key], codes: set[int], boxed: bool | None, field: str, sets: frozenset[str],
           default: bool) -> bool:
    role = key_role(keys)
    if not boxed:
        return False if boxed is False and role == "background" else default
    if role == "background":
        return not (Mode.EVENT in codes and field in sets)
    return default if role == "segment" else role != "event"


@functools.cache
def _control_codes() -> frozenset[int]:
    return frozenset(code for code in display_names("InterpolationType")
                     if (mode := interpolation(code)) is not None and mode.control)
