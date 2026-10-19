"""See docs/internals/overlay.md, Scene layer."""
from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

lazy from file_handlers.sdf.hlsl import compile_hlsl_source
from file_handlers.sdf.sdf_file import CULL_BACK, CULL_FRONT, CULL_NONE
lazy from file_handlers.tex.dxgi import (
    DXGI_FORMAT_R16G16B16A16_UINT, DXGI_FORMAT_R32G32B32A32_FLOAT, DXGI_FORMAT_R32G32B32_FLOAT, DXGI_FORMAT_R32_FLOAT,
    DXGI_FORMAT_R32_TYPELESS, DXGI_FORMAT_R32_UINT, DXGI_FORMAT_R8_UNORM,
)

lazy from .material_preview import RANGE_CBV, _NO_DEPTH_STENCIL, _RASTERIZER, RANGE_SRV, _TRIANGLE_LIST, RANGE_UAV
from .overlay import _GROUP, _UNWRITTEN, _VIEW, PIXEL_RULES, RULES, packed
lazy from .resident import BUFFER, RENDER_TARGET, TEXTURE_2D, UNORDERED_ACCESS, Resident

_LINE_LIST = 2
# Packed four per vertex element.
MOST_INFLUENCES = 16
_IDENTITY = np.eye(4)[:3]
NO_EDGES, ALL_EDGES, VISIBLE_EDGES = "", "all", "visible"
_SELECTED, _HOVERED = 2, 1


@dataclass(frozen=True, slots=True, eq=False)
class OverlayPart:
    indices: np.ndarray
    cull: int = CULL_BACK
    front_counter_clockwise: bool = True


@dataclass(frozen=True, slots=True, eq=False)
class OverlayGeometry:
    """``bones`` and ``weights`` are n x k."""
    positions: np.ndarray
    parts: tuple[OverlayPart, ...]
    bones: np.ndarray | None = None
    weights: np.ndarray | None = None
    edged: bool = True


@dataclass(frozen=True, slots=True)
class SceneShown:
    """``outline`` is a width, 0 for none; ``pick`` is a texel. Without ``filled``, marks test against the seen
    triangles' depth."""
    edges: str = NO_EDGES
    edge_width: float = 1.5
    edge_colour: tuple[int, int, int, int] = (51, 255, 77, 255)
    hovered: int | None = None
    selected: frozenset[int] = frozenset()
    hover_colour: tuple[int, int, int, int] = (13, 255, 64, 97)
    selection_colour: tuple[int, int, int, int] = (255, 219, 46, 56)
    outline_colour: tuple[int, int, int, int] = (255, 219, 46, 255)
    outline: int = 2
    pick: tuple[int, int] | None = None
    filled: bool = True


