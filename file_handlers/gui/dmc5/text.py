"""Recovered DMC5 ``via.gui.Text`` markup and layout semantics.

The markup lexer and instruction compiler in this module are transcriptions of
the DMC5 native text path around RVAs ``0x27CFB30`` and ``0x27D1BC0``.  The
output opcodes are the native 0x70-byte linked-node types, not HTML concepts.

Pixel rasterization remains a separate concern: a preview supplies decoded
font cmaps/metrics and icon-name resolution, while this module owns the game
specific tag, ruby, fallback, and Japanese line-breaking policy.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from bisect import bisect_right
from dataclasses import dataclass
from enum import Enum, IntEnum
from functools import cache, partial
import re
import struct

import numpy as np

from file_handlers.ift.model import IconGlyph

lazy from ..profiles import runtime_for_game


@dataclass(frozen=True, slots=True)
class Dmc5TextRules:
    rtl_language: int
    rtl_ranges: tuple[tuple[int, int], ...]
    rtl_starts: tuple[int, ...]
    space_delimiters: frozenset[int]
    line_end_openers: frozenset[int]
    line_starters: frozenset[int]
    non_break_before: frozenset[int]
    overflow_punctuation: frozenset[int]
    cjk_break_ranges: tuple[tuple[int, int], ...]
    follow_system_language: int
    ruby_height_language: int
    overflow_tolerance: np.float32
    fit_axes: dict[int, tuple[bool, bool]]
    fit_margin: np.float32
    fit_upper: np.float32
    fit_steps: int


@cache
def dmc5_text_rules(game: str) -> Dmc5TextRules:
    profile = runtime_for_game(game)
    rules, fit = profile.text, profile.text["region_fit"]
    return Dmc5TextRules(
        profile.language(rules["rtl_language"]),
        rules["rtl_ranges"],
        tuple(start for start, _end in rules["rtl_ranges"]),
        *(frozenset(rules[name]) for name in (
            "space_delimiters", "line_end_openers", "line_starters", "non_break_before", "overflow_punctuation")),
        rules["cjk_break_ranges"],
        profile.language(rules["follow_system_language"]),
        profile.language(rules["ruby_height_language"]),
        np.float32(rules["overflow_tolerance"]),
        {profile.enum("RegionFitType")[label]: tuple(axes) for label, axes in fit["axes"].items()},
        np.float32(fit["margin"]), np.float32(fit["upper"]), fit["steps"],
    )


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
    flushes: tuple[int, ...]


_FLUSHING_TAGS = frozenset({"FONT", "PAGE", "SIZE", "ICON", "RUBY", "COLOR"})
_OTHER_PAGE_TAGS = frozenset({"PAGE", "TIME", "SIZE", "FONT", "COLOR"})
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


def _rtl_trigger(rules: Dmc5TextRules, codepoint: int) -> bool:
    if codepoint < 0 or codepoint > 0x10FFFF:
        return False
    index = bisect_right(rules.rtl_starts, codepoint) - 1
    return index >= 0 and codepoint <= rules.rtl_ranges[index][1]


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
    rules: Dmc5TextRules,
    language_21_predicate: Callable[[int], bool] | None = None,
) -> Dmc5TextDirection:
    """Compute native context bytes +0x2F5, +0x2F6, and +0x2F7."""

    predicate = language_21_predicate or partial(_rtl_trigger, rules)
    present = any(predicate(codepoint) for codepoint in _utf16_codepoints(text))
    is_language_21 = language_index == rules.rtl_language
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
        rules: Dmc5TextRules,
        language_21_predicate: Callable[[int], bool] | None,
        font_slot: int,
        size: tuple[float, float],
        size_scale: tuple[float, float] | None,
        ruby_size_ratio: float,
        tag_resolver: TagResolver | None,
        icon_resolver: IconResolver | None,
        time_scale: float,
        include_initial_state: bool,
        page: int,
    ) -> None:
        self.text = text
        self.page, self.page_counter, self.pending, self.flushes = page, 0, 0, []
        self.direction = dmc5_text_direction(
            text,
            language_index,
            auto_rtl=auto_rtl,
            vertical_layout=vertical_layout,
            rules=rules,
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
            ratio = float(np.float32(_parse_float_prefix(token.parameter)))
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
        x = np.float32(_parse_float_prefix(first))
        if not x > 0.0:
            self.diagnostics.append(
                f"offset {token.source_start}: SIZE requires a positive first value"
            )
            return
        y = np.float32(_parse_float_prefix(second_text)) if separator else x
        if not y > 0.0:
            y = x
        if self.size_scale is not None:
            x *= np.float32(self.size_scale[0])
            y *= np.float32(self.size_scale[1])
        value = (float(x), float(y))
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

    def _flush(self) -> None:
        self.flushes.append(len(self.instructions))
        self.pending = 0

    def _tag(self, token: MarkupToken, *, recursion_suppressed: bool) -> None:
        name = token.name or ""
        if self.ruby_state == RubyState.NONE and name in _FLUSHING_TAGS and self.page_counter == self.page:
            self._flush()
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
        after_page = False
        for token in scan.tokens:
            if after_page and token.kind == MarkupTokenKind.NEWLINE:
                after_page = False
                continue
            after_page = False
            if self.page_counter != self.page and (
                token.kind != MarkupTokenKind.TAG or token.name not in _OTHER_PAGE_TAGS
            ):
                continue
            if token.kind == MarkupTokenKind.MALFORMED_TAG:
                continue
            if token.kind == MarkupTokenKind.TAG:
                self._tag(token, recursion_suppressed=recursion_suppressed)
                if token.name == "PAGE":
                    self.page_counter += 1
                    if self.page_counter > self.page:
                        break
                    after_page = True
                continue
            if token.kind == MarkupTokenKind.NEWLINE:
                if self.ruby_state == RubyState.NONE:
                    self._flush()
                    self._line_break(token)
                continue
            if not token.codepoint:
                break
            if token.codepoint < 0x20 and token.codepoint not in (9, 10) or self.ruby_state == RubyState.RUBY:
                continue
            self.pending += self.ruby_state == RubyState.NONE
            self._append(
                TextOpcode.GLYPH,
                source_start=token.source_start,
                source_end=token.source_end,
                codepoint=token.codepoint,
            )
        if self.pending and self.ruby_state == RubyState.NONE:
            self._flush()

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
            tuple(self.flushes),
        )


def compile_dmc5_markup(
    text: str,
    *,
    rules: Dmc5TextRules,
    language_index: int,
    auto_rtl: bool,
    vertical_layout: bool,
    language_21_predicate: Callable[[int], bool] | None = None,
    font_slot: int,
    size: tuple[float, float],
    size_scale: tuple[float, float] | None = None,
    ruby_size_ratio: float,
    tag_resolver: TagResolver | None = None,
    icon_resolver: IconResolver | None = None,
    time_scale: float = 1.0,
    include_initial_state: bool = True,
    page: int,
) -> Dmc5TextProgram:
    """Compile message text into the native DMC5 instruction-node model."""

    return _Compiler(
        text,
        language_index=language_index,
        auto_rtl=auto_rtl,
        vertical_layout=vertical_layout,
        rules=rules,
        language_21_predicate=language_21_predicate,
        font_slot=font_slot,
        size=size,
        size_scale=size_scale,
        ruby_size_ratio=ruby_size_ratio,
        tag_resolver=tag_resolver,
        icon_resolver=icon_resolver,
        time_scale=time_scale,
        include_initial_state=include_initial_state,
        page=page,
    ).compile()


def is_cjk_break_character(codepoint: int, rules: Dmc5TextRules) -> bool:
    return any(start <= codepoint <= end for start, end in rules.cjk_break_ranges)
