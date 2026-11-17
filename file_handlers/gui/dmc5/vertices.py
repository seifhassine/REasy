"""See docs/internals/gui-rendering.md, Vertices."""
from __future__ import annotations

import math
import struct
from functools import lru_cache
from itertools import product
from uuid import UUID

import numpy as np

from file_handlers.native_math import f32, fadd, fdiv, fmul, fsub
lazy from file_handlers.engine_math import EPSILON, crt
lazy from ..model import GuiNineSlice, GuiTextureSet

VERTEX = np.dtype([("position", "<f4", 3), ("color", "u1", 4), ("uv", "<f2", 2)])
MATERIAL_VERTEX = np.dtype([("position", "<f4", 3), ("color", "u1", 4), ("uv", "<f2", 2), ("uv1", "<f2", 2)])
CUT = 0xFFFF


def gui_half(value: float) -> float:
    bits = struct.unpack("<I", struct.pack("<f", f32(value)))[0]
    sign = (bits >> 16) & 0x8000
    mantissa = bits & 0x7FFFFF
    exponent = ((bits >> 23) & 0xFF) - 0x70
    if exponent <= 0:
        payload = 0 if exponent < -10 else ((mantissa | 0x800000) >> (1 - exponent)) >> 13
        half = sign | payload
    elif exponent == 0x8F:
        payload = mantissa >> 13
        half = sign | 0x7C00 | (payload or (1 if mantissa else 0))
    elif exponent > 0x1E:
        half = sign | 0x7C00
    else:
        half = sign | (exponent << 10) | (mantissa >> 13)
    return struct.unpack("<e", struct.pack("<H", half))[0]


@lru_cache(maxsize=256)
def linear8(value: int, srgb: tuple[float, float, float, float, float]) -> int:
    threshold, slope, offset, scale, power = (f32(item) for item in srgb)
    channel = f32(f32(float(value & 0xFF)) / f32(255.0))
    if channel <= threshold:
        linear = f32(channel / slope)
    else:
        linear = f32(math.pow(f32(f32(channel + offset) / scale), power))
    return int(math.floor(f32(f32(linear * 255.0) + 0.5)))


def add_color(node, name: str, srgb) -> list[float]:
    threshold, slope, offset, scale, power = (f32(item) for item in srgb)
    rgba, item = [0, 0, 0], node.parent
    while item is not None:
        rgba = [min(255, held + value) for held, value in zip(rgba, item.render_properties[name])]
        item = item.parent
    channels = [fdiv(float(value), 255.0) for value in rgba]
    return [*(crt("powf")(fdiv(fadd(value, offset), scale), power) if value > threshold else fdiv(value, slope)
              for value in channels), 0.0]


def color_channel(value: int, scale: float, offset: float) -> int:
    transformed = f32(f32(f32(f32(float(value)) * f32(scale)) + f32(offset)) + f32(0.5))
    return 0 if math.isnan(transformed) else int(min(255.0, max(0.0, transformed)))


def vertex_color(rgba, scale, offset, srgb) -> tuple[int, int, int, int]:
    return (*(color_channel(int(rgba[at]) if srgb is None else linear8(int(rgba[at]), srgb), scale[at], offset[at])
              for at in range(3)), color_channel(int(rgba[3]), scale[3], 0.0))


def preset_color(properties, name: str, fields, presets) -> tuple[int, int, int, int]:
    color = tuple(properties[name])
    preset, blend, rate = (properties[name.replace("Color", "Color" + field, 1)] for field in fields)
    first, rate = presets.get(UUID(str(preset))), f32(rate)
    if first is None:
        return color
    second, base = presets.get(UUID(str(blend))) if rate > 0.0 else None, first.colors[0]
    if second is None:
        return (*base[:3], base[3] if first.enable_alpha else color[3])
    mixed = [second.colors[0][at] if rate >= 1.0 else int(fadd(fmul(fsub(float(second.colors[0][at]), float(base[at])),
                                                                     rate), float(base[at]))) for at in range(4)]
    return (*mixed[:3], (mixed[3] if second.enable_alpha else base[3]) if first.enable_alpha else color[3])


def stored_half(node, side: float) -> float:
    return (gui_half if node.runtime.size_halves == "binary16" else f32)(fmul(side, 0.5))


