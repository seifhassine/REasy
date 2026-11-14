"""See docs/internals/gui-rendering.md, Text layout."""
from __future__ import annotations

import math
import struct
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np

from file_handlers.font.catalog import GuiFontCatalog
from file_handlers.font.raster import GlyphBitmap, GlyphRasterizer
from file_handlers.ift.model import IconGlyph

from .text import (Dmc5TextProgram, Dmc5TextRules, TextOpcode, compile_dmc5_markup, dmc5_float_to_half_bits,
                   is_cjk_break_character)

F = np.float32
ZERO, HALF, ONE, PIXEL, EPSILON32 = F(0), F(0.5), F(1), F(1 / 64), np.finfo(np.float32).eps


class TextLayoutGap(ValueError):
    pass


def _mulfix(a: int, b: int) -> int:
    c = (abs(a) * abs(b) + 0x8000) >> 16
    return -c if (a < 0) != (b < 0) else c


def _divfix(a: int, b: int) -> int:
    return ((a << 16) + (b >> 1)) // b


@dataclass(frozen=True, slots=True)
class TextParams:

    font_slot: int
    font_size: tuple[F, F]
    raster: tuple[int, int]
    region: tuple[F, F]
    letter_space: F
    line_space: F
    icon_size: F
    ruby_ratio: F
    ruby_line_space: F
    page_alignment: int
    letter_alignment: int
    auto_wrap: bool
    ruby_always: bool
    language: int
    system_language: int
    size_scale: tuple[F, F] | None = None


def _half(value: float) -> F:
    return np.uint16(dmc5_float_to_half_bits(value)).view(np.float16).astype(F)


def text_params(values: Mapping, rules: Dmc5TextRules, *, language: int, system_language: int, raster_scale: float,
                system_ruby_ratio: float, fit_scale: float = 1.0) -> TextParams:
    k, (fit_x, fit_y) = F(fit_scale), rules.fit_axes[int(values["AutoRegionFit"])]
    kx, ky = (k if fit_x else ONE), (k if fit_y else ONE)
    raster = [int(max(1.0, float(v))) & 0xFFFF for v in values["RasterSize"]]
    ratio = _half(float(values["RubySizeRatio"]))
    return TextParams(
        int(values["FontSlot"]), tuple(_half(max(1.0, float(v))) for v in values["FontSize"]),
        tuple(int(F(v) * F(raster_scale)) for v in raster),
        tuple(_half(float(F(max(0.0, float(v))) * HALF)) * F(2) for v in values["RegionSize"]),
        kx * F(values["LetterSpace"]), ky * F(values["LineSpace"]), F(int(values["IconFontSize"]) & 0x7FFF),
        ratio if ratio > ZERO else F(system_ruby_ratio), _half(float(values["RubyLineSpace"])),
        int(values["PageAlignment"]), int(values["LetterAlignment"]), bool(values["AutoWrap"]),
        bool(values["RubyHeightAlways"]) and language == rules.ruby_height_language, language, system_language,
        (kx, ky) if fit_x or fit_y else None)


@dataclass(frozen=True, slots=True)
class LayoutGlyph:

    face: str
    glyph_id: int
    left: F
    top: F
    advance: F
    width: F
    rows: F
    bitmap: GlyphBitmap


@dataclass(frozen=True, slots=True)
class GlyphQuad:
    glyph: LayoutGlyph | IconGlyph | None
    x: F
    y: F
    width: F
    height: F
    color: tuple[int, int] | None
    reading: bool
    index: int


@dataclass(frozen=True, slots=True)
class TextLayout:
    quads: tuple[GlyphQuad, ...]
    size: tuple[F, F]
    status: int


