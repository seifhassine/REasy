"""See docs/internals/textures-view.md, Light probes."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

lazy from .material_preview import RANGE_SRV
lazy from .resident import BUFFER

# The captured probe shaders' walk visits at most this many tetrahedra.
MOST_STEPS = 199
FIRST_REGISTER = 2
_LAYER = np.dtype([("bias", "<f4", 3), ("intensity", "<f4"), ("inverse_cell", "<f4", 3), ("transformed", "<u4"),
                   ("dimensions", "<u4", 3), ("terms", "<u4"), ("strides", "<u4", 2), ("grid", "<u4", 2),
                   ("tetrahedra", "<u4", 2), ("probes", "<u4"), ("first_term", "<u4"), ("boxes", "<u4", 2),
                   ("unused", "<u4", 2)])
SOURCE = f"""
struct ProbeLayer
{{
    float3 bias;
    float intensity;
    float3 inverse_cell;
    uint transformed;
    uint3 dimensions;
    uint terms;
    uint x_stride, y_stride, first_entry, entries;
    uint first_tetrahedron, tetrahedra, first_probe, first_term;
    uint first_box, boxes;
    uint2 unused;
}};

StructuredBuffer<ProbeLayer> probe_layers : register(t{FIRST_REGISTER});
// Each box's rows: its three axes with its centre's x, y and z as w, then its extent.
StructuredBuffer<float4> probe_boxes : register(t{FIRST_REGISTER + 1});
StructuredBuffer<uint> probe_grid : register(t{FIRST_REGISTER + 2});
// Each tetrahedron's probes (numbered among every layer's), neighbours (its layer's), and transform's three rows.
StructuredBuffer<uint4> probe_corners : register(t{FIRST_REGISTER + 3});
StructuredBuffer<int4> probe_neighbours : register(t{FIRST_REGISTER + 4});
StructuredBuffer<float4> probe_transforms : register(t{FIRST_REGISTER + 5});
StructuredBuffer<float4> probe_positions : register(t{FIRST_REGISTER + 6});
// Each probe's terms in turn (red, green, blue).
StructuredBuffer<float4> probe_terms : register(t{FIRST_REGISTER + 7});

float probe_dot(float3 a, float3 b)
{{
    precise float sum = (a.x * b.x + a.y * b.y) + a.z * b.z;
    return sum;
}}

float3 probe_cross(float3 a, float3 b)
{{
    precise float3 crossed = float3(a.y * b.z - a.z * b.y, a.z * b.x - a.x * b.z, a.x * b.y - a.y * b.x);
    return crossed;
}}

bool probe_held(ProbeLayer layer, float3 at)
{{
    if (layer.boxes == 0)
        return true;
    for (uint box = layer.first_box; box < layer.first_box + layer.boxes; ++box) {{
        float4 x = probe_boxes[box * 4u], y = probe_boxes[box * 4u + 1u], z = probe_boxes[box * 4u + 2u];
        float3 extent = probe_boxes[box * 4u + 3u].xyz;
        precise float3 offset = at - float3(x.w, y.w, z.w);
        precise float3 local = float3(probe_dot(offset, x.xyz), probe_dot(offset, y.xyz), probe_dot(offset, z.xyz));
        precise float3 reach = extent + 1e-4f;
        if (all(abs(local) <= reach))
            return true;
    }}
    return false;
}}

// The tetrahedron (its layer's number) of the grid cell holding ``at``, or -1.
int probe_start(ProbeLayer layer, float3 at)
{{
    precise float3 cell = floor(at * layer.inverse_cell + layer.bias);
    if (!(all(cell >= 0) && all(cell < (float3)layer.dimensions)))
        return -1;
    uint3 whole = (uint3)cell;
    uint entry = whole.x * layer.x_stride + whole.y * layer.y_stride + whole.z;
    if (entry >= layer.entries)
        return -1;
    uint found = probe_grid[layer.first_entry + entry];
    return found == 0xffffffffu || found >= layer.tetrahedra ? -1 : (int)found;
}}

