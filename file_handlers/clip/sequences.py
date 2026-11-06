"""See docs/internals/clip-format.md (.dlgtml)."""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Iterator

lazy from utils.hash_util import murmur3_hash
lazy from utils.hex_util import align

lazy from file_handlers.native_math import fadd, fsub

from .errors import ClipFormatError
from .model import Key, Node, Property, SequenceTrackInfo, Timeline, grouped, walk_nodes
from .rules import InterpolationType as Mode
from .rules import dialogue_tracks, property_type

_EVENTS = (Mode.EVENT, Mode.PASS_EVENT)
_BOUNDS = (Mode.DISCRETE, Mode.NONE, *_EVENTS)


def read_sequences(data: bytes, at: int, base: int, records: dict) -> list[SequenceTrackInfo]:
    head = records["sequence_info"].decode(data, at)
    count, ranges_at, names_at = head["node_count"], base + head["range_table"], base + head["name_table"]
    nodes_at = base + head["node_table"]
    if head["root_count"] > count or max(nodes_at + count * records["sequence_node"].size, names_at,
                                         ranges_at + head["range_count"] * records["sequence_range"].size) > len(data):
        raise ClipFormatError("The sequence track table overruns the file")
    nodes = records["sequence_node"].decode_table(data, nodes_at, count)
    ranges = records["sequence_range"].decode_table(data, ranges_at, head["range_count"])
    infos = [SequenceTrackInfo(_name(data, names_at, node["name"])) for node in nodes]
    for index, (info, node) in enumerate(zip(infos, nodes)):
        start, end = node["link"], node["link"] + (node["range_count"] or node["child_count"])
        if node["range_count"]:
            if end > len(ranges):
                raise ClipFormatError(f"Sequence track {info.name!r} links past the range table")
            info.ranges = [(item["begin"], item["end"]) for item in ranges[start:end]]
        elif node["child_count"]:
            if start <= index or end > count:
                raise ClipFormatError(f"Sequence track {info.name!r} links outside the nodes after it")
            info.children = infos[start:end]
    roots = infos[:head["root_count"]]
    if write_sequences(roots, at - base, records) != data[at:]:
        raise ClipFormatError("The sequence track table is not laid out as the game's files are")
    return roots


def write_sequences(roots: list[SequenceTrackInfo], at: int, records: dict) -> bytes:
    head, node, item = records["sequence_info"], records["sequence_node"], records["sequence_range"]
    order = grouped([roots], lambda info: info.children)
    index = {id(info): position for position, info in enumerate(order)}
    starts, ranges, stack = {}, [], list(reversed(roots))
    while stack:
        info = stack.pop()
        starts[id(info)] = len(ranges)
        ranges += info.ranges
        stack += reversed(info.children)
    names, pool = {}, bytearray()
    for info in order:
        if info.name not in names:
            names[info.name] = len(pool) // 2
            pool += info.name.encode("utf-16-le") + b"\0\0"
    nodes_at = at + head.size
    ranges_at = nodes_at + node.size * len(order)
    out = bytearray(head.encode({"root_count": len(roots), "node_count": len(order), "range_count": len(ranges),
                                 "node_table": nodes_at, "range_table": ranges_at,
                                 "name_table": ranges_at + item.size * len(ranges)}))
    for info in order:
        link = starts[id(info)] if info.ranges else index[id(info.children[0])] if info.children else 0
        out += node.encode({"range_count": len(info.ranges), "child_count": len(info.children),
                            "name_hash": murmur3_hash(info.name.encode("utf-16-le")), "name": names[info.name],
                            "link": link})
    for begin, end in ranges:
        out += item.encode({"begin": begin, "end": end})
    out += pool
    return bytes(out + bytes(align(at + len(out), 8) - at - len(out)))


def sequence_leaves(timeline: Timeline) -> Iterator[tuple[SequenceTrackInfo, list[Property], str, bool]]:
    if not timeline.sequences:
        return
    nodes: dict[str, list[Node]] = defaultdict(list)
    for node in walk_nodes(timeline.roots):
        if node.properties:
            nodes[node.name].append(node)
    boxed = dialogue_tracks(timeline.version, "box_tracks")
    for root in timeline.sequences:
        found = nodes[root.name]
        stack = [(info, found[0].properties if found else [], root.name) for info in root.children]
        del found[:1]
        while stack:
            info, level, path = stack.pop()
            hits, path = [prop for prop in level if prop.name == info.name], f"{path}/{info.name}"
            if info.children:
                stack += [(child, [prop for hit in hits for prop in hit.children], path) for child in info.children]
            else:
                yield info, hits, path, path in boxed or any(
                    (kind := property_type(hit.type)) is not None and kind.keys_since is None
                    and kind.children in ("fields", "elements") for hit in hits)