class SlotFont:

    def __init__(self, fonts: "LayoutFonts", language: int, slot: int):
        faces = fonts.catalog.slot_faces(language, slot)
        self.fonts, self.language, self.slot, self.faces = fonts, language, slot, faces.faces
        self.scale = F(faces.scale[0]), F(faces.scale[1])
        boxes = [(face.font.bounding_box, F(face.font.units_per_em)) for face in self.faces]
        self.ascent = tuple(max([ZERO] + [F(box[3 - vertical]) / em for box, em in boxes]) for vertical in (0, 1))
        self.height = tuple(max([ZERO] + [F(box[3 - vertical] - box[1 - vertical]) / em for box, em in boxes])
                            for vertical in (0, 1))

    def glyph(self, codepoint: int, raster: tuple[int, int], vertical: bool) -> LayoutGlyph | None:
        resolved = self.fonts.catalog.resolve_glyph(self.language, self.slot, codepoint, vertical=vertical)
        if resolved.missing or not raster[0] or not raster[1]:
            return None
        return self.fonts.glyph(resolved.face, resolved.glyph_id, raster, vertical)


class LayoutFonts:

    def __init__(self, catalog: GuiFontCatalog, darkening):
        self.catalog, self.darkening = catalog, darkening
        self._slots, self._glyphs, self._rasterizers = {}, {}, {}

    def slot(self, language: int, slot: int) -> SlotFont:
        key = language, slot
        if key not in self._slots:
            self._slots[key] = SlotFont(self, language, slot)
        return self._slots[key]

    def glyph(self, face, glyph_id: int, raster: tuple[int, int], vertical: bool) -> LayoutGlyph:
        key = face.asset_path, glyph_id, raster, vertical
        if key in self._glyphs:
            return self._glyphs[key]
        if face.asset_path not in self._rasterizers:
            if "CFF " not in face.font.tables:
                raise TextLayoutGap(f"its font {face.asset_path} is a TrueType face, which is not drawn")
            self._rasterizers[face.asset_path] = GlyphRasterizer(face.font, self.darkening)
        rasterizer, (x, y), font = self._rasterizers[face.asset_path], raster, face.font
        bitmap = rasterizer.bitmap(glyph_id, x, y)
        advance = _mulfix(bitmap.advance, _divfix(x << 6, font.units_per_em))
        left, top = bitmap.box[0], bitmap.box[3]
        if vertical:
            y_scale = _divfix(y << 6, font.units_per_em)
            if font.vertical_line_metrics is not None and font.vertical_line_metrics.metric_count:
                metric = font.glyph_metric(glyph_id, vertical=True)
                left, top, advance = left - int(advance / 2), metric.side_bearing, _mulfix(metric.advance, y_scale)
            else:
                os2 = font.tables.get("OS/2")
                lines = font.horizontal_line_metrics
                ascender, descender = (lines.ascender, lines.descender) if os2 is None else struct.unpack_from(
                    ">hh", font.data, os2.offset + 68)
                left, top, advance = 0, 0, _mulfix(ascender - descender, y_scale)
        left, top = F(math.ceil(left / 64)), F(math.ceil(top / 64))
        if 0 < bitmap.rows < 2:
            bitmap, top = rasterizer.bitmap(glyph_id, x, y * 2), top + ONE
        glyph = LayoutGlyph(face.asset_path, glyph_id, left, top, F(advance) * PIXEL, F(bitmap.width),
                            F(bitmap.rows), bitmap)
        self._glyphs[key] = glyph
        return glyph


@dataclass(frozen=True, slots=True)
class TextContext:

    fonts: LayoutFonts
    rules: Dmc5TextRules
    icon_font: tuple[float, float] | None
    icon_resolver: Callable | None
    tag_resolver: Callable | None
    system_language: int
    raster_scale: float
    system_ruby_ratio: float


