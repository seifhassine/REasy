"""See docs/internals/clip-format.md (MotClips)."""

from __future__ import annotations

from collections.abc import Iterator

lazy from utils.hash_util import murmur3_hash

from .model import Node, Property, Timeline, TrackInterval
from .rules import InterpolationType

Owners = tuple[bool, dict[str, dict]]


def _keyed(props: list[Property]) -> Iterator[list[Property]]:
    for prop in props:
        same = [held for held in props if held.name == prop.name]
        if prop.keys:
            yield same
        else:
            yield from _keyed([child for held in same for child in held.children])


def boxes(props: list[Property], flag: str) -> list[tuple[float, int]]:
    found = []
    for source in [prop for prop in props if prop.name == flag] if flag else next(_keyed(props), []):
        close = None
        for key in source.keys:
            closing, close = key.frame == close, None
            if not closing and (not flag or key.value is True):
                width = max(key.width, 1)
                found.append((key.frame, width))
                close = source.end_frame if key.interpolation == InterpolationType.DISCRETE_TO_END else \
                    key.frame + width - 1 if width > 1 else None
    return sorted(found)


def _owned(owners: Owners, track: Node) -> Iterator[tuple[int | None, list[tuple[float, int]], bool]]:
    held, classes = owners
    owner = classes.get(track.name, {})
    if "track" in owner:
        yield None, boxes(track.properties, owner["track"]), True
    for member, flag in owner.get("members", ()):
        props = [prop for prop in track.properties if prop.name == member]
        yield murmur3_hash(member.encode("utf-16-le")), boxes(
            [child for prop in props for child in prop.children] if flag else props, flag), bool(props) or not held


def baked_boxes(timeline: Timeline, owners: Owners) -> dict[tuple[int, int | None], list[tuple[float, int]]]:
    return {(id(track), ident): ranges for root in timeline.roots for track in root.children
            for ident, ranges, _made in _owned(owners, track)}


def made_entries(owners: Owners, track: Node) -> tuple[list[TrackInterval], list[TrackInterval]]:
    found = ([], [])
    for ident, ranges, made in _owned(owners, track):
        if made:
            found[ident is not None].append(TrackInterval(track, ranges, ident, True))
    return found


def track_shapes(timeline: Timeline, owners: Owners) -> dict[int, tuple]:
    return {id(track): (track.name, [ident for ident, _ranges, made in _owned(owners, track) if made])
            for root in timeline.roots for track in root.children}


def synced_tables(timeline: Timeline, owners: Owners, shapes: dict[int, tuple]) -> list[list[TrackInterval]]:
    now, tables = track_shapes(timeline, owners), []
    for level, stored in enumerate(timeline.intervals):
        table = []
        for root in timeline.roots:
            for track in root.children:
                mine, was = [entry for entry in stored if entry.track is track], shapes.get(id(track), ("",))
                if was != now[id(track)]:
                    held = {entry.property_hash: entry for entry in mine} if was[0] == track.name else {}
                    mine = [held.get(entry.property_hash, entry) for entry in made_entries(owners, track)[level]]
                table += mine
        tables.append(table)
    return tables


def missing_entries(timeline: Timeline, owners: Owners, track: Node) -> list[TrackInterval]:
    if timeline.intervals is None:
        return []
    stored = {entry.property_hash for table in timeline.intervals for entry in table if entry.track is track}
    return [TrackInterval(track, ranges, ident, True) for ident, ranges, _made in _owned(owners, track)
            if ident not in stored and (ident is None or len(timeline.intervals) > 1)]


def with_entry(timeline: Timeline, owners: Owners, entry: TrackInterval) -> list[list[TrackInterval]]:
    tracks = [id(track) for root in timeline.roots for track in root.children]
    order = [ident for ident, _ranges, _made in _owned(owners, entry.track)]

    def place(item: TrackInterval) -> tuple[int, int]:
        rank = order.index(item.property_hash) if item.property_hash in order else len(order)
        return tracks.index(id(item.track)), rank

    level = entry.property_hash is not None
    table = timeline.intervals[level]
    at = next((index for index, held in enumerate(table) if place(held) > place(entry)), len(table))
    return [[*table[:at], entry, *table[at:]] if index == level else held
            for index, held in enumerate(timeline.intervals)]


def member_name(owners: Owners, entry: TrackInterval) -> str:
    names = [member for member, _flag in owners[1].get(entry.track.name, {}).get("members", ())]
    return next((name for name in [*names, *(prop.name for prop in entry.track.properties)]
                 if murmur3_hash(name.encode("utf-16-le")) == entry.property_hash), f"0x{entry.property_hash:08X}")