float4 probe_weights(ProbeLayer layer, uint tetrahedron, float3 at)
{{
    uint first = layer.first_tetrahedron + tetrahedron;
    precise float3 b;
    if (layer.transformed != 0u) {{
        float4 x = probe_transforms[first * 3u], y = probe_transforms[first * 3u + 1u];
        float4 z = probe_transforms[first * 3u + 2u];
        b = float3(probe_dot(x.xyz, at - float3(x.w, y.w, z.w)), probe_dot(y.xyz, at - float3(x.w, y.w, z.w)),
                   probe_dot(z.xyz, at - float3(x.w, y.w, z.w)));
    }} else {{
        uint4 corners = probe_corners[first];
        float3 last = probe_positions[corners.w].xyz;
        precise float3 edge0 = probe_positions[corners.x].xyz - last, edge1 = probe_positions[corners.y].xyz - last;
        precise float3 edge2 = probe_positions[corners.z].xyz - last, offset = at - last;
        precise float3 across = probe_cross(edge1, edge2);
        precise float denominator = probe_dot(edge0, across);
        b = float3(probe_dot(offset, across), probe_dot(offset, probe_cross(edge2, edge0)),
                   probe_dot(offset, probe_cross(edge0, edge1))) / denominator;
    }}
    precise float rest = ((1.0f - b.x) - b.y) - b.z;
    return float4(b, rest);
}}

// Unsigned integers ordered as the floats of their bits are, those not a number left aside.
uint probe_order(uint bits)
{{
    return (bits & 0x80000000u) != 0u ? ~bits : bits | 0x80000000u;
}}

// Whether ``weights`` put the position outside its tetrahedron, and the corner it leaves by.
bool probe_outside(float4 weights, out uint corner)
{{
    uint4 bits = (asuint(weights) & 0xfffffffcu) | uint4(0u, 1u, 2u, 3u);
    for (corner = 0u; corner < 4u; ++corner)
        if ((bits[corner] & 0x7fffffffu) > 0x7f800000u)
            return true;
    corner = 0u;
    for (uint other = 1u; other < 4u; ++other)
        if (probe_order(bits[other]) < probe_order(bits[corner]))
            corner = other;
    return (bits[corner] & 0x80000000u) != 0u && (bits[corner] & 0x7fffffffu) != 0u;
}}

float3 probe_unit(float3 normal)
{{
    precise float length = sqrt(probe_dot(normal, normal));
    precise float3 unit = length > 1e-6f ? normal / length : 0;
    bool finite = all((asuint(unit) & 0x7f800000u) != 0x7f800000u);
    return !finite || length <= 1e-6f ? float3(0, 1, 0) : unit;
}}

// The three terms ``unit`` weighs and their weights, of an ambient cube.
void probe_cube(float3 unit, out uint3 terms, out float3 weights)
{{
    float3 axes = unit.xzy;
    terms = uint3(axes.x < 0 ? 3u : 0u, axes.y < 0 ? 4u : 1u, axes.z < 0 ? 5u : 2u);
    precise float3 squared = axes * axes;
    weights = squared;
}}

