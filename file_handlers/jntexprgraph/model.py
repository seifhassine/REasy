"""See docs/internals/jntexprgraph-format.md."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(eq=False, slots=True)
class Param:
    pin: int
    recv_type: int
    set_type: int
    value: object


@dataclass(eq=False, slots=True)
class Node:
    class_hash: int
    index: int
    flags: int = 1
    user_hash: int = 0
    params: list[Param] = field(default_factory=list)
    value_links: list[dict] = field(default_factory=list)
    exe_links: list[dict] = field(default_factory=list)
    extra: object = None
    node_id: bytes | None = None
    send_pins: list[dict] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class Graph:
    graph_id: bytes
    name_hash: int
    attr_flags: int = 0
    data_hash: int = 0
    outputs: list[tuple[int, list[int]]] = field(default_factory=list)
    native: list[Node] = field(default_factory=list)
    managed: list[Node] = field(default_factory=list)


@dataclass(eq=False, slots=True)
class ExprGraphResource:
    version: int
    name_id: int
    kind_id: int
    graphs: list[Graph] = field(default_factory=list)
    develop: bool = True
