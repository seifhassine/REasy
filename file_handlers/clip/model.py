"""See docs/internals/clip-format.md."""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from enum import Enum


class ResourceKind(Enum):
    CLIP = "clip"
    TIMELINE = "tml"
    USER_CURVE = "ucurve"


@dataclass(eq=False, slots=True)
class Key:
    frame: float = 0.0
    value: object = None
    interpolation: int = 0
    width: int = 0
    rate: float = 0.0
    instance_value: bool = False
    control: tuple[float, ...] | None = None


@dataclass(eq=False, slots=True)
class SpeedPoint:
    frame: float = 0.0
    rate: float = 0.0
    interpolation: int = 0
    control: tuple[float, ...] | None = None


@dataclass(eq=False, slots=True)
class Property:
    name: str = ""
    type: int = 0
    begin_frame: float = 0.0
    end_frame: float = 0.0
    array_index: int = -1
    editor_id: int = 0
    compact_keys: bool = False
    set_after_end_frame: bool = False
    set_delegate_enable: bool = False
    enum_closed: bool = False
    restoration: bool = False
    prev_diff_frame_set: bool = False
    next_diff_frame_set: bool = False
    prev_key_value_set: bool = False
    delayed_execution: bool = False
    array_count_set: bool = False
    keys: list[Key] = field(default_factory=list)
    extra_keys: dict[int, Key] = field(default_factory=dict)
    speed_points: list[SpeedPoint] = field(default_factory=list)
    children: list[Property] = field(default_factory=list)


FLAG_NAMES = ("set_after_end_frame", "set_delegate_enable", "enum_closed", "restoration", "prev_diff_frame_set",
              "next_diff_frame_set", "prev_key_value_set", "delayed_execution", "array_count_set")


@dataclass(eq=False, slots=True)
class Node:
    name: str = ""
    node_type: int = 0
    begin_frame: float = 0.0
    end_frame: float = 0.0
    guid: bytes = bytes(16)
    node_id: bytes = bytes(16)
    tag: str = ""
    editor_id: int = 0
    type_hash: int = 0
    children: list[Node] = field(default_factory=list)
    properties: list[Property] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class Clip:
    name: str = ""
    frame_in: float = 0.0
    frame_out: float = 0.0
    source_in: float = 0.0
    source_out: float = 0.0
    nodes: list[Node] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class Track:
    name: str = ""
    group: str = ""
    enabled: bool = True
    tool_flag: int = 0
    clips: list[Clip] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)

    @property
    def roots(self) -> list[Node]:
        return [*self.nodes, *(node for clip in self.clips for node in clip.nodes)]


@dataclass(eq=False, slots=True)
class Timeline:
    version: int
    kind: ResourceKind = ResourceKind.CLIP
    total_frame: float = 0.0
    header_guid: bytes = bytes(16)
    tracks: list[Track] = field(default_factory=list)
    root_order: list[Node] | None = None

    @property
    def roots(self) -> list[Node]:
        if self.root_order is not None:
            return self.root_order
        return [node for track in self.tracks for node in track.roots]


def walk_nodes(nodes: Iterable[Node]) -> Iterator[Node]:
    seen: set[int] = set()
    stack = list(reversed(list(nodes)))
    while stack:
        node = stack.pop()
        if id(node) not in seen:
            seen.add(id(node))
            yield node
            stack.extend(reversed(node.children))


def walk_properties(properties: Iterable[Property]) -> Iterator[Property]:
    stack = list(reversed(list(properties)))
    while stack:
        prop = stack.pop()
        yield prop
        stack.extend(reversed(prop.children))
