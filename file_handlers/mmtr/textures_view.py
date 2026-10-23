"""See docs/internals/textures-view.md."""
from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

lazy from file_handlers.sdf.hlsl import compile_hlsl_source
from file_handlers.sdf.sdf_file import (
    BLEND_INVERSE_SOURCE_ALPHA, BLEND_ONE, BLEND_OPERATION_ADD, BLEND_SOURCE_ALPHA, BLEND_ZERO, COMPARE_EQUAL,
    COMPARE_GREATER, COMPARE_GREATER_EQUAL, CULL_BACK,
)
from file_handlers.tex.dxgi import (
    DXGI_FORMAT_D32_FLOAT, DXGI_FORMAT_R16G16B16A16_UINT, DXGI_FORMAT_R16G16_UNORM, DXGI_FORMAT_R32G32B32A32_FLOAT,
    DXGI_FORMAT_R32G32B32_FLOAT, DXGI_FORMAT_R32G32_FLOAT, DXGI_FORMAT_R32_UINT, DXGI_FORMAT_R8G8B8A8_TYPELESS,
    DXGI_FORMAT_R8G8B8A8_UINT, DXGI_FORMAT_R8G8B8A8_UNORM,
)

lazy from .engine_buffers import Camera
lazy from .material_preview import (
    RANGE_CBV, _NO_DEPTH_STENCIL, _RASTERIZER, RANGE_SAMPLER, RANGE_SRV, _TRIANGLE_LIST, RANGE_UAV, _UNBLENDED,
)
from .mesh_render import DEPTH_VIEWS
lazy from .overlay import view_projection
from .overlay_scene import (
    MOST_INFLUENCES, _IDENTITY, _identity_palette, _influence_streams,
    _palette_rows, _rewrite, _rows,
)
from .probe_light import SOURCE as _PROBES, ProbeLight, ViewProbes
lazy from .resident import BUFFER, DEPTH_STENCIL, RENDER_TARGET, TEXTURE_2D, UNORDERED_ACCESS, Resident

# Reversed depth: greater is nearer.
EQUAL, NEARER, AS_NEAR = COMPARE_EQUAL, COMPARE_GREATER, COMPARE_GREATER_EQUAL
# RGB write mask; alpha stays 255.
_COLOUR = 0x7
# Each sRGB format's plain sibling (DXGI_FORMAT).
_PLAIN = {29: 28, 72: 71, 75: 74, 78: 77, 91: 87, 93: 88, 99: 98}
# D3D12_FILTER values MIN_MAG_MIP_LINEAR (trilinear) and ANISOTROPIC; 16 is D3D12's maximum anisotropy.
_LINEAR, _ANISOTROPIC, _MOST_ANISOTROPY = 0x15, 0x55, 16
_WRAP = 1
_WHITE = (DXGI_FORMAT_R8G8B8A8_UNORM, 1, 1, 1, 1, bytes([255]) * 4)


