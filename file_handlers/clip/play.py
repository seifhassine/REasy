"""See docs/internals/sequence-tracks.md, How the engine plays a sequence."""

from __future__ import annotations

from file_handlers.native_math import f32, fadd, fsub

from .evaluation import NUMERIC_TYPES, sample, surrounding_keys
from .model import Key, Node, Property
from .rules import InterpolationType as Mode
from .rules import PropertyType

EVENTS = (Mode.EVENT, Mode.PASS_EVENT)
INTERPOLATED = frozenset(Mode) - {*EVENTS, Mode.DISCRETE, Mode.DISCRETE_TO_END, Mode.RANGEV2, Mode.NONE, Mode.UNKNOWN}


def animatable(prop: Property, start: float, end: float, forced: bool = False, mode: int = 0) -> bool:
    begin, finish = f32(prop.begin_frame), f32(prop.end_frame)
    if not forced and not prop.set_after_end_frame:
        if start < end:
            return (begin <= start or begin <= end) and (start <= finish or end <= finish)
        return begin <= end <= finish
    if mode == 1:
        return begin <= start or begin <= end
    return not (start < begin and end < begin) and (start <= finish or end <= finish)


def play(prop: Property, start: float, end: float, current: object = None, *, events: bool = True,
         forced: bool = False, mode: int = 1) -> list:
    """``mode``: 1 a motion sequence's player, 0 a clip's own, 2-4 the LastKey/ExtraKey passes; ``current``: the field's
    value, which instance-value keys keep."""
    if mode > 1:
        key = prop.extra_keys.get(mode - 2)
        return [] if key is None else [_held(prop, key, current)]
    keys = prop.keys
    if not keys:
        return []
    start, end = f32(start), f32(end)
    if all(key.interpolation in EVENTS for key in keys):
        if not events or not animatable(prop, start, end, forced, mode):
            return []
        return [_held(prop, key, current) for key in _crossed(keys, start, end)]
    if not animatable(prop, start, end, forced, mode):
        return []
    at = end if mode != 0 or end <= f32(prop.end_frame) else f32(prop.end_frame)
    left, right = surrounding_keys(keys, at)
    if left is None:
        return [_held(prop, right, current)]
    frame, kind = f32(left.frame), left.interpolation
    if right is None and kind == Mode.EVENT and start < end and start == frame:
        return []
    if kind in (Mode.EVENT, Mode.NONE):
        written = (events or kind == Mode.NONE) and (start < frame or start == 0 or at == frame)
    elif kind == Mode.PASS_EVENT:
        written = events and start <= frame < at
    elif kind == Mode.RANGEV2:
        stop = fsub(fadd(f32(float(left.width)), frame), 1.0)
        written = start <= stop or at <= stop
    else:
        written = True
    if not written:
        return []
    if right is None or left is right or kind not in INTERPOLATED or prop.type not in NUMERIC_TYPES:
        return [_held(prop, left, current)]
    return [sample(prop, at)]


def _held(prop: Property, key: Key, current: object) -> object:
    if key.instance_value and not prop.compact_keys:
        return current
    return bool(key.value) if prop.type == PropertyType.BOOL else key.value


def _crossed(keys: list[Key], start: float, end: float) -> list[Key]:
    first, last = f32(keys[0].frame), f32(keys[-1].frame)
    if keys[0].interpolation == Mode.PASS_EVENT and (end <= first or start == end):
        return []
    if end < first or last < start:
        return []
    low = high = None
    for index, key in enumerate(keys):
        frame = f32(key.frame)
        if key.interpolation == Mode.PASS_EVENT:
            inside = frame < end if low is not None else start <= frame < end
        elif low is not None:
            inside = frame <= end
        elif start == end == frame:
            low = high = index
            continue
        else:
            inside = start < frame <= end
        if inside:
            if low is None:
                low = index
            else:
                high = index
        elif end < frame:
            break
    return [] if low is None else keys[low:(low if high is None else high) + 1]


def play_node(node: Node, start: float, end: float, fields: dict, *, events: bool = True, forced: bool = False,
              mode: int = 1) -> list[tuple[tuple, object]]:
    writes: list[tuple[tuple, object]] = []
    for prop in node.properties:
        _play_into(prop, start, end, fields, (), writes, events, forced, mode)
    return writes


def _play_into(prop: Property, start: float, end: float, fields: dict, path: tuple, writes: list, events: bool,
               forced: bool, mode: int) -> None:
    name = prop.name if prop.array_index < 0 else prop.array_index
    if prop.children:
        held = fields.setdefault(name, {})
        if isinstance(held, dict):
            for child in prop.children:
                _play_into(child, start, end, held, (*path, name), writes, events, forced, mode)
        return
    for value in play(prop, start, end, fields.get(name), events=events, forced=forced, mode=mode):
        fields[name] = value
        writes.append(((*path, name), value))
