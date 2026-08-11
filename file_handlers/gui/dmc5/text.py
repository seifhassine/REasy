"""Recovered DMC5 ``via.gui.Text`` markup and layout semantics.

The markup lexer and instruction compiler in this module are transcriptions of
the DMC5 native text path around RVAs ``0x27CFB30`` and ``0x27D1BC0``.  The
output opcodes are the native 0x70-byte linked-node types, not HTML concepts.

Pixel rasterization remains a separate concern: a preview supplies decoded
font cmaps/metrics and icon-name resolution, while this module owns the game
specific tag, ruby, fallback, and Japanese line-breaking policy.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum, IntEnum
import math
import re
import struct

from file_handlers.ift.model import IconGlyph


DMC5_ARABIC_LANGUAGE_INDEX = 21
DMC5_DEFAULT_RUBY_SIZE_RATIO = 0.375
DMC5_MISSING_GLYPH_FALLBACKS = (0x303C, 0x25A1, 0x002A)

# Exact output of DMC5's embedded Unicode property trie at RVA 0x27F9610.
# Keeping the compact positive ranges makes preview behavior independent from
# the host Python Unicode version (which disagrees with this build at 519
# codepoints).
DMC5_RTL_TRIGGER_RANGES = (
    (0x5BE, 0x5BE), (0x5C0, 0x5C0), (0x5C3, 0x5C3), (0x5C6, 0x5C6),
    (0x5D0, 0x5EA), (0x5F0, 0x5F4), (0x600, 0x605), (0x608, 0x608),
    (0x60B, 0x60B), (0x60D, 0x60D), (0x61B, 0x61C), (0x61E, 0x64A),
    (0x660, 0x669), (0x66B, 0x66F), (0x671, 0x6D5), (0x6DD, 0x6DD),
    (0x6E5, 0x6E6), (0x6EE, 0x6EF), (0x6FA, 0x70D), (0x70F, 0x710),
    (0x712, 0x72F), (0x74D, 0x7A5), (0x7B1, 0x7B1), (0x7C0, 0x7EA),
    (0x7F4, 0x7F5), (0x7FA, 0x7FA), (0x800, 0x815), (0x81A, 0x81A),
    (0x824, 0x824), (0x828, 0x828), (0x830, 0x83E), (0x840, 0x858),
    (0x85E, 0x85E), (0x8A0, 0x8B4), (0x200F, 0x200F),
    (0x202B, 0x202B), (0x202E, 0x202E), (0xFB1D, 0xFB1D),
    (0xFB1F, 0xFB28), (0xFB2A, 0xFB36), (0xFB38, 0xFB3C),
    (0xFB3E, 0xFB3E), (0xFB40, 0xFB41), (0xFB43, 0xFB44),
    (0xFB46, 0xFBC1), (0xFBD3, 0xFD3D), (0xFD50, 0xFD8F),
    (0xFD92, 0xFDC7), (0xFDF0, 0xFDFC), (0xFE70, 0xFE74),
    (0xFE76, 0xFEFC), (0x10800, 0x10805), (0x10808, 0x10808),
    (0x1080A, 0x10835), (0x10837, 0x10838), (0x1083C, 0x1083C),
    (0x1083F, 0x10855), (0x10857, 0x1089E), (0x108A7, 0x108AF),
    (0x108E0, 0x108F2), (0x108F4, 0x108F5), (0x108FB, 0x1091B),
    (0x10920, 0x10939), (0x1093F, 0x1093F), (0x10980, 0x109B7),
    (0x109BC, 0x109CF), (0x109D2, 0x10A00), (0x10A10, 0x10A13),
    (0x10A15, 0x10A17), (0x10A19, 0x10A33), (0x10A40, 0x10A47),
    (0x10A50, 0x10A58), (0x10A60, 0x10A9F), (0x10AC0, 0x10AE4),
    (0x10AEB, 0x10AF6), (0x10B00, 0x10B35), (0x10B40, 0x10B55),
    (0x10B58, 0x10B72), (0x10B78, 0x10B91), (0x10B99, 0x10B9C),
    (0x10BA9, 0x10BAF), (0x10C00, 0x10C48), (0x10C80, 0x10CB2),
    (0x10CC0, 0x10CF2), (0x10CFA, 0x10CFF), (0x10E60, 0x10E7E),
    (0x1E800, 0x1E8C4), (0x1E8C7, 0x1E8CF), (0x1EE00, 0x1EE03),
    (0x1EE05, 0x1EE1F), (0x1EE21, 0x1EE22), (0x1EE24, 0x1EE24),
    (0x1EE27, 0x1EE27), (0x1EE29, 0x1EE32), (0x1EE34, 0x1EE37),
    (0x1EE39, 0x1EE39), (0x1EE3B, 0x1EE3B), (0x1EE42, 0x1EE42),
    (0x1EE47, 0x1EE47), (0x1EE49, 0x1EE49), (0x1EE4B, 0x1EE4B),
    (0x1EE4D, 0x1EE4F), (0x1EE51, 0x1EE52), (0x1EE54, 0x1EE54),
    (0x1EE57, 0x1EE57), (0x1EE59, 0x1EE59), (0x1EE5B, 0x1EE5B),
    (0x1EE5D, 0x1EE5D), (0x1EE5F, 0x1EE5F), (0x1EE61, 0x1EE62),
    (0x1EE64, 0x1EE64), (0x1EE67, 0x1EE6A), (0x1EE6C, 0x1EE72),
    (0x1EE74, 0x1EE77), (0x1EE79, 0x1EE7C), (0x1EE7E, 0x1EE7E),
    (0x1EE80, 0x1EE89), (0x1EE8B, 0x1EE9B), (0x1EEA1, 0x1EEA3),
    (0x1EEA5, 0x1EEA9), (0x1EEAB, 0x1EEBB),
)
_DMC5_RTL_RANGE_STARTS = tuple(start for start, _end in DMC5_RTL_TRIGGER_RANGES)


class MarkupTokenKind(Enum):
    GLYPH = "glyph"
    NEWLINE = "newline"
    TAG = "tag"
    MALFORMED_TAG = "malformed_tag"


class TextOpcode(IntEnum):
    """Native linked-node type values used by DMC5's text layout engine."""

    PAGE = 0x01
    LINE_BREAK = 0x02
    SIZE = 0x03
    FONT = 0x04
    COLOR = 0x05
    GLYPH = 0x06
    WRAP = 0x08
    CENTER = 0x09
    LEFT = 0x0A
    RIGHT = 0x0B
    TOP = 0x0C
    BOTTOM = 0x0D
    RUBY = 0x0F
    RUBY_BASE = 0x10
    RUBY_TEXT = 0x11


