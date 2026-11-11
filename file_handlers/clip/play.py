"""See docs/internals/sequence-tracks.md, How the engine plays a sequence."""

from __future__ import annotations

lazy from file_handlers.engine_math import crt
from file_handlers.native_math import f32, fadd, fdiv, fmul, fsub

from .evaluation import (NUMERIC_TYPES, bezier_point, hermite_curve, hermite_parameter, hermite_value, sample,
                         stored, surrounding_keys)
from .model import Key, Node, Property
from .rules import InterpolationType as Mode
from .rules import PropertyType, property_type
from .values import default_value

EVENTS = (Mode.EVENT, Mode.PASS_EVENT)
UNKNOWN = object()
AXES = {name: index for index, (name, _kind) in enumerate(property_type(PropertyType.QUATERNION).members)}


def animatable(prop: Property, start: float, end: float, rules: dict, forced: bool = False, mode: int = 0) -> bool:
    begin, finish = f32(prop.begin_frame), f32(prop.end_frame)
    if not forced and not prop.set_after_end_frame:
        if start < end:
            return (begin <= start or begin <= end) and (start <= finish or end <= finish)
        return begin <= end <= finish
    if mode == 1 or rules["set_after_end"] == "begin":
        return begin <= start or begin <= end
    return not (start < begin and end < begin) and (start <= finish or end <= finish)


def play(prop: Property, start: float, end: float, rules: dict, current: object = None, *, events: bool = True,
         forced: bool = False, mode: int = 1, reader: str = "flat", unset: object = None) -> list:
    """``mode``: 1 a motion sequence's player, 0 a clip's own, 2-4 the LastKey/ExtraKey passes; ``current``: the field's
    value, which instance-value keys keep; ``reader``: "flat", "member" (of a type with members) or "array" (an
    element); ``unset``: what a read without a value writes (None: nothing)."""
    if prop.type == PropertyType.QUATERNION:
        return _quaternion(prop, f32(start), f32(end), rules, current, events, forced, mode)
    if mode > 1:
        key = prop.extra_keys.get(mode - 2)
        return [] if key is None else [_held(prop, key, current)]
    keys = prop.keys
    if not keys:
        return []
    start, end = f32(start), f32(end)
    if prop.type == PropertyType.ACTION:
        return _action(prop, start, end, rules, events, forced, mode)
    if prop.type == PropertyType.PATH_POINT3D:
        point = path_point(prop, start, end, rules, forced, mode)
        return [] if point is None else [point]
    reads = rules["key_reads"]
    read = reads.types.get(prop.type)
    if read is None:
        return []
    if reader == "flat" and keys[0].interpolation in EVENTS and (prop.type in reads.first_key_events or
                                                                 all(key.interpolation in EVENTS for key in keys)):
        if not events or rules["all_event_animatable"] and not animatable(prop, start, end, rules, forced, mode):
            return []
        return [_held(prop, key, current) for key in _crossed(keys, start, end, rules["all_event_kind"] == "first")]
    if not animatable(prop, start, end, rules, forced, mode):
        return []
    at = end if mode != 0 or not rules["end_clamp"] or end <= f32(prop.end_frame) else f32(prop.end_frame)
    left, right = surrounding_keys(keys, at)
    if left is None:
        return [_held(prop, right, current)]
    frame, kind = f32(left.frame), left.interpolation
    reached = start < frame or start == 0 or at == frame
    if left is right and rules["exact_key"] == "written" or (
            kind not in reads.tested and kind not in read.held and kind not in read.interpolated):
        written = right is None or left is right
        if not written and reader == "flat" and prop.type in reads.unset_zero:
            return [default_value(property_type(prop.type).value)]
        if not written:
            return [] if unset is None else [unset]
    elif kind == Mode.EVENT:
        alone = right is None and rules["element_event_alone" if reader == "member" else "event_alone"]
        written = events and (alone == "always" or reached and not (
            alone == "reached_not_started" and start < end and start == frame))
    elif kind == Mode.NONE:
        written = reached
    elif kind == Mode.PASS_EVENT:
        written = events and start <= frame < at
    elif kind == Mode.RANGEV2:
        stop = fsub(fadd(f32(float(left.width)), frame), 1.0)
        written = at <= stop or rules["range_v2"] == "reach" and start <= stop
    else:
        written = True
    if not written:
        return []
    if right is None or left is right or kind not in read.interpolated:
        return [_held(prop, left, current)]
    return [sample(prop, at, rules)] if prop.type in NUMERIC_TYPES else []


