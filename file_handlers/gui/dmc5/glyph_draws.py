"""See docs/internals/gui-rendering.md, Glyph draws."""
from __future__ import annotations

from math import inf

import fastglyph
import numpy as np

from file_handlers.engine_math import crt
from file_handlers.ift.model import IconGlyph

lazy from .vertices import bounds, corner_vertices, gui_half, mixed_color, strip_vertices, vertex_color, vertex_srgb

F = np.float32
ZERO, QUARTER, HALF, TWO, BYTE = F(0), F(0.25), F(0.5), F(2), F(255)


class FreeRects:

    def __init__(self, size: tuple[int, int]):
        self.size = size
        self.reset()

    def reset(self) -> None:
        self.free, self.used = [(0, 0, self.size[0] - 1, self.size[1] - 1)], []

    def place(self, width: int, height: int) -> tuple[int, int, int, int] | None:
        width, height = width + (width & 1), height + (height & 1)
        best = least = None
        for at, (_x, _y, free_width, free_height) in enumerate(self.free):
            if (free_width, free_height) == (width, height):
                best = at
                break
            if width <= free_width and height <= free_height and (
                    least is None or free_width * free_height - width * height < least):
                best, least = at, free_width * free_height - width * height
        if best is None:
            return None
        x, y, free_width, free_height = self.free.pop(best)
        tall = free_width <= free_height
        self.free += [piece for piece in ((x, y + height, free_width if tall else width, free_height - height),
                                          (x + width, y, free_width - width, height if tall else free_height))
                      if piece[2] and piece[3]]
        self.used.append((x, y, width, height))
        return x, y, width, height

    def release(self, box: tuple[int, int, int, int]) -> None:
        if box in self.used:
            self.used.remove(box)
            self.free.append(box)

    def merge(self) -> None:
        if not self.used:
            self.reset()
            return
        (width, height), buckets, merged, joined = self.size, [[] for _ in range(16)], [0] * 16, 0
        for x, y, free_width, free_height in self.free:
            buckets[x // (width >> 2) + y // (height >> 2) * 4].append(
                [x, y, x + free_width, y + free_height, free_width, free_height, free_width * free_height])
        for at, records in enumerate(buckets):
            while len(records) > 1 and (count := _merged(records)):
                merged[at] += count
        for at in (0, 2, 8, 10):
            for other in (1, 4, 5):
                _joined(buckets[at], buckets[at + other])
            if merged[at] + merged[at + 1] + merged[at + 4] + merged[at + 5]:
                while len(buckets[at]) > 1 and (count := _merged(buckets[at])):
                    joined += count
        for at in (2, 8, 10):
            _joined(buckets[0], buckets[at])
        while joined and len(buckets[0]) > 1:
            joined = _merged(buckets[0])
        self.free = [(x, y, free_width, free_height)
                     for x, y, _right, _bottom, free_width, free_height, area in buckets[0] if area]


def _merged(records: list) -> int:
    merged = 0
    for at, first in enumerate(records):
        for other in records[at + 1:] if first[6] else ():
            along = int(first[1] == other[3] or other[1] == first[3])
            across = 1 - along
            if not other[6] or not along and first[0] != other[2] and other[0] != first[2]:
                continue
            if first[across] != other[across] or first[across + 4] != other[across + 4]:
                big, small = (other, first) if first[across + 4] < other[across + 4] else (first, other)
                if not (big[across] <= small[across] and small[across + 2] <= big[across + 2] and big[6] < (
                        other[along + 4] + first[along + 4]) * small[across + 4]):
                    continue
                _pieces(records, big, small, across)
                other[across:across + 5:2] = small[across:across + 5:2]
            other[along], other[along + 4] = min(first[along], other[along]), other[along + 4] + first[along + 4]
            other[along + 2], other[6], first[6] = other[along] + other[along + 4], other[4] * other[5], 0
            merged += 1
            break
    return merged


def _pieces(records: list, big: list, small: list, axis: int) -> None:
    for start, end in ((big[axis], small[axis]), (small[axis + 2], big[axis + 2])):
        if start != end:
            piece = big[:]
            piece[axis:axis + 5:2] = start, end, end - start
            piece[6] = piece[4] * piece[5]
            records.append(piece)


def _joined(records: list, others: list) -> None:
    holes = (at for at, record in enumerate(records) if not record[6])
    for record in others:
        if record[6] and (at := next(holes, None)) is not None:
            records[at] = record
        elif record[6]:
            records.append(record)


