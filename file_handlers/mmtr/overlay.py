"""See docs/internals/overlay.md."""
from __future__ import annotations

from collections.abc import Sequence
from functools import lru_cache

import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.sdf.hlsl import compile_hlsl_source
from file_handlers.tex.dxgi import (
    DXGI_FORMAT_R32G32B32_FLOAT, DXGI_FORMAT_R32_UINT, DXGI_FORMAT_R8G8B8A8_TYPELESS, DXGI_FORMAT_R8_UNORM,
)

lazy from .engine_buffers import Camera
from .material_preview import (
    RANGE_CBV, _NO_DEPTH_STENCIL, _RASTERIZER, RANGE_SRV, _TRIANGLE_LIST, RANGE_UAV, _UNBLENDED, MaterialPreviewError,
)
lazy from .resident import BUFFER, RENDER_TARGET, TEXTURE_2D, UNORDERED_ACCESS, Resident

# Positions in 1/256 pixel.
GRID = 256
_WIDEST = 16
_KEPT = 8192 - _WIDEST // 2
_LARGEST = 2.0 ** 64
# fastmatpreview refuses a draw that binds neither a render target nor depth.
_UNWRITTEN = (*_UNBLENDED[:-1], 0)
_GROUP = 8
# Kind 0 marks an unused slot.
_TRIANGLE, _LINE, _POINT, _LABEL = range(1, 5)
# ``mark`` is the mark number + 1, 0 for none.
_CORNER = np.dtype([("position", "<f4", 3), ("mark", "<u4")])
_ELEMENTS = (("REASY_STREAM", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0, 0, 0),
             ("REASY_STREAM", 1, DXGI_FORMAT_R32_UINT, 0, 12, 0, 0))
_MARK = np.dtype([("kind", "<u4"), ("colour", "<u4"), ("tested", "<u4"), ("size", "<u4"), ("rectangle", "<i4", 4),
                  ("margin", "<i4"), ("pad", "<u4", 3)])