def key_frames(prop: Property, start: float, end: float, rules: dict, mode: int, current: object) -> list[tuple]:
    keys, start, end, reads = prop.keys, f32(start), f32(end), rules["key_reads"]
    if keys[0].interpolation in EVENTS and (prop.type in reads.first_key_events or
                                            all(key.interpolation in EVENTS for key in keys)):
        crossed = _crossed(keys, start, end, rules["all_event_kind"] == "first")
        after = [key is crossed[-1] for key in keys].index(True) + 1
        following = fsub(keys[after].frame, end) if after < len(keys) else 0.0
        return [(fsub(end, key.frame), following, None) for key in crossed]
    at = end if mode != 0 or not rules["end_clamp"] or end <= f32(prop.end_frame) else f32(prop.end_frame)
    left, right = surrounding_keys(keys, at)
    return [(0.0 if left is None else fsub(at, left.frame), 0.0 if right is None else fsub(right.frame, at),
             None if left is None else _held(prop, left, current))]


def _action(prop: Property, start: float, end: float, rules: dict, events: bool, forced: bool, mode: int) -> list:
    if rules["action"] == "crossed" and not events or not animatable(prop, start, end, rules, forced, mode):
        return []
    if rules["action"] == "crossed":
        return [key.value for key in _crossed(prop.keys, start, end, False)]
    left, _right = surrounding_keys(prop.keys, end)
    frame = None if left is None else f32(left.frame)
    return [] if frame is None or not (start <= frame or start == 0 or end == frame) else [left.value]


def path_point(prop: Property, start: float, end: float, rules: dict, forced: bool = False,
               mode: int = 1) -> tuple[float, ...] | None:
    keys, points = prop.keys, prop.speed_points
    if not animatable(prop, start, end, rules, forced, mode) or len(points) < 2:
        return None
    finish, first = f32(prop.end_frame), f32(keys[0].frame)
    at = finish if rules["path_end_clamp"] and finish < end else end
    span = fsub(keys[-1].frame, first)
    time = fdiv(fsub(at, first), span) if span else 0.0
    last = len(points) - 1
    found = next((index for index, point in enumerate(points) if time < f32(point.frame)), None)
    left, right = (last, last) if found is None else (found - 1 if found else last, found)
    low, high = points[left], points[right]
    width = fsub(high.frame, low.frame)
    ratio = fdiv(fsub(time, low.frame), width) if width else fsub(time, low.frame)
    if low.interpolation == Mode.LINEAR:
        distance = f32(low.rate) if low is high else fadd(fmul(fsub(1.0, ratio), low.rate), fmul(ratio, high.rate))
    elif low.interpolation == Mode.HERMITE:
        out_x, out_y, in_x, in_y = low.control
        parameter = hermite_parameter(low.frame, high.frame, time, out_x, in_x, rules["hermite"])
        distance = hermite_value(PropertyType.F32, low.rate, high.rate, parameter, out_y, in_y, rules)
    elif low.interpolation == Mode.AUTO_HERMITE:
        before = points[max(left - 1, 0)]
        distance = hermite_curve(low.rate, fmul(fsub(low.rate, before.rate), 0.5), high.rate,
                                 fmul(fsub(high.rate, low.rate), 0.5), ratio)
    else:
        return keys[0].value
    if distance in (0.0, 1.0):
        return keys[0 if distance == 0.0 else -1].value
    index = next((index for index in range(len(keys) - 1) if distance < f32(keys[index + 1].rate)), None)
    if index is None:
        return None
    left, right = keys[index], keys[index + 1]
    fraction = fdiv(fsub(distance, left.rate), fsub(right.rate, left.rate))
    if left.interpolation == Mode.LINEAR:
        return tuple(fadd(fmul(a, fsub(1.0, fraction)), fmul(b, fraction)) for a, b in zip(left.value, right.value))
    if left.interpolation == Mode.BEZIER3D and left.control is not None:
        return bezier_point(left.value, left.control[:3], left.control[4:7], right.value, fraction, rules["bezier3d"])
    return None


