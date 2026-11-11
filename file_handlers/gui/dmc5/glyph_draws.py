"""See docs/internals/gui-rendering.md, Glyph draws."""
from __future__ import annotations

import numpy as np

from file_handlers.engine_math import crt
from file_handlers.ift.model import IconGlyph

lazy from .vertices import bounds, corner_vertices, gui_half, strip_vertices, vertex_color

F = np.float32
ZERO, HALF, TWO, BYTE = F(0), F(0.5), F(2), F(255)


class Pages:

    def __init__(self, size: tuple[int, int], largest: tuple[int, int]):
        self.size, self.largest, self.shapes, self._rows = size, largest, [], []

    def place(self, width: int, height: int) -> tuple[int, int, int, int, int] | None:
        width, height = width + (width & 1), height + (height & 1)
        for page in range(len(self.shapes) + 1):
            if page == len(self.shapes):
                shape = tuple(min(side << (need // side).bit_length(), most)
                              for side, need, most in zip(self.size, (width, height), self.largest))
                if width > shape[0] or height > shape[1]:
                    return None
                self.shapes.append(shape)
                self._rows.append([])
            rows, (columns, lines) = self._rows[page], self.shapes[page]
            for row in rows:
                if height <= row[1] and row[2] + width <= columns:
                    row[2] += width
                    return page, row[2] - width, row[0], width, height
            top = rows[-1][0] + rows[-1][1] if rows else 0
            if top + height <= lines and width <= columns:
                rows.append([top, height, width])
                return page, 0, top, width, height
        return None


class GlyphAtlas:

    def __init__(self, size: tuple[int, int], largest: tuple[int, int], padding: int):
        self.pages, self.padding, self.images, self.changed, self._cells = Pages(size, largest), padding, [], set(), {}

    def place(self, glyph) -> tuple[int, tuple[F, F, F, F]] | None:
        if glyph not in self._cells:
            bitmap, pad = glyph.bitmap, self.padding
            spot = self.pages.place(bitmap.width + 2 * pad, bitmap.rows + 2 * pad)
            if spot is None:
                self._cells[glyph] = None
                return None
            page, x, y, _width, _height = spot
            self.images += [np.zeros(shape[::-1], np.uint8) for shape in self.pages.shapes[len(self.images):]]
            x, y = x + pad, y + pad
            self.images[page][y:y + bitmap.rows, x:x + bitmap.width] = np.frombuffer(
                bitmap.pixels, np.uint8).reshape(bitmap.rows, bitmap.width)
            self.changed.add(page)
            columns, rows = (F(side) for side in self.pages.shapes[page])
            self._cells[glyph] = page, (F(x) / columns, F(y) / rows, F(x + bitmap.width) / columns,
                                        F(y + bitmap.rows) / rows)
        return self._cells[glyph]


class GlyphBlurs:

    def __init__(self, size: tuple[int, int], scratch: tuple[int, int], padding: int):
        self.pages, self.scratch, self.padding = Pages(size, size), Pages(scratch, scratch), padding
        self.pending, self._cells = [], {}

    def place(self, glyph, blur: tuple[F, F], inner: bool) -> tuple[int, tuple[F, F, F, F]] | None:
        key = glyph, blur, inner
        if key not in self._cells:
            width, height = (int(np.ceil((side + F(self.padding)) * TWO + extent))
                             for side, extent in zip(blur, (glyph.width, glyph.rows)))
            cell = self.pages.place(width, height)
            scratch = None if cell is None else self.scratch.place(int(F(width) * HALF), int(F(height) * HALF))
            self._cells[key] = None
            if scratch is not None:
                page, x, y, width, height = cell
                (columns, rows), pad = (F(1) / F(side) for side in self.pages.shapes[page]), self.padding
                self._cells[key] = page, (F(x + pad) * columns, F(y + pad) * rows, F(width - pad + x) * columns,
                                          F(height - pad + y) * rows)
                self.pending.append((glyph, blur, inner, page, cell[1:], scratch[0], scratch[1:]))
        return self._cells[key]


def filter_constants(sharp_box, sharp_size, scratch_cell, scratch_size, blur, padding: int,
                     margin) -> tuple[dict, dict]:
    (width, height), (x, y, columns, lines) = (F(side) for side in sharp_size), scratch_cell
    inverse = F(1) / width, F(1) / height
    scratch = tuple(F(1) / F(side) for side in scratch_size)
    pad, (blur_x, blur_y) = F(padding), (F(side) for side in blur)
    horizontal = {"texRegion": sharp_box, "offsetSize": (pad / width, pad / height),
                  "blurSize": (inverse[0] * blur_x, blur_y * inverse[1]), "blurSizeRaw": (blur_x, blur_y),
                  "blurRangeMargin": tuple(F(side) * scale for side, scale in zip(margin, inverse))}
    vertical = {"texRegion": (F(x) * scratch[0], F(y) * scratch[1], F(columns + x) * scratch[0],
                              F(lines + y) * scratch[1]), "texRegion2": sharp_box,
                "offsetSize": (pad / width + inverse[0] * blur_x, pad / height + inverse[1] * blur_y),
                "blurSize": (blur_x * (HALF * scratch[0]), blur_y * (HALF * scratch[1])),
                "blurSizeRaw": (blur_x, blur_y),
                "blurRangeMargin": tuple(F(side) * scale for side, scale in zip(margin, scratch))}
    return horizontal, vertical


def _colors(node, facts, color: tuple[int, int] | None, icon: bool) -> list[tuple[int, int, int, int]]:
    properties, runtime = node.render_properties, node.runtime
    scale, offset, srgb = node.color_scale, node.color_offset, runtime.srgb_to_linear
    kind = runtime.enum_label(properties, "ColorType", "FontColorType")
    raw = [properties[name] for name in facts["glyph_colors"][kind]]
    if icon:
        mode = runtime.enum_label(properties, "IconColorType")
        if mode == "None":
            return [(255, 255, 255, 255)] * 2
        if mode == "AlphaOnly":
            return [(255, 255, 255, vertex_color(rgba, scale, offset, srgb)[3]) for rgba in raw]
    if color is None:
        return [vertex_color(rgba, scale, offset, srgb) for rgba in raw]
    return [vertex_color((tag & 0xFF, tag >> 8 & 0xFF, tag >> 16 & 0xFF, rgba[3]), scale, offset, srgb)
            for tag, rgba in zip(color, raw)]


def _packed(channels) -> tuple[int, ...]:
    return tuple(int(min(BYTE, max(ZERO, channel * BYTE))) for channel in channels)


def _clipped(node, offset: tuple[float, float], records: list) -> list:
    left, top, right, bottom = (F(gui_half(side)) for side in node.render_properties["ScissorRect"])
    clip_x, clip_y = right - left >= ZERO, bottom - top >= ZERO
    if not clip_x and not clip_y:
        return records
    left, right, top, bottom = left + F(offset[0]), right + F(offset[0]), top + F(offset[1]), bottom + F(offset[1])
    kept = []
    for colors, uvs, (x0, y0, x1, y1), z in records:
        width, height, colors, rect = x1 - x0, y1 - y0, list(colors), [x0, y0, x1, y1]
        (u_left, v_top), (u_right, _v), (_u, v_bottom), _corner = uvs
        uvs = [list(uv) for uv in uvs]
        if width == ZERO or height == ZERO:
            continue
        if clip_x and left > x0:
            if left > x1:
                continue
            rect[0] = left
            uvs[0][0] = uvs[2][0] = (left - x0) / width * (u_right - u_left) + u_left
        if clip_x and x1 > right:
            if x0 > right:
                continue
            rect[2] = right
            uvs[1][0] = uvs[3][0] = u_right - (x1 - right) / width * (u_right - u_left)
        upper, lower = ([F(channel) / BYTE for channel in colors[at]] for at in (0, 2))
        if clip_y and top > y0:
            if top > y1:
                continue
            share, rect[1] = (top - y0) / height, top
            uvs[0][1] = uvs[1][1] = (v_bottom - v_top) * share + v_top
            colors[0] = colors[1] = _packed((low - high) * share + high for high, low in zip(upper, lower))
        if clip_y and y1 > bottom:
            if y0 > bottom:
                continue
            share, rect[3] = (y1 - bottom) / height, bottom
            uvs[2][1] = uvs[3][1] = v_bottom - (v_bottom - v_top) * share
            colors[2] = colors[3] = _packed(low - (low - high) * share for high, low in zip(upper, lower))
        kept.append((colors, uvs, rect, z))
    return kept


def _draws(node, offset, groups: dict) -> list[tuple[object, bool, np.ndarray, np.ndarray]]:
    draws = []
    for (texture, icon), records in groups.items():
        kept = _clipped(node, offset, records)
        if kept:
            draws.append((texture, icon, *strip_vertices([
                corner_vertices(rect, colors, [(gui_half(u), gui_half(v)) for u, v in uvs], z)
                for colors, uvs, rect, z in kept])))
    return draws


def _corner(node, facts) -> tuple[float, float]:
    return bounds(node, [max(0.0, float(side)) for side in node.render_properties[facts["size"]]])[:2]


def text_draws(node, facts, layout, atlas: GlyphAtlas) -> list[tuple[object, bool, np.ndarray, np.ndarray]]:
    left, top = _corner(node, facts)
    groups = {}
    for quad in layout.quads:
        icon = isinstance(quad.glyph, IconGlyph)
        placed = (quad.glyph.texture_path, quad.glyph.uv_rect) if icon else atlas.place(quad.glyph)
        if placed is None:
            continue
        texture, uv = placed
        (u0, v0, u1, v1), x0, y0 = (F(value) for value in uv), F(left) + quad.x, F(top) + quad.y
        upper, lower = _colors(node, facts, quad.color, icon)
        groups.setdefault((texture, icon), []).insert(0, (
            [upper, upper, lower, lower], [(u0, v0), (u1, v0), (u0, v1), (u1, v1)],
            (x0, y0, x0 + quad.width, y0 + quad.height), 0.0))
    return _draws(node, (left, top), groups)


def effect_draws(node, facts, layout, blurs: GlyphBlurs, kind: str, raster_scale: float,
                 degree_factors: tuple[float, float]) -> list[tuple[object, bool, np.ndarray, np.ndarray]]:
    properties, (left, top) = node.render_properties, _corner(node, facts)
    blur = tuple(F(side) for side in properties[f"{kind}BlurSize"])
    inner = kind == "Glow" and bool(properties["GlowInner"])
    font = [F(gui_half(max(1.0, float(side)))) for side in properties["FontSize"]]
    raster = [F(int(max(1.0, float(side))) & 0xFFFF) for side in properties["RasterSize"]]
    margin = [size / (pixels * F(raster_scale)) * side for size, pixels, side in zip(font, raster, blur)]
    if kind == "Shadow":
        angle = F(properties["ShadowRotation"]) * F(degree_factors[0]) * F(degree_factors[1])
        distance = F(properties["ShadowDistance"])
        left, top = F(left) + F(crt("cosf")(angle)) * distance, F(top) + F(crt("sinf")(angle)) * distance
    z = float(F(properties["ShadowDistanceZ"])) if kind == "Shadow" else 0.0
    rgb = vertex_color(properties[f"{kind}Color"], node.color_scale, node.color_offset, node.runtime.srgb_to_linear)
    multiply = node.runtime.enum_label(properties, "EffectAlphaType") == "MultiplyTextColorAlpha"
    groups = {}
    for quad in layout.quads:
        if isinstance(quad.glyph, IconGlyph):
            continue
        found = blurs.place(quad.glyph, blur, inner)
        if found is None:
            continue
        page, (u0, v0, u1, v1) = found
        alphas = [rgb[3] * color[3] // 255 if multiply else rgb[3] for color in _colors(node, facts, quad.color, False)]
        upper, lower = ((*rgb[:3], alpha) for alpha in alphas)
        x, y = F(left) + quad.x, F(top) + quad.y
        groups.setdefault((page, False), []).insert(0, (
            [upper, upper, lower, lower], [(u0, v0), (u1, v0), (u0, v1), (u1, v1)],
            (x - margin[0], y - margin[1], x + quad.width + margin[0], y + quad.height + margin[1]), z))
    return _draws(node, _corner(node, facts), groups)