// The three terms ``unit`` weighs and their weights, of an icosahedron.
void probe_icosahedron(float3 unit, out uint3 terms, out float3 weights)
{{
    float sx = unit.x < 0 ? -1.0f : 1.0f, sy = unit.y < 0 ? -1.0f : 1.0f, sz = unit.z < 0 ? -1.0f : 1.0f;
    uint px = sx > 0 ? 1u : 0u, py = sy > 0 ? 1u : 0u, pz = sz > 0 ? 1u : 0u;
    precise float sx_mid = sx * 0.525731086730957f, sy_mid = sy * 0.525731086730957f;
    precise float sz_mid = sz * 0.525731086730957f;
    precise float sx_edge = sx * 0.850651f, sy_edge = sy * 0.850651f, sz_edge = sz * 0.850651f;
    float3 size = abs(unit);
    bool xy = probe_dot(size, float3(1.0f, 0.381966f, -0.618034f)) > 0;
    bool yz = probe_dot(size, float3(-0.618034f, 1.0f, 0.381966f)) > 0;
    bool zx = probe_dot(size, float3(0.381966f, -0.618034f, 1.0f)) > 0;
    float3 v0 = xy ? float3(sx_edge, sy_mid, 0) : float3(-sx_mid, 0, sz_edge);
    float3 v1 = yz ? float3(0, sy_edge, sz_mid) : float3(sx_edge, -sy_mid, 0);
    float3 v2 = zx ? float3(sx_mid, 0, sz_edge) : float3(0, sy_edge, -sz_mid);
    terms = uint3(xy ? (1u - py) * 2u + (1u - px) : px + 8u + (1u - pz) * 2u,
                  yz ? 5u - py + (1u - pz) * 2u : py * 2u + (1u - px),
                  zx ? 9u - px + (1u - pz) * 2u : 5u - py + pz * 2u);
    terms = min(terms, 11u);
    precise float3 axis1 = v1 - (v2 + v0) * 0.5f, axis2 = v2 - (v1 + v0) * 0.5f;
    precise float numerator1 = probe_dot(unit - v0, axis1), denominator1 = probe_dot(v1 - v0, axis1);
    precise float numerator2 = probe_dot(unit - v0, axis2), denominator2 = probe_dot(v2 - v0, axis2);
    precise float weight1 = saturate(abs(denominator1) > 1e-8f ? numerator1 / denominator1 : 0);
    precise float weight2 = saturate(abs(denominator2) > 1e-8f ? numerator2 / denominator2 : 0);
    precise float weight0 = saturate((1.0f - weight1) - weight2);
    precise float sum = (weight0 + weight1) + weight2;
    weights = sum <= 1e-6f ? float3(1, 0, 0) : float3(weight0, weight1, weight2);
}}