def _checker(size: int = 64, squares: int = 8) -> tuple:
    cell = np.arange(size) * squares // size
    image = np.where((cell[:, None] + cell[None, :]) % 2, 0.85, 0.15)
    mips = [image]
    while len(mips[-1]) > 1:
        mips.append(mips[-1].reshape(len(mips[-1]) // 2, 2, -1, 2).mean(axis=(1, 3)))
    texels = b"".join(np.dstack([np.rint(mip * 255)] * 3 + [np.full(mip.shape, 255)]).astype(np.uint8).tobytes()
                      for mip in mips)
    return DXGI_FORMAT_R8G8B8A8_UNORM, size, size, 1, len(mips), texels


CHECKER = _checker()
_VIEW = """cbuffer View : register(b0)
{
    // Clip space's x, y, z and w of a position in the world (x, y, z, 1), each the dot product with one.
    float4 clip_x, clip_y, clip_z, clip_w;
    // The view's x, y and z of a normal in the world likewise (w unused).
    float4 view_x, view_y, view_z;
    // The unit direction towards the light in the view; w whether a light shades each texel.
    float4 light;
    // The light's ambient and diffuse amounts.
    float4 shade;
    // The unit direction towards the light in the world; w whether a light shades each vertex.
    float4 vertex_light;
    // x the UVs' pattern shown instead of the colours: 0 none, 1 a checker, 2 the coordinates; y the checker's
    // squares across a UV unit.
    float4 pattern;
    // x whether light probes shade each vertex; y their exposure.
    float4 probes;
};

cbuffer Batch : register(b1)
{
    float4 tint;
    uint cutout;
    uint coloured;
};

bool finite(float value)
{
    return (asuint(value) & 0x7f800000u) != 0x7f800000u;
}
"""
_SKINNING = """StructuredBuffer<float4> palette : register(t1);

void influence(inout float3 moved, inout float3 turned, float4 at, float3 normal, uint bone, float weight)
{
    if (weight > 0) {
        float4 x = palette[bone * 3u], y = palette[bone * 3u + 1u], z = palette[bone * 3u + 2u];
        moved += float3(dot(x, at), dot(y, at), dot(z, at)) * weight;
        turned += float3(dot(x.xyz, normal), dot(y.xyz, normal), dot(z.xyz, normal)) * weight;
    }
}
"""


_PROBED = """    if (probes.x != 0)
        shown = float4(shown.rgb * probe_colour(placed.xyz, facing, probes.y), 1);
"""


def _vertex_source(groups: int, probed: bool) -> str:
    inputs = "".join(f",\n            uint4 bones{group} : REASY_STREAM{4 + group}, "
                     f"float4 weights{group} : REASY_STREAM{8 + group}" for group in range(groups))
    skinned = ""
    if groups:
        influences = "".join(
            f"    influence(moved, turned, at, normal, bones{group}[{lane}], weights{group}[{lane}]);\n"
            for group in range(groups) for lane in range(4))
        skinned = ("    precise float3 moved = 0, turned = 0;\n" + influences
                   + "    at = float4(moved, 1);\n    normal = turned;\n")
    return _VIEW + f"""
cbuffer Placed : register(b2)
{{
    // The world's x, y and z of the geometry's position (x, y, z, 1), and of its normal (w unused).
    float4 world_x, world_y, world_z;
    float4 turn_x, turn_y, turn_z;
}};
{_SKINNING if groups else ""}{_PROBES if probed else ""}
struct Corner
{{
    float4 position : SV_Position;
    float3 normal : NORMAL;
    float2 uv : UV;
    float4 colour : COLOUR;
    float valid : VALID;
}};

Corner main(float3 position : REASY_STREAM0, float3 normal : REASY_STREAM1, float2 uv : REASY_STREAM2,
            float4 colour : REASY_STREAM3{inputs})
{{
    Corner corner;
    float4 at = float4(position, 1);
{skinned}    // Positions the same in every variant of the shader: which of two surfaces as deep shows does not change.
    precise float4 placed = float4(dot(world_x, at), dot(world_y, at), dot(world_z, at), 1);
    float3 facing = float3(dot(turn_x.xyz, normal), dot(turn_y.xyz, normal), dot(turn_z.xyz, normal));
    precise float4 clip = float4(dot(clip_x, placed), dot(clip_y, placed), dot(clip_z, placed), dot(clip_w, placed));
    corner.position = clip;
    corner.normal = float3(dot(view_x.xyz, facing), dot(view_y.xyz, facing), dot(view_z.xyz, facing));
    corner.uv = uv;
    corner.valid = 1;
    if (pattern.x != 0) {{
        bool kept = finite(uv.x) && finite(uv.y);
        corner.uv = kept ? uv : 0;
        corner.valid = kept ? 1 : 0;
    }}
    float4 shown = coloured != 0u ? colour : 1;
    if (vertex_light.w != 0) {{
        float length2 = dot(facing, facing);
        float3 lit = length2 > 0 ? facing * rsqrt(length2) : 0;
        lit = finite(lit.x) && finite(lit.y) && finite(lit.z) ? lit : float3(0, 0, 1);
        shown = float4(shown.rgb * saturate(dot(lit, vertex_light.xyz) * shade.y + shade.x), 1);
    }}
{_PROBED if probed else ""}    corner.colour = shown;
    return corner;
}}
"""


_PIXEL = _VIEW + """
Texture2D<float4> image : register(t0);
SamplerState filter : register(s0);

float4 main(float4 position : SV_Position, float3 normal : NORMAL, float2 uv : UV, float4 colour : COLOUR,
            float valid : VALID) : SV_Target
{
    if (pattern.x != 0) {
        float2 at = uv * pattern.y;
        float2 width = max(fwidth(at), 0.0001f);
        // Each square wave's mean over the texel's footprint, so far squares fade to grey rather than shimmer.
        float2 mean = 2 * (abs(frac((at - 0.5f * width) * 0.5f) - 0.5f)
                           - abs(frac((at + 0.5f * width) * 0.5f) - 0.5f)) / width;
        float checker = saturate(0.5f - 0.5f * mean.x * mean.y);
        if (valid < 0.99999f)
            return float4(1, 0, 1, 1);
        return pattern.x == 2 ? float4(frac(uv), 0.25f, 1) : float4((float3)lerp(0.15f, 0.85f, checker), 1);
    }
    float4 base = tint * colour;
    if (light.w != 0)
        base.rgb = saturate(base.rgb * (shade.x + shade.y * max(dot(normalize(normal), light.xyz), 0)));
    float4 texel = base * image.Sample(filter, uv);
    if (cutout != 0u && !(texel.a > 0.5f))
        discard;
    return texel;
}
"""
_BACKGROUND_VERTEX = "float4 main(float2 position : REASY_STREAM0) : SV_Position { return float4(position, 0, 1); }\n"
_BACKGROUND_PIXEL = """cbuffer Background : register(b0)
{
    float4 colour;
};

float4 main() : SV_Target
{
    return colour;
}
"""
_COVERING = np.array([[-1, 1], [3, 1], [-1, -3]], "<f4").tobytes()
_GROUP = 8
_PACKING = f"""Texture2D<uint4> image : register(t0);
RWTexture2D<uint> shown : register(u0);

[numthreads({_GROUP}, {_GROUP}, 1)]
void main(uint3 id : SV_DispatchThreadID)
{{
    uint width, height;
    shown.GetDimensions(width, height);
    if (id.x >= width || id.y >= height)
        return;
    uint4 texel = image[id.xy];
    shown[id.xy] = texel.r | texel.g << 8 | texel.b << 16 | texel.a << 24;
}}
"""


@dataclass(frozen=True, slots=True)
class DrawStates:
    """``depth_function`` None: untested; ``blend`` None: replaces; ``channels`` 0: depth only. Values are D3D12's."""
    cull: int = CULL_BACK
    front_counter_clockwise: bool = True
    depth_function: int | None = NEARER
    depth_write: bool = True
    blend: tuple[int, int, int] | None = None
    channels: int = _COLOUR
    depth_bias: tuple[int, float, float] = (0, 0.0, 0.0)
    depth_clip: bool = True


BLENDED = (BLEND_SOURCE_ALPHA, BLEND_INVERSE_SOURCE_ALPHA, BLEND_OPERATION_ADD)


@dataclass(frozen=True, slots=True, eq=False)
class ViewGeometry:
    """``bones`` and ``weights`` (n x k) of skinned vertices."""
    positions: np.ndarray
    normals: np.ndarray
    uvs: np.ndarray | None = None
    colours: np.ndarray | None = None
    bones: np.ndarray | None = None
    weights: np.ndarray | None = None


@dataclass(frozen=True, slots=True, eq=False)
class ViewBatch:
    """``texture``: (format, width, height, layers, mips, texels), None for white; vertex colours multiply ``tint`` when
    ``coloured``."""
    indices: np.ndarray
    texture: tuple | None = None
    tint: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    cutout: bool = False
    states: DrawStates = DrawStates()
    object: int = 0
    coloured: bool = True


@dataclass(frozen=True, slots=True)
class ViewLight:
    """Lit per texel; ``direction`` points towards the light, in view space."""
    direction: tuple[float, float, float]
    ambient: float
    diffuse: float


@dataclass(frozen=True, slots=True)
class VertexLight:
    """``direction`` points towards the light, in the world."""
    direction: tuple[float, float, float]
    ambient: float
    diffuse: float


@dataclass(frozen=True, slots=True)
class UvPattern:
    """``density`` is checker squares per UV unit; ``coordinates`` shows the UV fractions instead."""
    density: float = 8.0
    coordinates: bool = False


@dataclass(slots=True)
class _Held:
    count: int
    textured: bool
    coloured: bool
    bones: int
    vertices: list[tuple[int, int]]
    elements: tuple[tuple, ...]


@dataclass(slots=True)
class _Object:
    """``placed``: placement buffer and bytes; ``palette``: bone buffer, None unskinned."""
    geometry: _Held
    placed: list
    palette: int | None


@lru_cache
def _vertex_shader(groups: int, probed: bool) -> bytes:
    return bytes(compile_hlsl_source(_vertex_source(groups, probed), entry_point="main", target="vs_5_0"))


@lru_cache
def _shaders() -> tuple[bytes, bytes, bytes, bytes]:
    return tuple(bytes(compile_hlsl_source(source, entry_point="main", target=target)) for source, target in (
        (_PIXEL, "ps_5_0"), (_BACKGROUND_VERTEX, "vs_5_0"), (_BACKGROUND_PIXEL, "ps_5_0"), (_PACKING, "cs_5_0")))


def _floats(values, width: int, count: int | None = None) -> np.ndarray:
    held = np.ascontiguousarray(values, "<f4").reshape(-1, width)
    if count is not None and len(held) != count:
        raise ValueError(f"the geometry has {count} vertices, a channel of it {len(held)}")
    return held


def _view_bytes(camera: Camera, light: ViewLight | VertexLight | ProbeLight | None,
                pattern: UvPattern | None) -> bytes:
    normal = np.zeros((3, 4))
    normal[:, :3] = np.linalg.inv(camera.view_matrix()[:3, :3]).T
    # Row 0: light direction in view space; row 1: in the world.
    towards = np.zeros((2, 4))
    shade, probes = np.zeros(4), np.zeros(4)
    if isinstance(light, ProbeLight):
        probes[:2] = 1, light.exposure
    elif light is not None:
        direction = np.asarray(light.direction, np.float64)
        length = np.sqrt((direction * direction).sum())
        if not length > 0:
            raise ValueError("the light's direction has no length")
        towards[int(isinstance(light, VertexLight))] = *(direction / length), 1
        shade[:2] = light.ambient, light.diffuse
    shown = np.zeros(4)
    if pattern is not None:
        if not pattern.density > 0:
            raise ValueError(f"a checker has squares, not {pattern.density}")
        shown[:2] = 2 if pattern.coordinates else 1, pattern.density
    clip = view_projection(camera)
    return np.concatenate([clip.T.reshape(-1), normal.reshape(-1), towards[0], shade, towards[1], shown,
                           probes]).astype("<f4").tobytes()


def _placed_bytes(world) -> bytes:
    world, = _rows(world)
    normal = np.zeros((3, 4))
    normal[:, :3] = np.linalg.inv(world[:, :3]).T
    return np.concatenate([world.reshape(-1), normal.reshape(-1)]).astype("<f4").tobytes()


class TexturesView:
    def __init__(self, device: Resident, size: Sequence[int], geometries: Sequence[ViewGeometry],
                 batches: Sequence[ViewBatch], *, objects: Sequence[int] | None = None, anisotropy: int = 1,
                 probes: ViewProbes | None = None):
        if not 1 <= anisotropy <= _MOST_ANISOTROPY:
            raise ValueError(f"anisotropy lies in [1, {_MOST_ANISOTROPY}], not {anisotropy}")
        self.device, self.size, self._probes = device, tuple(size), probes
        self._screen()
        self._sampler = ((_ANISOTROPIC if anisotropy > 1 else _LINEAR), _WRAP, _WRAP, _WRAP, 0.0, anisotropy, 1,
                         (0.0, 0.0, 0.0, 0.0), 0.0, 3.402823466e38)
        scene = device.scene
        self._view = [scene.buffer(bytes(192), 0), bytes(192)]
        self._background = [scene.buffer(bytes(16), 0), bytes(16)]
        self._geometries = [self._held(geometry) for geometry in geometries]
        self._objects = [self._object(geometry) for geometry in
                         (range(len(self._geometries)) if objects is None else objects)]
        self._given = list(batches)
        self._indices: dict[tuple[int, int], tuple[int, int]] = {}
        self._steps()

    def _screen(self) -> None:
        self._image = self.device.texture(DXGI_FORMAT_R8G8B8A8_TYPELESS, self.size, RENDER_TARGET)
        self.shown = self.device.texture(DXGI_FORMAT_R32_UINT, self.size, UNORDERED_ACCESS)
        resource, read, _stencil = DEPTH_VIEWS[DXGI_FORMAT_D32_FLOAT]
        self.depth = (read, self.device.texture(resource, self.size, DEPTH_STENCIL))

    def _steps(self) -> None:
        self._first = [self.device.scene.clear(DXGI_FORMAT_D32_FLOAT, self.depth[1]), self._background_step()]
        self._batches = [self._batch_step(batch, self._indices) for batch in self._given]
        self._last = self._packing_step()

    def resize(self, size: Sequence[int]) -> None:
        """New targets and steps; the geometries, objects and textures stay."""
        old = [self._image, self.shown, self.depth[1]]
        self.size = tuple(size)
        self._screen()
        self._steps()
        self.device.release(old)

    def add(self, geometries: Sequence[ViewGeometry], batches: Sequence[ViewBatch], *,
            objects: Sequence[int] | None = None) -> None:
        """Adds ``geometries`` and ``objects`` after its own; ``batches`` are all it draws, in order: those it drew
        before keep their steps."""
        first = len(self._geometries)
        self._geometries += [self._held(geometry) for geometry in geometries]
        self._objects += [self._object(geometry) for geometry in
                          (range(first, len(self._geometries)) if objects is None else objects)]
        recorded = {id(batch): step for batch, step in zip(self._given, self._batches)}
        self._given = list(batches)
        self._batches = [recorded[id(batch)] if id(batch) in recorded else self._batch_step(batch, self._indices)
                         for batch in self._given]

    def _held(self, geometry: ViewGeometry) -> _Held:
        positions = _floats(geometry.positions, 3)
        count = len(positions)
        if not count:
            raise ValueError("a geometry has no vertices")
        streams = [(positions.tobytes(), 12, (DXGI_FORMAT_R32G32B32_FLOAT,)),
                   (_floats(geometry.normals, 3, count).tobytes(), 12, (DXGI_FORMAT_R32G32B32_FLOAT,)),
                   (bytes(4 * count), 4, (DXGI_FORMAT_R16G16_UNORM,)) if geometry.uvs is None
                   else (_floats(geometry.uvs, 2, count).tobytes(), 8, (DXGI_FORMAT_R32G32_FLOAT,)),
                   (bytes([255]) * (4 * count), 4, (DXGI_FORMAT_R8G8B8A8_UNORM,)) if geometry.colours is None
                   else (_floats(geometry.colours, 4, count).tobytes(), 16, (DXGI_FORMAT_R32G32B32A32_FLOAT,))]
        elements = [("REASY_STREAM", slot, form, slot, 0, 0, 0)
                    for slot, (_data, _stride, (form,)) in enumerate(streams)]
        bones = 0
        if geometry.bones is not None:
            named, weighed, groups, bones = _influence_streams(geometry.bones, geometry.weights, count)
            streams += [(named, 8 * groups, (DXGI_FORMAT_R16G16B16A16_UINT,) * groups),
                        (weighed, 16 * groups, (DXGI_FORMAT_R32G32B32A32_FLOAT,) * groups)]
            elements += [("REASY_STREAM", 4 * slot + group, form, 3 + slot, group * step, 0, 0)
                         for slot, form, step in ((1, DXGI_FORMAT_R16G16B16A16_UINT, 8),
                                                  (2, DXGI_FORMAT_R32G32B32A32_FLOAT, 16))
                         for group in range(groups)]
        scene = self.device.scene
        # Own buffers, written in place: another geometry with the same bytes keeps its own.
        vertices = [(scene.buffer(data, 0, 1), stride) for data, stride, _forms in streams]
        return _Held(count, geometry.uvs is not None, geometry.colours is not None, bones, vertices, tuple(elements))

    def _object(self, geometry: int) -> _Object:
        if not 0 <= geometry < len(self._geometries):
            raise ValueError(f"an object is of geometry {geometry}, which the view has not")
        held = self._geometries[geometry]
        scene = self.device.scene
        placed = _placed_bytes(_IDENTITY)
        palette = _identity_palette(scene, held.bones)
        return _Object(held, [scene.buffer(placed, 0, 1), placed], palette)

    def _placed(self) -> dict:
        width, height = self.size
        return {"heap": [], "size": (width, height), "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0),
                "scissor": (0, 0, width, height), "topology": _TRIANGLE_LIST, "index_format": DXGI_FORMAT_R32_UINT}

    def _background_step(self) -> int:
        vertex, pixel = _shaders()[1:3]
        return self.device.scene.draw(self._placed() | {
            "vertex_shader": vertex, "pixel_shader": pixel,
            "vertex_buffers": [(self.device.buffer(_COVERING, 1), 8)],
            "elements": (("REASY_STREAM", 0, DXGI_FORMAT_R32G32_FLOAT, 0, 0, 0, 0),),
            "indices": self.device.buffer(np.arange(3, dtype="<u4").tobytes(), 1), "draw": (3, 1, 0, 0, 0),
            "rasterizer": _RASTERIZER, "depth_stencil": _NO_DEPTH_STENCIL, "blend": (0, 0, [_UNBLENDED]),
            "tables": [(RANGE_CBV, 0, 0, 1, 0, 5, ((0, self._background[0]),))],
            "targets": [(DXGI_FORMAT_R8G8B8A8_UNORM, self._image)], "depth": None})

    def _packing_step(self) -> int:
        width, height = self.size
        return self.device.scene.dispatch({
            "compute_shader": _shaders()[3],
            "tables": [(RANGE_SRV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R8G8B8A8_UINT, self._image),)),
                       (RANGE_UAV, 0, 0, 1, TEXTURE_2D, 0, ((0, DXGI_FORMAT_R32_UINT, self.shown),))],
            "heap": [], "groups": (-(-width // _GROUP), -(-height // _GROUP), 1)})

    def _batch_step(self, batch: ViewBatch, checked: dict[tuple[int, int], tuple[int, int]]) -> int:
        if not 0 <= batch.object < len(self._objects):
            raise ValueError(f"a batch is of object {batch.object}, which the view has not")
        placed = self._objects[batch.object]
        held = placed.geometry
        # Batches outlive this dict, so ids don't collide.
        key = id(batch.indices), held.count
        if key not in checked:
            indices = np.ascontiguousarray(batch.indices, "<u4").reshape(-1)
            if len(indices) % 3 or (indices >= held.count).any():
                raise ValueError("a batch's indices are not triangles of its geometry's vertices")
            checked[key] = self.device.buffer(indices.tobytes(), 1), len(indices)
        index_buffer, count = checked[key]
        states = batch.states
        tested = states.depth_function is not None
        blend = _UNBLENDED[:-1] if states.blend is None else (1, *states.blend, BLEND_ZERO, BLEND_ONE, BLEND_OPERATION_ADD)
        form, *shape, texels = batch.texture if held.textured and batch.texture is not None else _WHITE
        _index, form, texture = self.device.read(TEXTURE_2D, (0, _PLAIN.get(form, form), *shape, texels))
        tables = [(RANGE_CBV, 0, 0, 1, 0, 0, ((0, self._view[0]),)),
                  (RANGE_CBV, 1, 0, 1, 0, 0, ((0, self.device.buffer(
                      struct.pack("<4f2I8x", *batch.tint, int(batch.cutout), int(batch.coloured)), 1)),)),
                  (RANGE_CBV, 2, 0, 1, 0, 1, ((0, placed.placed[0]),)),
                  (RANGE_SRV, 0, 0, 1, TEXTURE_2D, 5, ((0, form, texture),)),
                  (RANGE_SAMPLER, 0, 0, 1, 0, 5, ((0, *self._sampler),))]
        if placed.palette is not None:
            tables.append((RANGE_SRV, 1, 0, 1, BUFFER, 1, ((0, 0, 16, placed.palette),)))
        if self._probes is not None:
            tables.append(self._probes.table(self.device, 1))
        return self.device.scene.draw(self._placed() | {
            "vertex_shader": _vertex_shader((len(held.elements) - 4) // 2, self._probes is not None),
            "pixel_shader": _shaders()[0],
            "vertex_buffers": held.vertices, "elements": held.elements,
            "indices": index_buffer, "draw": (count, 1, 0, 0, 0),
            "rasterizer": (_RASTERIZER[0], states.cull, int(states.front_counter_clockwise), *states.depth_bias,
                           int(states.depth_clip)),
            "depth_stencil": (int(tested), int(tested and states.depth_write),
                              states.depth_function if tested else _NO_DEPTH_STENCIL[2], *_NO_DEPTH_STENCIL[3:]),
            "blend": (0, 0, [(*blend, states.channels & _COLOUR)]), "tables": tables,
            "targets": [(DXGI_FORMAT_R8G8B8A8_UNORM, self._image)], "depth": (DXGI_FORMAT_D32_FLOAT, self.depth[1])})

    def close(self) -> None:
        self.device.close()

    def place(self, placed: int, world) -> None:
        """``world`` is 3 x 4."""
        _rewrite(self.device.scene, self._objects[placed].placed, _placed_bytes(world))

    def pose(self, posed: int, bones) -> None:
        """``bones`` are 3 x 4 each, at least one per bone named."""
        held = self._objects[posed]
        self.device.scene.write(held.palette, _palette_rows(held, posed, bones))

    def write(self, geometry: int, *, positions=None, normals=None, colours=None) -> None:
        held = self._geometries[geometry]
        if colours is not None and not held.coloured:
            raise ValueError(f"geometry {geometry} was made without colours")
        for slot, values, width in ((0, positions, 3), (1, normals, 3), (3, colours, 4)):
            if values is not None:
                self.device.scene.write(held.vertices[slot][0], _floats(values, width, held.count).tobytes())

    def frame(self, camera: Camera, light: ViewLight | VertexLight | ProbeLight | None, background: Sequence[int],
              batches: Sequence[int] | None = None, pattern: UvPattern | None = None) -> list[int]:
        if tuple(camera.size) != self.size:
            raise ValueError(f"the view is {self.size} pixels, the camera's {tuple(camera.size)}")
        if isinstance(light, ProbeLight) and light.probes is not self._probes:
            raise ValueError("the light's probes are not the view's")
        red, green, blue = (int(channel) for channel in background)
        if not all(0 <= channel <= 255 for channel in (red, green, blue)):
            raise ValueError(f"colour channels lie in [0, 255], not {background}")
        _rewrite(self.device.scene, self._view, _view_bytes(camera, light, pattern))
        _rewrite(self.device.scene, self._background, struct.pack("<4f", red / 255, green / 255, blue / 255, 1.0))
        drawn = self._batches if batches is None else [self._batches[batch] for batch in batches]
        return [*self._first, *drawn, self._last]


__all__ = ["AS_NEAR", "BLENDED", "CHECKER", "DrawStates", "EQUAL", "MOST_INFLUENCES",
           "NEARER", "ProbeLight", "TexturesView", "UvPattern", "VertexLight", "ViewBatch", "ViewGeometry", "ViewLight",
           "ViewProbes"]