def edges_of(positions: np.ndarray, triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    positions = np.ascontiguousarray(positions, np.float32).reshape(-1, 3)
    _unique, first, points = np.unique(positions.view(np.dtype((np.void, 12))).reshape(-1), return_index=True,
                                       return_inverse=True)
    points = first[points.reshape(-1)].astype(np.uint32)
    sides = points[np.asarray(triangles, np.int64).reshape(-1, 3)[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)]
    sides = np.unique(np.sort(sides, axis=1), axis=0)
    return points, sides[sides[:, 0] != sides[:, 1]].astype(np.uint32)


_CORNER = """
struct Corner
{
    float4 clip : CLIP;
    uint vertex : VERTEX;
};
"""


def _vertex_source(groups: int) -> str:
    inputs = "".join(f", uint4 bones{group} : REASY_STREAM{1 + group}, float4 weights{group} : REASY_STREAM{5 + group}"
                     for group in range(groups))
    skinned = ""
    if groups:
        skinned = "    precise float3 moved = 0;\n" + "".join(
            f"    if (weights{group}[{lane}] > 0) {{\n"
            f"        uint bone = bones{group}[{lane}] * 3u;\n"
            f"        moved = moved + moved_by(palette[bone], palette[bone + 1u], palette[bone + 2u], position)"
            f" * weights{group}[{lane}];\n    }}\n"
            for group in range(groups) for lane in range(4)) + "    at = moved;\n"
    return _VIEW + f"""
cbuffer Object : register(b1)
{{
    float4 world[3];
}};
StructuredBuffer<float4> palette : register(t3);

float3 moved_by(float4 x, float4 y, float4 z, float3 p)
{{
    precise float3 v = float3(((x.x * p.x + x.y * p.y) + x.z * p.z) + x.w, ((y.x * p.x + y.y * p.y) + y.z * p.z) + y.w,
                              ((z.x * p.x + z.y * p.y) + z.z * p.z) + z.w);
    return v;
}}

{_CORNER}
Corner main(float3 position : REASY_STREAM0{inputs}, uint vertex : SV_VertexID)
{{
    precise float3 at = position;
{skinned}    precise float3 placed = moved_by(world[0], world[1], world[2], at);
    precise float4 clip = ((placed.x * rows[0] + placed.y * rows[1]) + placed.z * rows[2]) + rows[3];
    Corner corner;
    corner.clip = clip;
    corner.vertex = vertex;
    return corner;
}}
"""


_SEEN_SHAPE = """
struct Shape
{
    float4 position : SV_Position;
    nointerpolation float2 origin : ORIGIN;
    nointerpolation float3 plane : PLANE;
    nointerpolation uint object : OBJECT;
    nointerpolation uint triangle_number : TRIANGLE;
};
"""
_SEEN_GEOMETRY = _SEEN_SHAPE + RULES + _CORNER + """
cbuffer Part : register(b2)
{
    // The object's number plus one, the part's first triangle's number, its culling and whether a front face turns
    // counter-clockwise.
    uint object;
    uint first;
    uint cull;
    uint counter_clockwise;
};

[maxvertexcount(21)]
void main(triangle Corner corners[3], uint primitive : SV_PrimitiveID, inout TriangleStream<Shape> stream)
{
    float4 a = corners[0].clip, b = corners[1].clip, c = corners[2].clip;
    if (!usable(a) || !usable(b) || !usable(c))
        return;
    Shape shape = (Shape)0;
    shape.object = object;
    shape.triangle_number = first + primitive;
    emit_cut_triangle(stream, shape, a, b, c, cull, counter_clockwise != 0u);
}
"""
_SEEN_PIXEL = PIXEL_RULES + """
RasterizerOrderedTexture2D<uint> seen_depth : register(u0);
RasterizerOrderedTexture2D<uint> seen_object : register(u1);
RasterizerOrderedTexture2D<uint> seen_triangle : register(u2);

void main(float4 position : SV_Position, nointerpolation float2 origin : ORIGIN, nointerpolation float3 plane : PLANE,
          nointerpolation uint object : OBJECT, nointerpolation uint triangle_number : TRIANGLE)
{
    uint2 at = uint2(position.xy);
    uint near = asuint(depth_at(at, origin, plane));
    if (!number(near))
        return;
    uint held = seen_object[at];
    if (held != 0u) {
        uint mine = ordered(near), theirs = ordered(seen_depth[at]);
        bool later = object > held || object == held && triangle_number >= seen_triangle[at];
        if (mine < theirs || mine == theirs && later)
            return;
    }
    seen_depth[at] = near;
    seen_object[at] = object;
    seen_triangle[at] = triangle_number;
}
"""
_MASK_PIXEL = """RWTexture2D<uint> mask : register(u0);

void main(float4 position : SV_Position)
{
    mask[uint2(position.xy)] = 1u;
}
"""
_EDGE_SHAPE = """
struct Shape
{
    float4 position : SV_Position;
    nointerpolation float2 origin : ORIGIN;
    nointerpolation float3 plane : PLANE;
    nointerpolation uint2 points : POINTS;
};
"""
_EDGES = """cbuffer Edges : register(b2)
{
    // The object's number plus one, the edges' whole width, colour (8-bit RGBA, red lowest) and whether they are
    // drawn only where seen.
    uint object;
    uint width;
    uint colour;
    uint tested;
};
"""
_EDGE_GEOMETRY = _EDGE_SHAPE + RULES + _CORNER + _EDGES + """
[maxvertexcount(6)]
void main(line Corner ends[2], inout TriangleStream<Shape> stream)
{
    if (!usable(ends[0].clip) || !usable(ends[1].clip))
        return;
    Shape shape = (Shape)0;
    shape.points = uint2(ends[0].vertex, ends[1].vertex);
    emit_line(stream, shape, ends[0].clip, ends[1].clip, width);
}
"""
_EDGE_PIXEL = PIXEL_RULES + _EDGES + """
RasterizerOrderedTexture2D<uint> image : register(u0);
Texture2D<uint> seen_depth : register(t0);
Texture2D<uint> seen_object : register(t1);
Texture2D<uint> seen_triangle : register(t2);
// Each triangle's corners' points, three a triangle.
StructuredBuffer<uint> corner_points : register(t5);

void main(float4 position : SV_Position, nointerpolation float2 origin : ORIGIN, nointerpolation float3 plane : PLANE,
          nointerpolation uint2 points : POINTS)
{
    uint2 at = uint2(position.xy);
    if (tested != 0u) {
        uint held = seen_object[at];
        bool own = false;
        if (held == object) {
            uint first = seen_triangle[at] * 3u;
            [unroll] for (uint corner = 0; corner < 3; ++corner) {
                uint seen_point = corner_points[first + corner];
                own = own || seen_point == points.x || seen_point == points.y;
            }
        }
        if (!own) {
            uint near = asuint(depth_at(at, origin, plane));
            if (!number(near) || held != 0u && ordered(near) < ordered(seen_depth[at]))
                return;
        }
    }
    image[at] = blended(colour, image[at]);
}
"""
_CLEAR = f"""RWTexture2D<uint> cleared : register(u0);

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    cleared.GetDimensions(width, height);
    if (id.x < width && id.y < height)
        cleared[id.xy] = 0u;
}}
"""
_TINT = PIXEL_RULES + f"""
Texture2D<uint> seen_object : register(t0);
StructuredBuffer<uint> flags : register(t1);
RWTexture2D<uint> image : register(u0);

cbuffer Tint : register(b0)
{{
    uint selection_colour;
    uint hover_colour;
}};

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    image.GetDimensions(width, height);
    if (id.x >= width || id.y >= height)
        return;
    uint held = seen_object[id.xy];
    if (held == 0u)
        return;
    uint flag = flags[held - 1u], texel = image[id.xy];
    if ((flag & {_SELECTED}u) != 0u)
        texel = blended(selection_colour, texel);
    if ((flag & {_HOVERED}u) != 0u)
        texel = blended(hover_colour, texel);
    image[id.xy] = texel;
}}
"""
_OUTLINE = PIXEL_RULES + f"""
Texture2D<uint> mask : register(t0);
RWTexture2D<uint> image : register(u0);

cbuffer Outline : register(b0)
{{
    uint colour;
    int reach;
}};

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    int width, height;
    image.GetDimensions(width, height);
    int2 at = int2(id.xy);
    if (at.x >= width || at.y >= height || mask[at] != 0u)
        return;
    for (int y = max(at.y - reach, 0); y <= min(at.y + reach, height - 1); ++y)
        for (int x = max(at.x - reach, 0); x <= min(at.x + reach, width - 1); ++x)
            if (mask[int2(x, y)] != 0u) {{
                image[at] = blended(colour, image[at]);
                return;
            }}
}}
"""
_PICK = """Texture2D<uint> seen_object : register(t0);
Texture2D<uint> seen_triangle : register(t1);
RWStructuredBuffer<uint2> picked : register(u0);

cbuffer Pick : register(b0)
{
    uint2 at;
};

[numthreads(1, 1, 1)]
void main()
{
    picked[0] = uint2(seen_object[at], seen_triangle[at]);
}
"""


@lru_cache
def _compiled(source: str, target: str) -> bytes:
    return bytes(compile_hlsl_source(source, entry_point="main", target=target))


def _rows(rows, count: int | None = None) -> np.ndarray:
    held = np.asarray(rows, np.float64)
    if held.shape[-2:] != (3, 4) or not np.isfinite(held).all():
        raise ValueError("a placement or a bone is three finite rows of four")
    held = held.reshape(-1, 3, 4)
    return held if count is None else held[:count]


def _influence_streams(bones, weights, count: int) -> tuple[bytes, bytes, int, int]:
    named, weights = np.asarray(bones), np.asarray(weights, "<f4")
    if named.ndim != 2 or named.shape != weights.shape or len(named) != count:
        raise ValueError("a skinned geometry has as many bones and weights for each of its vertices")
    influences = named.shape[1]
    if not 1 <= influences <= MOST_INFLUENCES or named.min() < 0 or named.max() > 0xFFFF:
        raise ValueError(f"a vertex has 1 to {MOST_INFLUENCES} influences, of bones up to 65535")
    groups = -(-influences // 4)
    padded = np.zeros((2, count, 4 * groups))
    padded[0, :, :influences], padded[1, :, :influences] = named, weights
    return padded[0].astype("<u2").tobytes(), padded[1].astype("<f4").tobytes(), groups, int(named.max()) + 1


def _identity_palette(scene, bones: int) -> int | None:
    return scene.buffer(np.tile(_IDENTITY, (bones, 1, 1)).astype("<f4").tobytes(), 0) if bones else None


def _palette_rows(item, posed: int, bones) -> bytes:
    named = item.geometry.bones
    if item.palette is None:
        raise ValueError(f"object {posed} is not skinned")
    rows = _rows(bones, named)
    if len(rows) < named:
        raise ValueError(f"object {posed} names {named} bones, and {len(rows)} are given")
    return rows.astype("<f4").tobytes()


def _rewrite(scene, buffer: list, data: bytes) -> bool:
    if data == buffer[1]:
        return False
    scene.write(buffer[0], data)
    buffer[1] = data
    return True


@dataclass(slots=True)
class _Held:
    count: int
    bones: int
    vertices: list[tuple[int, int]]
    elements: tuple[tuple, ...]
    parts: list[tuple[int, int, int, int, bool]]
    positions: np.ndarray
    triangles: np.ndarray
    edged: bool
    edges: tuple[int, int, int] | None = None


@dataclass(slots=True)
class _Object:
    number: int
    geometry: _Held
    placed: list
    palette: int | None
    parts: list[int]
    edges: list


class OverlayScene:
    def __init__(self, device: Resident, size: Sequence[int], geometries: Sequence[OverlayGeometry],
                 objects: Sequence[int] | None = None):
        self.device, self.size = device, tuple(size)
        scene = device.scene
        self._geometries = [self._held(geometry) for geometry in geometries]
        numbers = range(len(self._geometries)) if objects is None else objects
        self._objects = [self._object(index, geometry) for index, geometry in enumerate(numbers)]
        self._screen()
        self.picked = scene.buffer(bytes(8), UNORDERED_ACCESS)
        self._flags = [scene.buffer(bytes(4 * max(len(self._objects), 1)), 0), b""]
        self._tint = [scene.buffer(bytes(16), 0), b""]
        self._outline = [scene.buffer(bytes(16), 0), b""]
        self._pick = [scene.buffer(bytes(16), 0), b""]

    def _screen(self) -> None:
        made = [self.device.texture(DXGI_FORMAT_R32_TYPELESS if index == 0 else DXGI_FORMAT_R32_UINT, self.size,
                                    UNORDERED_ACCESS) for index in range(4)]
        self._seen, self._mask = made[:3], made[3]
        self.seen_depth = (DXGI_FORMAT_R32_FLOAT, self._seen[0])
        self._unwritten = self.device.texture(DXGI_FORMAT_R8_UNORM, self.size, RENDER_TARGET)
        self._view: int | None = None
        self._image: int | None = None
        self._steps: dict[tuple, int] = {}
        self._seen_for: bytes | None = None
        self._masked: tuple | None = None

    def resize(self, size: Sequence[int]) -> None:
        old = [*self._seen, self._mask, self._unwritten]
        self.size = tuple(size)
        self._screen()
        self.device.release(old)

    def _held(self, geometry: OverlayGeometry) -> _Held:
        positions = np.ascontiguousarray(geometry.positions, "<f4").reshape(-1, 3)
        count = len(positions)
        if not count:
            raise ValueError("a geometry has no vertices")
        scene = self.device.scene
        vertices = [(scene.buffer(positions.tobytes(), 0), 12)]
        elements = [("REASY_STREAM", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0, 0, 0)]
        bones = 0
        if geometry.bones is not None:
            named, weighed, groups, bones = _influence_streams(geometry.bones, geometry.weights, count)
            vertices += [(scene.buffer(named, 0), 8 * groups), (scene.buffer(weighed, 0), 16 * groups)]
            elements += [("REASY_STREAM", base + group, form, slot, group * step, 0, 0)
                         for base, form, slot, step in ((1, DXGI_FORMAT_R16G16B16A16_UINT, 1, 8),
                                                        (5, DXGI_FORMAT_R32G32B32A32_FLOAT, 2, 16))
                         for group in range(groups)]
        parts, triangles, first = [], [], 0
        for part in geometry.parts:
            indices = np.ascontiguousarray(part.indices, "<u4").reshape(-1)
            if len(indices) % 3 or (indices >= count).any():
                raise ValueError("a part's indices are not triangles of its geometry's vertices")
            if part.cull not in (CULL_NONE, CULL_FRONT, CULL_BACK):
                raise ValueError(f"a part culls none, the front or the back faces, not {part.cull}")
            parts.append((self.device.buffer(indices.tobytes()), len(indices) // 3, first, part.cull,
                          bool(part.front_counter_clockwise)))
            triangles.append(indices.reshape(-1, 3))
            first += len(indices) // 3
        every = np.concatenate(triangles) if triangles else np.zeros((0, 3), np.uint32)
        return _Held(count, bones, vertices, tuple(elements), parts, positions, every, bool(geometry.edged))

    def _edges(self, held: _Held) -> tuple[int, int, int]:
        if held.edges is None:
            points, edges = edges_of(held.positions, held.triangles) if held.edged else (None, None)
            held.edges = (0, 0, 0) if edges is None else (
                self.device.buffer(edges.astype("<u4").tobytes()), len(edges),
                self.device.buffer(points[held.triangles].astype("<u4").tobytes() or bytes(4)))
        return held.edges

    def _object(self, number: int, geometry: int) -> _Object:
        if not 0 <= geometry < len(self._geometries):
            raise ValueError(f"an object is of geometry {geometry}, which the scene has not")
        held = self._geometries[geometry]
        scene = self.device.scene
        placed = np.asarray(_IDENTITY, "<f4").tobytes()
        palette = _identity_palette(scene, held.bones)
        parts = [self.device.buffer(struct.pack("<4I", number + 1, first, cull, int(counter_clockwise)))
                 for _indices, _count, first, cull, counter_clockwise in held.parts]
        return _Object(number, held, [scene.buffer(placed, 0), placed], palette, parts,
                       [scene.buffer(bytes(16), 0), b""])

    def place(self, placed: int, world) -> None:
        """``world`` is 3 x 4."""
        if _rewrite(self.device.scene, self._objects[placed].placed, _rows(world)[0].astype("<f4").tobytes()):
            self._seen_for = self._masked = None

    def pose(self, posed: int, bones) -> None:
        """``bones`` are 3 x 4 each."""
        held = self._objects[posed]
        self.device.scene.write(held.palette, _palette_rows(held, posed, bones))
        self._seen_for = self._masked = None

    def write(self, geometry: int, positions) -> None:
        """Edges keep the original topology."""
        held = self._geometries[geometry]
        positions = np.ascontiguousarray(positions, "<f4").reshape(-1, 3)
        if len(positions) != held.count:
            raise ValueError(f"the geometry has {held.count} vertices, not {len(positions)}")
        self.device.scene.write(held.vertices[0][0], positions.tobytes())
        self._seen_for = self._masked = None

    @staticmethod
    def picked_of(data: bytes) -> tuple[int, int] | None:
        held, triangle = struct.unpack("<2I", data[:8])
        return None if held == 0 else (held - 1, triangle)

    def _made(self, key: tuple, make) -> int:
        if key not in self._steps:
            self._steps[key] = make()
        return self._steps[key]

    def _draw_job(self, item: _Object, geometry: bytes, pixel: bytes, tables: list, *, edges=False,
                  part: int = 0) -> dict:
        width, height = self.size
        held = item.geometry
        indices, count = self._edges(held)[:2] if edges else held.parts[part][:2]
        tables = [(RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._view),)), (RANGE_CBV, 1, 0, 1, 0, 1, ((0, item.placed[0]),)),
                  *tables]
        if item.palette is not None:
            tables.append((RANGE_SRV, 3, 0, 1, BUFFER, 1, ((0, 0, 16, item.palette),)))
        return {"vertex_shader": _compiled(_vertex_source((len(held.elements) - 1) // 2), "vs_5_0"),
                "geometry_shader": geometry, "pixel_shader": pixel, "vertex_buffers": held.vertices,
                "elements": held.elements, "indices": indices, "index_format": DXGI_FORMAT_R32_UINT,
                "topology": _LINE_LIST if edges else _TRIANGLE_LIST,
                "draw": ((2 if edges else 3) * count, 1, 0, 0, 0), "rasterizer": _RASTERIZER,
                "depth_stencil": _NO_DEPTH_STENCIL, "blend": (0, 0, [_UNWRITTEN]), "tables": tables, "heap": [],
                "size": (width, height), "viewport": (-8192.0, -8192.0, 16384.0, 16384.0, 0.0, 1.0),
                "scissor": (0, 0, width, height), "targets": [(DXGI_FORMAT_R8_UNORM, self._unwritten)], "depth": None}

    def _dispatch(self, source: str, tables: list) -> int:
        width, height = self.size
        return self.device.scene.dispatch({"compute_shader": _compiled(source, "cs_5_0"), "tables": tables,
                                           "heap": [], "groups": (-(-width // _GROUP), -(-height // _GROUP), 1)})

    def _cleared(self, texture: int) -> int:
        return self._made(("clear", texture), lambda: self._dispatch(_CLEAR, [
            (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, texture),))]))

    def _seen_steps(self) -> list[int]:
        steps = [self._cleared(texture) for texture in self._seen]
        uavs = [(RANGE_UAV, register, 0, 1, TEXTURE_2D, 5, ((0, DXGI_FORMAT_R32_UINT, texture),))
                for register, texture in enumerate(self._seen)]
        for item in self._objects:
            for part, (_indices, count, *_rest) in enumerate(item.geometry.parts):
                if count:
                    steps.append(self._made(("seen", item.number, part), lambda item=item, part=part: (
                        self.device.scene.draw(self._draw_job(
                            item, _compiled(_SEEN_GEOMETRY, "gs_5_0"),
                            _compiled(_SEEN_PIXEL, "ps_5_0"),
                            [(RANGE_CBV, 2, 0, 1, 0, 4, ((0, item.parts[part]),)), *uavs], part=part)))))
        return steps

    def _mask_steps(self, selected: frozenset[int]) -> list[int]:
        steps = [self._cleared(self._mask)]
        for number in sorted(selected):
            item = self._objects[number]
            for part, (_indices, count, first, *_states) in enumerate(item.geometry.parts):
                if count:
                    uncut = self.device.buffer(struct.pack("<4I", number + 1, first, CULL_NONE, 0))
                    steps.append(self._made(("mask", number, part), lambda item=item, part=part, uncut=uncut: (
                        self.device.scene.draw(self._draw_job(
                            item, _compiled(_SEEN_GEOMETRY, "gs_5_0"),
                            _compiled(_MASK_PIXEL, "ps_5_0"),
                            [(RANGE_CBV, 2, 0, 1, 0, 4, ((0, uncut),)),
                             (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 5, ((0, DXGI_FORMAT_R32_UINT, self._mask),))],
                            part=part)))))
        return steps

    def _edge_steps(self, shown: SceneShown) -> list[int]:
        steps = []
        constants = (int(max(round(shown.edge_width), 1)), packed(shown.edge_colour),
                     int(shown.edges == VISIBLE_EDGES))
        for item in self._objects:
            if not self._edges(item.geometry)[1]:
                continue
            _rewrite(self.device.scene, item.edges, struct.pack("<4I", item.number + 1, *constants))
            steps.append(self._made(("edges", item.number), lambda item=item: self.device.scene.draw(self._draw_job(
                item, _compiled(_EDGE_GEOMETRY, "gs_5_0"), _compiled(_EDGE_PIXEL, "ps_5_0"),
                [(RANGE_CBV, 2, 0, 1, 0, 0, ((0, item.edges[0]),)),
                 (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 5, ((0, DXGI_FORMAT_R32_UINT, self._image),)),
                 (RANGE_SRV, 0, 0, 3, TEXTURE_2D, 5, tuple((index, DXGI_FORMAT_R32_UINT, texture)
                                                       for index, texture in enumerate(self._seen))),
                 (RANGE_SRV, 5, 0, 1, BUFFER, 5, ((0, 0, 4, self._edges(item.geometry)[2]),))], edges=True))))
        return steps

    def steps(self, view: int, image: int, shown: SceneShown, camera: bytes) -> list[int]:
        if (view, image) != (self._view, self._image):
            self._view, self._image, self._steps = view, image, {}
            self._seen_for = self._masked = None
        tinted = shown.hovered is not None or bool(shown.selected)
        steps = []
        if (shown.edges == VISIBLE_EDGES or tinted or shown.pick is not None or not shown.filled) \
                and self._seen_for != camera:
            steps += self._seen_steps()
            self._seen_for = camera
        if tinted:
            flags = np.zeros(max(len(self._objects), 1), "<u4")
            flags[sorted(shown.selected)] |= _SELECTED
            if shown.hovered is not None:
                flags[shown.hovered] |= _HOVERED
            _rewrite(self.device.scene, self._flags, flags.tobytes())
            _rewrite(self.device.scene, self._tint,
                     struct.pack("<2I8x", packed(shown.selection_colour), packed(shown.hover_colour)))
            steps.append(self._made(("tint",), lambda: self._dispatch(_TINT, [
                (RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._seen[1]),)),
                (RANGE_SRV, 1, 0, 1, BUFFER, 0, ((0, 0, 4, self._flags[0]),)),
                (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._image),)),
                (RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._tint[0]),))])))
        if shown.edges:
            steps += self._edge_steps(shown)
        if shown.selected and shown.outline > 0:
            if self._masked != (camera, shown.selected):
                steps += self._mask_steps(shown.selected)
                self._masked = camera, shown.selected
            _rewrite(self.device.scene, self._outline,
                     struct.pack("<Ii8x", packed(shown.outline_colour), int(shown.outline)))
            steps.append(self._made(("outline",), lambda: self._dispatch(_OUTLINE, [
                (RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._mask),)),
                (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._image),)),
                (RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._outline[0]),))])))
        if shown.pick is not None:
            column, row = shown.pick
            width, height = self.size
            if not (0 <= column < width and 0 <= row < height):
                raise ValueError(f"the texel picked, {shown.pick}, lies outside the image")
            _rewrite(self.device.scene, self._pick, struct.pack("<2I8x", column, row))
            steps.append(self._made(("pick",), lambda: self.device.scene.dispatch({
                "compute_shader": _compiled(_PICK, "cs_5_0"), "heap": [], "groups": (1, 1, 1),
                "tables": [(RANGE_SRV, 0, 0, 2, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._seen[1]),
                                                           (1, DXGI_FORMAT_R32_UINT, self._seen[2]))),
                           (RANGE_UAV, 0, 0, 1, BUFFER, 0, ((0, 0, 8, self.picked),)),
                           (RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._pick[0]),))]})))
        return steps


__all__ = ["ALL_EDGES", "MOST_INFLUENCES", "NO_EDGES", "OverlayGeometry", "OverlayPart", "OverlayScene", "SceneShown",
           "VISIBLE_EDGES", "edges_of"]