def _quaternion(prop: Property, start: float, end: float, rules: dict, current: object, events: bool, forced: bool,
                mode: int) -> list:
    turn = rules["quaternion"]
    rows = _rows(*(f32(value) for value in ((0.0,) * 4 if current is None else current)), turn["diagonal"])
    angles = _inverse(rows, turn) if turn["angles"] == "inverse" and mode < 2 else _legacy(rows, turn)
    written = last = None
    for child in prop.children:
        index = AXES.get(child.name[:1])
        for value in () if index is None else play(
                child, start, end, rules, angles[index], events=events, forced=forced, mode=mode, reader="member",
                unset=angles[index] if rules["element_unset"] == "current" else last):
            angles[index] = written = last = value
    return [] if written is None else [_rotation(angles, turn)]


def _rows(x: float, y: float, z: float, w: float, diagonal: str) -> tuple:
    x2, y2, z2 = fadd(x, x), fadd(y, y), fadd(z, z)
    xx, yy, zz = fmul(x, x2), fmul(y, y2), fmul(z, z2)
    xy, xz, yz, wx, wy, wz = fmul(x, y2), fmul(x, z2), fmul(y, z2), fmul(w, x2), fmul(w, y2), fmul(w, z2)
    if diagonal == "sum":
        first, second, third = fsub(1.0, fadd(zz, yy)), fsub(1.0, fadd(zz, xx)), fsub(1.0, fadd(yy, xx))
    else:
        first, second, third = fsub(fsub(1.0, yy), zz), fsub(fsub(1.0, xx), zz), fsub(fsub(1.0, xx), yy)
    return ((first, fadd(xy, wz), fsub(xz, wy)), (fsub(xy, wz), second, fadd(yz, wx)),
            (fadd(xz, wy), fsub(yz, wx), third))


def _legacy(m: tuple, turn: dict) -> list[float]:
    atan2, right = crt("atan2f"), turn["right_angle"]
    if 1.0 > m[0][2]:
        if m[0][2] > -1.0:
            return [-atan2(-m[1][2], m[2][2]), -crt("asinf")(m[0][2]), -atan2(-m[0][1], m[0][0])]
        return [atan2(m[1][0], m[1][1]), right, 0.0]
    return [-atan2(m[1][0], m[1][1]), -right, 0.0]


def _inverse(m: tuple, turn: dict) -> list[float]:
    atan2, right = crt("atan2f"), turn["right_angle"]
    if 1.0 > m[2][1]:
        angles = [-crt("asinf")(m[2][1]), -atan2(-m[2][0], m[2][2]), -atan2(-m[0][1], m[1][1])] if m[2][1] > -1.0 \
            else [right, 0.0, atan2(m[0][2], m[0][0])]
    else:
        angles = [-right, 0.0, -atan2(m[0][2], m[0][0])]
    return [fmul(angle, turn["to_degrees"]) for angle in angles]


def _rotation(angles: list[float], turn: dict) -> tuple[float, float, float, float]:
    degree, pi = turn["to_radians"]
    radians = [fmul(fmul(angle, degree), pi) for angle in angles]
    (sx, sy, sz), (cx, cy, cz) = ([crt(name)(value) for value in radians] for name in ("sinf", "cosf"))
    m = ((fadd(fmul(fmul(sx, sy), sz), fmul(cy, cz)), fmul(sz, cx), fsub(fmul(fmul(sx, sz), cy), fmul(sy, cz))),
         (fsub(fmul(fmul(sx, cz), sy), fmul(sz, cy)), fmul(cx, cz), fadd(fmul(fmul(sx, cy), cz), fmul(sy, sz))),
         (fmul(sy, cx), -sx, fmul(cx, cy)))
    trace = fadd(fadd(m[0][0], m[1][1]), m[2][2])
    if trace > 0.0:
        root = crt("sqrtf")(fadd(trace, 1.0))
        scale = fdiv(0.5, root)
        return (fmul(fsub(m[1][2], m[2][1]), scale), fmul(fsub(m[2][0], m[0][2]), scale),
                fmul(fsub(m[0][1], m[1][0]), scale), fmul(root, 0.5))
    i = 1 if m[1][1] > m[0][0] else 0
    i = 2 if m[2][2] > m[i][i] else i
    j, k = (i + 1) % 3, (i + 2) % 3
    root = crt("sqrtf")(fadd(fsub(fsub(m[i][i], m[j][j]), m[k][k]), 1.0))
    scale, result = fdiv(0.5, root), [0.0] * 4
    result[i], result[3] = fmul(root, 0.5), fmul(fsub(m[j][k], m[k][j]), scale)
    result[j], result[k] = fmul(fadd(m[i][j], m[j][i]), scale), fmul(fadd(m[i][k], m[k][i]), scale)
    return tuple(result)


