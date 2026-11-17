"""See docs/internals/gui-rendering.md, Glyphs."""
from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

import fastglyph

from .sfnt import SfntFont, SfntFormatError


def mulfix(a: int, b: int) -> int:
    c = (abs(a) * abs(b) + 0x8000) >> 16
    return -c if (a < 0) != (b < 0) else c


def divfix(a: int, b: int) -> int:
    return ((a << 16) + (b >> 1)) // b


@dataclass(frozen=True, slots=True)
class GlyphBitmap:
    """A glyph's 8-bit coverage, rows top first; left and top place it from the pen position (y up); box is the
    scaled outline's control box in 26.6 (xMin, yMin, xMax, yMax); advance 26.6 without the embolden's strength
    (26.6, added to the advance and yMax)."""

    width: int
    rows: int
    left: int
    top: int
    pixels: bytes
    box: tuple[int, int, int, int]
    advance: int
    strength: int


class _TrueType:
    def __init__(self, font: SfntFont, x_scale: int, y_scale: int):
        self.font, self.x_scale, self.y_scale = font, x_scale, y_scale
        self.points: list[tuple[int, int]] = []
        self.tags, self.ends = bytearray(), []

    def load(self, glyph: int, depth: int = 0) -> tuple[int, int]:
        font, data = self.font, self.font.glyph_record(glyph)
        contours, x_min = struct.unpack_from(">hh", data) if data else (0, 0)
        if contours < -1 or depth > max(1, struct.unpack_from(">H", font.data, font.tables["maxp"].offset + 30)[0]):
            raise SfntFormatError(f"glyph {glyph} of {font.full_name} is not a loadable TrueType glyph")
        metric, start = font.glyph_metric(glyph), len(self.points)
        left = (x_min if contours else 0) - metric.side_bearing
        phantom = mulfix(left, self.x_scale), mulfix(left + metric.advance, self.x_scale)
        if contours > 0:
            self._simple(data, contours)
        at = 10
        while contours < 0:
            flags, part = struct.unpack_from(">HH", data, at)
            first, second = struct.unpack_from(">hh" if flags & 1 else ">bb", data, at + 4)
            at += 8 if flags & 1 else 6
            xx, xy, yx, yy = 0x10000, 0, 0, 0x10000
            if flags & 8:
                xx = yy = struct.unpack_from(">h", data, at)[0] << 2
            elif flags & 0x40:
                xx, yy = (value << 2 for value in struct.unpack_from(">hh", data, at))
            elif flags & 0x80:
                xx, yx, xy, yy = (value << 2 for value in struct.unpack_from(">hhhh", data, at))
            at += 2 if flags & 8 else 4 if flags & 0x40 else 8 if flags & 0x80 else 0
            base = len(self.points)
            held = self.load(part, depth + 1)
            if flags & 0x200:
                phantom = held
            if len(self.points) > base:
                self._place(flags, first, second, (xx, xy, yx, yy), start, base)
            contours = -(flags >> 5 & 1)
        return phantom

    def _simple(self, data: bytes, contours: int) -> None:
        ends, base = struct.unpack_from(f">{contours}H", data, 10), len(self.points)
        at, flags, axes = 12 + 2 * contours + struct.unpack_from(">H", data, 10 + 2 * contours)[0], bytearray(), []
        while len(flags) <= ends[-1]:
            flags += data[at:at + 1] * (1 + (data[at + 1] if data[at] & 8 else 0))
            at += 2 if data[at] & 8 else 1
        for short, same, scale in ((2, 0x10, self.x_scale), (4, 0x20, self.y_scale)):
            value, held = 0, []
            for flag in flags:
                if flag & short:
                    value += data[at] if flag & same else -data[at]
                    at += 1
                elif not flag & same:
                    value += struct.unpack_from(">h", data, at)[0]
                    at += 2
                held.append(mulfix(value, scale))
            axes.append(held)
        self.points += zip(*axes)
        self.tags += bytes(flag & 1 for flag in flags)
        self.ends += [base + end for end in ends]

    def _place(self, flags: int, first: int, second: int, matrix: tuple[int, int, int, int], start: int,
               base: int) -> None:
        points, (xx, xy, yx, yy) = self.points, matrix
        if flags & 0xC8:
            points[base:] = [(mulfix(x, xx) + mulfix(y, xy), mulfix(x, yx) + mulfix(y, yy)) for x, y in points[base:]]
        if flags & 2:
            x, y = first, second
            if flags & 0xC8 and flags & 0x800:
                x, y = mulfix(x, fastglyph.vector_length(xx, xy)), mulfix(y, fastglyph.vector_length(yy, yx))
            x, y = mulfix(x, self.x_scale), mulfix(y, self.y_scale)
            if flags & 4:
                x, y = x + 32 & -64, y + 32 & -64
        elif 0 <= first + start < base and 0 <= second + base < len(points):
            (x, y), (near_x, near_y) = points[first + start], points[second + base]
            x, y = x - near_x, y - near_y
        else:
            return
        points[base:] = [(near_x + x, near_y + y) for near_x, near_y in points[base:]]


class GlyphRasterizer:
    """``darkening``: the CFF driver's stem darkening parameters (x1, y1 ... x4, y4), None for none."""

    def __init__(self, font: SfntFont, darkening: Sequence[int] | None):
        record = font.tables.get("CFF ")
        self._font, self._darkening = font, None if darkening is None else tuple(darkening)
        self._native = record and fastglyph.CffFont(bytes(font.data[record.offset:record.offset + record.length]),
                                                    font.units_per_em)
        self._whole = not record and struct.unpack_from(">H", font.data, font.tables["head"].offset + 16)[0] & 8

    def scales(self, x_size: int, y_size: int) -> tuple[int, int, int]:
        x_size, y_size = max(x_size or y_size, 64), max(y_size or x_size, 64)
        if self._whole:
            x_size, y_size = x_size + 32 & -64, y_size + 32 & -64
        return divfix(x_size, self._font.units_per_em), divfix(y_size, self._font.units_per_em), y_size + 32 >> 6

    def widths(self) -> bytes:
        return self._native.widths()

    def bitmap(self, glyph_id: int, x_size: int, y_size: int, shear: float = 0.0, bold: bool = False) -> GlyphBitmap:
        x_scale, y_scale, ppem = self.scales(x_size, y_size)
        if self._native:
            return GlyphBitmap(*self._native.render(glyph_id, x_scale, y_scale, ppem, self._darkening, shear, bold))
        glyph = _TrueType(self._font, x_scale, y_scale)
        left, right = glyph.load(glyph_id)
        return GlyphBitmap(*fastglyph.outline(
            struct.pack(f"<{2 * len(glyph.points)}i", *(value for x, y in glyph.points for value in (x - left, y))),
            bytes(glyph.tags), struct.pack(f"<{len(glyph.ends)}H", *glyph.ends), shear,
            mulfix(self._font.units_per_em, y_scale) // 24 if bold else 0, right - left))