def bounds(node, size) -> tuple[float, float, float, float]:
    control = node.runtime.enum_value(node.render_properties, "ControlPoint")

    def axis(side: float, alignment: int) -> tuple[float, float]:
        half = stored_half(node, side)
        return ((0.0, half + half), (-half, half), (-(half + half), 0.0))[min(alignment, 2)]

    (left, right), (top, bottom) = axis(size[0], control & 3), axis(size[1], control >> 2)
    return left, top, right, bottom


def display_bounds(node, shown, alignment: int) -> tuple[float, float, float, float]:
    left, top, _right, _bottom = bounds(node, node.size)
    ends = []
    for start, side, held, step in ((left, node.size[0], shown[0], alignment & 3),
                                    (top, node.size[1], shown[1], alignment >> 2)):
        half = f32(fmul(held, 0.5))
        gap, end = fsub(stored_half(node, side), half), fadd(fadd(half, start), half)
        for _ in range(step if step < 3 else 0):
            start, end = fadd(start, gap), fadd(gap, end)
        ends.append((start, end))
    (left, right), (top, bottom) = ends
    return left, top, right, bottom


def authored_size(node, facts) -> tuple[float, float]:
    return tuple(f32(side) for side in node.render_properties[facts["size"]])


def world_matrix(node, facts) -> np.ndarray:
    world = np.array(node.world_matrix, dtype=np.float32)
    if "cell_scale" in facts:
        x, y = (f32(value) for value in node.render_properties[facts["cell_scale"]])
        world = np.array([[fmul(scale, value) for value in row]
                          for scale, row in zip((x, y, 1.0, 1.0), world.tolist())], dtype=np.float32)
    return world


def circle_points(node, size, *, hit_test: bool = False):
    left, top, _right, _bottom = bounds(node, size)
    properties = node.render_properties
    arc_min, arc_max = sorted(min(f32(360.0), max(f32(0.0), f32(value))) for value in properties["ArcAngle"])
    arc_start, subdivisions = f32(properties["ArcStart"]), int(properties["Subdiv"])
    if not 0 < subdivisions <= CUT // 4:
        return None
    step = fdiv(360.0, float(subdivisions))
    first_step = fsub(step, f32(math.fmod(arc_min, step)))
    count = subdivisions - math.trunc(fdiv(fsub(360.0, arc_max), step)) - math.trunc(fdiv(arc_min, step))
    if count < 0:
        return None
    angles, current, increment = [], fadd(arc_min, arc_start), first_step
    for index in range(count + 1):
        angles.append(fadd(arc_max, arc_start) if index == count else current)
        current, increment = fadd(current, increment), step
    ratio = f32(properties["InnerRatio"])
    radius_x, radius_y = fmul(size[0], 0.5), fmul(size[1], 0.5)
    center = fadd(left, radius_x), fadd(top, radius_y)
    if hit_test:
        radius_x, radius_y = (stored_half(node, size[axis])
                              for axis in node.runtime.runtime_type(node.object.type_name).facts["hit_radii"])
    cos, sin = crt("cosf"), crt("sinf")

    def point(angle: float, scale: float) -> tuple[float, float]:
        value = fmul(fmul(angle, node.runtime.degree_factors[0]), node.runtime.degree_factors[1])
        if hit_test:
            across, down = fmul(scale, fmul(radius_x, cos(value))), fmul(scale, fmul(radius_y, sin(value)))
        else:
            across, down = fmul(fmul(scale, radius_x), cos(value)), fmul(fmul(scale, radius_y), sin(value))
        return fadd(center[0], across), fadd(center[1], down)
    return [point(angle, 1.0) for angle in angles], [point(angle, ratio) for angle in angles]


def hit_polygon(node, size) -> list[tuple[float, float]]:
    left, top, right, bottom = bounds(node, size)
    shape = node.runtime.runtime_type(node.object.type_name).facts["polygons"][
        node.runtime.enum_label(node.render_properties, "Shape", "HitAreaShape")]
    radius_x, radius_y = stored_half(node, size[0]), stored_half(node, size[1])
    center_x, center_y = fmul(fadd(left, right), 0.5), fmul(fadd(top, bottom), 0.5)
    cosine, sine = crt("cosf"), crt("sinf")
    angles = [fmul(fadd(float(index), shape["offset"]), shape["step"]) for index in range(shape["sides"])]
    return [(fadd(fmul(radius_x, cosine(angle)), center_x), fadd(fmul(radius_y, sine(angle)), center_y))
            for angle in angles]


