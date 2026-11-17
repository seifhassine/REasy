"""See docs/internals/gui-format.md."""

from __future__ import annotations

from functools import cache

lazy from file_handlers.clip.model import Node, ResourceKind, Timeline
lazy from file_handlers.clip.rules import rules_for

from .model import GuiAnimation, GuiObject, GuiSymbol
from .rules import gui_layout

UNSET = -1.0


def _span(frames: list[float], empty: tuple[float, float]) -> tuple[float, float]:
    return (min(frames), max(frames)) if frames else empty


@cache
def _stored(version: int) -> tuple[float, tuple[float, float], frozenset[str], frozenset[str]]:
    layout = gui_layout(version)
    rules = rules_for(layout.clip_version, ResourceKind.MOT_CLIP)
    return (layout.empty_state_length, layout.empty_track_span, frozenset(rules.header.names),
            frozenset(rules.records["node"].names))


def shape(animation: GuiAnimation, version: int) -> list[tuple[object, dict]]:
    empty, keyless, _header, fields = _stored(version)
    clip = animation.clip
    root, wanted, first, length, keyed = clip.roots[0], [], UNSET, UNSET, False
    for node in root.children:
        begin, end = UNSET, UNSET
        for track in node.properties:
            frames = [key.frame for key in track.keys]
            for member in track.children:
                mine = [key.frame for key in member.keys]
                wanted.append((member, _span(mine, keyless)))
                frames += mine
            begin, last = _span(frames, keyless)
            wanted.append((track, (begin, last)))
            end, keyed = max(end, last), keyed or bool(frames)
        wanted.append((node, (begin, end)))
        first, length = begin, max(length, end)
    wanted.append((root, (first, length)))
    changes = [(target, {"begin_frame": begin, "end_frame": end}) for target, (begin, end) in wanted
               if (not isinstance(target, Node) or "begin_frame" in fields)
               and (target.begin_frame, target.end_frame) != (begin, end)]
    total = length if keyed else empty
    return changes if clip.total_frame == total else [*changes, (clip, {"total_frame": total})]


def _node(name: str, guid: bytes, version: int, children=()) -> Node:
    fields = _stored(version)[3]
    spans = {"begin_frame": UNSET, "end_frame": UNSET} if "begin_frame" in fields else {}
    return Node(name=name, guid=guid if "guid" in fields else bytes(16),
                node_id=guid if "node_id" in fields else bytes(16), children=list(children), **spans)


def object_node(obj: GuiObject, version: int) -> Node:
    return _node(obj.name, obj.instance_guid.bytes_le, version)


def state_clip(symbol: GuiSymbol, guid: bytes, version: int) -> Timeline:
    empty, _keyless, header, _fields = _stored(version)
    return Timeline(gui_layout(version).clip_version, ResourceKind.MOT_CLIP, empty,
                    guid if "header_guid" in header else bytes(16), [],
                    [_node(symbol.name, guid, version, [object_node(obj, version) for obj in symbol.objects])])