_VIEW = """cbuffer View : register(b0)
{
    // The camera's view and projection: clip space's coordinates of (x, y, z) are ((x rows[0] + y rows[1]) +
    // z rows[2]) + rows[3].
    float4 rows[4];
    // The planes' factors: (k_x + 1, k_x - 1, k_y + 1, k_y - 1).
    float4 reach;
    // 128 times the image's width and height, and its width and height.
    float2 half_size;
    int2 size;
};
"""
_CORNER_STRUCT = """
struct Corner
{
    float4 clip : CLIP;
    uint mark : MARK;
};
"""
_VERTEX = _VIEW + _CORNER_STRUCT + """
Corner main(float3 position : REASY_STREAM0, uint mark : REASY_STREAM1)
{
    Corner corner;
    precise float4 clip = ((position.x * rows[0] + position.y * rows[1]) + position.z * rows[2]) + rows[3];
    corner.clip = clip;
    corner.mark = mark;
    return corner;
}
"""
# Each geometry shader declares the ``Shape`` it emits before these.
RULES = _VIEW + """
static const float LARGEST = 18446744073709551616.0f;

float reciprocal(float b)
{
    precise float m = abs(b);
    precise float r = asfloat(0x7ef311c3u - asuint(m));
    r = r * (2 - m * r);
    r = r * (2 - m * r);
    r = r * (2 - m * r);
    return b < 0 ? -r : r;
}

float divided(float a, float b)
{
    precise float r = reciprocal(b);
    precise float q = a * r;
    precise float result = q + r * (a - b * q);
    return result;
}

bool usable(float4 c)
{
    return all(abs(c) < LARGEST);
}

float plane_distance(float4 c, uint plane)
{
    precise float d;
    switch (plane) {
    case 0: d = c.z; break;
    case 1: d = c.w - c.z; break;
    case 2: d = c.x + reach.x * c.w; break;
    case 3: d = reach.y * c.w - c.x; break;
    case 4: d = reach.z * c.w - c.y; break;
    default: d = c.y + reach.w * c.w; break;
    }
    return d;
}

bool kept(float4 c)
{
    [unroll] for (uint plane = 0; plane < 6; ++plane)
        if (!(plane_distance(c, plane) >= 0))
            return false;
    return true;
}

// Where a point of clip space lies on the window's grid, and its depth; false where its w is not above 0.
bool window(float4 c, out int2 grid, out float depth)
{
    grid = 0;
    depth = 0;
    if (!(c.w > 0))
        return false;
    precise float x = divided(c.x, c.w), y = divided(c.y, c.w);
    precise float gx = (x + 1) * half_size.x, gy = (1 - y) * half_size.y;
    grid = int2(round(gx), round(gy));
    depth = divided(c.z, c.w);
    return true;
}

// The line between a and b cut to the kept space, false where none of it is kept.
bool clip_line(inout float4 a, inout float4 b)
{
    precise float t0 = 0, t1 = 1;
    [unroll] for (uint plane = 0; plane < 6; ++plane) {
        precise float da = plane_distance(a, plane), db = plane_distance(b, plane);
        if (da < 0 && db < 0)
            return false;
        if (da < 0) {
            precise float t = divided(da, da - db);
            t0 = t > t0 ? t : t0;
        } else if (db < 0) {
            precise float t = divided(da, da - db);
            t1 = t < t1 ? t : t1;
        }
    }
    if (t0 > t1)
        return false;
    precise float4 span = b - a;
    precise float4 start = a + t0 * span, end = a + t1 * span;
    a = t0 > 0 ? start : a;
    b = t1 < 1 ? end : b;
    return true;
}

float4 cut(float4 inside, float4 beyond, float a, float b)
{
    precise float t = divided(a, a - b);
    precise float4 crossed = inside + t * (beyond - inside);
    return crossed;
}

// The polygon of corners[0..count) cut to the kept space; its corners left.
uint clip_polygon(inout float4 corners[9], uint count)
{
    [loop] for (uint plane = 0; plane < 6 && count > 0; ++plane) {
        float4 left[9] = (float4[9])0;
        uint kept_count = 0;
        [loop] for (uint i = 0; i < count; ++i) {
            float4 corner = corners[i], before = corners[(i + count - 1) % count];
            precise float d = plane_distance(corner, plane), d_before = plane_distance(before, plane);
            if (d >= 0) {
                if (d_before < 0)
                    left[kept_count++] = cut(corner, before, d, d_before);
                left[kept_count++] = corner;
            } else if (d_before >= 0) {
                left[kept_count++] = cut(before, corner, d_before, d);
            }
        }
        [loop] for (uint j = 0; j < kept_count; ++j)
            corners[j] = left[j];
        count = kept_count;
    }
    return count;
}

// The depth plane (z0, a, b) of the triangle a, b, c of depths za, zb, zc.
float3 plane_of(int2 a, int2 b, int2 c, float za, float zb, float zc)
{
    precise float x1 = float(b.x - a.x) * (1.0f / 256), y1 = float(b.y - a.y) * (1.0f / 256);
    precise float x2 = float(c.x - a.x) * (1.0f / 256), y2 = float(c.y - a.y) * (1.0f / 256);
    precise float area = x1 * y2 - x2 * y1;
    if (area == 0)
        return float3(za, 0, 0);
    precise float z1 = zb - za, z2 = zc - za;
    return float3(za, divided(z1 * y2 - z2 * y1, area), divided(x1 * z2 - x2 * z1, area));
}

// Whether the triangle a, b, c on the grid is drawn by the faces it culls (Direct3D 12's numbering: 1 none, 2 the
// front ones, 3 the back ones), a front face turning counter-clockwise on the screen when ``counter_clockwise``: by
// the sign of x1 y2 - x2 y1 (positive turning clockwise, y running down), none of area 0.
bool facing(int2 a, int2 b, int2 c, uint cull, bool counter_clockwise)
{
    precise float x1 = float(b.x - a.x) * (1.0f / 256), y1 = float(b.y - a.y) * (1.0f / 256);
    precise float x2 = float(c.x - a.x) * (1.0f / 256), y2 = float(c.y - a.y) * (1.0f / 256);
    precise float area = x1 * y2 - x2 * y1;
    if (area == 0)
        return false;
    bool front = (area > 0) != counter_clockwise;
    return cull == 1u || (cull == 2u) != front;
}

Shape corner_at(int2 grid, Shape shape)
{
    shape.position = float4(float2(grid) * (1.0f / 2097152) * float2(1, -1), 0, 1);
    return shape;
}

void emit(inout TriangleStream<Shape> stream, Shape shape, int2 a, int2 b, int2 c)
{
    stream.Append(corner_at(a, shape));
    stream.Append(corner_at(b, shape));
    stream.Append(corner_at(c, shape));
    stream.RestartStrip();
}

void emit_triangle(inout TriangleStream<Shape> stream, Shape shape, int2 a, int2 b, int2 c, float za, float zb,
                   float zc)
{
    shape.origin = float2(a) * (1.0f / 256);
    shape.plane = plane_of(a, b, c, za, zb, zc);
    emit(stream, shape, a, b, c);
}

void emit_quad(inout TriangleStream<Shape> stream, Shape shape, int2 a, int2 b, int2 c, int2 d)
{
    emit(stream, shape, a, b, c);
    emit(stream, shape, a, c, d);
}

// The line from a to b (``clip_line``'s) of whole width ``width``.
void emit_line(inout TriangleStream<Shape> stream, Shape shape, float4 a, float4 b, uint width)
{
    int2 start, end;
    float z0, z1;
    if (!clip_line(a, b) || !window(a, start, z0) || !window(b, end, z1))
        return;
    int2 span = end - start;
    if (all(span == 0))
        return;
    uint along = abs(span.x) < abs(span.y) ? 1u : 0u;
    int2 across = along == 1u ? int2(int(width) * 128, 0) : int2(0, int(width) * 128);
    precise float slope = divided(z1 - z0, float(along == 1u ? span.y : span.x) * (1.0f / 256));
    shape.origin = float2(start) * (1.0f / 256);
    shape.plane = along == 1u ? float3(z0, 0, slope) : float3(z0, slope, 0);
    emit_quad(stream, shape, start - across, end - across, end + across, start + across);
}

// The point at c of whole size ``whole``.
void emit_point(inout TriangleStream<Shape> stream, Shape shape, float4 c, uint whole)
{
    int2 at;
    float z;
    if (!kept(c) || !window(c, at, z))
        return;
    int half_width = int(whole) * 128;
    int2 low = int2(((at.x - half_width - 128) >> 8) + 1, -((128 + half_width - at.y) >> 8)) * 256;
    int2 high = low + 2 * half_width;
    shape.origin = float2(low) * (1.0f / 256);
    shape.plane = float3(z, 0, 0);
    emit_quad(stream, shape, low, int2(high.x, low.y), high, int2(low.x, high.y));
}

// The triangle a, b, c cut to the kept space, fanning from its first corner left; only the triangles of the fan
// ``facing`` passes by ``cull`` and ``counter_clockwise``.
void emit_cut_triangle(inout TriangleStream<Shape> stream, Shape shape, float4 a, float4 b, float4 c, uint cull,
                       bool counter_clockwise)
{
    float4 corners[9] = (float4[9])0;
    corners[0] = a;
    corners[1] = b;
    corners[2] = c;
    // Cutting a triangle of corners all kept leaves it as it is.
    uint count = kept(a) && kept(b) && kept(c) ? 3u : clip_polygon(corners, 3);
    int2 grids[9] = (int2[9])0;
    float depths[9] = (float[9])0;
    [loop] for (uint i = 0; i < count; ++i)
        if (!window(corners[i], grids[i], depths[i]))
            return;
    [loop] for (uint j = 1; j + 1 < count; ++j)
        if (facing(grids[0], grids[j], grids[j + 1], cull, counter_clockwise))
            emit_triangle(stream, shape, grids[0], grids[j], grids[j + 1], depths[0], depths[j], depths[j + 1]);
}
"""
_SHAPE = """
struct Shape
{
    float4 position : SV_Position;
    nointerpolation float2 origin : ORIGIN;
    nointerpolation float3 plane : PLANE;
    nointerpolation uint colour : COLOUR;
    nointerpolation uint tested : TESTED;
    nointerpolation uint sampled : SAMPLED;
    nointerpolation int2 source : SOURCE;
};
"""
_MARKS_GEOMETRY = _SHAPE + RULES + _CORNER_STRUCT + """
struct Mark
{
    uint kind;
    uint colour;
    uint tested;
    uint size;
    int4 rectangle;
    int margin;
    uint3 pad;
};

StructuredBuffer<Mark> marks : register(t2);

[maxvertexcount(21)]
void main(triangle Corner corners[3], inout TriangleStream<Shape> stream)
{
    if (corners[0].mark == 0u)
        return;
    Mark mark = marks[corners[0].mark - 1u];
    float4 a = corners[0].clip, b = corners[1].clip, c = corners[2].clip;
    if (!usable(a) || !usable(b) || !usable(c))
        return;
    Shape shape = (Shape)0;
    shape.colour = mark.colour;
    shape.tested = mark.tested;
    if (mark.kind == 1u) {
        emit_cut_triangle(stream, shape, a, b, c, 1u, false);
    } else if (mark.kind == 2u) {
        emit_line(stream, shape, a, b, mark.size);
    } else if (mark.kind == 3u) {
        emit_point(stream, shape, a, mark.size);
    } else if (mark.kind == 4u) {
        int2 at;
        float z;
        if (!kept(a) || !window(a, at, z))
            return;
        int2 extent = mark.rectangle.zw;
        int2 low = (at >> 8) + int2(mark.margin, -mark.margin) - int2(0, extent.y);
        int2 first = max(low, 0), last = min(low + extent, size);
        if (any(first >= last))
            return;
        shape.sampled = 1;
        shape.source = mark.rectangle.xy - low;
        emit_quad(stream, shape, first * 256, int2(last.x, first.y) * 256, last * 256, int2(first.x, last.y) * 256);
    }
}
"""
PIXEL_RULES = """
// A float's bits as an unsigned integer of the same order, either zero the least positive.
uint ordered(uint bits)
{
    bits = bits == 0x80000000u ? 0u : bits;
    return (bits & 0x80000000u) != 0u ? ~bits : bits | 0x80000000u;
}

bool number(uint bits)
{
    return (bits & 0x7fffffffu) <= 0x7f800000u;
}

// The depth plane (z0, a, b) of origin ``origin`` at the centre of texel ``at``.
float depth_at(uint2 at, float2 origin, float3 plane)
{
    precise float2 offset = float2(at) + 0.5f - origin;
    precise float z = plane.x + (plane.y * offset.x + plane.z * offset.y);
    return z;
}

// ``colour`` (8-bit RGBA, red lowest) blended over ``below`` by its alpha, the alpha left ``below``'s.
uint blended(uint colour, uint below)
{
    uint alpha = colour >> 24, result = below & 0xff000000u;
    [unroll] for (uint shift = 0; shift < 24; shift += 8) {
        uint top = (colour >> shift) & 0xffu, bottom = (below >> shift) & 0xffu;
        result |= ((top * alpha + bottom * (255u - alpha) + 127u) / 255u) << shift;
    }
    return result;
}
"""
_PIXEL = PIXEL_RULES + """
RasterizerOrderedTexture2D<uint> image : register(u0);
Texture2D<float> depth : register(t0);
Texture2D<uint> atlas : register(t1);

void main(float4 position : SV_Position, nointerpolation float2 origin : ORIGIN, nointerpolation float3 plane : PLANE,
          nointerpolation uint colour : COLOUR, nointerpolation uint tested : TESTED,
          nointerpolation uint sampled : SAMPLED, nointerpolation int2 source : SOURCE)
{
    uint2 at = uint2(position.xy);
    if (sampled != 0u)
        colour = atlas[int2(at) + source];
    if (tested != 0u) {
        uint near = asuint(depth_at(at, origin, plane)), far = asuint(depth[at]);
        if (!number(near) || !number(far) || ordered(near) < ordered(far))
            return;
    }
    image[at] = blended(colour, image[at]);
}
"""


