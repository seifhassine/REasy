"""See docs/internals/gui-format.md."""

from __future__ import annotations

lazy from file_handlers.clip.model import Node

from .model import GuiAnimation, GuiObject, GuiSymbol

UNSET = -1.0


def _span(frames: list[float]) -> tuple[float, float]:
    return (min(frames), max(frames)) if frames else (UNSET, UNSET)


def shape(animation: GuiAnimation) -> list[tuple[object, dict]]:
    clip = animation.clip
    root, wanted, first, length = clip.roots[0], [], UNSET, UNSET
    for node in root.children:
        begin, end = UNSET, UNSET
        for track in node.properties:
            frames = [key.frame for key in track.keys]
            for member in track.children:
                mine = [key.frame for key in member.keys]
                wanted.append((member, _span(mine)))
                frames += mine
            begin, last = _span(frames)
            wanted.append((track, (begin, last)))
            end = max(end, last)
        wanted.append((node, (begin, end)))
        first, length = begin, max(length, end)
    wanted.append((root, (first, length)))
    changes = [(target, {"begin_frame": begin, "end_frame": end}) for target, (begin, end) in wanted
               if (target.begin_frame, target.end_frame) != (begin, end)]
    return changes if clip.total_frame == length else [*changes, (clip, {"total_frame": length})]


def object_node(obj: GuiObject) -> Node:
    guid = obj.instance_guid.bytes_le
    return Node(name=obj.name, begin_frame=UNSET, end_frame=UNSET, guid=guid, node_id=guid)


def clip_root(symbol: GuiSymbol, guid: bytes) -> Node:
    return Node(name=symbol.name, begin_frame=UNSET, end_frame=UNSET, guid=guid, node_id=guid,
                children=[object_node(obj) for obj in symbol.objects])