def _held(prop: Property, key: Key, current: object) -> object:
    return current if key.instance_value and not prop.compact_keys else stored(prop.type, key.value)


def _crossed(keys: list[Key], start: float, end: float, first_kind: bool) -> list[Key]:
    first, last = f32(keys[0].frame), f32(keys[-1].frame)
    if keys[0].interpolation == Mode.PASS_EVENT and (end <= first or start == end):
        return []
    if end < first or last < start:
        return []
    low = high = None
    for index, key in enumerate(keys):
        frame = f32(key.frame)
        if (keys[0] if first_kind else key).interpolation == Mode.PASS_EVENT:
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


def play_node(node: Node, start: float, end: float, fields: dict, rules: dict, *, events: bool = True,
              forced: bool = False, mode: int = 1) -> list[tuple[tuple, object]]:
    writes: list[tuple[tuple, object]] = []
    members = rules["attributes"].get(node.name)
    for prop in node.properties:
        _play_into(prop, start, end, fields, (), writes, rules, events, forced, mode, "flat", members=members)
    return writes


def _play_into(prop: Property, start: float, end: float, fields: dict, path: tuple, writes: list, rules: dict,
               events: bool, forced: bool, mode: int, reader: str, name: str | int | None = None,
               buffer: object = None, members: dict | None = None) -> None:
    """``buffer``: what the read before this one wrote last (an array's reads share one); ``members``: the clip
    attributes of the type ``prop`` is a property of (attributes.json)."""
    name = prop.name if name is None else name
    if prop.type in (rules["array_no_case"] if reader == "array" else rules["key_reads"].no_case) or (
            prop.type in rules["animatable_cases"] and not animatable(prop, f32(start), f32(end), rules, forced, mode)):
        return
    found = members.get(prop.name) if members else None
    counted = found.get("count") if found and rules["array_count"] else None
    if (prop.children or counted) and prop.type != PropertyType.QUATERNION:
        held = fields.setdefault(name, {})
        info = property_type(prop.type)
        kind = "array" if info and info.children == "elements" else "member" if info and info.members else "flat"
        frame = rules["key_frame"] if prop.type == PropertyType.KEY_FRAME else None
        inner = rules["attributes"].get(prop.name) if reader == "array" else None
        if isinstance(held, dict):
            last, children = None, prop.children if frame is None else prop.children[:frame["children"]]
            for position, child in enumerate(sorted(children, key=lambda item: item.delayed_execution or inner is not
                                                    None and "delayed" in inner.get(item.name, ()))
                                             if prop.type in rules["delayed_last"] else children):
                slot = None if frame is None else frame["names"].get(child.name)
                if kind == "array":
                    slot = position if child.array_index < 0 else child.array_index
                if frame is None or slot is not None:
                    count = len(writes)
                    _play_into(child, start, end, held, (*path, name), writes, rules, events, forced, mode, kind,
                               slot, last if frame is None else None, inner)
                    last = writes[-1][1] if len(writes) > count else last
        if counted:
            fields[counted] = len(prop.children)
            writes.append(((*path, counted), len(prop.children)))
        return
    if reader == "array":
        unset = stored(prop.type, mode) if prop.type in rules["array_mode_unset"] else buffer
    else:
        unset = (fields.get(name) if rules["element_unset"] == "current" else buffer) if reader == "member" else None
    current, hands = fields.get(name), found and reader == "flat" and mode < 2 and (
        "diff" in found or "previous" in found) and prop.type in rules["key_reads"].types
    values = play(prop, start, end, rules, current, events=events, forced=forced, mode=mode, reader=reader,
                  unset=UNKNOWN if hands else unset)
    if not hands:
        for value in values:
            fields[name] = value
            writes.append(((*path, name), value))
        return
    for value, (difference, following, left) in zip(values, key_frames(prop, start, end, rules, mode, current)
                                                    if values else ()):
        if value is not UNKNOWN:
            fields[name] = value
            writes.append(((*path, name), value))
        later = [*zip(found.get("diff", ()), (difference, following))]
        if left is not None and prop.type in rules["previous_key"]:
            later.append((found.get("previous"), left))
        for member, item in later:
            if member is not None:
                fields[member] = item
                writes.append(((*path, member), item))