def layout_dmc5_text(message: str, values: Mapping, context: TextContext) -> TextLayout:
    rules, language = context.rules, int(values["Language"])
    language = context.system_language if language == rules.follow_system_language else language

    def build(scale: F) -> TextLayout:
        params = text_params(values, rules, language=language, system_language=context.system_language,
                             raster_scale=context.raster_scale, system_ruby_ratio=context.system_ruby_ratio,
                             fit_scale=scale)
        size = params.font_size if params.size_scale is None else tuple(
            a * b for a, b in zip(params.font_size, params.size_scale))
        program = compile_dmc5_markup(
            message, rules=context.rules, language_index=language, auto_rtl=bool(values["AutoRTL"]),
            vertical_layout=bool(values["VerticalLayout"]), font_slot=params.font_slot,
            size=tuple(float(s) for s in size), size_scale=params.size_scale,
            ruby_size_ratio=float(params.ruby_ratio), tag_resolver=context.tag_resolver,
            icon_resolver=context.icon_resolver, page=int(values["PageIndex"]) & 0x1FF)
        return Dmc5TextLayout(program, params, context.fonts, context.rules, context.icon_font).run()

    layout, (fit_x, fit_y) = build(ONE), rules.fit_axes[int(values["AutoRegionFit"])]
    mask = (0x10 if fit_x else 0) | (0x20 if fit_y else 0)
    content = [_half(float(v)) for v in layout.size]
    region = [_half(float(F(max(0.0, float(v))) * HALF)) * F(2) for v in values["RegionSize"]]
    if not layout.status & mask or min(content) < EPSILON32 or min(region) < EPSILON32:
        return layout
    ratios = [min(r / c - rules.fit_margin, ONE) for r, c in zip(region, content)]
    low = min(ratio for ratio, fitted in ((ratios[1], fit_y), (ratios[0], fit_x)) if fitted)
    high, scale = rules.fit_upper, (low + rules.fit_upper) * HALF
    for _ in range(rules.fit_steps):
        if layout.status & mask:
            high = scale
        else:
            low = scale
        middle = (high + low) * HALF
        if EPSILON32 > abs(scale - middle):
            break
        layout, scale = build(middle), middle
    return build(low) if layout.status & mask else layout


@dataclass(slots=True, eq=False)
class _Node:
    op: TextOpcode
    closing: bool = False
    codepoint: int = 0
    icon: IconGlyph | None = None
    glyph: LayoutGlyph | None = None
    reading: bool = False
    removed: bool = False
    slot: int = 0
    color: tuple[int, int] | None = None
    reset: bool = False
    next_line: "_Node | None" = None
    x_offset: F = ZERO
    top: F = ZERO
    advance: F = ZERO
    extra: F = ZERO
    x: F = ZERO
    y: F = ZERO
    width: F = ZERO
    height: F = ZERO
    pen: F = ZERO
    ascent: F = ZERO
    ruby: F = ZERO
    ratio: F = ZERO
    offset: F = ZERO


@dataclass(slots=True)
class _State:
    nodes: list = field(default_factory=list)
    line: _Node | None = None
    font: _Node | None = None
    size: _Node | None = None
    first_line: _Node | None = None
    pending: list = field(default_factory=list)
    ruby: _Node | None = None
    ruby_base: _Node | None = None
    ruby_text: _Node | None = None
    ruby_phase: int = 0
    status: int = 0