class Pages:

    def __init__(self, size: tuple[int, int], largest: tuple[int, int]):
        self.size, self.largest, self.shapes, self.packers = size, largest, [], []

    def place(self, width: int, height: int) -> tuple[int, int, int, int, int] | None:
        for page, packer in enumerate(self.packers):
            if (spot := packer.place(width, height)) is not None:
                return page, *spot
        shape = tuple(side << (need // side).bit_length() for side, need in zip(self.size, (width, height)))
        if shape[0] > self.largest[0] or shape[1] > self.largest[1]:
            return None
        self.shapes.append(shape)
        self.packers.append(FreeRects(shape))
        spot = self.packers[-1].place(width, height)
        return None if spot is None else (len(self.packers) - 1, *spot)


class CellCache:

    def __init__(self, pages: Pages, unused_frames: int):
        self.pages, self.unused_frames, self.holders, self._cells, self._boxes = pages, unused_frames, {}, {}, {}
        self.images, self.changed = [], set()

    def collect(self, alive: set, refresh: bool) -> list[int]:
        for path in self.holders.keys() - alive:
            del self.holders[path]
        held = set().union(*self.holders.values())
        sets, dropped = self._unused_sets([key for key in reversed(self._cells) if key not in held]), []
        for frame in range(self.unused_frames + 1):
            for unused in sets:
                for key in unused if frame == self.unused_frames else ():
                    if (box := self._boxes.pop(key)) is not None:
                        self.pages.packers[box[0]].release(box[1:])
                    del self._cells[key]
                for packer in self.pages.packers if unused else ():
                    packer.merge()
            if refresh and not frame:
                dropped = [page for page, packer in enumerate(self.pages.packers) if page and not packer.used]
                index = {old: new for new, old in enumerate(
                    page for page in range(len(self.pages.packers)) if page not in dropped)}
                self.pages.shapes, self.pages.packers, self.images = ([items[old] for old in index if old < len(items)]
                                                                      for items in (self.pages.shapes,
                                                                                    self.pages.packers, self.images))
                self._cells, self._boxes = ({key: cell and (index[cell[0]], *cell[1:]) for key, cell in cells.items()}
                                            for cells in (self._cells, self._boxes))
                self.changed = {index[page] for page in self.changed if page in index}
        return dropped

    def _unused_sets(self, keys: list) -> tuple[list, ...]:
        return keys,


class GlyphAtlas(CellCache):

    def __init__(self, size: tuple[int, int], largest: tuple[int, int], padding: int, margin: int, unused_frames: int):
        super().__init__(Pages(size, largest), unused_frames)
        self.padding, self.margin = padding, margin

    def place(self, glyph) -> tuple[int, tuple[F, F, F, F]] | None:
        if glyph not in self._cells:
            bitmap, pad = glyph.bitmap, self.padding
            spot = self._boxes[glyph] = self.pages.place(bitmap.width + 2 * pad, bitmap.rows + 2 * pad)
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
            x, y, margin = x - self.margin, y - self.margin, 2 * self.margin
            self._cells[glyph] = page, (F(x) / columns, F(y) / rows, F(x + bitmap.width + margin) / columns,
                                        F(y + bitmap.rows + margin) / rows)
        return self._cells[glyph]


class GlyphBlurs(CellCache):

    def __init__(self, size: tuple[int, int], scratch: tuple[int, int], quarter: tuple[int, int] | None,
                 padding: int, unused_frames: int):
        super().__init__(Pages(size, size), unused_frames)
        self.scratch, self.quarter = (None if side is None else Pages(side, side) for side in (scratch, quarter))
        self.padding, self.pending = padding, []

    def place(self, glyph, blur: tuple[F, F], inner: bool, knockout: bool) -> tuple[int, tuple[F, F, F, F]] | None:
        key = glyph, blur, inner, knockout
        if key not in self._cells:
            extents = (glyph.width, glyph.height) if isinstance(glyph, IconGlyph) else (glyph.width, glyph.rows)
            width, height = (int(np.ceil((side + F(self.padding)) * TWO + extent))
                             for side, extent in zip(blur, extents))
            cell = self._boxes[key] = self.pages.place(width, height)
            scratch = None if cell is None else self.scratch.place(int(F(width) * HALF), int(F(height) * HALF))
            quarter = None if scratch is None or self.quarter is None else self.quarter.place(
                int(F(width) * QUARTER), int(F(height) * QUARTER))
            self._cells[key] = None
            if scratch is not None and (self.quarter is None or quarter is not None):
                page, x, y, width, height = cell
                (columns, rows), pad = (F(1) / F(side) for side in self.pages.size), self.padding
                self._cells[key] = page, (F(x + pad) * columns, F(y + pad) * rows, F(width - pad + x) * columns,
                                          F(height - pad + y) * rows)
                self.pending.append((key, page, cell[1:], scratch, quarter))
        return self._cells[key]

    def _unused_sets(self, keys: list) -> tuple[list, ...]:
        return tuple([key for key in keys if isinstance(key[0], IconGlyph) is icon] for icon in (False, True))

    def restart(self) -> None:
        for packer in self.scratch.packers + (self.quarter.packers if self.quarter else []):
            packer.reset()


class GlyphDistances(CellCache):

    def __init__(self, size: tuple[int, int], spread: float, unused_frames: int):
        super().__init__(Pages(size, size), unused_frames)
        self.spread = F(spread)

    def place(self, glyph) -> tuple[int, tuple[F, F, F, F], tuple[int, int]] | None:
        if glyph not in self._cells:
            bitmap, self._cells[glyph] = glyph.bitmap, None
            spread = tuple(int(F(side) * self.spread) for side in glyph.raster)
            spot = self._boxes[glyph] = self.pages.place(
                bitmap.width + 2 + 2 * spread[0], bitmap.rows + 2 + 2 * spread[1]) if bitmap.pixels else None
            if spot is not None:
                page, x, y, width, height = spot
                self.images += [np.zeros(shape[::-1], np.uint8) for shape in self.pages.shapes[len(self.images):]]
                cell = fastglyph.distance_field(bitmap.pixels, bitmap.width, bitmap.rows, width, height, min(spread))
                self.images[page][y:y + height, x:x + width] = np.frombuffer(cell, np.uint8).reshape(height, width)
                self.changed.add(page)
                (columns, rows), x, y = (F(side) for side in self.pages.shapes[page]), F(x + 1), F(y + 1)
                self._cells[glyph] = page, (x / columns, y / rows, (F(bitmap.width + 2 * spread[0]) + x) / columns,
                                            (F(bitmap.rows + 2 * spread[1]) + y) / rows), spread
        return self._cells[glyph]


def _distance_row(border: int, softness: int, thickness: int, strength: int, threshold) -> tuple[F, F, F, F]:
    base, soft, thick, one = HALF - F(border) * F(0.005), F(softness) * F(0.01), F(thickness) * F(0.01), F(1)
    low, high = (min(max(value, ZERO), one) for value in (base - thick * soft, (one - thick) * soft + base))
    if low == high:
        low, high = (low, high + F(1e-6)) if low == ZERO else (low - F(1e-6), high)
    power = F(abs(strength)) * F(0.02) + one
    return low, high, one / power if strength > 0 else power, F(threshold)


def distance_constants(node, rule) -> dict:
    properties, srgb = node.render_properties, vertex_srgb(node)
    colors = [[F(channel) / BYTE for channel in vertex_color(properties[f"DistanceField{side}Color"], node.color_scale,
                                                             node.color_offset, srgb)] for side in ("", "Out")]
    if not properties["DistanceFieldType"]:
        colors[1] = [*colors[0][:3], ZERO]
    rows = [_distance_row(*(properties[f"DistanceField{side}{name}"] for name in ("Border", "Softness", "Thickness",
                                                                                   "Strength")), threshold)
            for side, threshold in (("", properties["DistanceFieldBaseColorThreshold"]),
                                    ("Out", rule["out_threshold"]))]
    return dict(zip(rule["constants"], (rows[0], colors[0], rows[1], colors[1])))


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


def wide_constants(horizontal: dict, quarter_cell, quarter_size, scratch_size, blur, margin) -> tuple[dict, dict]:
    (x, y, columns, lines), (blur_x, blur_y) = quarter_cell, (F(side) for side in blur)
    inverse, scratch = (tuple(F(1) / F(side) for side in size) for size in (quarter_size, scratch_size))
    return {**horizontal, "blurSize": (ZERO, ZERO), "blurInfo": (F(1), ZERO)}, {
        **horizontal, "texRegionSub": (F(x) * inverse[0], F(y) * inverse[1], F(columns + x) * inverse[0],
                                       F(lines + y) * inverse[1]),
        "offsetSizeSub": (ZERO, ZERO), "blurSizeSub": (scratch[0] * blur_x, scratch[1] * blur_y),
        "blurRangeMargin": tuple(F(side) * scale for side, scale in zip(margin, inverse))}


def _colors(node, facts, color: tuple[int, int] | None, icon: bool, assets,
            alpha: int | None = None) -> list[tuple[int, int, int, int]]:
    properties, runtime = node.render_properties, node.runtime
    scale, offset, srgb = node.color_scale, node.color_offset, vertex_srgb(node)
    kind = runtime.enum_label(properties, "ColorType", "FontColorType")
    raw = [_alpha(mixed_color(node, name, assets), alpha) for name in facts["glyph_colors"][kind]]
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


def _alpha(rgba, alpha: int | None):
    return rgba if alpha is None else (*rgba[:3], alpha)


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


def _draws(node, offset, groups: dict, clip: bool = True) -> list[tuple[object, bool, np.ndarray, np.ndarray]]:
    draws = []
    for (texture, icon), records in groups.items():
        kept = _clipped(node, offset, records) if clip else records
        if kept:
            draws.append((texture, icon, *strip_vertices([
                corner_vertices(rect, colors, [(gui_half(u), gui_half(v)) for u, v in uvs], z)
                for colors, uvs, rect, z in kept])))
    return draws


def sprite_corners(vertices: np.ndarray) -> np.ndarray:
    corners = vertices["position"][:, :2].reshape(-1, 4, 2)
    centre = -((corners[:, 3:] - corners[:, :1]) * HALF) - corners[:, :1]
    vertices["position"][:, :2] = ((corners + centre) - centre).reshape(-1, 2)
    return vertices


def _corner(node, facts) -> tuple[float, float]:
    return bounds(node, [max(0.0, float(side)) for side in node.render_properties[facts["size"]]])[:2]


def text_draws(node, facts, layout, atlas: GlyphAtlas, assets, typed: float = inf, clip: bool = True,
               alphas: tuple = ()) -> list[tuple[object, bool, np.ndarray, np.ndarray]]:
    left, top = _corner(node, facts)
    groups = {}
    for at, quad in enumerate(layout.quads):
        icon = isinstance(quad.glyph, IconGlyph)
        placed = (quad.glyph.texture_path, quad.glyph.uv_rect) if icon else atlas.place(quad.glyph)
        if placed is None:
            continue
        texture, uv = placed
        (u0, v0, u1, v1), x0, y0 = (F(value) for value in uv), F(left) + quad.x, F(top) + quad.y
        upper, lower = _colors(node, facts, quad.color, icon, assets, alphas[at] if at < len(alphas) else None) if (
            quad.index < typed) else [(0, 0, 0, 0)] * 2
        groups.setdefault((texture, icon), []).insert(0, (
            [upper, upper, lower, lower], [(u0, v0), (u1, v0), (u0, v1), (u1, v1)],
            (x0, y0, x0 + quad.width, y0 + quad.height), 0.0))
    return _draws(node, (left, top), groups, clip)


def distance_draws(node, facts, layout, distances: GlyphDistances, raster_scale: float,
                   typed: float = inf) -> list[tuple[object, bool, np.ndarray, np.ndarray]]:
    properties, (left, top), scale = node.render_properties, _corner(node, facts), F(raster_scale)
    raster = [F(int(max(1.0, float(side))) & 0xFFFF) for side in properties["RasterSize"]]
    fill = (0, 0, 0, 0) if properties["DistanceFieldDrawGlyph"] else vertex_color(
        properties["DistanceFieldBaseColor"], node.color_scale, node.color_offset, vertex_srgb(node))
    groups = {}
    for quad in layout.quads:
        found = distances.place(quad.glyph)
        if found is None:
            continue
        page, (u0, v0, u1, v1), spread = found
        margin = [size / (pixels * scale) * F(side - 1) for size, pixels, side in zip(quad.run, raster, spread)]
        color, x, y = fill if quad.index < typed else (0, 0, 0, 0), F(left) + quad.x, F(top) + quad.y
        groups.setdefault((page, False), []).insert(0, (
            [color] * 4, [(u0, v0), (u1, v0), (u0, v1), (u1, v1)],
            (x - margin[0], y - margin[1], x + quad.width + margin[0], y + quad.height + margin[1]), 0.0))
    return _draws(node, (left, top), groups)


def effect_key(node, facts, kind: str, raster_scale: float, rules) -> tuple[tuple[F, F], bool, bool, bool]:
    properties, scale = node.render_properties, F(raster_scale)
    blur = tuple(F(side) * scale if rules.effect_blur_scaled else F(side) for side in properties[f"{kind}BlurSize"])
    inner = kind == "Glow" and bool(properties["GlowInner"])
    knockout = kind == "Glow" and not inner and "glow_knockout" in facts and bool(properties[facts["glow_knockout"]])
    return blur, inner, knockout, "icon_effects" in facts and bool(properties[facts["icon_effects"]]) and min(blur) > 0


def glyph_cells(node, facts, layout, atlas: GlyphAtlas, blurs: GlyphBlurs | None, distances: GlyphDistances | None,
                raster_scale: float, rules) -> tuple[set, set, set]:
    keys, held = [effect_key(node, facts, kind, raster_scale, rules) for kind in ("Glow", "Shadow")
                  if node.render_properties[f"{kind}Enable"]], (set(), set(), set())
    for quad in sorted(layout.quads, key=lambda quad: isinstance(quad.glyph, IconGlyph)):
        icon = isinstance(quad.glyph, IconGlyph)
        if not icon:
            atlas.place(quad.glyph)
            held[0].add(quad.glyph)
        for blur, inner, knockout, icons in keys:
            if icons or not icon:
                blurs.place(quad.glyph, blur, inner, knockout)
                held[1].add((quad.glyph, blur, inner, knockout))
        if distances is not None and not icon:
            distances.place(quad.glyph)
            held[2].add(quad.glyph)
    return held


def effect_draws(node, facts, layout, blurs: GlyphBlurs, kind: str, raster_scale: float, rules,
                 degree_factors: tuple[float, float], assets, typed: float = inf, clip: bool = True,
                 alphas: tuple = ()) -> list[tuple[object, bool, np.ndarray, np.ndarray]]:
    properties, (left, top), scale = node.render_properties, _corner(node, facts), F(raster_scale)
    blur, inner, knockout, icons = effect_key(node, facts, kind, raster_scale, rules)
    font = [F(gui_half(max(1.0, float(side)))) for side in properties["FontSize"]]
    raster = [F(int(max(1.0, float(side))) & 0xFFFF) for side in properties["RasterSize"]]
    if kind == "Shadow":
        angle = F(properties["ShadowRotation"]) * F(degree_factors[0]) * F(degree_factors[1])
        distance = F(properties["ShadowDistance"])
        left, top = F(left) + F(crt("cosf")(angle)) * distance, F(top) + F(crt("sinf")(angle)) * distance
    z = float(F(properties["ShadowDistanceZ"])) if kind == "Shadow" else 0.0
    mixed, srgb = mixed_color(node, f"{kind}Color", assets), vertex_srgb(node)
    multiply = node.runtime.enum_label(properties, "EffectAlphaType") in facts["effect_alpha_multiplied"]
    groups = {}
    for at, quad in enumerate(layout.quads):
        icon = isinstance(quad.glyph, IconGlyph)
        found = None if icon and not icons else blurs.place(quad.glyph, blur, inner, knockout)
        if found is None:
            continue
        page, (u0, v0, u1, v1) = found
        margin = (ZERO, ZERO) if icon else [size / (pixels * scale) * side for size, pixels, side in zip(
            quad.run if rules.effect_run_size else font, raster, blur)]
        alpha = alphas[at] if at < len(alphas) else None
        rgb = vertex_color(_alpha(mixed, alpha), node.color_scale, node.color_offset, srgb)
        shown = [rgb[3] * color[3] // 255 if multiply else rgb[3]
                 for color in _colors(node, facts, quad.color, icon, assets, alpha)]
        upper, lower = ((*rgb[:3], value) if quad.index < typed else (0, 0, 0, 0) for value in shown)
        x, y = F(left) + quad.x, F(top) + quad.y
        groups.setdefault((page, False), []).insert(0, (
            [upper, upper, lower, lower], [(u0, v0), (u1, v0), (u0, v1), (u1, v1)],
            (x - margin[0], y - margin[1], x + quad.width + margin[0], y + quad.height + margin[1]), z))
    return _draws(node, _corner(node, facts), groups, clip)