def mixed_color(node, name: str, assets) -> tuple[int, int, int, int]:
    properties, rule = node.render_properties, node.runtime.color_presets
    return properties[name] if rule is None else preset_color(properties, name, rule["fields"], assets.color_presets)


def vertex_srgb(node):
    runtime, view = node.runtime, node
    while view.parent is not None:
        view = view.parent
    return runtime.srgb_to_linear if runtime.vertex_degamma is None or view.render_properties[
        runtime.vertex_degamma] else None


def _colors(node, names, assets) -> list[tuple[int, int, int, int]]:
    srgb = vertex_srgb(node)
    return [vertex_color(mixed_color(node, name, assets), node.color_scale, node.color_offset, srgb) for name in names]


def _hidden(node, facts, assets) -> bool:
    return "hidden_alpha" in facts and not any(mixed_color(node, name, assets)[3] for name in facts["hidden_alpha"][
        node.runtime.enum_label(node.render_properties, "ColorType")])


def _corner_colors(node, facts, assets) -> list[tuple[int, int, int, int]]:
    corners = facts.get("corner_colors")
    if corners is None:
        return _colors(node, ("Color",) * 4, assets)
    return _colors(node, corners[node.runtime.enum_label(node.render_properties, "ColorType")], assets)


def _halves(corners) -> list[tuple[float, float]]:
    return [(gui_half(u), gui_half(v)) for u, v in corners]


def _uv_corners(left: float, top: float, right: float, bottom: float) -> list[tuple[float, float]]:
    return _halves(((left, top), (right, top), (left, bottom), (right, bottom)))


def corner_vertices(rect, colors, uvs, z: float = 0.0) -> list[tuple]:
    left, top, right, bottom = rect
    return [((x, y, z), color, uv) for (x, y), color, uv in
            zip(((left, top), (right, top), (left, bottom), (right, bottom)), colors, uvs)]


def pattern_source(assets, reference: str, sequence: int, pattern: int):
    if reference:
        uvs = assets.load_uvs(reference)[1]
        if 0 <= sequence < len(uvs.sequences) and 0 <= pattern < len(uvs.sequences[sequence].patterns):
            resolved = assets.resolve_uv(reference, sequence, pattern)
            return resolved.texture_reference, resolved.uv_bounds
    return None, (0.0, 0.0, 1.0, 1.0)


def runtime_texture(reference: str) -> bool:
    return reference.replace("\\", "/").lstrip("@/").casefold().endswith(".rtex")


def direct_texture_gap(node) -> str | None:
    properties = node.render_properties
    if node.object.type_name != "via.gui.Texture" or node.runtime.enum_label(
            properties, "AssetType", "TextureAssetType") != "Texture":
        return None
    return "its texture is one the game renders while it runs" if runtime_texture(str(properties["Texture"])) else None


def texture_source(node, assets):
    properties = node.render_properties
    if node.runtime.enum_label(properties, "AssetType", "TextureAssetType") == "Texture":
        reference = str(properties["Texture"])
        if not reference:
            return None, (0.0, 0.0, 1.0, 1.0)
        header = assets.load_tex(reference)[1].header
        u0, v0, u1, v1 = (f32(properties[name]) for name in ("U0", "V0", "U1", "V1"))
        width, height = float(header.width), float(header.height)
        return reference, (fdiv(u0, width), fdiv(v0, height), fdiv(u1, width), fdiv(v1, height))
    return pattern_source(assets, str(properties["UVSequence"]), int(properties["UVSequenceNo"]),
                          int(properties["UVPatternNo"]))


def _spans(start: float, end: float, step: float, first: float, last: float) -> list[tuple]:
    if step <= 0.0:
        return [(start, end, first, last)]
    spans = []
    while start < end:
        stop = min(fadd(start, step), end)
        spans.append((start, stop, first, fadd(first, fmul(fsub(last, first), fdiv(fsub(stop, start), step)))))
        start = stop
    return spans