class Dmc5TextLayout:

    def __init__(self, program: Dmc5TextProgram, params: TextParams, fonts: LayoutFonts, rules: Dmc5TextRules,
                 icon_font: tuple[float, float] | None):
        direction = program.direction
        if direction.effective_rtl:
            raise TextLayoutGap("right-to-left text is not drawn")
        self.program, self.params, self.fonts, self.rules = program, params, fonts, rules
        self.vertical = direction.effective_vertical_layout
        self.flip = direction.auto_rtl_alignment_flip
        self.icon_font = None if icon_font is None else (F(icon_font[0]), F(icon_font[1]))
        self.state = _State()

    def run(self) -> TextLayout:
        self._measure()
        state, params = self.state, self.params
        if params.auto_wrap and state.status & (0x20 if self.vertical else 0x10):
            self._wrap()
        size = self._place()
        return TextLayout(self._quads(), size, state.status)

    def _measure(self) -> None:
        state, flushes = self.state, Counter(self.program.flushes)
        for index, instruction in enumerate(self.program.instructions):
            for _ in range(flushes[index]):
                self._flush()
            node = _Node(instruction.opcode, instruction.closing)
            op = node.op
            if op == TextOpcode.LINE_BREAK:
                if state.line is not None:
                    state.line.next_line = node
                state.line, state.first_line = node, state.first_line or node
            elif op == TextOpcode.FONT:
                node.slot, state.font = instruction.font_slot, node
                font = self.fonts.slot(self.params.system_language, node.slot)
                scale = font.scale[1 if not self.vertical else 0]
                node.ascent, node.height = font.ascent[self.vertical] * scale, font.height[self.vertical] * scale
            elif op == TextOpcode.SIZE:
                node.x, node.y = F(instruction.size[0]), F(instruction.size[1])
                state.size = node
            elif op == TextOpcode.COLOR:
                node.color, node.reset = (instruction.color_top, instruction.color_bottom), instruction.reset
            elif op == TextOpcode.GLYPH:
                node.codepoint, node.icon = instruction.codepoint or 0, instruction.icon_glyph
            elif op == TextOpcode.RUBY:
                node.ratio = F(instruction.ruby_ratio)
            state.nodes.append(node)
            self._compile_effects(node)
        for _ in range(flushes[len(self.program.instructions)]):
            self._flush()

    def _compile_effects(self, node: _Node) -> None:
        state, op = self.state, node.op
        if op == TextOpcode.GLYPH:
            if node.icon is not None:
                self._icon(node)
            elif state.ruby_phase in (2, 3):
                node.reading = state.ruby_phase == 3
            elif state.ruby_phase == 0:
                state.pending.append(node)
        elif op == TextOpcode.RUBY and not node.closing:
            state.ruby, state.ruby_base, state.ruby_text, state.ruby_phase = node, None, None, 1
        elif op == TextOpcode.RUBY_BASE:
            state.ruby_phase = 1 if node.closing else 2
            state.ruby_base = state.ruby_base if node.closing else node
        elif op == TextOpcode.RUBY_TEXT:
            state.ruby_phase = 1 if node.closing else 3
            state.ruby_text = state.ruby_text if node.closing else node
        elif op == TextOpcode.RUBY:
            self._close_ruby()
            state.ruby, state.ruby_base, state.ruby_text, state.ruby_phase = None, None, None, 0

    def _sizes(self, font: SlotFont) -> tuple[tuple[F, F], tuple[F, F]]:
        size, raster = self.state.size, self.params.raster
        return ((font.scale[0] * size.x, font.scale[1] * size.y),
                (F(raster[0]) * font.scale[0], F(raster[1]) * font.scale[1]))

    def _line_metrics(self, font: SlotFont, size: tuple[F, F], ruby: bool = True) -> None:
        line, vertical = self.state.line, self.vertical
        ascent, height = font.ascent[vertical], font.height[vertical]
        if not vertical:
            line.ascent, line.height = max(line.ascent, size[1] * ascent), max(line.height, size[1] * height)
        else:
            line.ascent, line.width = max(line.ascent, height * HALF * size[0]), max(line.width, size[0] * height)
        if ruby and self.params.ruby_always:
            along = size[0] if vertical else size[1]
            line.ruby = max(line.ruby, along * self.params.ruby_ratio * height + self.params.ruby_line_space)

    def _flush(self) -> None:
        state = self.state
        font = self.fonts.slot(self.params.language, state.font.slot)
        size, raster = self._sizes(font)
        self._line_metrics(font, size)
        for node in state.pending:
            self._glyph_metrics(node, font, size, raster)
            self._advance(node)
        state.pending = []
        self._overflow()

    def _advance(self, node: _Node) -> None:
        line = self.state.line
        reach = node.advance + line.pen
        if self.vertical:
            line.height = reach
        else:
            line.width = reach
        line.pen = reach + (node.extra + self.params.letter_space)

    def _overflow(self) -> None:
        line, region = self.state.line, self.params.region
        if not self.vertical and line.width > region[0]:
            self.state.status |= 0x10
        elif self.vertical and line.height > region[1]:
            self.state.status |= 0x20

    def _glyph_metrics(self, node: _Node, font: SlotFont, size: tuple[F, F], raster: tuple[F, F]) -> None:
        glyph = font.glyph(node.codepoint, (int(raster[0]), int(raster[1])), self.vertical)
        node.glyph = glyph
        if glyph is None:
            policy = self.fonts.catalog.profile.glyph_policy
            factor = {9: policy.tab_advance_scale, 0x20: policy.space_advance_scale}.get(node.codepoint)
            along = size[1] if self.vertical else size[0]
            node.advance = along if factor is None else along * F(factor)
            return
        sx, sy = size[0] / raster[0], size[1] / raster[1]
        node.x_offset, node.top = sx * glyph.left, sy * glyph.top
        node.advance = (sy if self.vertical else sx) * glyph.advance
        node.width, node.height = sx * glyph.width, sy * glyph.rows

    def _icon(self, node: _Node) -> None:
        if self.icon_font is None:
            raise TextLayoutGap("it shows an icon, and the game's icon font is not found")
        base, descent = self.icon_font
        size, icon = self.state.size, node.icon
        if self.params.icon_size <= np.finfo(F).eps:
            fx, fy = size.x / base, size.y / base
            if not self.vertical:
                node.top = (base - descent) * fy
            else:
                node.x_offset = -(base * HALF * fx)
        else:
            fy = fx = self.params.icon_size / base
            if not self.vertical:
                node.top = (base - descent) * fy - (fy - size.y / base) * (size.y * HALF)
            else:
                node.x_offset = -((fy - size.x / base) * (size.x * HALF))
        node.width, node.height, node.extra = fx * F(icon.width), fy * F(icon.height), ZERO
        node.advance = node.height if self.vertical else node.width
        line = self.state.line
        line.width = node.advance + line.pen
        line.pen = line.width + (self.params.letter_space + node.extra)
        self._overflow()

    def _close_ruby(self) -> None:
        state, params = self.state, self.params
        if state.ruby is None or state.ruby_base is None:
            return
        font = self.fonts.slot(params.language, state.font.slot)
        size, raster = self._sizes(font)
        line, start = state.line, state.line.pen
        self._line_metrics(font, size, False)
        base_end = start
        for node in self._run_after(state.ruby_base):
            self._glyph_metrics(node, font, size, raster)
            self._advance(node)
            base_end = line.height if self.vertical else line.width
        self._overflow()
        if state.ruby_text is None:
            return
        ratio, base = params.ruby_ratio, base_end - start
        height = font.height[self.vertical]
        reading, count = ZERO, ZERO
        for node in self._run_after(state.ruby_text):
            self._glyph_metrics(node, font, (size[0] * ratio, size[1] * ratio), (raster[0] * ratio, raster[1] * ratio))
            reading, count = reading + node.advance, count + ONE
            line.ruby = max(line.ruby, size[1] * ratio * height + params.ruby_line_space)
        if reading <= base:
            start = ZERO
            for node in self._run_after(state.ruby_text):
                node.advance = base / count
                reading = (node.height if self.vertical else node.width) + start
                start = start + base / count
            state.ruby.offset = (base - reading) * HALF
        else:
            state.ruby.offset = (reading - base) * F(-0.5)

    def _run_after(self, start: _Node):
        nodes = self.state.nodes
        for node in nodes[nodes.index(start) + 1:]:
            if node.op in (TextOpcode.RUBY, TextOpcode.RUBY_BASE, TextOpcode.RUBY_TEXT):
                return
            if node.op == TextOpcode.GLYPH:
                yield node

    def _wrap(self) -> None:
        state, region, vertical = self.state, self.params.region, self.vertical
        line = state.first_line
        while line is not None:
            if (line.height if vertical else line.width) - self.rules.overflow_tolerance > region[vertical]:
                self._split(line)
            line = line.next_line
        state.status ^= 0x20 if vertical else 0x10

    def _split(self, line: _Node) -> None:
        nodes = self.state.nodes
        after = self._break_after(nodes.index(line) + 1)
        if after is None:
            return
        new = _Node(TextOpcode.LINE_BREAK, next_line=line.next_line)
        line.next_line = new
        at = nodes.index(after) + 1
        nodes.insert(at, new)
        for node in nodes[at + 1:]:
            if node.op == TextOpcode.LINE_BREAK:
                break
            if node.op != TextOpcode.GLYPH or node.removed:
                continue
            if node.icon is not None or node.codepoint not in self.rules.space_delimiters:
                break
            node.removed = True
        self._remeasure()

    def _break_after(self, start: int) -> _Node | None:
        rules, limit = self.rules, self.params.region[self.vertical]
        phase, before_ruby, candidate, result, previous, previous_cp = 0, None, None, None, None, None
        reach = ZERO
        for node in self.state.nodes[start:]:
            op = node.op
            if op == TextOpcode.LINE_BREAK:
                break
            if op == TextOpcode.RUBY:
                phase, before_ruby = (0, None) if node.closing else (1, previous)
            elif op == TextOpcode.RUBY_TEXT:
                phase = 1 if node.closing else 3
            elif op == TextOpcode.RUBY_BASE:
                phase = 1 if node.closing else 2
            elif op != TextOpcode.GLYPH or node.removed:
                continue
            elif phase != 0:
                previous = node
                if phase == 2:
                    candidate, reach = node, reach + node.advance
                    if limit < reach and before_ruby is not None:
                        result = before_ruby
                        break
            else:
                cp = 0 if node.icon is not None else node.codepoint
                if self._breaks_before(previous, previous_cp, cp):
                    candidate = previous
                reach = reach + (node.extra + self.params.letter_space + node.advance)
                previous = node
                if limit < reach:
                    if cp in rules.overflow_punctuation:
                        continue
                    result = candidate
                    if candidate is not None:
                        break
                previous_cp = cp
        return result if reach > limit else None

    def _breaks_before(self, previous: _Node | None, previous_cp: int | None, cp: int) -> bool:
        rules = self.rules
        if previous is not None and previous.codepoint in rules.line_end_openers:
            return False
        if cp in rules.line_starters or cp in rules.non_break_before:
            return False
        if cp in rules.space_delimiters or is_cjk_break_character(cp, rules):
            return True
        if previous_cp is not None and is_cjk_break_character(previous_cp, rules):
            return True
        return cp in rules.line_end_openers or previous_cp in rules.line_starters

    def _remeasure(self) -> None:
        params, vertical = self.params, self.vertical
        font = size = line = None
        phase, ratio = 0, params.ruby_ratio
        for node in self.state.nodes:
            op = node.op
            if op in (TextOpcode.LINE_BREAK, TextOpcode.SIZE, TextOpcode.FONT):
                if op == TextOpcode.LINE_BREAK:
                    line = node
                    line.pen = line.width = line.height = line.ascent = line.ruby = ZERO
                elif op == TextOpcode.SIZE:
                    size = node
                else:
                    font = node
                self._entry_metrics(font, size, line, ratio)
            elif op == TextOpcode.GLYPH and not node.removed and phase in (0, 2) and line is not None:
                reach = line.pen + node.advance
                if vertical:
                    line.height = reach
                else:
                    line.width = reach
                line.pen = (node.extra + params.letter_space) + reach
            elif op == TextOpcode.GLYPH and not node.removed and phase == 3 and None not in (font, size, line):
                entry = self.fonts.slot(params.system_language, font.slot)
                along = entry.scale[0] * ratio * size.x if vertical else entry.scale[1] * ratio * size.y
                line.ruby = max(line.ruby, entry.height[vertical] * along + params.ruby_line_space)
            elif op == TextOpcode.RUBY:
                phase, ratio = (0, params.ruby_ratio) if node.closing else (1, node.ratio)
            elif op == TextOpcode.RUBY_BASE:
                phase = 1 if node.closing else 2
            elif op == TextOpcode.RUBY_TEXT:
                phase = 1 if node.closing else 3

    def _entry_metrics(self, font: _Node | None, size: _Node | None, line: _Node | None, ratio: F) -> None:
        if None in (font, size, line):
            return
        params, vertical = self.params, self.vertical
        entry = self.fonts.slot(params.system_language, font.slot)
        height = entry.height[vertical]
        if not vertical:
            along = entry.scale[1] * size.y
            line.ascent = max(line.ascent, entry.ascent[0] * along)
            line.height = max(line.height, height * along)
        else:
            along = entry.scale[0] * size.x
            line.ascent = max(line.ascent, height * HALF * along)
            line.width = max(line.width, height * along)
        if params.ruby_always:
            line.ruby = max(line.ruby, height * (along * ratio) + params.ruby_line_space)

    def _page_sizes(self) -> tuple[F, F] | None:
        params, vertical, nodes = self.params, self.vertical, self.state.nodes
        across = down = widest = tallest = ZERO
        page = None
        for node in nodes + [None]:
            if node is not None and node.op == TextOpcode.LINE_BREAK:
                if not vertical:
                    across = node.width if node.width > across else across
                    down = down + (node.ruby + node.height + params.line_space)
                else:
                    down = node.height if node.height > down else down
                    across = across + (node.ruby + node.width + params.line_space)
                continue
            if node is not None and node.op != TextOpcode.PAGE:
                continue
            if node is None and page is None:
                self.state.status |= 0x40
                return None
            if not vertical:
                down = down - params.line_space
            else:
                across = across - params.line_space
            if page is not None:
                page.width, page.height = across, down
            widest, tallest = max(across, widest), max(down, tallest)
            if node is None:
                break
            across = down = ZERO
            page = node
        region = params.region
        tolerance = self.rules.overflow_tolerance
        if not vertical and ZERO < region[1] < tallest - tolerance:
            self.state.status |= 0x20
        elif vertical and ZERO < region[0] < widest - tolerance:
            self.state.status |= 0x10
        return widest, tallest, across

    def _place(self) -> tuple[F, F]:
        sizes = self._page_sizes()
        if sizes is None:
            return ZERO, ZERO
        params, region, vertical = self.params, self.params.region, self.vertical
        page_h, page_v = params.page_alignment & 3, params.page_alignment >> 2 & 3
        letter_h, letter_v = params.letter_alignment & 3, params.letter_alignment >> 2 & 7
        if self.flip:
            page_h = 2 - page_h
        x = y = ascent = height = reading_at = ZERO
        ratio, page, line, font, reading = params.ruby_ratio, None, None, None, False
        tags = {TextOpcode.CENTER: (1, 1), TextOpcode.LEFT: (0, 0), TextOpcode.RIGHT: (2, 2),
                TextOpcode.TOP: (0, 0), TextOpcode.BOTTOM: (2, 2)}
        for node in self.state.nodes:
            op = node.op
            if op == TextOpcode.PAGE:
                page, line = node, None
                if vertical:
                    free = region[0] - sizes[2]
                    x = {0: region[0] - free, 1: region[0] - free * HALF, 2: region[0]}.get(page_h, x)
                else:
                    y = self._align(node.height, ZERO, region[1], page_v, 0)
            elif op == TextOpcode.LINE_BREAK:
                if vertical:
                    x = x - (ZERO if line is None else line.ruby + line.width + params.line_space)
                    y = self._align(node.height if page is None else page.height, node.height, region[1], page_v,
                                    0 if letter_v == 3 else letter_v) + ZERO
                else:
                    y = y + (ZERO if line is None else line.ruby + line.height + params.line_space)
                    x = ZERO + self._align(node.width if page is None else page.width, node.width, region[0],
                                           page_h, letter_h)
                node.x, node.y, line = x, y, node
            elif op == TextOpcode.SIZE and font is not None:
                along = node.x if vertical else node.y
                height, ascent = along * font.height, along * font.ascent
            elif op == TextOpcode.FONT:
                font = node
            elif op == TextOpcode.GLYPH and line is not None and not node.removed:
                pen = node.extra + params.letter_space + node.advance
                if reading:
                    node.x, node.y = (x - height * HALF * ratio, reading_at) if vertical else (
                        reading_at, ratio * ascent + y)
                    reading_at = reading_at + pen
                elif vertical:
                    node.x, node.y, y = x - line.ruby - line.ascent, y, y + pen
                else:
                    node.x, node.y, x = x, self._baseline(line, y, letter_v, ascent, height, node.y), x + pen
            elif op in tags and line is not None and (op in (TextOpcode.TOP, TextOpcode.BOTTOM)) == vertical:
                block = (line.height if page is None else page.height) if vertical else (
                    line.width if page is None else page.width)
                offset = self._align(block, line.height if vertical else line.width, region[vertical], *tags[op])
                if not vertical:
                    x = line.x = ZERO + offset
                    line.y = y
                elif op == TextOpcode.BOTTOM:
                    y = offset
                else:
                    y, line.x = offset, x
                    line.y = y
            elif op == TextOpcode.CENTER and line is not None:
                y = self._align(line.height if page is None else page.height, line.height, region[1], 1, 1)
                line.x, line.y = x, y
            elif op == TextOpcode.RUBY:
                ratio = params.ruby_ratio if node.closing else node.ratio
                if not node.closing:
                    reading_at = (y if vertical else x) + node.offset
            elif op == TextOpcode.RUBY_TEXT:
                reading = not node.closing
        return sizes[:2]

    @staticmethod
    def _align(block: F, inner: F, extent: F, page: int, letter: int) -> F:
        free, slack = extent - block, block - inner
        return {(0, 1): slack * HALF, (0, 2): slack, (1, 0): free * HALF, (1, 1): (slack + free) * HALF,
                (1, 2): free * HALF + slack, (2, 0): free, (2, 1): slack * HALF + free,
                (2, 2): slack + free}.get((page, letter), ZERO)

    @staticmethod
    def _baseline(line: _Node, y: F, letter_v: int, ascent: F, height: F, keep: F) -> F:
        if letter_v == 0:
            return y + line.ruby + ascent
        if letter_v == 1:
            return (line.height - height) * HALF + (y + line.ruby) + ascent
        if letter_v == 2:
            return y + line.ruby + line.height - (height - ascent)
        if letter_v == 3:
            return y + line.ruby + line.ascent
        return keep

    def _quads(self) -> tuple[GlyphQuad, ...]:
        quads, color, index = [], None, 0
        for node in self.state.nodes:
            if node.op == TextOpcode.COLOR:
                color = None if node.reset else node.color
            elif node.op == TextOpcode.GLYPH and not node.removed:
                if node.glyph is not None or node.icon:
                    top = node.top if self.vertical else -node.top
                    quads.append(GlyphQuad(node.icon or node.glyph, node.x_offset + node.x, top + node.y, node.width,
                                           node.height, color, node.reading, index))
                index += not node.reading
        return tuple(quads)
