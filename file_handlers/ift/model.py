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
class IftData:
    version: int
    descent: float
    font_size: float
    reserved: int
    uv_sequence_path: str
    entries: list[IftEntry]
    uv_sequence_path_offset: int = field(default=0, compare=False)


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
