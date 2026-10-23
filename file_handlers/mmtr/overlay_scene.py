"""See docs/internals/overlay.md, Scene layer."""
from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
from threading import Thread

import numpy as np

lazy from file_handlers.sdf.hlsl import compile_hlsl_source
from file_handlers.sdf.sdf_file import CULL_BACK, CULL_FRONT, CULL_NONE
lazy from file_handlers.tex.dxgi import (
    DXGI_FORMAT_D32_FLOAT, DXGI_FORMAT_R16G16B16A16_UINT, DXGI_FORMAT_R32G32B32A32_FLOAT, DXGI_FORMAT_R32G32B32_FLOAT,
    DXGI_FORMAT_R32_FLOAT, DXGI_FORMAT_R32_TYPELESS, DXGI_FORMAT_R32_UINT, DXGI_FORMAT_R8_UNORM,
)
lazy from utils.native_build import ensure_fastmesh

lazy from .engine_buffers import Camera
lazy from .material_preview import (
    RANGE_CBV, _NO_DEPTH_STENCIL, _RASTERIZER, RANGE_SRV, _TRIANGLE_LIST, _UNBLENDED, RANGE_UAV,
)
lazy from .mesh_render import boxes_rects, boxes_seen, rects_meet
from .overlay import _GROUP, _UNWRITTEN, _VIEW, PIXEL_RULES, RULES, _view_bytes, packed, view_projection
lazy from .resident import BUFFER, DEPTH_STENCIL, RENDER_TARGET, TEXTURE_2D, UNORDERED_ACCESS, Resident

_LINE_LIST = 2
# Packed four per vertex element.
MOST_INFLUENCES = 16
_IDENTITY = np.eye(4)[:3]
NO_EDGES, ALL_EDGES, VISIBLE_EDGES = "", "all", "visible"
_SELECTED, _HOVERED = 2, 1
# Past this many objects tinted, testing the others against each costs more than drawing them.
_MOST_TESTED = 64


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
    """``outline`` is a width, 0 for none. Without ``filled``, marks test against the seen triangles' depth."""
    edges: str = NO_EDGES
    edge_width: float = 1.5
    edge_colour: tuple[int, int, int, int] = (51, 255, 77, 255)
    hovered: int | None = None
    selected: frozenset[int] = frozenset()
    hover_colour: tuple[int, int, int, int] = (13, 255, 64, 97)
    selection_colour: tuple[int, int, int, int] = (255, 219, 46, 56)
    outline_colour: tuple[int, int, int, int] = (255, 219, 46, 255)
    outline: int = 2
    filled: bool = True


