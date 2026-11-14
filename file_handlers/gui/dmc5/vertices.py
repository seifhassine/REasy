"""See docs/internals/gui-rendering.md, Vertices."""
from __future__ import annotations

import math
import struct
from functools import lru_cache

import numpy as np

from file_handlers.native_math import f32, fadd, fdiv, fmul, fsub
lazy from file_handlers.engine_math import crt
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


def color_channel(value: int, scale: float, offset: float) -> int:
    transformed = f32(f32(f32(f32(float(value)) * f32(scale)) + f32(offset)) + f32(0.5))
    return 0 if math.isnan(transformed) else int(min(255.0, max(0.0, transformed)))


def vertex_color(rgba, scale, offset, srgb) -> tuple[int, int, int, int]:
    return (*(color_channel(linear8(int(rgba[at]), srgb), scale[at], offset[at]) for at in range(3)),
            color_channel(int(rgba[3]), scale[3], 0.0))


def bounds(node, size) -> tuple[float, float, float, float]:
    control = node.runtime.enum_value(node.render_properties, "ControlPoint")

    def axis(side: float, alignment: int) -> tuple[float, float]:
        half = gui_half(fmul(side, 0.5))
        return ((0.0, half + half), (-half, half), (-(half + half), 0.0))[min(alignment, 2)]

    (left, right), (top, bottom) = axis(size[0], control & 3), axis(size[1], control >> 2)
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
        radius_x, radius_y = gui_half(radius_y), gui_half(radius_x)
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
    radius_x, radius_y = gui_half(fmul(size[0], 0.5)), gui_half(fmul(size[1], 0.5))
    center_x, center_y = fmul(fadd(left, right), 0.5), fmul(fadd(top, bottom), 0.5)
    cosine, sine = crt("cosf"), crt("sinf")
    angles = [fmul(fadd(float(index), shape["offset"]), shape["step"]) for index in range(shape["sides"])]
    return [(fadd(fmul(radius_x, cosine(angle)), center_x), fadd(fmul(radius_y, sine(angle)), center_y))
            for angle in angles]


def _colors(node, names) -> list[tuple[int, int, int, int]]:
    properties = node.render_properties
    return [vertex_color(properties[name], node.color_scale, node.color_offset, node.runtime.srgb_to_linear)
            for name in names]


def _corner_colors(node, facts) -> list[tuple[int, int, int, int]]:
    corners = facts.get("corner_colors")
    if corners is None:
        return _colors(node, ("Color",) * 4)
    return _colors(node, corners[node.runtime.enum_label(node.render_properties, "ColorType")])


def _uv_corners(left: float, top: float, right: float, bottom: float) -> list[tuple[float, float]]:
    return [(gui_half(u), gui_half(v)) for u, v in ((left, top), (right, top), (left, bottom), (right, bottom))]


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


def _texture_quads(node, facts, assets) -> list[list[tuple]]:
    left, top, right, bottom = bounds(node, authored_size(node, facts))
    u0, v0, u1, v1 = texture_source(node, assets)[1]
    tile_x, tile_y = (f32(side) for side in node.render_properties["TilingSize"])
    colors = _corner_colors(node, facts)
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


def _nine_quads(node, facts, assets) -> list[list[tuple]]:
    colors, reference = _colors(node, ("Color",) * 4), str(node.render_properties["UVSequence"])
    return [corner_vertices(rect, colors, _uv_corners(*pattern_source(assets, reference, cell.sequence,
                                                                      cell.pattern)[1]))
            for rect, cell in zip(nine_cells(node, authored_size(node, facts)), node.object.special_data.cells[:9])]


def pattern_corners(resolved, orientations) -> list[tuple[float, float]]:
    left, top, right, bottom = resolved.uv_bounds
    sides = {"L": left, "R": right, "T": top, "B": bottom}
    return [(gui_half(sides[u]), gui_half(sides[v])) for u, v in orientations[resolved.pattern_flags & 7]]