def packed(colour: Sequence[int]) -> int:
    red, green, blue, alpha = (int(channel) for channel in colour)
    if not all(0 <= channel <= 255 for channel in (red, green, blue, alpha)):
        raise ValueError(f"colour channels lie in [0, 255], not {colour}")
    return red | green << 8 | blue << 16 | alpha << 24


def _whole(size: float) -> int:
    whole = max(round(size), 1)
    if whole > _WIDEST:
        raise ValueError(f"lines and points are at most {_WIDEST} pixels wide, not {size}")
    return whole


def _colours(colours, count: int) -> np.ndarray:
    given = np.asarray(colours, np.int64)
    if given.ndim == 1:
        return np.full(count, packed(given), np.uint32)
    if given.shape != (count, 4) or ((given < 0) | (given > 255)).any():
        raise ValueError(f"{count} marks take one 8-bit RGBA colour or one each")
    return (given[:, 0] | given[:, 1] << 8 | given[:, 2] << 16 | given[:, 3] << 24).astype(np.uint32)


class Marks:
    """Drawn in the order added."""

    def __init__(self, atlas: np.ndarray | None = None):
        self.atlas = None if atlas is None else np.ascontiguousarray(atlas, np.uint32)
        self._corners: list[np.ndarray] = []
        self._marks: list[np.ndarray] = []

    def __len__(self) -> int:
        return sum(len(marks) for marks in self._marks)

    @property
    def corners(self) -> np.ndarray:
        return np.concatenate(self._corners) if self._corners else np.zeros((0, 3, 3), np.float32)

    @property
    def marks(self) -> np.ndarray:
        return np.concatenate(self._marks) if self._marks else np.zeros(0, _MARK)

    def _add(self, kind: int, corners: np.ndarray, colours, tested: bool, size: int = 0,
             rectangles: np.ndarray | None = None, margin: int = 0) -> None:
        corners = np.asarray(corners, np.float32).reshape(-1, 3, 3)
        marks = np.zeros(len(corners), _MARK)
        marks["kind"], marks["tested"], marks["size"] = kind, bool(tested), size
        marks["colour"] = _colours(colours, len(corners))
        if rectangles is not None:
            marks["rectangle"], marks["margin"] = rectangles, margin
        self._corners.append(corners)
        self._marks.append(marks)

    def triangles(self, corners: np.ndarray, colours, tested: bool) -> None:
        """``corners`` is n x 3 x 3."""
        self._add(_TRIANGLE, corners, colours, tested)

    def lines(self, ends: np.ndarray, width: float, colours, tested: bool) -> None:
        """``width`` in pixels."""
        ends = np.asarray(ends, np.float32).reshape(-1, 2, 3)
        self._add(_LINE, ends[:, [0, 1, 1]], colours, tested, _whole(width))

    def points(self, positions: np.ndarray, size: float, colours, tested: bool) -> None:
        """``size`` in pixels."""
        positions = np.asarray(positions, np.float32).reshape(-1, 1, 3)
        self._add(_POINT, positions[:, [0, 0, 0]], colours, tested, _whole(size))

    def labels(self, anchors: np.ndarray, rectangles: np.ndarray, margin: int) -> None:
        """``rectangles`` are (column, row, width, height) in the atlas; ``margin`` in texels."""
        if self.atlas is None:
            raise ValueError("labels need the marks' atlas")
        anchors = np.asarray(anchors, np.float32).reshape(-1, 1, 3)
        rectangles = np.asarray(rectangles, np.int64).reshape(-1, 4)
        if len(rectangles) != len(anchors) or (rectangles[:, 2:] < 0).any():
            raise ValueError("a label is an anchor and a rectangle of the atlas")
        self._add(_LABEL, anchors[:, [0, 0, 0]], (0, 0, 0, 0), False, 0, rectangles, int(margin))