class RubyState(IntEnum):
    NONE = 0
    RUBY = 1
    BASE = 2
    TEXT = 3


@dataclass(frozen=True)
class MarkupToken:
    kind: MarkupTokenKind
    source_start: int
    source_end: int
    raw: str
    codepoint: int | None = None
    name: str | None = None
    packed_name: int | None = None
    parameter: str = ""
    closing: bool = False
    name_truncated: bool = False
    error: str | None = None


@dataclass(frozen=True)
class MarkupScan:
    tokens: tuple[MarkupToken, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True)
class TextInstruction:
    opcode: TextOpcode
    source_start: int = -1
    source_end: int = -1
    codepoint: int | None = None
    font_slot: int | None = None
    size: tuple[float, float] | None = None
    color_top: int | None = None
    color_bottom: int | None = None
    ruby_ratio: float | None = None
    closing: bool = False
    reset: bool = False
    wrap_disable: bool | None = None
    icon_name: str | None = None
    icon_glyph: IconGlyph | None = None


@dataclass(frozen=True)
class UnresolvedTag:
    name: str
    parameter: str
    source_start: int
    source_end: int
    recursion_suppressed: bool


@dataclass(frozen=True)
class PageTiming:
    index: int
    first: float
    second: float
    first_half_bits: int
    second_half_bits: int
    parameter: str


@dataclass(frozen=True)
class Dmc5TextDirection:
    language_index: int
    language_21_predicate_present: bool
    effective_rtl: bool
    auto_rtl_alignment_flip: bool
    requested_vertical_layout: bool
    effective_vertical_layout: bool


@dataclass(frozen=True)
class Dmc5TextProgram:
    instructions: tuple[TextInstruction, ...]
    diagnostics: tuple[str, ...]
    unresolved_tags: tuple[UnresolvedTag, ...]
    page_timings: tuple[PageTiming, ...]
    final_ruby_state: RubyState
    direction: Dmc5TextDirection


@dataclass(frozen=True)
class GlyphResolution:
    requested_codepoint: int
    resolved_codepoint: int | None
    glyph_id: int
    face_index: int | None
    fallback_index: int | None
    vertical_glyph_id: int | None


@dataclass(frozen=True)
class RubyLayoutPlan:
    base_width: float
    measured_reading_width: float
    reading_advances: tuple[float, ...]
    reading_alignment_metrics: tuple[float, ...]
    assigned_advances: tuple[float, ...]
    marker_offset: float
    reading_wider_than_base: bool


