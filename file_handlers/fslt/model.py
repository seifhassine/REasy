from __future__ import annotations

from dataclasses import dataclass, field

SLOT_COUNT = 16


@dataclass
class FsltFont:
    adjust: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    path: str = ""


@dataclass
class FsltCharRule:
    texts: tuple[str, ...] = ("",)
    reserved: int = 0
    adjust: tuple[float, float] = (0.0, 0.0)
    params: tuple[int, int, int, int] = (0, 0, 0, 0)


@dataclass
class FsltSlot:
    fonts: list[FsltFont] = field(default_factory=list)
    rules: list[FsltCharRule] = field(default_factory=list)
    tail_rules: list[FsltCharRule] = field(default_factory=list)


@dataclass
class FsltData:
    version: int = 2
    extra: int = 0
    slots: list[FsltSlot] = field(
        default_factory=lambda: [FsltSlot() for _ in range(SLOT_COUNT)]
    )