def texture_corners(node, facts, assets) -> tuple[str | None, list[tuple[float, float]], bool]:
    properties, runtime, whole = node.render_properties, node.runtime, [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)]
    if runtime.enum_label(properties, "AssetType", "TextureAssetType") != "Texture":
        reference, sequence, pattern = (str(properties["UVSequence"]), int(properties["UVSequenceNo"]),
                                        int(properties["UVPatternNo"]))
        if pattern_source(assets, reference, sequence, pattern)[0] is None:
            return None, whole, False
        resolved = assets.resolve_uv(reference, sequence, pattern)
        return (resolved.texture_reference, pattern_floats(resolved, runtime.uv_orientations),
                bool(resolved.pattern_flags & 1))
    reference = str(properties["Texture"])
    if not reference:
        return None, whole, False
    header, rule = assets.load_tex(reference)[1].header, facts["uv_types"][str(properties["UVType"])]
    first = [f32(properties[name]) for name in rule["origin"]]
    last = [f32(properties[name]) for name in rule["end"]] if "end" in rule else [
        fadd(start, f32(properties[name])) for start, name in zip(first, rule["extent"])]
    if rule["texels"]:
        first, last = ([fdiv(value, float(side)) for value, side in zip(pair, (header.width, header.height))]
                       for pair in (first, last))
    reverse = runtime.enum_value(properties, facts["uv_reverse"], "UVReverseType")
    (u0, u1), (v0, v1) = ((last[0], first[0]) if reverse & 1 else (first[0], last[0]),
                          (last[1], first[1]) if reverse & 2 else (first[1], last[1]))
    return reference, [(u0, v0), (u1, v0), (u0, v1), (u1, v1)], False


def _tile_spans(start: float, end: float, size: float, tile: float) -> list[tuple[float, float, float | None]]:
    tile = size if abs(f32(tile)) <= EPSILON else f32(tile)
    step, sign = max(f32(1.0), abs(tile)), -1.0 if size < 0.0 else 1.0
    share, spans = fdiv(abs(size), step), []
    for _ in range(int(share) + (fsub(share, float(int(share))) > EPSILON)):
        stop = fadd(fmul(sign, step), start)
        clipped = end < stop if size > 0.0 else stop < end
        spans.append((start, end if clipped else stop, fdiv(abs(fsub(end, start)), step) if clipped else None))
        start = fadd(start, fmul(sign, step))
    return spans


def _repeat_spans(start: float, end: float, tile: float) -> list[tuple[float, float, float | None]]:
    span = fsub(end, start) if f32(tile) <= EPSILON else f32(tile)
    share, step, spans = fdiv(abs(fsub(end, start)), span), max(f32(1.0), abs(span)), []
    for _ in range(0 if math.isnan(share) else int(share) + (fsub(share, float(int(share))) > EPSILON)):
        stop = fadd(start, step)
        spans.append((start, end, fdiv(fsub(end, start), step)) if end < stop else (start, stop, None))
        start = stop
    return spans


def _lerp(first: float, last: float, share: float) -> float:
    return fadd(fmul(fsub(last, first), share), first)


def _tile_color(first, last, share: float) -> tuple[int, ...]:
    return tuple(int(min(255.0, max(0.0, fmul(_lerp(fdiv(float(a), 255.0), fdiv(float(b), 255.0), share), 255.0))))
                 for a, b in zip(first, last))


def _tile_uvs(corners, row: float | None, column: float | None, rotated: bool) -> list[list[float]]:
    uvs, along, across = [list(corner) for corner in corners], int(not rotated), int(rotated)
    if row is not None:
        uvs[2][along], uvs[3][along] = (_lerp(corners[0][along], corners[2][along], row),
                                        _lerp(corners[1][along], corners[3][along], row))
    if column is not None:
        uvs[1][across], uvs[3][across] = (_lerp(corners[0][across], corners[1][across], column),
                                          _lerp(corners[2][across], corners[3][across], column))
    return uvs


