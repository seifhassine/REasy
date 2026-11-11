"""See docs/internals/gui-rendering.md, Glyphs."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import fastglyph

from .sfnt import SfntFont


@dataclass(frozen=True, slots=True)
class GlyphBitmap:
    """A glyph's 8-bit coverage, rows top first; left and top place it from the pen position (y up); box is the
    scaled outline's control box in 26.6 (xMin, yMin, xMax, yMax); advance the charstring width in font units."""

    width: int
    rows: int
    left: int
    top: int
    pixels: bytes
    box: tuple[int, int, int, int]
    advance: int


class GlyphRasterizer:
    """``darkening``: the CFF driver's stem darkening parameters (x1, y1 ... x4, y4), None for none."""

    def __init__(self, font: SfntFont, darkening: Sequence[int] | None):
        record = font.tables.get("CFF ")
        if record is None:
            raise ValueError(f"{font.full_name} has TrueType outlines; only CFF faces are rasterized yet")
        self._native = fastglyph.CffFont(bytes(font.data[record.offset:record.offset + record.length]),
                                         font.units_per_em)
        self._darkening = None if darkening is None else tuple(darkening)

    def bitmap(self, glyph_id: int, x_ppem: int, y_ppem: int) -> GlyphBitmap:
        return GlyphBitmap(*self._native.render(glyph_id, x_ppem, y_ppem, self._darkening))
