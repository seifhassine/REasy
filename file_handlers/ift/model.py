from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class IftEntry:
    name: str
    uv_sequence_no: int
    uv_pattern_no: int
    width: float
    height: float
    name_offset: int = field(default=0, compare=False)
    source_offset: int = field(default=0, compare=False)


@dataclass
class IftSubRecord:
    name_a: str
    name_b: str
    blob: bytes = b""
    width: int = 0
    reserved: int = 0


@dataclass
class IftGroup:
    name: str
    subs: list[IftSubRecord] = field(default_factory=list)


@dataclass
class IftData:
    version: int
    descent: float
    font_size: float
    reserved: int
    uv_sequence_path: str
    entries: list[IftEntry]
    extra_floats: tuple[float, float] = (0.0, 1.0)
    aux_path: str = ""
    aux_entries: list[IftEntry] = field(default_factory=list)
    groups: list[IftGroup] = field(default_factory=list)

    @property
    def all_entries(self) -> list[IftEntry]:
        return [*self.entries, *self.aux_entries]


@dataclass(frozen=True, slots=True)
class IconGlyph:
    name: str
    uv_sequence_no: int
    uv_pattern_no: int
    width: float
    height: float
    uv_rect: tuple[float, float, float, float] | None = None
    texture_index: int | None = None
    texture_path: str | None = None
    pattern_flags: int | None = None


@dataclass(frozen=True, slots=True)
class IftAtlasValidation:
    entry_count: int
    resolved_count: int
    invalid_entries: tuple[str, ...]
    unused_patterns: tuple[tuple[int, int], ...]