# Data at RVAs 0x4641730, 0x4641738, 0x4641760, 0x464180C, and
# 0x4641818.  The duplicate U+31F7 in the native starter array is naturally
# irrelevant to membership testing.
DMC5_SPACE_DELIMITERS = frozenset((0x0020, 0x3000, 0x0009))
DMC5_PROHIBITED_LINE_END_OPENERS = frozenset(
    (
        0x0028,
        0xFF08,
        0x005B,
        0xFF3B,
        0x007B,
        0xFF5B,
        0x3014,
        0x3008,
        0x300A,
        0x300C,
        0x300E,
        0x3010,
        0x3018,
        0x3016,
        0x301D,
        0x2018,
        0x201C,
        0xFF5F,
        0x00AB,
    )
)
DMC5_PROHIBITED_LINE_STARTERS = frozenset(
    (
        0x002C,
        0x3001,
        0x0029,
        0xFF09,
        0x005D,
        0xFF3D,
        0x007D,
        0xFF5D,
        0x3015,
        0x3009,
        0x300B,
        0x300D,
        0x300F,
        0x3011,
        0x3019,
        0x3017,
        0x301F,
        0x2019,
        0x201D,
        0xFF60,
        0x00BB,
        0x2010,
        0x30A0,
        0x2013,
        0x301C,
        0xFF5E,
        0x003F,
        0x0021,
        0x203C,
        0x2047,
        0x2048,
        0x2049,
        0x30FB,
        0x003A,
        0x003B,
        0x002F,
        0x3002,
        0x002E,
        0x309D,
        0x309E,
        0x30FC,
        0x30A1,
        0x30A3,
        0x30A5,
        0x30A7,
        0x30A9,
        0x30C3,
        0x30E3,
        0x30E5,
        0x30E7,
        0x30EE,
        0x30F5,
        0x30F6,
        0x3041,
        0x3043,
        0x3045,
        0x3047,
        0x3049,
        0x3063,
        0x3083,
        0x3085,
        0x3087,
        0x308E,
        0x3095,
        0x3096,
        0x31F0,
        0x31F1,
        0x31F2,
        0x31F3,
        0x31F4,
        0x31F5,
        0x31F6,
        0x31F7,
        0x31F8,
        0x31F9,
        0x309A,
        0x31FA,
        0x31FB,
        0x31FC,
        0x31FD,
        0x31FE,
        0x31FF,
        0x3005,
        0x303B,
    )
)
DMC5_NON_BREAK_BEFORE = frozenset(
    (0x2014, 0x2026, 0x2025, 0x3033, 0x3034, 0x3035)
)
DMC5_OVERFLOW_PUNCTUATION = frozenset((0x3001, 0x3002))


_FLOAT_PREFIX = re.compile(
    r"^[\t\n\v\f\r ]*([+-]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))"
    r"(?:[eE][+-]?\d+)?|[+-]?(?:inf(?:inity)?|nan))",
    re.IGNORECASE,
)


def _utf16_units(text: str) -> int:
    return sum(2 if ord(character) > 0xFFFF else 1 for character in text)


def _packed_tag_name(name: str) -> int:
    value = 0
    for index, character in enumerate(name[:8]):
        codepoint = ord(character)
        if codepoint > 0xFF:
            break
        if codepoint > ord("Z"):
            codepoint -= 0x20
        value |= codepoint << (index * 8)
    return value


def _unpack_tag_name(value: int) -> str:
    result: list[str] = []
    for shift in range(0, 64, 8):
        byte = (value >> shift) & 0xFF
        if not byte:
            break
        result.append(chr(byte))
    return "".join(result)


def _scan_tag(text: str, start: int) -> tuple[MarkupToken, int]:
    cursor = start + 1
    if cursor >= len(text):
        token = MarkupToken(
            MarkupTokenKind.MALFORMED_TAG,
            start,
            len(text),
            text[start:],
            error="tag has no name or terminator",
        )
        return token, len(text)

    closing = text[cursor] == "/"
    if closing:
        cursor += 1
    name_characters: list[str] = []
    valid = True
    truncated = False
    current: str | None = None
    while cursor < len(text):
        current = text[cursor]
        cursor += 1
        if current in (" ", ">"):
            break
        if len(name_characters) >= 8:
            # Native checks its 64-bit shift count after fetching this ninth
            # scalar.  It returns success with the cursor after that scalar.
            truncated = True
            break
        codepoint = ord(current)
        if codepoint & 0xFF00:
            valid = False
            break
        if codepoint > ord("Z"):
            codepoint -= 0x20
        name_characters.append(chr(codepoint & 0xFF))

    parameter = ""
    if current == " ":
        parameter_start = cursor
        parameter_units = 0
        while cursor < len(text):
            character = text[cursor]
            units = 2 if ord(character) > 0xFFFF else 1
            if parameter_units + units >= 0x100 and character != ">":
                valid = False
                cursor += 1
                break
            cursor += 1
            if character == ">":
                parameter = text[parameter_start : cursor - 1]
                break
            parameter_units += units
        else:
            valid = False
            parameter = text[parameter_start:cursor]
        if cursor <= len(text) and (not parameter) and parameter_start < len(text):
            if text[parameter_start] != ">" and ">" not in text[parameter_start:cursor]:
                parameter = text[parameter_start:cursor]

    name = "".join(name_characters)
    packed = _packed_tag_name(name)
    raw = text[start:cursor]
    if valid:
        return (
            MarkupToken(
                MarkupTokenKind.TAG,
                start,
                cursor,
                raw,
                name=name,
                packed_name=packed,
                parameter=parameter,
                closing=closing,
                name_truncated=truncated,
            ),
            cursor,
        )
    return (
        MarkupToken(
            MarkupTokenKind.MALFORMED_TAG,
            start,
            cursor,
            raw,
            name=name,
            packed_name=packed,
            parameter=parameter,
            closing=closing,
            name_truncated=truncated,
            error="non-ASCII or oversized/missing tag parameter",
        ),
        cursor,
    )


