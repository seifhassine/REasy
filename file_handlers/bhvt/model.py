"""See docs/internals/bhvt-format.md."""

from __future__ import annotations

from dataclasses import dataclass


class Record:
    def __init__(self, fields: dict):
        self.__dict__.update({name: [Record(item) if isinstance(item, dict) else item for item in value]
                              if isinstance(value, list) else value for name, value in fields.items()})

    def fields(self) -> dict:
        return {name: [item.fields() if isinstance(item, Record) else item for item in value]
                if isinstance(value, list) else value for name, value in vars(self).items()}


class Node(Record):
    pass


@dataclass(eq=False)
class Tree:
    version: int
    header: Record
    nodes: list[Node]
    action_ex_ids: list[int]
    static_action_ex_ids: list[int]
    blocks: dict[str, object]
    reference_prefab_game_objects: list[Record]
    resource_paths: list[str]
    variable: object
    base_variables: list[object]


@dataclass(eq=False)
class MotionFsm:
    version: int
    header: Record
    tree: Tree
    transition_maps: list[Record]
    transition_data: list[Record]
    transition_extras: list[Record]


@dataclass(eq=False)
class TimelineTree:
    name: str
    layer_id: bytes
    tree: Tree


@dataclass(eq=False)
class TimelineEntry:
    name: str
    timeline_id: bytes
    timeline: object


@dataclass(eq=False)
class TimelineFsm:
    version: int
    trees: list[TimelineTree]
    timelines: list[TimelineEntry]
    hold_points: list[Record]


@dataclass(eq=False)
class Hfsm:
    version: int
    header: Record
    root: Record
    blocks: dict[str, object]
    conditions: list[Record]
    resource_paths: list[str]
    variable: object


@dataclass(eq=False)
class MotionFsmEntry:
    info: Record
    hfsm: Hfsm
    transitions: list[Record]


@dataclass(eq=False)
class MotionFsmV1:
    version: int
    fsms: list[MotionFsmEntry]
