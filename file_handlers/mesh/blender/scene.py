from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

lazy from ..contract_utils import layer_payloads, layer_vertex_offset
from ..mesh_file import MPLY_MAGIC

LAYERS = ("main", "shadow", "occluder")
UV_PLANES = ("uv0", "uv1", "uv2")
WEIGHT_SETS = (("primary", ("skin_weights", "extra_skin_weights"), ""),
               ("additional", ("additional_vertex_weights",), "[additional] "),
               ("secondary", ("secondary_skin_weights",), "[secondary] "))


def layer_lods(mesh, layer):
    if layer == "main":
        return [(mi, li, lod) for mi, data in enumerate(mesh.meshes) for li, lod in enumerate(data.lods)]
    owner = getattr(mesh, f"{layer}_mesh", None)
    return [] if owner is None else [(0, li, lod) for li, lod in enumerate(owner.geometry_lods)]


@dataclass
class VertexDomains:
    payloads: list = field(default_factory=list)
    bases: list = field(default_factory=list)
    keys: list = field(default_factory=list)  # (layer, buffer index) that first named the payload
    index: dict = field(default_factory=dict)  # id(payload) -> domain number

    @classmethod
    def of(cls, mesh):
        domains = cls()
        total = 0
        for layer in LAYERS:
            for buffer_index, payload in sorted(layer_payloads(mesh, layer).items()):
                if id(payload) in domains.index:
                    continue
                domains.index[id(payload)] = len(domains.payloads)
                domains.payloads.append(payload)
                domains.keys.append((layer, int(buffer_index)))
                domains.bases.append(total)
                total += len(payload.positions) // 3
        domains.total = total
        return domains

    def base(self, payload):
        return self.bases[self.index[id(payload)]]

    def locate(self, ids):
        ids = np.asarray(ids, dtype=np.int64)
        domain = np.searchsorted(np.asarray(self.bases, dtype=np.int64), ids, side="right") - 1
        return domain, ids - np.asarray(self.bases, dtype=np.int64)[domain]


@dataclass
class SceneMesh:
    key: str
    domain: int
    gpu: np.ndarray            # global id of every source vertex in the object, in object order
    vertex_of_gpu: np.ndarray  # Blender vertex of each source vertex
    representative: np.ndarray # global id standing for each Blender vertex
    corners: np.ndarray        # source vertex (object order) of each corner
    face_sizes: np.ndarray
    # (body index, channel position, shape-key name, per-source-vertex deltas, target normals or None)
    channels: list = field(default_factory=list)

    @property
    def corner_vertices(self):
        return self.vertex_of_gpu[self.corners]

    @property
    def corner_ids(self):
        return self.gpu[self.corners]


@dataclass
class SceneObject:
    key: str
    name: str
    layer: str
    mesh_index: int
    lod: int
    group_index: int
    part_id: int
    draw: int          # submesh position in its group; -1 for merged meshlets
    material_index: int
    is_quad: bool
    mesh_key: str
    submeshes: tuple = ()
    alias_of: str = ""


@dataclass
class BlendScene:
    domains: VertexDomains
    objects: list
    meshes: dict
    meshlets: bool
    merge_seams: bool


def _material_name(mesh, index):
    names = mesh.material_names
    return names[index] if 0 <= index < len(names) else f"material_{index}"


def _draw_indices(payload, submesh):
    start, count = int(submesh.faces_index_offset), int(submesh.indices_count)
    values = payload.index_values
    if start < 0 or count < 0 or start + count > len(values):
        raise ValueError(f"Draw index range [{start}, {start + count}) leaves its buffer")
    return np.asarray(values[start:start + count], dtype=np.int64)


def _draw_vertices(mesh, layer, submesh, payload):
    base = layer_vertex_offset(mesh, layer, submesh)
    local = _draw_indices(payload, submesh) + base
    available = len(payload.positions) // 3
    count = int(getattr(submesh, "vert_count", 0))
    start, end = base, base + max(count, 0)
    if len(local):
        start, end = min(start, int(local.min())), max(end, int(local.max()) + 1)
    if start < 0 or end > available:
        raise ValueError(f"Draw vertex span [{start}, {end}) leaves its buffer")
    return start, end, local