def scan_dmc5_markup(text: str) -> MarkupScan:
    """Lex one DMC5 UTF-16 message into scalar, newline, and tag tokens."""

    tokens: list[MarkupToken] = []
    diagnostics: list[str] = []
    cursor = 0
    while cursor < len(text):
        start = cursor
        character = text[cursor]
        if character == "&" and cursor + 2 < len(text):
            escaped = text[cursor + 1]
            if escaped in "<>" and text[cursor + 2] == ";":
                tokens.append(
                    MarkupToken(
                        MarkupTokenKind.GLYPH,
                        start,
                        cursor + 3,
                        text[cursor : cursor + 3],
                        codepoint=ord(escaped),
                    )
                )
                cursor += 3
                continue
        if character == "<":
            token, cursor = _scan_tag(text, cursor)
            tokens.append(token)
            if token.error:
                diagnostics.append(f"offset {start}: {token.error}")
            continue
        if character == "\r":
            cursor += 1
            if cursor < len(text) and text[cursor] == "\n":
                cursor += 1
            tokens.append(
                MarkupToken(
                    MarkupTokenKind.NEWLINE,
                    start,
                    cursor,
                    text[start:cursor],
                )
            )
            continue
        if character == "\n":
            cursor += 1
            tokens.append(
                MarkupToken(
                    MarkupTokenKind.NEWLINE,
                    start,
                    cursor,
                    character,
                )
            )
            continue
        cursor += 1
        tokens.append(
            MarkupToken(
                MarkupTokenKind.GLYPH,
                start,
                cursor,
                character,
                codepoint=ord(character),
            )
        )
    return MarkupScan(tuple(tokens), tuple(diagnostics))


def tokenize_dmc5_markup(text: str) -> tuple[MarkupToken, ...]:
    return scan_dmc5_markup(text).tokens


def _parse_float_prefix(text: str) -> float:
    match = _FLOAT_PREFIX.match(text)
    if not match:
        return 0.0
    try:
        return float(match.group(1))
    except ValueError:
        return 0.0


def _hex_nibble_native(character: str) -> int:
    lowered = character.lower()
    value = ord(lowered[0]) if lowered else 0
    return value - 0x57 if value >= 0x61 else value - 0x30


def _packed_rgb_native(text: str) -> int:
    values = [_hex_nibble_native(character) for character in text[:6]]
    red = ((values[0] << 4) | values[1]) & 0xFF
    green = ((values[2] << 4) | values[3]) & 0xFF
    blue = ((values[4] << 4) | values[5]) & 0xFF
    return red | (green << 8) | (blue << 16)


def _float32_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def dmc5_float_to_half_bits(value: float) -> int:
    """Transcribe RVA 0x4A660's truncating float32-to-half conversion."""

    bits = _float32_bits(value)
    mantissa = bits & 0x7FFFFF
    sign = (bits >> 16) & 0x8000
    exponent = ((bits >> 23) & 0xFF) - 0x70
    if exponent <= 0:
        if exponent < -10:
            return 0
        mantissa |= 0x800000
        return sign | ((mantissa >> (1 - exponent)) >> 13)
    if exponent == 0x8F:
        if not mantissa:
            return sign | 0x7C00
        payload = mantissa >> 13
        if not payload:
            payload = 1
        return sign | 0x7C00 | payload
    if exponent > 0x1E:
        return sign | 0x7C00
    return sign | ((exponent & 0x1F) << 10) | (mantissa >> 13)


def _default_language_21_predicate(codepoint: int) -> bool:
    if codepoint < 0 or codepoint > 0x10FFFF:
        return False
    index = bisect_right(_DMC5_RTL_RANGE_STARTS, codepoint) - 1
    return index >= 0 and codepoint <= DMC5_RTL_TRIGGER_RANGES[index][1]


def _utf16_codepoints(text: str) -> tuple[int, ...]:
    raw = text.encode("utf-16-le", errors="surrogatepass")
    return tuple(
        int.from_bytes(raw[offset : offset + 2], "little")
        for offset in range(0, len(raw), 2)
    )


def dmc5_text_direction(
    text: str,
    language_index: int,
    *,
    auto_rtl: bool,
    vertical_layout: bool,
    language_21_predicate: Callable[[int], bool] | None = None,
) -> Dmc5TextDirection:
    """Compute native context bytes +0x2F5, +0x2F6, and +0x2F7."""

    predicate = language_21_predicate or _default_language_21_predicate
    present = any(predicate(codepoint) for codepoint in _utf16_codepoints(text))
    is_language_21 = language_index == DMC5_ARABIC_LANGUAGE_INDEX
    effective_rtl = is_language_21 and present
    auto_flip = is_language_21 and auto_rtl
    effective_vertical = vertical_layout and not effective_rtl
    return Dmc5TextDirection(
        language_index,
        present,
        effective_rtl,
        auto_flip,
        vertical_layout,
        effective_vertical,
    )