def view_projection(camera: Camera) -> np.ndarray:
    """For row vectors; summed in a fixed order (BLAS may not)."""
    view, projection = camera.view_matrix().T, camera.projection()
    return sum(view[:, k, None] * projection[None, k, :] for k in range(4))


def _reach(size: Sequence[int]) -> np.ndarray:
    width, height = size
    if max(width, height) > _KEPT:
        raise ValueError(f"the image is at most {_KEPT} pixels wide and high, not {width} x {height}")
    reach_x, reach_y = 2 * _KEPT / width, 2 * _KEPT / height
    return np.array([reach_x + 1, reach_x - 1, reach_y + 1, reach_y - 1], np.float32)


def _view_bytes(camera: Camera) -> bytes:
    width, height = camera.size
    return (view_projection(camera).astype("<f4").tobytes() + _reach(camera.size).astype("<f4").tobytes()
            + np.array([width * 128, height * 128], "<f4").tobytes() + np.array([width, height], "<i4").tobytes())


# Shader float32 arithmetic in numpy: each operation rounded, denormals flushed.
_TINY = np.finfo(np.float32).tiny


def _flushed(values) -> np.ndarray:
    values = np.asarray(values, np.float32)
    return np.where(np.abs(values) < _TINY, np.copysign(np.float32(0), values), values).astype(np.float32)