def derived_ranges(timeline: Timeline, leaves: list | None = None
                   ) -> Iterator[tuple[SequenceTrackInfo, list[tuple[float, float]]]]:
    for info, hits, _path, boxed in sequence_leaves(timeline) if leaves is None else leaves:
        yield info, _ranges(hits, timeline.total_frame) if boxed else []


def key_role(keys: list[Key]) -> str:
    first = keys[0].interpolation
    if first in _EVENTS:
        return "event"
    if first not in (Mode.DISCRETE, Mode.NONE):
        return "segment"
    if first == Mode.DISCRETE and len(keys) > 1 and all(
            key.interpolation in _BOUNDS and key.value == keys[0].value for key in keys):
        return "background"
    return "hold"


def kept_boxes(stored: list[tuple[float, float]], derived: list[tuple[float, float]]) -> list | None:
    left = list(stored)
    for pair in derived:
        if pair not in left:
            return None
        left.remove(pair)
    return left if sorted([*derived, *left]) == stored else None


def _ranges(instances: list[Property], total: float) -> list[tuple[float, float]]:
    clips: dict[tuple[float, float], Counter] = defaultdict(Counter)
    starts, ends, last = [], [], fadd(total, 1.0)
    stack = [(prop, prop.name) for prop in instances]
    while stack:
        prop, path = stack.pop()
        stack += [(child, f"{path}/{child.name}") for child in prop.children]
        keys = prop.keys
        role = key_role(keys) if keys else ""
        if role == "segment":
            clips[prop.begin_frame, _last_frame(keys, prop.end_frame)][path] += 1
        elif role == "background":
            for at, key in enumerate(keys):
                frame = last if 0 < last - key.frame <= math.ulp(key.frame) * 2 ** 29 else key.frame
                (starts if at and keys[at - 1].interpolation == Mode.DISCRETE != key.interpolation else ends).append(
                    frame)
    names = {pair: {path.rsplit("/", 1)[-1] for path in paths} for pair, paths in clips.items()}
    best, several = Counter(), []
    for (begin, end), found in names.items():
        best[begin] = max(best[begin], len(found))
        if len(found) > 1:
            several.append((begin, end))
    voted = sorted((begin, end) for (begin, end), found in names.items() if 2 * len(found) >= best[begin] and (
        len(found) > 1 or not any(other <= begin and end <= other_end for other, other_end in several)))
    firsts, lasts, out = [begin for begin, _end in voted], sorted(end for _begin, end in voted), []
    starts, ends = [(frame, fadd(frame, 1.0)) for frame in starts], [(frame, fsub(frame, 1.0)) for frame in ends]
    for begin, end in voted:
        at, after, over = bisect_left(firsts, begin), bisect_right(firsts, begin), bisect_right(lasts, end)
        previous, following = firsts[at - 1] if at else -math.inf, firsts[after] if after < len(firsts) else math.inf
        later = lasts[over] if over < len(lasts) else math.inf
        out += [(max((start for frame, start in starts if previous <= frame and start <= begin), default=begin),
                 min((stop for frame, stop in ends if frame <= following and end <= stop < later), default=end))
                ] * max(clips[begin, end].values())
    return out


def _last_frame(keys: list[Key], end_frame: float) -> float:
    if len(keys) == 1:
        return end_frame
    before, last = keys[-2], keys[-1]
    if last.frame - before.frame <= 1.0 and (len(keys) > 2 or before.interpolation == Mode.DISCRETE_TO_END
                                             and last.value != before.value):
        return min(before.frame, end_frame)
    return min(last.frame, end_frame)


def _name(data: bytes, pool: int, offset: int) -> str:
    start = end = pool + 2 * offset
    while end + 2 <= len(data) and data[end:end + 2] != b"\0\0":
        end += 2
    return data[start:end].decode("utf-16-le", "replace")