TagResolver = Callable[[str, str], str | None]
IconResolver = Callable[[str], str | Sequence[int] | IconGlyph | None]


class _Compiler:
    def __init__(
        self,
        text: str,
        *,
        language_index: int,
        auto_rtl: bool,
        vertical_layout: bool,
        language_21_predicate: Callable[[int], bool] | None,
        font_slot: int,
        size: tuple[float, float],
        size_scale: tuple[float, float] | None,
        ruby_size_ratio: float,
        tag_resolver: TagResolver | None,
        icon_resolver: IconResolver | None,
        time_scale: float,
        include_initial_state: bool,
    ) -> None:
        self.text = text
        self.direction = dmc5_text_direction(
            text,
            language_index,
            auto_rtl=auto_rtl,
            vertical_layout=vertical_layout,
            language_21_predicate=language_21_predicate,
        )
        self.size_scale = size_scale
        self.default_ruby_size_ratio = ruby_size_ratio
        self.tag_resolver = tag_resolver
        self.icon_resolver = icon_resolver
        self.time_scale = time_scale
        self.instructions: list[TextInstruction] = []
        self.diagnostics: list[str] = []
        self.unresolved: list[UnresolvedTag] = []
        self.timings: list[PageTiming] = []
        self.font_stack: list[int] = [font_slot]
        self.size_stack: list[tuple[float, float]] = [size]
        self.color_stack: list[tuple[int, int]] = []
        self.ruby_state = RubyState.NONE
        self.ruby_ratio = ruby_size_ratio
        self.current_line_index: int | None = None
        if include_initial_state:
            self._append(TextOpcode.PAGE)
            self._line_break()
            self._append(TextOpcode.FONT, font_slot=font_slot)
            self._append(TextOpcode.SIZE, size=size)

    def _append(self, opcode: TextOpcode, **values: object) -> TextInstruction:
        instruction = TextInstruction(opcode, **values)
        self.instructions.append(instruction)
        return instruction

    def _line_break(self, token: MarkupToken | None = None) -> None:
        values = {}
        if token is not None:
            values = {
                "source_start": token.source_start,
                "source_end": token.source_end,
            }
        self._append(TextOpcode.LINE_BREAK, **values)
        self.current_line_index = len(self.instructions) - 1

    def _insert_alignment(self, opcode: TextOpcode, token: MarkupToken) -> None:
        instruction = TextInstruction(
            opcode,
            source_start=token.source_start,
            source_end=token.source_end,
        )
        if self.current_line_index is None:
            return
        # Native RVA 0x27D1530 inserts directly after the active line node.
        self.instructions.insert(self.current_line_index + 1, instruction)

    def _close_rb(self, token: MarkupToken) -> None:
        if self.ruby_state != RubyState.BASE:
            return
        self._append(
            TextOpcode.RUBY_BASE,
            source_start=token.source_start,
            source_end=token.source_end,
            closing=True,
        )
        self.ruby_state = RubyState.RUBY

    def _close_rt(self, token: MarkupToken) -> None:
        if self.ruby_state != RubyState.TEXT:
            return
        self._append(
            TextOpcode.RUBY_TEXT,
            source_start=token.source_start,
            source_end=token.source_end,
            closing=True,
        )
        self.ruby_state = RubyState.RUBY

    def _ruby(self, token: MarkupToken) -> None:
        if self.direction.effective_rtl:
            return
        if not token.closing:
            if self.ruby_state != RubyState.NONE:
                return
            ratio = _parse_float_prefix(token.parameter)
            if not ratio > 0.0:
                ratio = self.default_ruby_size_ratio
            self.ruby_ratio = ratio
            self._append(
                TextOpcode.RUBY,
                source_start=token.source_start,
                source_end=token.source_end,
                ruby_ratio=ratio,
            )
            self.ruby_state = RubyState.RUBY
            return
        if self.ruby_state == RubyState.BASE:
            self._close_rb(token)
        elif self.ruby_state == RubyState.TEXT:
            self._close_rt(token)
        if self.ruby_state != RubyState.RUBY:
            return
        self._append(
            TextOpcode.RUBY,
            source_start=token.source_start,
            source_end=token.source_end,
            ruby_ratio=self.ruby_ratio,
            closing=True,
        )
        self.ruby_state = RubyState.NONE

    def _rb(self, token: MarkupToken) -> None:
        if self.direction.effective_rtl:
            return
        if token.closing:
            self._close_rb(token)
            return
        if self.ruby_state == RubyState.TEXT:
            self._close_rt(token)
        if self.ruby_state != RubyState.RUBY:
            return
        self._append(
            TextOpcode.RUBY_BASE,
            source_start=token.source_start,
            source_end=token.source_end,
        )
        self.ruby_state = RubyState.BASE

    def _rt(self, token: MarkupToken) -> None:
        if self.direction.effective_rtl:
            return
        if token.closing:
            self._close_rt(token)
            return
        if self.ruby_state == RubyState.BASE:
            self._close_rb(token)
        if self.ruby_state != RubyState.RUBY:
            return
        self._append(
            TextOpcode.RUBY_TEXT,
            source_start=token.source_start,
            source_end=token.source_end,
        )
        self.ruby_state = RubyState.TEXT

    def _size(self, token: MarkupToken) -> None:
        if token.closing:
            if self.size_stack:
                self.size_stack.pop()
            if self.size_stack:
                self._append(
                    TextOpcode.SIZE,
                    source_start=token.source_start,
                    source_end=token.source_end,
                    size=self.size_stack[-1],
                )
            return
        first, separator, second_text = token.parameter.partition(" ")
        x = _parse_float_prefix(first)
        if not x > 0.0:
            self.diagnostics.append(
                f"offset {token.source_start}: SIZE requires a positive first value"
            )
            return
        y = _parse_float_prefix(second_text) if separator else x
        if not y > 0.0:
            y = x
        if self.size_scale is not None:
            x *= self.size_scale[0]
            y *= self.size_scale[1]
        value = (x, y)
        self._append(
            TextOpcode.SIZE,
            source_start=token.source_start,
            source_end=token.source_end,
            size=value,
        )
        self.size_stack.append(value)

    def _font(self, token: MarkupToken) -> None:
        if token.closing:
            if self.font_stack:
                self.font_stack.pop()
            if self.font_stack:
                self._append(
                    TextOpcode.FONT,
                    source_start=token.source_start,
                    source_end=token.source_end,
                    font_slot=self.font_stack[-1],
                )
            return
        if _utf16_units(token.parameter) != 1:
            self.diagnostics.append(
                f"offset {token.source_start}: FONT parameter must be one UTF-16 unit"
            )
            return
        value = int(token.parameter) if token.parameter.isdecimal() else 0
        if value >= 10:
            self.diagnostics.append(
                f"offset {token.source_start}: FONT slot is outside 0..9"
            )
            return
        self._append(
            TextOpcode.FONT,
            source_start=token.source_start,
            source_end=token.source_end,
            font_slot=value,
        )
        self.font_stack.append(value)

    def _color(self, token: MarkupToken) -> None:
        if token.closing:
            if self.color_stack:
                self.color_stack.pop()
            if self.color_stack:
                top, bottom = self.color_stack[-1]
                reset = False
            else:
                top = bottom = 0
                reset = True
            self._append(
                TextOpcode.COLOR,
                source_start=token.source_start,
                source_end=token.source_end,
                color_top=top,
                color_bottom=bottom,
                reset=reset,
            )
            return
        if _utf16_units(token.parameter) < 6:
            self.diagnostics.append(
                f"offset {token.source_start}: COLOR requires six hexadecimal units"
            )
            return
        top = _packed_rgb_native(token.parameter[:6])
        remainder = token.parameter[6:]
        space = remainder.find(" ")
        second = remainder[space + 1 :] if space >= 0 else ""
        bottom = _packed_rgb_native(second[:6]) if len(second) >= 6 else top
        self._append(
            TextOpcode.COLOR,
            source_start=token.source_start,
            source_end=token.source_end,
            color_top=top,
            color_bottom=bottom,
        )
        self.color_stack.append((top, bottom))

    def _page(self, token: MarkupToken) -> None:
        self._append(
            TextOpcode.PAGE,
            source_start=token.source_start,
            source_end=token.source_end,
        )
        self._line_break(token)
        if self.font_stack and self.size_stack:
            self._append(
                TextOpcode.FONT,
                source_start=token.source_start,
                source_end=token.source_end,
                font_slot=self.font_stack[-1],
            )
            self._append(
                TextOpcode.SIZE,
                source_start=token.source_start,
                source_end=token.source_end,
                size=self.size_stack[-1],
            )
        else:
            self.diagnostics.append(
                f"offset {token.source_start}: PAGE has no active FONT/SIZE state"
            )

    def _time(self, token: MarkupToken) -> None:
        parameter = token.parameter
        split_at = parameter.find(",")
        if split_at < 0:
            split_at = parameter.find(" ")
        if split_at >= 0:
            first = _parse_float_prefix(parameter)
            second = _parse_float_prefix(parameter[split_at + 1 :])
        else:
            first = second = _parse_float_prefix(parameter)
        first *= self.time_scale
        second *= self.time_scale
        self.timings.append(
            PageTiming(
                len(self.timings),
                first,
                second,
                dmc5_float_to_half_bits(first),
                dmc5_float_to_half_bits(second),
                parameter,
            )
        )

    def _icon(self, token: MarkupToken) -> None:
        for raw_name in token.parameter.split(","):
            name = raw_name.strip("\t\n\v\f\r \x1c\x1d\x1e\x1f\x20")
            if not name:
                continue
            resolved = self.icon_resolver(name) if self.icon_resolver else None
            if resolved is None:
                self.unresolved.append(
                    UnresolvedTag(
                        "ICON", name, token.source_start, token.source_end, False
                    )
                )
                continue
            if isinstance(resolved, IconGlyph):
                # Native IFT icons remain opcode-6 glyph nodes, but bit 10 of
                # the node header marks them as UV-sequence backed.  Keeping
                # the atlas record here avoids inventing a Unicode codepoint.
                self._append(
                    TextOpcode.GLYPH,
                    source_start=token.source_start,
                    source_end=token.source_end,
                    icon_name=name,
                    icon_glyph=resolved,
                )
                continue
            if isinstance(resolved, str):
                codepoints = tuple(ord(character) for character in resolved)
            else:
                codepoints = tuple(int(codepoint) for codepoint in resolved)
            for codepoint in codepoints:
                self._append(
                    TextOpcode.GLYPH,
                    source_start=token.source_start,
                    source_end=token.source_end,
                    codepoint=codepoint,
                    icon_name=name,
                )

    def _unknown(
        self, token: MarkupToken, *, recursion_suppressed: bool
    ) -> None:
        name = _unpack_tag_name(token.packed_name or 0)
        self.unresolved.append(
            UnresolvedTag(
                name,
                token.parameter,
                token.source_start,
                token.source_end,
                recursion_suppressed,
            )
        )
        if recursion_suppressed:
            return
        if self.tag_resolver is None:
            self.diagnostics.append(
                f"offset {token.source_start}: no resolver for tag {name or '<empty>'}"
            )
            return
        replacement = self.tag_resolver(name, token.parameter)
        if replacement is not None:
            self._compile_fragment(replacement, recursion_suppressed=True)

    def _tag(self, token: MarkupToken, *, recursion_suppressed: bool) -> None:
        name = token.name or ""
        if name == "PAGE":
            self._page(token)
        elif name == "TOP":
            self._insert_alignment(TextOpcode.TOP, token)
        elif name == "RT":
            self._rt(token)
        elif name == "RB":
            self._rb(token)
        elif name == "TIME":
            self._time(token)
        elif name == "SIZE":
            self._size(token)
        elif name == "ICON":
            self._icon(token)
        elif name == "WRAP":
            self._append(
                TextOpcode.WRAP,
                source_start=token.source_start,
                source_end=token.source_end,
                closing=token.closing,
                wrap_disable=token.parameter.startswith("-"),
            )
        elif name == "LEFT":
            self._insert_alignment(TextOpcode.LEFT, token)
        elif name == "COLOR":
            self._color(token)
        elif name == "FONT":
            self._font(token)
        elif name == "RUBY":
            self._ruby(token)
        elif name == "BLANK":
            return
        elif name == "RIGHT":
            self._insert_alignment(TextOpcode.RIGHT, token)
        elif name == "BOTTOM":
            self._insert_alignment(TextOpcode.BOTTOM, token)
        elif name == "CENTER":
            self._insert_alignment(TextOpcode.CENTER, token)
        else:
            self._unknown(token, recursion_suppressed=recursion_suppressed)

    def _compile_fragment(
        self, fragment: str, *, recursion_suppressed: bool
    ) -> None:
        scan = scan_dmc5_markup(fragment)
        self.diagnostics.extend(scan.diagnostics)
        for token in scan.tokens:
            if token.kind == MarkupTokenKind.MALFORMED_TAG:
                continue
            if token.kind == MarkupTokenKind.TAG:
                self._tag(token, recursion_suppressed=recursion_suppressed)
                continue
            if token.kind == MarkupTokenKind.NEWLINE:
                if self.ruby_state == RubyState.NONE:
                    self._line_break(token)
                continue
            self._append(
                TextOpcode.GLYPH,
                source_start=token.source_start,
                source_end=token.source_end,
                codepoint=token.codepoint,
            )

    def compile(self) -> Dmc5TextProgram:
        self._compile_fragment(self.text, recursion_suppressed=False)
        if self.ruby_state != RubyState.NONE:
            self.diagnostics.append(
                f"text ended with open ruby state {self.ruby_state.name}"
            )
        return Dmc5TextProgram(
            tuple(self.instructions),
            tuple(self.diagnostics),
            tuple(self.unresolved),
            tuple(self.timings),
            self.ruby_state,
            self.direction,
        )