def main_buffer_index(mesh, payload):
    return next((int(index) for index, candidate in mesh.mesh_buffer.buffer_payloads.items()
                 if candidate is payload), None)


def _channel_deltas(mesh, buffer_index, gpu_local):
    blend = mesh.blend_shape_data
    if buffer_index is None or blend is None or not len(gpu_local):
        return []
    low, high = int(gpu_local.min()), int(gpu_local.max()) + 1
    result = []
    for body_index, body in enumerate(blend.bodies):
        if body.alias_of_lod is not None:
            continue
        for channel_position, channel in enumerate(body.channels):
            delta = None
            normals = None
            for segment in channel.segments:
                if int(segment.buffer_index) != buffer_index:
                    continue
                first = int(segment.base_vertex_location)
                values = np.asarray(segment.position_deltas, dtype=np.float32).reshape(-1, 3)
                if first >= high or first + len(values) <= low:
                    continue
                rows = gpu_local - first
                inside = (rows >= 0) & (rows < len(values))
                if not inside.any():
                    continue
                if delta is None:
                    delta = np.zeros((len(gpu_local), 3), dtype=np.float32)
                delta[inside] += values[rows[inside]]
                if segment.target_normals is not None:
                    if normals is None:
                        normals = np.zeros((len(gpu_local), 3), dtype=np.float32)
                    normals[inside] = np.asarray(segment.target_normals, dtype=np.float32).reshape(-1, 3)[rows[inside]]
            if delta is not None:
                result.append((body_index, channel_position, channel, delta, normals))
    return result


def _weld(payload, gpu_local, used, extra_columns):
    count = len(gpu_local)
    columns = [np.asarray(payload.positions, dtype=np.float32).reshape(-1, 3)[gpu_local].view(np.uint32)]
    if len(payload.colors):
        columns.append(np.asarray(payload.colors, dtype=np.uint8).reshape(-1, 4)[gpu_local].astype(np.uint32))
    if len(payload.normal_ws):
        columns.append(np.asarray(payload.normal_ws, dtype=np.uint8)[gpu_local, None].astype(np.uint32))
    for _label, names, _prefix in WEIGHT_SETS:
        for name in names:
            stream = getattr(payload, name, None)
            if stream is None:
                continue
            width = stream.influence_count
            columns.append(np.asarray(stream.deform_indices, dtype=np.uint32).reshape(-1, width)[gpu_local])
            columns.append(np.asarray(stream.weights, dtype=np.float32).reshape(-1, width)[gpu_local].view(np.uint32))
    columns.extend(np.ascontiguousarray(values, dtype=np.float32).view(np.uint32) for values in extra_columns)
    keys = np.ascontiguousarray(np.concatenate(columns, axis=1))
    # Vertices without corners keep their own Blender vertex: point data is all they can carry.
    isolated = ~used
    keys = np.concatenate([keys, np.where(isolated, np.arange(count), -1).astype(np.int64)[:, None].view(np.uint32)
                           .reshape(count, 2)], axis=1)
    rows = keys.view(np.dtype((np.void, keys.dtype.itemsize * keys.shape[1]))).ravel()
    _unique, first, inverse = np.unique(rows, return_index=True, return_inverse=True)
    # Number Blender vertices in order of first appearance so unwelded objects stay in source order.
    order = np.argsort(first, kind="stable")
    rank = np.empty_like(order)
    rank[order] = np.arange(len(order))
    vertex_of_gpu = rank[inverse.ravel()]
    representative = np.sort(first)
    return vertex_of_gpu, representative