def blur_quads(node, facts, assets, orientations) -> list[tuple[str | None, list[list[tuple]]]]:
    if not isinstance(node.object.special_data, GuiNineSlice):
        return []
    colors, reference = _colors(node, ("Color",) * 4), str(node.render_properties["UVSequence"])
    cells = nine_cells(node, authored_size(node, facts))
    if not reference:
        return [(None, [corner_vertices(rect, colors, [(0.0, 0.0)] * 4) for rect in cells])]
    uvs = assets.load_uvs(reference)[1]
    placed = [(rect, assets.resolve_uv(reference, cell.sequence, cell.pattern))
              for rect, cell in zip(cells, node.object.special_data.cells[:9])
              if 0 <= cell.sequence < len(uvs.sequences)
              and 0 <= cell.pattern < len(uvs.sequences[cell.sequence].patterns)]
    groups = [(texture.path, [corner_vertices(rect, colors, pattern_corners(resolved, orientations))
                              for rect, resolved in placed if resolved.texture_reference == texture.path])
              for texture in uvs.textures]
    return [(path, quads) for path, quads in groups if quads]


def material_quads(node, facts) -> list[list[tuple]]:
    colors = _colors(node, ("Color",) * 4)
    if not colors[0][3]:
        return []
    first, second = (_uv_corners(*rect) for rect in facts["material_uv_rects"])
    return [[(*vertex, uv) for vertex, uv in zip(
        corner_vertices(bounds(node, authored_size(node, facts)), colors, first), second)]]


def _set_quads(node, assets) -> list[list[tuple]]:
    colors, reference = _colors(node, ("Color",) * 4), str(node.render_properties["UVSequence"])
    return [corner_vertices(entry.bounds, colors, _uv_corners(*pattern_source(assets, reference, entry.sequence,
                                                                     entry.pattern)[1]))
            for entry in node.object.special_data.entries]


def _circle_quads(node, facts, assets) -> list[list[tuple]]:
    properties = node.render_properties
    points = circle_points(node, authored_size(node, facts))
    if points is None:
        return []
    outer_color, inner_color = _colors(
        node, facts["ring_colors"][node.runtime.enum_label(properties, "ColorType", "CircleColorType")])
    reference = str(properties["UVSequence"])
    left, top, right, _bottom = (pattern_source(assets, reference, int(properties["UVSequenceNo"]),
                                                int(properties["UVPatternNo"]))[1] if reference else (0.0,) * 4)
    outer_uv, inner_uv = (gui_half(left), gui_half(top)), (gui_half(right), gui_half(top))
    outer, inner = points
    return [[((*outer[at], 0.0), outer_color, outer_uv), ((*inner[at], 0.0), inner_color, inner_uv),
             ((*outer[at + 1], 0.0), outer_color, outer_uv), ((*inner[at + 1], 0.0), inner_color, inner_uv)]
            for at in range(len(outer) - 1)]


def _rect_quads(node, facts, _assets) -> list[list[tuple]]:
    return [corner_vertices(bounds(node, authored_size(node, facts)), _corner_colors(node, facts), [(0.0, 0.0)] * 4)]


def _special_quads(rule, special_type):
    def quads(node, facts, assets) -> list[list[tuple]]:
        return rule(node, facts, assets) if isinstance(node.object.special_data, special_type) else []
    return quads


RULES = {"via.gui.Rect": _rect_quads, "via.gui.Texture": _texture_quads, "via.gui.Circle": _circle_quads,
         "via.gui.Scale9Grid": _special_quads(_nine_quads, GuiNineSlice),
         "via.gui.TextureSet": _special_quads(lambda node, _facts, assets: _set_quads(node, assets), GuiTextureSet)}


def strip_vertices(quads, layout: np.dtype = VERTEX) -> tuple[np.ndarray, np.ndarray]:
    vertices = np.array([vertex for quad in quads for vertex in quad], dtype=layout)
    indices = np.array([index for at in range(len(quads)) for index in (*range(at * 4, at * 4 + 4), CUT)],
                       dtype=np.uint16)
    return vertices, indices


def element_vertices(node, facts, assets) -> tuple[np.ndarray, np.ndarray]:
    return strip_vertices(RULES[node.object.type_name](node, facts, assets))