def compile_dmc5_markup(
    text: str,
    *,
    language_index: int = 1,
    auto_rtl: bool = True,
    vertical_layout: bool = False,
    language_21_predicate: Callable[[int], bool] | None = None,
    font_slot: int = 0,
    size: tuple[float, float] = (32.0, 32.0),
    size_scale: tuple[float, float] | None = None,
    ruby_size_ratio: float = DMC5_DEFAULT_RUBY_SIZE_RATIO,
    tag_resolver: TagResolver | None = None,
    icon_resolver: IconResolver | None = None,
    time_scale: float = 1.0,
    include_initial_state: bool = True,
) -> Dmc5TextProgram:
    """Compile message text into the native DMC5 instruction-node model."""

    return _Compiler(
        text,
        language_index=language_index,
        auto_rtl=auto_rtl,
        vertical_layout=vertical_layout,
        language_21_predicate=language_21_predicate,
        font_slot=font_slot,
        size=size,
        size_scale=size_scale,
        ruby_size_ratio=ruby_size_ratio,
        tag_resolver=tag_resolver,
        icon_resolver=icon_resolver,
        time_scale=time_scale,
        include_initial_state=include_initial_state,
    ).compile()


def is_dmc5_cjk_break_character(codepoint: int) -> bool:
    """Transcribe the range predicate at native RVA ``0x27D1590``."""

    return any(
        start <= codepoint <= end
        for start, end in (
            (0x2E80, 0x2FFF),
            (0x3040, 0x30FF),
            (0x31F0, 0x31FF),
            (0xFF01, 0xFF5A),
            (0xFF61, 0xFF9F),
            (0x4E00, 0x9FCF),
            (0x3400, 0x4DBF),
            (0x20000, 0x2A6DF),
            (0xF900, 0xFAFF),
            (0x2F800, 0x2FA1F),
            (0xA000, 0xA4CF),
            (0xFE62, 0xFE66),
            (0xAC00, 0xD7AF),
        )
    )