def build_scene(mesh, *, merge_seams=True):
    """``mesh`` must be fully loaded."""
    domains = VertexDomains.of(mesh)
    meshlets = mesh.header.magic == MPLY_MAGIC
    objects, meshes = [], {}
    seen_draws = {}
    for layer in LAYERS:
        payloads = layer_payloads(mesh, layer)
        prefix = "" if layer == "main" else layer.capitalize() + "_"
        for mesh_index, lod_index, lod in layer_lods(mesh, layer):
            for group_index, group in enumerate(lod.mesh_groups):
                part_id = int(group.group_id)
                if meshlets and layer == "main":
                    batches = {}
                    for submesh in group.submeshes:
                        batches.setdefault(int(submesh.material_index), []).append(submesh)
                    draws = [(-1, material, tuple(items), False) for material, items in batches.items()]
                else:
                    draws = [(index, int(s.material_index), (s,), bool(s.is_quad))
                             for index, s in enumerate(group.submeshes)]
                for draw, material, submeshes, is_quad in draws:
                    key = (f"{layer}.m{mesh_index}.l{lod_index}.g{group_index}." +
                           (f"mat{material}" if draw < 0 else f"d{draw}"))
                    name = (f"{prefix}LOD{lod_index}_P{part_id}_" + ("" if draw < 0 else f"D{draw}_") +
                            _material_name(mesh, material))
                    identity = tuple(id(s) for s in submeshes)
                    if identity in seen_draws:
                        alias = seen_draws[identity]
                        objects.append(SceneObject(key, name, layer, mesh_index, lod_index, group_index, part_id,
                                                   draw, material, is_quad, alias.mesh_key, submeshes, alias.key))
                        continue
                    payload = payloads[int(submeshes[0].buffer_index)]
                    scene_mesh = _scene_mesh(mesh, layer, key, submeshes, payload, domains, merge_seams or meshlets)
                    meshes[key] = scene_mesh
                    item = SceneObject(key, name, layer, mesh_index, lod_index, group_index, part_id, draw,
                                       material, is_quad, key, submeshes)
                    seen_draws[identity] = item
                    objects.append(item)
    return BlendScene(domains, objects, meshes, meshlets, merge_seams)


def _scene_mesh(mesh, layer, key, submeshes, payload, domains, weld):
    spans, corners, offset = [], [], 0
    for submesh in submeshes:
        start, end, local = _draw_vertices(mesh, layer, submesh, payload)
        spans.append(np.arange(start, end, dtype=np.int64))
        corners.append(local - start + offset)
        offset += end - start
    gpu_local = np.concatenate(spans) if spans else np.empty(0, dtype=np.int64)
    corners = np.concatenate(corners) if corners else np.empty(0, dtype=np.int64)
    width = 4 if submeshes and submeshes[0].is_quad else 3
    if len(corners) % width:
        raise ValueError(f"{key} has an incomplete primitive")
    face_sizes = np.full(len(corners) // width, width, dtype=np.int32)
    channels = _channel_deltas(mesh, main_buffer_index(mesh, payload), gpu_local)
    count = len(gpu_local)
    if weld and count:
        used = np.zeros(count, dtype=bool)
        used[corners] = True
        extra = [delta for *_ids, delta, _n in channels] + [n for *_ids, _d, n in channels if n is not None]
        vertex_of_gpu, representative = _weld(payload, gpu_local, used, extra)
    else:
        vertex_of_gpu = np.arange(count, dtype=np.int64)
        representative = np.arange(count, dtype=np.int64)
    base = domains.base(payload)
    names = _shape_key_names(channel for _b, _c, channel, _d, _n in channels)
    return SceneMesh(key, domains.index[id(payload)], gpu_local + base, vertex_of_gpu, gpu_local[representative] + base,
                     corners, face_sizes,
                     [(body, position, name, delta, normals)
                      for (body, position, _channel, delta, normals), name in zip(channels, names)])


def fit_name(name, used, limit=63):
    """Blender limits bone, vertex-group and shape-key names to 63 bytes."""
    data = name.encode("utf-8")
    candidate, number = data[:limit].decode("utf-8", "ignore"), 1
    while candidate in used or candidate == "Basis":
        number += 1
        suffix = f" #{number}"
        candidate = data[:limit - len(suffix)].decode("utf-8", "ignore") + suffix
    used.add(candidate)
    return candidate


def _shape_key_names(channels):
    used = set()
    return [fit_name(channel.name or f"channel_{channel.index}", used) for channel in channels]