def _add(a, b) -> np.ndarray:
    with np.errstate(all="ignore"):
        return _flushed(_flushed(a) + _flushed(b))


def _subtract(a, b) -> np.ndarray:
    with np.errstate(all="ignore"):
        return _flushed(_flushed(a) - _flushed(b))


def _multiply(a, b) -> np.ndarray:
    with np.errstate(all="ignore"):
        return _flushed(_flushed(a) * _flushed(b))


def _reciprocal(b) -> np.ndarray:
    b = _flushed(b)
    m = np.abs(b)
    r = (np.uint32(0x7ef311c3) - m.view(np.uint32)).astype(np.uint32).view(np.float32)
    for _ in range(3):
        r = _multiply(r, _subtract(np.float32(2), _multiply(m, r)))
    return np.where(b < 0, -r, r).astype(np.float32)


def _divided(a, b) -> np.ndarray:
    r = _reciprocal(b)
    q = _multiply(a, r)
    return _add(q, _multiply(r, _subtract(a, _multiply(b, q))))


def _clip(points: np.ndarray, rows: np.ndarray) -> np.ndarray:
    points = _flushed(points)
    rows = _flushed(rows)
    terms = [_multiply(points[..., k, None], rows[k]) for k in range(3)]
    return _add(_add(_add(terms[0], terms[1]), terms[2]), rows[3])