def dmc5_break_opportunity(previous: int | None, current: int) -> bool:
    """Return whether RVA ``0x27D1230`` records a break before ``current``."""

    if previous is None:
        return False
    if previous in DMC5_PROHIBITED_LINE_END_OPENERS:
        return False
    if current in DMC5_PROHIBITED_LINE_STARTERS:
        return False
    if current in DMC5_NON_BREAK_BEFORE:
        return False
    if current in DMC5_SPACE_DELIMITERS:
        return True
    if is_dmc5_cjk_break_character(current):
        return True
    if is_dmc5_cjk_break_character(previous):
        return True
    if current in DMC5_PROHIBITED_LINE_END_OPENERS:
        return True
    if previous in DMC5_PROHIBITED_LINE_STARTERS:
        return True
    return False


def find_dmc5_wrap_break(
    codepoints: Sequence[int], widths: Sequence[float], maximum_width: float
) -> int | None:
    """Return the glyph index after which DMC5 inserts its wrap line node.

    ``widths`` contains the already measured per-glyph contribution used by the
    native loop.  Ruby sub-runs are laid out separately and should be supplied
    as their resolved base-run contributions.
    """

    if len(codepoints) != len(widths):
        raise ValueError("codepoints and widths must have equal lengths")
    accumulated = 0.0
    candidate: int | None = None
    previous: int | None = None
    for index, (codepoint, width) in enumerate(zip(codepoints, widths)):
        if dmc5_break_opportunity(previous, codepoint):
            candidate = index - 1
        accumulated += width
        if accumulated > maximum_width and codepoint not in DMC5_OVERFLOW_PUNCTUATION:
            return candidate
        previous = codepoint
    return None