def _tiled_quads(node, facts, rect, colors, corners, rotated: bool) -> list[list[tuple]]:
    left, top, right, bottom = rect
    width, height = (fadd(half, half) for half in (stored_half(node, side) for side in authored_size(node, facts)))
    kind, tiling = node.runtime.enum_label(node.render_properties, "ColorType"), node.render_properties["TilingSize"]
    quads = []
    for y0, y1, row in _tile_spans(top, bottom, height, tiling[1]):
        for x0, x1, column in _tile_spans(left, right, width, tiling[0]):
            tile = colors
            if kind == "Vertical":
                upper, lower = (_tile_color(colors[0], colors[2], fdiv(fsub(y, top), fsub(bottom, top)))
                                for y in (y0, y1))
                tile = [upper, upper, lower, lower]
            elif kind == "Horizontal":
                first, last = (_tile_color(colors[0], colors[1], fdiv(fsub(x, left), fsub(right, left)))
                               for x in (x0, x1))
                tile = [first, last, first, last]
            quads.append(corner_vertices((x0, y0, x1, y1), tile, _halves(_tile_uvs(corners, row, column, rotated))))
    return quads


def _texture_quads(node, facts, assets) -> list[list[tuple]]:
    if "uv_types" in facts:
        if _hidden(node, facts, assets):
            return []
        rect, colors = bounds(node, authored_size(node, facts)), _corner_colors(node, facts, assets)
        _reference, corners, rotated = texture_corners(node, facts, assets)
        return (_tiled_quads(node, facts, rect, colors, corners, rotated) if any(node.render_properties["TilingSize"])
                else [corner_vertices(rect, colors, _halves(corners))])
    left, top, right, bottom = bounds(node, authored_size(node, facts))
    u0, v0, u1, v1 = texture_source(node, assets)[1]
    tile_x, tile_y = (f32(side) for side in node.render_properties["TilingSize"])
    colors = _corner_colors(node, facts, assets)
    return [corner_vertices((x0, y0, x1, y1), colors, _uv_corners(ua, va, ub, vb))
            for y0, y1, va, vb in _spans(top, bottom, tile_y, v0, v1)
            for x0, x1, ua, ub in _spans(left, right, tile_x, u0, u1)]


def nine_cells(node, size) -> list[tuple[float, float, float, float]]:
    left, top, right, bottom = bounds(node, size)
    first, upper, last, lower = node.object.special_data.borders
    first, last = min(first, (right - left) * 0.5), min(last, (right - left) * 0.5)
    upper, lower = min(upper, (bottom - top) * 0.5), min(lower, (bottom - top) * 0.5)
    xs = (left, fadd(left, first), fsub(right, last), right)
    ys = (top, fadd(top, upper), fsub(bottom, lower), bottom)
    return [(xs[column], ys[row], xs[column + 1], ys[row + 1]) for row in range(3) for column in range(3)]


def _texture_passes(node, assets, placed, swap: int = 0) -> list[tuple[str, list[list[tuple]]]]:
    reference = str(node.render_properties["UVSequence"])
    if not reference:
        return []
    colors, uvs = _colors(node, ("Color",) * 4, assets), assets.load_uvs(reference)[1]
    quads = [(resolved.texture_reference, corner_vertices((x0, y0, x1, y1), colors, _halves(_tile_uvs(
        pattern_floats(resolved, node.runtime.uv_orientations), row, column, bool(resolved.pattern_flags & swap)))))
        for (left, top, right, bottom), sequence, pattern, repeat in placed
        if 0 <= sequence < len(uvs.sequences) and 0 <= pattern < len(uvs.sequences[sequence].patterns)
        for resolved in (assets.resolve_uv(reference, sequence, pattern),)
        for (y0, y1, row), (x0, x1, column) in (product(_repeat_spans(top, bottom, repeat[1]), _repeat_spans(
            left, right, repeat[0])) if any(repeat) else [((top, bottom, None), (left, right, None))])]
    passes = [(texture.path, [quad for path, quad in quads if path == texture.path]) for texture in uvs.textures]
    return [(path, quads) for path, quads in passes if quads]


def _nine_passes(node, facts, assets, tiled: bool = True) -> list[tuple[str, list[list[tuple]]]]:
    return _texture_passes(node, assets, [
        (rect, cell.sequence, cell.pattern, cell.repeat_size if tiled else (0.0, 0.0)) for rect, cell in zip(
            nine_cells(node, authored_size(node, facts)), node.object.special_data.cells[:9])],
        facts["repeat_axis_flag"] if tiled else 0)


def pattern_floats(resolved, orientations) -> list[tuple[float, float]]:
    left, top, right, bottom = resolved.uv_bounds
    sides = {"L": left, "R": right, "T": top, "B": bottom}
    return [(sides[u], sides[v]) for u, v in orientations[resolved.pattern_flags & 7]]


