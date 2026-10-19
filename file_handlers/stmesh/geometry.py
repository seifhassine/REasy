"""See docs/internals/speedtree.md, Decoding."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .meshlets import MeshletChunk
from .stmesh_file import BatchType, Geometry, StmeshFile

# The shaders' constants, as float32: 255/256 and the golden angle.
_Z_TOP = np.float32(0.99609375)
_GOLDEN = np.float32(2.3999631404876709)


def fibonacci(index) -> np.ndarray:
    b = np.asarray(index, np.float32)
    z = _Z_TOP - b * np.float32(1 / 128)
    r = np.sqrt(np.maximum(np.float32(1) - z * z, 0)).astype(np.float32)
    angle = (b * _GOLDEN).astype(np.float32)
    return np.stack([np.cos(angle) * r, np.sin(angle) * r, z], axis=-1).astype(np.float32)


@dataclass(frozen=True, slots=True, eq=False)
class DecodedVertices:
    """Tangents are (xyz, handedness); colors are RGBA bytes."""
    positions: np.ndarray
    texcoords: np.ndarray
    normals: np.ndarray
    tangents: np.ndarray
    colors: np.ndarray

    @property
    def count(self) -> int:
        return len(self.positions)


def decode_geometry(geometry: Geometry, signed: bool) -> DecodedVertices:
    slot0 = geometry.stream(0)
    if "texcoord" in slot0.dtype.names:  # RE4: float3 position, half2 texcoord
        positions = slot0["position"].astype(np.float32)
        texcoords = slot0["texcoord"].astype(np.float32)
    else:
        positions = slot0["position"][:, :3].astype(np.float32)
        texcoords = np.stack([slot0["position"][:, 3], slot0["lod_position"][:, 3]], axis=-1).astype(np.float32)
    directions = geometry.stream(1)["directions"]
    normal, binormal, tangent = (fibonacci(directions[:, index]) for index in (0, 1, 2))
    sign = (np.where(np.einsum("ij,ij->i", np.cross(normal, tangent), binormal) > 0, 1, -1) if signed
            else np.ones(len(normal))).astype(np.float32)
    colors = geometry.stream(2)["color"] if any(stream.slot == 2 for stream in geometry.streams) \
        else np.full((len(positions), 4), 255, np.uint8)
    return DecodedVertices(positions, texcoords, normal, np.concatenate([tangent, sign[:, None]], axis=1), colors)


def decode_billboards(geometry: Geometry) -> DecodedVertices:
    """Left as stored: the game's shaders turn billboards to the camera."""
    slot0 = geometry.stream(0)
    positions = slot0["position"][:, :3].astype(np.float32)
    count = len(positions)
    normals = np.tile(np.float32([0, 0, 1]), (count, 1))
    tangents = np.tile(np.float32([1, 0, 0, 1]), (count, 1))
    colors = geometry.stream(1)["color"] if any(stream.slot == 1 for stream in geometry.streams) \
        else np.full((count, 4), 255, np.uint8)
    return DecodedVertices(positions, slot0["texcoord"].astype(np.float32), normals, tangents, colors)


def _dequantize(values: np.ndarray, level: int) -> np.ndarray:
    if level == 0:
        return values.astype(np.float32) - np.float32(32768)
    if level == 1:
        words = values.astype(np.uint32)
        return np.stack([(words >> shift) & 1023 for shift in (0, 10, 20)], axis=-1).astype(np.float32) - 512
    return values.astype(np.float32) - np.float32(128)


def chunk_origin(chunk: MeshletChunk) -> np.ndarray:
    scale = np.float32(2.0) ** np.float32(((chunk.flags >> 16) & 0xff) - 143)
    offset = (np.float32(chunk.relative_bounds[0::2]) - np.float32(32768)) * scale
    return np.float32(chunk.center) + offset


def decode_chunk(chunk: MeshletChunk) -> DecodedVertices:
    count = chunk.vertex_count
    scale = np.float32(2.0) ** np.float32((chunk.flags >> 24) - 143)
    positions = chunk_origin(chunk) + _dequantize(chunk.positions, chunk.position_level) * scale
    texcoords = np.broadcast_to(chunk.texcoords.astype(np.float32), (count, 2)).copy()
    normals = fibonacci(chunk.directions[:, 0])
    colors = np.broadcast_to(chunk.colors, (count, 4)).copy()
    tangents = np.zeros((count, 4), np.float32)
    return DecodedVertices(positions.astype(np.float32), texcoords, normals, tangents, colors)


def meshlet_material_order(names) -> list[int]:
    """Meshlet material ids number the material names in sorted order."""
    return sorted(range(len(names)), key=lambda index: names[index])


def draws(mesh: StmeshFile):
    if mesh.meshlets is not None:
        order = meshlet_material_order(mesh.material_names)
        for lod, entry in enumerate(mesh.meshlets.lods):
            for number, chunk in enumerate(entry.chunks):
                yield lod, order[chunk.material_id], None, ("chunk", lod, number), chunk.triangles.astype(np.int64)
        return
    for lod, entry in enumerate(mesh.lods):
        for cluster in entry.clusters:
            key = "billboards" if cluster.batch_type == BatchType.BILLBOARDS else "3d"
            drawn = mesh.indices[cluster.start_index:cluster.start_index + cluster.index_count].astype(np.int64)
            yield lod, cluster.material_id, cluster.batch_type, key, (drawn + cluster.base_vertex).reshape(-1, 3)


__all__ = ["DecodedVertices", "chunk_origin", "decode_billboards", "decode_chunk", "decode_geometry", "draws",
           "fibonacci", "meshlet_material_order"]