def resolve_dmc5_glyph(
    codepoint: int,
    face_cmaps: Sequence[Mapping[int, int]],
    *,
    vertical_substitutions: Sequence[Mapping[int, int]] | None = None,
) -> GlyphResolution:
    """Apply DMC5's two-face order and exact missing-glyph fallback chain."""

    if vertical_substitutions is not None and len(vertical_substitutions) != len(
        face_cmaps
    ):
        raise ValueError("vertical_substitutions must match face_cmaps")
    candidates = (codepoint, *DMC5_MISSING_GLYPH_FALLBACKS)
    for fallback_index, candidate in enumerate(candidates):
        for face_index, cmap in enumerate(face_cmaps):
            glyph_id = int(cmap.get(candidate, 0))
            if not glyph_id:
                continue
            vertical_id = glyph_id
            if vertical_substitutions is not None:
                vertical_id = int(
                    vertical_substitutions[face_index].get(glyph_id, glyph_id)
                )
            return GlyphResolution(
                codepoint,
                candidate,
                glyph_id,
                face_index,
                fallback_index,
                vertical_id,
            )
    return GlyphResolution(codepoint, None, 0, None, None, None)


def dmc5_missing_glyph_advance(codepoint: int, base_dimension: float) -> float:
    if codepoint == 0x0009:
        return base_dimension * 2.0
    if codepoint == 0x0020:
        return base_dimension * 0.5
    return base_dimension


def ruby_layout_plan(
    base_width: float,
    reading_advances: Sequence[float],
    *,
    reading_alignment_metrics: Sequence[float] | None = None,
) -> RubyLayoutPlan:
    """Transcribe the width distribution branch at RVAs 0x27D3049..30DC."""

    advances = tuple(float(value) for value in reading_advances)
    if reading_alignment_metrics is None:
        metrics = advances
    else:
        metrics = tuple(float(value) for value in reading_alignment_metrics)
        if len(metrics) != len(advances):
            raise ValueError("reading metrics and advances must have equal lengths")
    measured = math.fsum(advances)
    if measured > base_width:
        assigned = advances
        offset = -0.5 * (measured - base_width)
        wider = True
    elif advances:
        assigned = (base_width / len(advances),) * len(advances)
        offset = 0.5 * (base_width - math.fsum(metrics))
        wider = False
    else:
        assigned = ()
        offset = 0.0
        wider = False
    return RubyLayoutPlan(
        float(base_width),
        measured,
        advances,
        metrics,
        assigned,
        offset,
        wider,
    )