def pattern_corners(resolved, orientations) -> list[tuple[float, float]]:
    return _halves(pattern_floats(resolved, orientations))


def blur_quads(node, facts, assets) -> list[tuple[str | None, list[list[tuple]]]]:
    if not isinstance(node.object.special_data, GuiNineSlice):
        return []
    if not str(node.render_properties["UVSequence"]):
        colors = _colors(node, ("Color",) * 4, assets)
        return [(None, [corner_vertices(rect, colors, [(0.0, 0.0)] * 4)
                        for rect in nine_cells(node, authored_size(node, facts))])]
    return _nine_passes(node, facts, assets, False)


def material_quads(node, facts, assets) -> list[list[tuple]]:
    colors = _colors(node, ("Color",) * 4, assets)
    if not colors[0][3]:
        return []
    first, second = (_uv_corners(*rect) for rect in facts["material_uv_rects"])
    return [[(*vertex, uv) for vertex, uv in zip(
        corner_vertices(bounds(node, authored_size(node, facts)), colors, first), second)]]


def _set_passes(node, _facts, assets) -> list[tuple[str, list[list[tuple]]]]:
    return _texture_passes(node, assets, [(entry.bounds, entry.sequence, entry.pattern, (0.0, 0.0))
                                          for entry in node.object.special_data.entries])


def _circle_quads(node, facts, assets) -> list[list[tuple]]:
    properties = node.render_properties
    points = circle_points(node, authored_size(node, facts))
    if points is None:
        return []
    outer_color, inner_color = _colors(
        node, facts["ring_colors"][node.runtime.enum_label(properties, "ColorType", "CircleColorType")], assets)
    reference = str(properties["UVSequence"])
    left, top, right, _bottom = (pattern_source(assets, reference, int(properties["UVSequenceNo"]),
                                                int(properties["UVPatternNo"]))[1] if reference else (0.0,) * 4)
    outer_uv, inner_uv = (gui_half(left), gui_half(top)), (gui_half(right), gui_half(top))
    outer, inner = points
    return [[((*outer[at], 0.0), outer_color, outer_uv), ((*inner[at], 0.0), inner_color, inner_uv),
             ((*outer[at + 1], 0.0), outer_color, outer_uv), ((*inner[at + 1], 0.0), inner_color, inner_uv)]
            for at in range(len(outer) - 1)]


def _rect_quads(node, facts, assets) -> list[list[tuple]]:
    return [] if _hidden(node, facts, assets) else [corner_vertices(
        bounds(node, authored_size(node, facts)), _corner_colors(node, facts, assets), [(0.0, 0.0)] * 4)]


def _special_quads(rule, special_type):
    def quads(node, facts, assets) -> list[list[tuple]]:
        return rule(node, facts, assets) if isinstance(node.object.special_data, special_type) else []
    return quads


PASSES = {"via.gui.Scale9Grid": _special_quads(_nine_passes, GuiNineSlice),
          "via.gui.TextureSet": _special_quads(_set_passes, GuiTextureSet)}
RULES = {"via.gui.Rect": _rect_quads, "via.gui.Texture": _texture_quads, "via.gui.Circle": _circle_quads, **{
    kind: lambda node, facts, assets, rule=rule: [quad for _path, quads in rule(node, facts, assets) for quad in quads]
    for kind, rule in PASSES.items()}}


def strip_vertices(quads, layout: np.dtype = VERTEX) -> tuple[np.ndarray, np.ndarray]:
    vertices = np.array([vertex for quad in quads for vertex in quad], dtype=layout)
    indices = np.array([index for at in range(len(quads)) for index in (*range(at * 4, at * 4 + 4), CUT)],
                       dtype=np.uint16)
    return vertices, indices


def element_vertices(node, facts, assets) -> tuple[np.ndarray, np.ndarray]:
    quads = RULES[node.object.type_name](node, facts, assets)
    if "shared_ring" not in facts or not quads:
        return strip_vertices(quads)
    return (np.array([*quads[0], *(vertex for quad in quads[1:] for vertex in quad[2:])], dtype=VERTEX),
            np.array([index for at in range(len(quads)) for index in (*range(2 * at, 2 * at + 4), CUT)], np.uint16))