def edges_of(positions: np.ndarray, triangles: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    points, edges = ensure_fastmesh().edges_of(np.ascontiguousarray(positions, "<f4"),
                                               np.ascontiguousarray(triangles, "<u4"))
    return np.frombuffer(points, np.uint32), np.frombuffer(edges, np.uint32).reshape(-1, 2)


_CORNER = """
struct Corner
{
    float4 clip : CLIP;
    uint vertex : VERTEX;
};
"""


_OBJECT = """
cbuffer Object : register(b1)
{
    float4 world[3];
    // The object's number plus one.
    uint object;
};
"""


@lru_cache
def _vertex_source(groups: int, rasterized: bool = False) -> str:
    """``rasterized``: for the device's own rasterizer, the clip position its output."""
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
    return _VIEW + _OBJECT + f"""
StructuredBuffer<float4> palette : register(t3);

float3 moved_by(float4 x, float4 y, float4 z, float3 p)
{{
    precise float3 v = float3(((x.x * p.x + x.y * p.y) + x.z * p.z) + x.w, ((y.x * p.x + y.y * p.y) + y.z * p.z) + y.w,
                              ((z.x * p.x + z.y * p.y) + z.z * p.z) + z.w);
    return v;
}}

{"" if rasterized else _CORNER}
{"float4" if rasterized else "Corner"} main(float3 position : REASY_STREAM0{inputs}, uint vertex : SV_VertexID)
    {": SV_Position" if rasterized else ""}
{{
    precise float3 at = position;
{skinned}    precise float3 placed = moved_by(world[0], world[1], world[2], at);
    precise float4 clip = ((placed.x * rows[0] + placed.y * rows[1]) + placed.z * rows[2]) + rows[3];
{"    return clip;" if rasterized else """    Corner corner;
    corner.clip = clip;
    corner.vertex = vertex;
    return corner;"""}
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
_SEEN_GEOMETRY = _SEEN_SHAPE + RULES + _CORNER + _OBJECT + """
cbuffer Run : register(b2)
{
    // The run's first triangle's number, its parts' culling and whether a front face turns counter-clockwise.
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
_MASK_PIXEL = """uint main() : SV_Target
{
    return 1u;
}
"""
# The object nearest at each texel, the device rasterizing and testing depth.
_IDS_PIXEL = _OBJECT + """
uint main() : SV_Target
{
    return object;
}
"""
# Greater is nearer; of equal depths the first drawn stays.
_NEAREST = (1, 1, 5, 0, 0xFF, 0xFF, (1, 1, 1, 8), (1, 1, 1, 8), 0)
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


@lru_cache
def _compiled(source: str, target: str) -> bytes:
    return bytes(compile_hlsl_source(source, entry_point="main", target=target))


_WARMED: list[Thread] = []


def _warm() -> None:
    """Compiles the passes' shaders on a thread of their own, once a process: a first hover would wait for them."""
    if not _WARMED:
        shaders = ((_vertex_source(0), "vs_5_0"), (_vertex_source(0, True), "vs_5_0"), (_SEEN_GEOMETRY, "gs_5_0"),
                   (_SEEN_PIXEL, "ps_5_0"), (_MASK_PIXEL, "ps_5_0"), (_IDS_PIXEL, "ps_5_0"), (_EDGE_GEOMETRY, "gs_5_0"),
                   (_EDGE_PIXEL, "ps_5_0"), (_CLEAR, "cs_5_0"), (_TINT, "cs_5_0"), (_OUTLINE, "cs_5_0"))
        _WARMED.append(Thread(target=lambda: [_compiled(*shader) for shader in shaders], name="Overlay shaders",
                              daemon=True))
        _WARMED[0].start()


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
    return scene.buffer(np.tile(_IDENTITY, (bones, 1, 1)).astype("<f4").tobytes(), 0, 16) if bones else None


def _palette_rows(item, posed: int, bones) -> bytes:
    named = item.geometry.bones
    if item.palette is None:
        raise ValueError(f"object {posed} is not skinned")
    rows = _rows(bones, named)
    if len(rows) < named:
        raise ValueError(f"object {posed} names {named} bones, and {len(rows)} are given")
    return rows.astype("<f4").tobytes()


def _box(positions: np.ndarray) -> np.ndarray:
    return np.array([positions.min(axis=0), positions.max(axis=0)], np.float64)


def _placed_boxes(worlds: np.ndarray, centre: np.ndarray, extent: np.ndarray) -> np.ndarray:
    """Boxes of ``centre`` and half ``extent`` placed by ``worlds``, n x 3 x 4."""
    rotations, offsets = worlds[:, :, :3], worlds[:, :, 3]
    centres = np.einsum("oij,oj->oi", rotations, centre) + offsets
    extents = np.einsum("oij,oj->oi", np.abs(rotations), extent)
    return np.stack([centres - extents, centres + extents], axis=1)


def _rewrite(scene, buffer: list, data: bytes) -> bool:
    if data == buffer[1]:
        return False
    scene.write(buffer[0], data)
    buffer[1] = data
    return True


@dataclass(slots=True)
class _Held:
    """``indices`` holds every part's triangles, part by part; ``runs`` are its parts of one culling and facing in a
    row, each its constants, first triangle, count, and culling and facing. ``triangles`` are each part's, n x 3, as
    given."""
    count: int
    bones: int
    vertices: list[tuple[int, int]]
    elements: tuple[tuple, ...]
    indices: int | None
    runs: list[tuple[int, int, int, tuple[int, int]]]
    positions: np.ndarray
    triangles: tuple[np.ndarray, ...]
    edged: bool
    box: np.ndarray
    edges: tuple[int, int, int] | None = None


@dataclass(slots=True)
class _Object:
    geometry: _Held
    placed: list
    palette: int | None
    edges: list


class OverlayScene:
    def __init__(self, device: Resident, size: Sequence[int], geometries: Sequence[OverlayGeometry],
                 objects: Sequence[int] | None = None):
        _warm()
        self.device, self.size = device, tuple(size)
        scene = device.scene
        self._uncut = device.buffer(struct.pack("<4I", 0, CULL_NONE, 0, 0), 1)
        self._geometries: list[_Held] = []
        self._objects: list[_Object] = []
        self._worlds = np.zeros((0, 3, 4))
        # The buffer, its bytes written and the objects it has room for.
        self._flags: list = [None, b"", 0]
        # Each object's geometry's box centre and half extent, whether it is skinned, and its placed box.
        self._boxes: tuple | None = None
        # Objects drawn hidden: no pass draws them.
        self._hidden = np.zeros(0, bool)
        self._hidden_given: np.ndarray | None = None
        self._screen()
        self.add(geometries, range(len(geometries)) if objects is None else objects)
        self._tint = [scene.buffer(bytes(16), 0), b""]
        self._outline = [scene.buffer(bytes(16), 0), b""]

    def _screen(self) -> None:
        self._seen = [self.device.texture(DXGI_FORMAT_R32_TYPELESS if index == 0 else DXGI_FORMAT_R32_UINT, self.size,
                                          UNORDERED_ACCESS) for index in range(3)]
        self.seen_depth = (DXGI_FORMAT_R32_FLOAT, self._seen[0])
        # The objects the device finds nearest, its depth, and the selected objects' cover.
        self._ids = self.device.texture(DXGI_FORMAT_R32_UINT, self.size, RENDER_TARGET)
        self._ids_depth = self.device.texture(DXGI_FORMAT_D32_FLOAT, self.size, DEPTH_STENCIL)
        self._mask = self.device.texture(DXGI_FORMAT_R32_UINT, self.size, RENDER_TARGET)
        self._unwritten = self.device.texture(DXGI_FORMAT_R8_UNORM, self.size, RENDER_TARGET)
        self._view: int | None = None
        self._image: int | None = None
        self._steps: dict[tuple, int] = {}
        self._moved()

    def _moved(self) -> None:
        """What frames drew of the objects as they were."""
        self._seen_for = self._ids_for = self._masked = self._culled = None

    def add(self, geometries: Sequence[OverlayGeometry], objects: Sequence[int]) -> None:
        """More geometries, and objects of the scene's geometries numbered on from its others, whose steps stay."""
        if not geometries and not objects:
            return
        self._geometries += [self._held(geometry) for geometry in geometries]
        self._objects += [self._object(number, geometry) for number, geometry in enumerate(objects, len(self._objects))]
        self._worlds = np.concatenate([self._worlds, np.broadcast_to(_IDENTITY, (len(objects), 3, 4))])
        if len(self._objects) > self._flags[2]:
            if self._flags[0] is not None:
                self.device.release([self._flags[0]])
            room = max(2 * len(self._objects), 1)
            self._flags = [self.device.scene.buffer(bytes(4 * room), 0), b"", room]
            self._steps.pop(("tint",), None)
        self._boxes = None
        self._moved()

    def hide(self, hidden: np.ndarray) -> None:
        """Whether each object is hidden; ``hidden`` is not to be changed after."""
        if hidden is self._hidden_given:
            return
        self._hidden_given = hidden
        if not np.array_equal(hidden, self._hidden):
            self._hidden = np.asarray(hidden, bool).copy()
            self._moved()

    def recast(self, objects: Mapping[int, int]) -> None:
        """Objects of other geometries of the scene's, by object number, placed as they were: their bones are given
        again."""
        if not objects:
            return
        old = []
        for number, geometry in objects.items():
            item = self._objects[number]
            old += [item.placed[0], item.edges[0], *(() if item.palette is None else (item.palette,))]
            self._objects[number] = self._object(number, geometry)
            self.place(number, self._worlds[number])
        self._steps = {key: step for key, step in self._steps.items()
                       if key[0] not in ("seen", "ids", "mask", "edges") or key[1] not in objects}
        self.device.release(old)
        self._boxes = None
        self._moved()

    def resize(self, size: Sequence[int]) -> None:
        old = [*self._seen, self._ids, self._ids_depth, self._mask, self._unwritten]
        self.size = tuple(size)
        self._screen()
        self.device.release(old)

    def release(self) -> None:
        """Releases its own resources on the device, which others go on using: its steps may not run again. The
        buffers the device shares by their bytes stay."""
        self.device.release([
            *(buffer for held in self._geometries for buffer, _stride in held.vertices),
            *(resource for held in self._geometries
              for resource in (held.indices, *(held.edges[::2] if held.edges and held.edges[1] else ()))),
            *(resource for item in self._objects for resource in (item.placed[0], item.palette, item.edges[0])),
            *self._seen, self._ids, self._ids_depth, self._mask, self._unwritten, self._flags[0], self._tint[0],
            self._outline[0]])

    def _held(self, geometry: OverlayGeometry) -> _Held:
        positions = np.ascontiguousarray(geometry.positions, "<f4").reshape(-1, 3)
        count = len(positions)
        if not count:
            raise ValueError("a geometry has no vertices")
        scene = self.device.scene
        vertices = [(scene.buffer(positions.tobytes(), 0, 1), 12)]
        elements = [("REASY_STREAM", 0, DXGI_FORMAT_R32G32B32_FLOAT, 0, 0, 0, 0)]
        bones = 0
        if geometry.bones is not None:
            named, weighed, groups, bones = _influence_streams(geometry.bones, geometry.weights, count)
            vertices += [(scene.buffer(named, 0, 1), 8 * groups), (scene.buffer(weighed, 0, 1), 16 * groups)]
            elements += [("REASY_STREAM", base + group, form, slot, group * step, 0, 0)
                         for base, form, slot, step in ((1, DXGI_FORMAT_R16G16B16A16_UINT, 1, 8),
                                                        (5, DXGI_FORMAT_R32G32B32A32_FLOAT, 2, 16))
                         for group in range(groups)]
        runs, triangles, first = [], [], 0
        for part in geometry.parts:
            indices = np.ascontiguousarray(part.indices, "<u4").reshape(-1)
            if len(indices) % 3 or (indices >= count).any():
                raise ValueError("a part's indices are not triangles of its geometry's vertices")
            if part.cull not in (CULL_NONE, CULL_FRONT, CULL_BACK):
                raise ValueError(f"a part culls none, the front or the back faces, not {part.cull}")
            state = (part.cull, int(bool(part.front_counter_clockwise)))
            if runs and runs[-1][0] == state:
                runs[-1][2] += len(indices) // 3
            else:
                runs.append([state, first, len(indices) // 3])
            triangles.append(indices.reshape(-1, 3))
            first += len(indices) // 3
        # Draws of one geometry's run are alike but for the object, so they share the run's constants.
        return _Held(count, bones, vertices, tuple(elements),
                     scene.buffer(b"".join(part.tobytes() for part in triangles), 0, 1) if first else None,
                     [(self.device.buffer(struct.pack("<4I", start, *state, 0), 1), start, length, state)
                      for state, start, length in runs if length], positions, tuple(triangles), bool(geometry.edged),
                     _box(positions))

    def _edges(self, held: _Held) -> tuple[int, int, int]:
        if held.edges is None:
            every = np.concatenate(held.triangles) if held.triangles else np.zeros((0, 3), np.uint32)
            points, edges = edges_of(held.positions, every) if held.edged else (None, None)
            scene = self.device.scene
            held.edges = (0, 0, 0) if edges is None or not len(edges) else (
                scene.buffer(edges.astype("<u4").tobytes(), 0, 1), len(edges),
                scene.buffer(points[every].astype("<u4").tobytes(), 0, 4))
        return held.edges

    def _object(self, number: int, geometry: int) -> _Object:
        if not 0 <= geometry < len(self._geometries):
            raise ValueError(f"an object is of geometry {geometry}, which the scene has not")
        held = self._geometries[geometry]
        scene = self.device.scene
        placed = self._placed(number, _IDENTITY)
        palette = _identity_palette(scene, held.bones)
        return _Object(held, [scene.buffer(placed, 0, 1), placed], palette, [scene.buffer(bytes(16), 0, 1), b""])

    @staticmethod
    def _placed(number: int, world) -> bytes:
        return np.asarray(world, "<f4").tobytes() + struct.pack("<4I", number + 1, 0, 0, 0)

    def place(self, placed: int, world) -> None:
        """``world`` is 3 x 4."""
        rows = _rows(world)[0]
        if _rewrite(self.device.scene, self._objects[placed].placed, self._placed(placed, rows)):
            self._worlds[placed] = rows
            if self._boxes is not None and self._boxes[3] is not None:
                centre, extent, _skinned, boxes = self._boxes
                boxes[placed] = _placed_boxes(rows[None], centre[placed:placed + 1], extent[placed:placed + 1])[0]
            self._moved()

    def pose(self, posed: int, bones) -> None:
        """``bones`` are 3 x 4 each."""
        held = self._objects[posed]
        self.device.scene.write(held.palette, _palette_rows(held, posed, bones))
        self._moved()

    def write(self, geometry: int, positions) -> None:
        """Edges keep the original topology."""
        held = self._geometries[geometry]
        positions = np.ascontiguousarray(positions, "<f4").reshape(-1, 3)
        if len(positions) != held.count:
            raise ValueError(f"the geometry has {held.count} vertices, not {len(positions)}")
        self.device.scene.write(held.vertices[0][0], positions.tobytes())
        held.box = _box(positions)
        self._boxes = None
        self._moved()

    def _made(self, key: tuple, make) -> int:
        if key not in self._steps:
            self._steps[key] = make()
        return self._steps[key]

    def _draw_job(self, item: _Object, geometry: bytes, pixel: bytes, tables: list, *, edges=False,
                  triangles: tuple[int, int] = (0, 0)) -> dict:
        """``triangles`` are the first and the count drawn of the object's, unless it draws its ``edges``."""
        width, height = self.size
        held = item.geometry
        (indices, count), first = (self._edges(held)[:2], 0) if edges else ((held.indices, triangles[1]), triangles[0])
        tables = [(RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._view),)), (RANGE_CBV, 1, 0, 1, 0, 0, ((0, item.placed[0]),)),
                  *tables]
        if item.palette is not None:
            tables.append((RANGE_SRV, 3, 0, 1, BUFFER, 1, ((0, 0, 16, item.palette),)))
        return {"vertex_shader": _compiled(_vertex_source((len(held.elements) - 1) // 2), "vs_5_0"),
                "geometry_shader": geometry, "pixel_shader": pixel, "vertex_buffers": held.vertices,
                "elements": held.elements, "indices": indices, "index_format": DXGI_FORMAT_R32_UINT,
                "topology": _LINE_LIST if edges else _TRIANGLE_LIST,
                "draw": ((2 if edges else 3) * count, 1, 3 * first, 0, 0), "rasterizer": _RASTERIZER,
                "depth_stencil": _NO_DEPTH_STENCIL, "blend": (0, 0, [_UNWRITTEN]), "tables": tables, "heap": [],
                "size": (width, height), "viewport": (-8192.0, -8192.0, 16384.0, 16384.0, 0.0, 1.0),
                "scissor": (0, 0, width, height), "targets": [(DXGI_FORMAT_R8_UNORM, self._unwritten)], "depth": None}

    def _rasterized_job(self, item: _Object, pixel: bytes, run: tuple, targets: list, depth: bool,
                        uncut: bool = False) -> dict:
        """A run of the object's triangles, or all of them ``uncut`` by culling, the device rasterizing."""
        constants, first, count, (cull, counter_clockwise) = run
        width, height = self.size
        held = item.geometry
        tables = [(RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._view),)), (RANGE_CBV, 1, 0, 1, 0, 0, ((0, item.placed[0]),)),
                  (RANGE_CBV, 2, 0, 1, 0, 0, ((0, constants),))]
        if item.palette is not None:
            tables.append((RANGE_SRV, 3, 0, 1, BUFFER, 1, ((0, 0, 16, item.palette),)))
        return {"vertex_shader": _compiled(_vertex_source((len(held.elements) - 1) // 2, True), "vs_5_0"),
                "pixel_shader": pixel, "vertex_buffers": held.vertices, "elements": held.elements,
                "indices": held.indices, "index_format": DXGI_FORMAT_R32_UINT, "topology": _TRIANGLE_LIST,
                "draw": (3 * count, 1, 3 * first, 0, 0),
                "rasterizer": (3, CULL_NONE if uncut else cull, counter_clockwise, 0, 0.0, 0.0, 1),
                "depth_stencil": _NEAREST if depth else _NO_DEPTH_STENCIL, "blend": (0, 0, [_UNBLENDED] * len(targets)),
                "tables": tables, "heap": [], "size": (width, height),
                "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0), "scissor": (0, 0, width, height),
                "targets": targets, "depth": (DXGI_FORMAT_D32_FLOAT, self._ids_depth) if depth else None}

    def _id_steps(self, numbers: np.ndarray) -> list[int]:
        scene = self.device.scene
        steps = [self._made(("clear", self._ids), lambda: scene.clear(DXGI_FORMAT_R32_UINT, self._ids)),
                 self._made(("clear", self._ids_depth), lambda: scene.clear(DXGI_FORMAT_D32_FLOAT, self._ids_depth))]
        targets = [(DXGI_FORMAT_R32_UINT, self._ids)]
        for number in numbers.tolist():
            if ("ids", number) not in self._steps:
                item = self._objects[number]
                self._steps["ids", number] = [scene.draw(self._rasterized_job(
                    item, _compiled(_IDS_PIXEL, "ps_5_0"), run, targets, True)) for run in item.geometry.runs]
            steps += self._steps["ids", number]
        return steps

    def _dispatch(self, source: str, tables: list) -> int:
        width, height = self.size
        return self.device.scene.dispatch({"compute_shader": _compiled(source, "cs_5_0"), "tables": tables,
                                           "heap": [], "groups": (-(-width // _GROUP), -(-height // _GROUP), 1)})

    def _cleared(self, texture: int) -> int:
        return self._made(("clear", texture), lambda: self._dispatch(_CLEAR, [
            (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, texture),))]))

    def boxes(self) -> tuple[np.ndarray, np.ndarray]:
        """Each object's geometry's box centre and half extent; arrays not changed after."""
        self._placed_boxes()
        return self._boxes[0], self._boxes[1]

    def _placed_boxes(self) -> np.ndarray:
        """Each object's placed box, in the world."""
        if self._boxes is None:
            boxes = np.array([item.geometry.box for item in self._objects]).reshape(-1, 2, 3)
            self._boxes = ((boxes[:, 0] + boxes[:, 1]) / 2, (boxes[:, 1] - boxes[:, 0]) / 2,
                           np.array([item.palette is not None for item in self._objects], bool), None)
        centre, extent, skinned, placed = self._boxes
        if placed is None:
            placed = _placed_boxes(self._worlds, centre, extent)
            self._boxes = centre, extent, skinned, placed
        return placed

    def _in_view(self, camera: Camera, key: bytes) -> np.ndarray:
        """The numbers of the objects that may be in view; skinned ones always, as bones move their vertices."""
        if self._culled is None or self._culled[0] != key:
            seen = boxes_seen(self._placed_boxes(), view_projection(camera))
            shown = seen | self._boxes[2]
            if len(self._hidden):
                shown[:len(self._hidden)] &= ~self._hidden
            self._culled = key, np.flatnonzero(shown)
        return self._culled[1]

    def _under(self, camera: Camera, numbers: np.ndarray, tinted: Sequence[int]) -> np.ndarray:
        """Of the objects ``numbers`` in view, those whose box on screen meets a tinted one's, a texel wider: the
        objects found nearest there are theirs alone. Unsure boxes (crossing the eye's plane, skinned) meet
        every one."""
        tinted = np.asarray(tinted, np.int64)
        at = np.searchsorted(numbers, tinted)
        inside = at < len(numbers)
        at = at[inside][numbers[at[inside]] == tinted[inside]]
        if len(at) > _MOST_TESTED:
            return numbers
        rects = boxes_rects(self._placed_boxes()[numbers], view_projection(camera))
        rects[self._boxes[2][numbers]] = (-np.inf, -np.inf, np.inf, np.inf)
        width, height = self.size
        targets = rects[at] + np.array([-2 / width, -2 / height, 2 / width, 2 / height])
        return numbers[rects_meet(rects, targets)]

    def _seen_steps(self, numbers: np.ndarray) -> list[int]:
        steps = [self._cleared(texture) for texture in self._seen]
        for number in numbers.tolist():
            if ("seen", number) not in self._steps:
                item = self._objects[number]
                uavs = [(RANGE_UAV, register, 0, 1, TEXTURE_2D, 5, ((0, DXGI_FORMAT_R32_UINT, texture),))
                        for register, texture in enumerate(self._seen)]
                self._steps["seen", number] = [self.device.scene.draw(self._draw_job(
                    item, _compiled(_SEEN_GEOMETRY, "gs_5_0"), _compiled(_SEEN_PIXEL, "ps_5_0"),
                    [(RANGE_CBV, 2, 0, 1, 0, 4, ((0, constants),)), *uavs], triangles=(first, count)))
                    for constants, first, count, _state in item.geometry.runs]
            steps += self._steps["seen", number]
        return steps

    def _mask_steps(self, selected: frozenset[int]) -> list[int]:
        scene = self.device.scene
        steps = [self._made(("clear", self._mask), lambda: scene.clear(DXGI_FORMAT_R32_UINT, self._mask))]
        for number in sorted(selected):
            item = self._objects[number]
            # Drawn uncut, its parts are one draw.
            if item.geometry.indices is not None and not (number < len(self._hidden) and self._hidden[number]):
                steps.append(self._made(("mask", number), lambda item=item: scene.draw(self._rasterized_job(
                    item, _compiled(_MASK_PIXEL, "ps_5_0"),
                    (self._uncut, 0, sum(map(len, item.geometry.triangles)), (CULL_NONE, 0)),
                    [(DXGI_FORMAT_R32_UINT, self._mask)], False, uncut=True))))
        return steps

    def _edge_steps(self, shown: SceneShown, numbers: np.ndarray) -> list[int]:
        steps = []
        constants = (int(max(round(shown.edge_width), 1)), packed(shown.edge_colour),
                     int(shown.edges == VISIBLE_EDGES))
        for number in numbers.tolist():
            item = self._objects[number]
            if not self._edges(item.geometry)[1]:
                continue
            if item.edges[1][4:] != struct.pack("<3I", *constants):
                _rewrite(self.device.scene, item.edges, struct.pack("<4I", number + 1, *constants))
            steps.append(self._made(("edges", number), lambda item=item: self.device.scene.draw(self._draw_job(
                item, _compiled(_EDGE_GEOMETRY, "gs_5_0"), _compiled(_EDGE_PIXEL, "ps_5_0"),
                [(RANGE_CBV, 2, 0, 1, 0, 0, ((0, item.edges[0]),)),
                 (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 5, ((0, DXGI_FORMAT_R32_UINT, self._image),)),
                 (RANGE_SRV, 0, 0, 3, TEXTURE_2D, 5, tuple((index, DXGI_FORMAT_R32_UINT, texture)
                                                       for index, texture in enumerate(self._seen))),
                 (RANGE_SRV, 5, 0, 1, BUFFER, 5, ((0, 0, 4, self._edges(item.geometry)[2]),))], edges=True))))
        return steps

    def steps(self, view: int, image: int, shown: SceneShown, seen_from: Camera) -> list[int]:
        """Objects out of ``seen_from``'s view draw nothing on screen, so passes leave them out."""
        if (view, image) != (self._view, self._image):
            self._view, self._image, self._steps = view, image, {}
            self._moved()
        camera = _view_bytes(seen_from)
        tinted = shown.hovered is not None or bool(shown.selected)
        steps = []
        # Edges and marks test against the seen triangles everywhere.
        if (shown.edges == VISIBLE_EDGES or not shown.filled) and self._seen_for != camera:
            steps += self._seen_steps(self._in_view(seen_from, camera))
            self._seen_for = camera
        if tinted:
            # Tints read the nearest objects the device finds, where they are read alone. Drawn for a camera of some
            # objects, those are drawn again only once others are wanted.
            asked = tuple(sorted({*shown.selected, *(() if shown.hovered is None else (shown.hovered,))}))
            held = self._ids_for if self._ids_for is not None and self._ids_for[0] == camera else None
            if held is None or asked != held[2]:
                drawn = self._under(seen_from, self._in_view(seen_from, camera), asked)
                if held is not None:
                    drawn = np.union1d(held[1], drawn)
                if held is None or len(drawn) != len(held[1]):
                    steps += self._id_steps(drawn)
                self._ids_for = camera, drawn, asked
            flags = np.zeros(self._flags[2], "<u4")
            flags[sorted(shown.selected)] |= _SELECTED
            if shown.hovered is not None:
                flags[shown.hovered] |= _HOVERED
            _rewrite(self.device.scene, self._flags, flags.tobytes())
            _rewrite(self.device.scene, self._tint,
                     struct.pack("<2I8x", packed(shown.selection_colour), packed(shown.hover_colour)))
            steps.append(self._made(("tint",), lambda: self._dispatch(_TINT, [
                (RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._ids),)),
                (RANGE_SRV, 1, 0, 1, BUFFER, 0, ((0, 0, 4, self._flags[0]),)),
                (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self._image),)),
                (RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._tint[0]),))])))
        if shown.edges:
            steps += self._edge_steps(shown, self._in_view(seen_from, camera))
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
        return steps


__all__ = ["ALL_EDGES", "MOST_INFLUENCES", "NO_EDGES", "OverlayGeometry", "OverlayPart", "OverlayScene", "SceneShown",
           "VISIBLE_EDGES", "edges_of"]