// The colour the probes give a vertex at ``at`` in the world of ``normal`` there, by ``exposure``.
float3 probe_colour(float3 at, float3 normal, float exposure)
{{
    uint layers, stride;
    probe_layers.GetDimensions(layers, stride);
    for (uint number = 0u; number < layers; ++number) {{
        ProbeLayer layer = probe_layers[number];
        if (!probe_held(layer, at))
            continue;
        int start = probe_start(layer, at);
        if (start < 0)
            return float3(0.05f, 0.05f, 0.06f);
        uint tetrahedron = (uint)start, corner;
        float4 weights = 0;
        for (uint step = 0u; step < {MOST_STEPS}u; ++step) {{
            weights = probe_weights(layer, tetrahedron, at);
            if (!probe_outside(weights, corner))
                break;
            int next = probe_neighbours[layer.first_tetrahedron + tetrahedron][corner];
            if (next < 0 || (uint)next >= layer.tetrahedra)
                return float3(0.05f, 0.05f, 0.06f);
            tetrahedron = (uint)next;
        }}
        float3 unit = probe_unit(normal);
        uint3 terms;
        float3 term_weights;
        if (layer.terms == 6u)
            probe_cube(unit, terms, term_weights);
        else
            probe_icosahedron(unit, terms, term_weights);
        uint4 corners = probe_corners[layer.first_tetrahedron + tetrahedron] - layer.first_probe;
        precise float3 rgb = 0;
        [unroll] for (uint term = 0u; term < 3u; ++term) {{
            precise float3 blended = 0;
            [unroll] for (uint at_corner = 0u; at_corner < 4u; ++at_corner) {{
                uint found = layer.first_term + corners[at_corner] * layer.terms + terms[term];
                blended = at_corner == 0u ? probe_terms[found].rgb * weights[0]
                                          : blended + probe_terms[found].rgb * weights[at_corner];
            }}
            rgb = term == 0u ? blended * term_weights[0] : rgb + blended * term_weights[term];
        }}
        precise float scale = exposure * max(layer.intensity, 0.0f);
        precise float3 mapped = saturate(1.0f - exp(-max(rgb, 0) * scale));
        return pow(mapped, 1.0f / 2.2f);
    }}
    return 0;
}}
"""


@dataclass(frozen=True, slots=True, eq=False)
class ViewProbes:
    """``of`` takes the layers in the order the shader tries them."""
    buffers: tuple[tuple[bytes, int], ...]

    @classmethod
    def of(cls, layers: Sequence) -> ViewProbes:
        if not layers:
            raise ValueError("light probes are one layer at least")
        records = np.zeros(len(layers), _LAYER)
        boxes, grid, corners, neighbours, transforms, positions, terms = [], [], [], [], [], [], []
        counts = np.zeros(5, np.int64)
        for record, layer in zip(records, layers, strict=True):
            probes = layer.probe_set
            tetrahedra = int(probes.tetrahedron_count)
            held = np.asarray(probes.terms_rgb, np.float32)
            record["bias"], record["inverse_cell"] = probes.grid_bias, probes.inv_cell_size
            record["intensity"] = layer.intensity
            record["transformed"] = probes.tetra_transforms is not None
            record["dimensions"], record["terms"] = probes.grid_dimensions, held.shape[1]
            record["strides"] = probes.linear_x_stride, probes.linear_y_stride
            record["grid"] = counts[1], len(probes.grid_indices)
            record["tetrahedra"], record["probes"] = (counts[2], tetrahedra), counts[3]
            record["first_term"], record["boxes"] = counts[4], (counts[0], len(layer.boxes))
            for box in layer.boxes:
                rows = np.zeros((4, 4), np.float32)
                rows[:3, :3], rows[:3, 3], rows[3, :3] = box.axes, box.center, box.extent
                boxes.append(rows)
            grid.append(np.asarray(probes.grid_indices, np.uint32))
            corners.append(np.asarray(probes.tetra_probe_indices, np.uint32).reshape(-1, 4) + np.uint32(counts[3]))
            neighbours.append(np.asarray(probes.tetra_neighbors, np.int32).reshape(-1, 4))
            transforms.append(np.zeros((tetrahedra, 12), np.float32) if probes.tetra_transforms is None
                              else np.asarray(probes.tetra_transforms, np.float32).reshape(-1, 12))
            at = np.zeros((len(probes.probe_positions), 4), np.float32)
            at[:, :3] = probes.probe_positions
            positions.append(at)
            padded = np.zeros((*held.shape[:2], 4), np.float32)
            padded[..., :3] = held
            terms.append(padded.reshape(-1, 4))
            counts += len(layer.boxes), len(probes.grid_indices), tetrahedra, len(at), len(padded.reshape(-1, 4))

        def joined(chunks: list, width: int, kind) -> bytes:
            # A buffer holds one element at least: a layer may have no boxes, grid or tetrahedra.
            return (np.concatenate(chunks) if chunks else np.zeros(width, kind)).astype(kind).tobytes()
        return cls(((records.tobytes(), _LAYER.itemsize),
                    (joined(boxes, 16, "<f4"), 16), (joined(grid, 1, "<u4"), 4), (joined(corners, 4, "<u4"), 16),
                    (joined(neighbours, 4, "<i4"), 16), (joined(transforms, 12, "<f4"), 16),
                    (joined(positions, 4, "<f4"), 16), (joined(terms, 4, "<f4"), 16)))

    def table(self, device, visibility: int) -> tuple:
        return (RANGE_SRV, FIRST_REGISTER, 0, len(self.buffers), BUFFER, visibility,
                tuple((index, 0, stride, device.buffer(data)) for index, (data, stride) in enumerate(self.buffers)))


@dataclass(frozen=True, slots=True)
class ProbeLight:
    probes: ViewProbes
    exposure: float


__all__ = ["FIRST_REGISTER", "MOST_STEPS", "ProbeLight", "SOURCE", "ViewProbes"]
