"""See docs/internals/jcns-format.md."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(eq=False, slots=True)
class Text:
    value: str


@dataclass(eq=False, slots=True)
class Record:
    kind: str
    values: dict[str, object] = field(default_factory=dict)
    links: dict[str, list[Record] | Text | None] = field(default_factory=dict)


@dataclass(eq=False, slots=True)
class JointConstraints:
    version: int
    expression_type: int
    root: Record
    err_flags: int = 0
    master_size: int = 0
    pooled: bool = False