def _distances(clip: np.ndarray, reach: np.ndarray) -> np.ndarray:
    x, y, z, w = np.moveaxis(clip, -1, 0)
    return np.stack([z, _subtract(w, z), _add(x, _multiply(reach[0], w)), _subtract(_multiply(reach[1], w), x),
                     _subtract(_multiply(reach[2], w), y), _add(y, _multiply(reach[3], w))], axis=-1)


def _usable(clip: np.ndarray) -> np.ndarray:
    with np.errstate(invalid="ignore"):
        return (np.abs(clip) < _LARGEST).all(axis=-1)


def _window(clip: np.ndarray, size: Sequence[int]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    x, y, z, w = np.moveaxis(clip, -1, 0)
    width, height = size
    with np.errstate(invalid="ignore"):
        placed = w > 0
    gx = _multiply(_add(_divided(x, w), np.float32(1)), np.float32(width * 128))
    gy = _multiply(_subtract(np.float32(1), _divided(y, w)), np.float32(height * 128))
    grid = np.stack([np.rint(gx), np.rint(gy)], axis=-1)
    grid = np.where(placed[..., None], grid, 0).astype(np.int64)
    return placed, grid, np.where(placed, _divided(z, w), np.float32(0)).astype(np.float32)


def project_points(points: np.ndarray, camera: Camera) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    rows = view_projection(camera).astype(np.float32)
    clip = _clip(np.asarray(points, np.float32).reshape(-1, 3), rows)
    with np.errstate(invalid="ignore"):
        inside = (_distances(clip, _reach(camera.size)) >= 0).all(axis=1)
    placed, grid, depth = _window(clip, camera.size)
    drawn = np.flatnonzero(_usable(clip) & inside & placed)
    return drawn, grid[drawn], depth[drawn]


@lru_cache
def _shaders() -> tuple[bytes, bytes, bytes]:
    return tuple(bytes(compile_hlsl_source(source, entry_point="main", target=target)) for source, target in (
        (_VERTEX, "vs_5_0"), (_MARKS_GEOMETRY, "gs_5_0"), (_PIXEL, "ps_5_0")))


@lru_cache
def _background_shader(background: int) -> bytes:
    source = f"""Texture2D<uint> shown : register(t0);
RWTexture2D<uint> image : register(u0);

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    image.GetDimensions(width, height);
    if (id.x >= width || id.y >= height)
        return;
    uint texel = shown[id.xy];
    image[id.xy] = texel >> 24 != 0u ? texel : {background | 0xff000000}u;
}}
"""
    return bytes(compile_hlsl_source(source, entry_point="main", target="cs_5_0"))


class Overlay:
    """The image is RGBA8, written as R32_UINT with red lowest."""

    def __init__(self, device: Resident, size: Sequence[int], depth: tuple[int, int]):
        if not device.scene.rasterizer_ordered():
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The graphics adapter cannot draw the viewer's overlays: they need "
                                   "rasterizer-ordered views (Direct3D feature level 12_1)."))
        self.device = device
        self._screen(size, depth)
        self.view = device.scene.buffer(bytes(96), 0)
        self._view = bytes(96)
        # Draws cover the whole capacity; slots past the marks stay zero (kind 0, unused).
        self._corners: int | None = None
        self._marks: int | None = None
        self._capacity = 0
        self._written: Marks | None = None

    def _screen(self, size: Sequence[int], depth: tuple[int, int]) -> None:
        _reach(size)
        self.size, self.depth = tuple(size), depth
        self.image = self.device.texture(DXGI_FORMAT_R8G8B8A8_TYPELESS, self.size, UNORDERED_ACCESS)
        self._unwritten = self.device.texture(DXGI_FORMAT_R8_UNORM, self.size, RENDER_TARGET)
        self._blank: int | None = None
        self._backgrounds: dict[tuple[int, int], int] = {}
        self._draws: dict[tuple, int] = {}

    def resize(self, size: Sequence[int], depth: tuple[int, int]) -> None:
        old = [self.image, self._unwritten, self._blank]
        self._screen(size, depth)
        self.device.release(old)

    def steps(self, shown: int | None, background: Sequence[int], camera: Camera, marks: Marks, scene=None,
              scene_shown=None) -> list[int]:
        if tuple(camera.size) != self.size:
            raise ValueError(f"the overlay is {self.size} pixels, the camera's {tuple(camera.size)}")
        if shown is None:
            if self._blank is None:
                self._blank = self.device.texture(DXGI_FORMAT_R32_UINT, self.size, 0,
                                                  bytes(4 * self.size[0] * self.size[1]))
            shown = self._blank
        view = _view_bytes(camera)
        if view != self._view:
            self.device.scene.write(self.view, view)
            self._view = view
        key = (shown, packed((*background, 0)))
        if key not in self._backgrounds:
            width, height = self.size
            self._backgrounds[key] = self.device.scene.dispatch({
                "compute_shader": _background_shader(key[1]),
                "tables": [(RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, shown),)),
                           (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self.image),))],
                "heap": [], "groups": (-(-width // _GROUP), -(-height // _GROUP), 1)})
        steps = [self._backgrounds[key]]
        depth = self.depth
        if scene is not None:
            steps += scene.steps(self.view, self.image, scene_shown, camera)
            if not scene_shown.filled:
                depth = scene.seen_depth
        if len(marks):
            steps.append(self._marks_step(marks, depth))
        return steps

    def _marks_step(self, marks: Marks, depth: tuple[int, int]) -> int:
        count = len(marks)
        scene, atlas = self.device.scene, marks.atlas
        if atlas is None:
            texture = self.device.zeros(DXGI_FORMAT_R32_UINT, (1, 1), 0, 4)
        else:
            texels = atlas.astype("<u4").tobytes()
            texture = self.device.read(TEXTURE_2D, (0, DXGI_FORMAT_R32_UINT, *atlas.shape[::-1], 1, 1, texels))[2]
        if count > self._capacity:
            self._capacity = max(64, 1 << (count - 1).bit_length())
            self._corners = scene.buffer(bytes(self._capacity * 3 * _CORNER.itemsize), 0)
            self._marks = scene.buffer(bytes(self._capacity * _MARK.itemsize), 0)
            self._draws = {}
            self._written = None
        if (depth, texture) not in self._draws:
            width, height = self.size
            vertex, geometry, pixel = _shaders()
            self._draws[depth, texture] = scene.draw({
                "vertex_shader": vertex, "geometry_shader": geometry, "pixel_shader": pixel,
                "vertex_buffers": [(self._corners, _CORNER.itemsize)], "elements": _ELEMENTS,
                "indices": None, "topology": _TRIANGLE_LIST, "draw": (self._capacity * 3, 1, 0, 0, 0),
                "rasterizer": _RASTERIZER, "depth_stencil": _NO_DEPTH_STENCIL, "blend": (0, 0, [_UNWRITTEN]),
                "tables": [(RANGE_CBV, 0, 0, 1, 0, 0, ((0, self.view),)),
                           (RANGE_SRV, 2, 0, 1, BUFFER, 0, ((0, 0, _MARK.itemsize, self._marks),)),
                           (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 5, ((0, DXGI_FORMAT_R32_UINT, self.image),)),
                           (RANGE_SRV, 0, 0, 1, TEXTURE_2D, 5, ((0, *depth),)),
                           (RANGE_SRV, 1, 0, 1, TEXTURE_2D, 5, ((0, DXGI_FORMAT_R32_UINT, texture),))],
                "heap": [], "size": (width, height),
                "viewport": (-8192.0, -8192.0, 16384.0, 16384.0, 0.0, 1.0), "scissor": (0, 0, width, height),
                "targets": [(DXGI_FORMAT_R8_UNORM, self._unwritten)], "depth": None})
        if marks is not self._written:
            corners = np.zeros((count, 3), _CORNER)
            corners["position"] = marks.corners
            corners["mark"] = np.arange(1, count + 1, dtype=np.uint32)[:, None]
            held = bytearray(self._capacity * 3 * _CORNER.itemsize)
            held[:corners.nbytes] = corners.tobytes()
            scene.write(self._corners, held.take_bytes())
            held = bytearray(self._capacity * _MARK.itemsize)
            held[:count * _MARK.itemsize] = marks.marks.tobytes()
            scene.write(self._marks, held.take_bytes())
            self._written = marks
        return self._draws[depth, texture]


__all__ = ["GRID", "Marks", "Overlay", "packed", "project_points", "view_projection"]
