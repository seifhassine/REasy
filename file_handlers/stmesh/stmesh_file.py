"""See docs/internals/speedtree.md."""
from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

from .meshlets import MeshletError, StmeshMeshlets, read_meshlets
from .wind import SIZE as WIND_SIZE, WindConfig

MAGIC = b"STM8"
FLAG_RAY_TRACING = 0x02
FLAG_BVH = 0x04
FLAG_SDF_TEXTURE = 0x08
FLAG_BILLBOARD_LOD = 0x10
FLAG_MESHLETS = 0x20
HEADER = 16
CLUSTER = 0x20
STREAMS = 8


class StmeshError(ValueError):
    pass


class BufferLayout(IntEnum):
    """Values are the buffer chunk sizes: EARLY in RE4/SF6, LATE in later builds."""
    EARLY = 0xe0
    LATE = 0x210

    @property
    def fixed_block(self) -> int:
        return 0x470 if self is BufferLayout.EARLY else 0x480

    @property
    def cluster_material(self) -> int:
        return 0x10 if self is BufferLayout.EARLY else 0x1c

    @property
    def fields(self) -> tuple[int, ...]:
        if self is BufferLayout.EARLY:
            return 0x10, 0x30, 0x50, 0x70, 0x90, 0xb0, 0xd0, 0xd8
        return 0x10, 0x30, 0x130, 0x150, 0x170, 0x190, 0x1b0, 0x1b8


MESHLET_SPAN = 0x1c0
# How a scene places a SpeedTree mesh: as a foliage (``.fol``) instance or a ``SpeedTreeV8`` component's mesh.
FOLIAGE, COMPONENT = "foliage", "component"
PLACEMENTS = (FOLIAGE, COMPONENT)


class BatchType(IntEnum):
    """``via.speedtree.BatchType``."""
    BRANCH = 0
    LEAF_TWO_SIDE = 1
    LEAF = 2
    BILLBOARDS = 3
    GRASS = 4
    BRANCH_TWO_SIDE = 5
    FACING_LEAF = 6
    FACING_LEAF_TWO_SIDE = 7


class BillboardFaceCrossFadeThreshold(IntEnum):
    """``via.speedtree.BillboardFaceCrossFadeThresholdType``."""
    LOOSE = 0
    TIGHT = 1


@dataclass(frozen=True, slots=True)
class LodProfile:
    """``via::speedtree::LodProfile``: camera distances (m)."""
    high_detail_3d_distance: float
    range_3d: float
    billboard_start_distance: float
    billboard_range: float
    low_detail_3d_distance: float
    billboard_final_distance: float
    billboard_cull_distance: float
    lod_is_present: bool
    near_fade_start: float
    near_fade_range: float
    far_fade_start: float
    far_fade_range: float
    power: float

    FORMAT = "<7f?3x5f12x"

    def pack(self) -> bytes:
        return struct.pack(self.FORMAT, *(getattr(self, name) for name in self.__dataclass_fields__))


@dataclass(frozen=True, slots=True)
class MaterialCluster:
    """``MaterialClusterChunk``; ``bvh_size`` in later builds only."""
    index_count: int
    start_index: int
    base_vertex: int
    bvh_offset: int
    bvh_size: int
    material_id: int
    batch_type: int


@dataclass(frozen=True, slots=True)
class Lod:
    """``LodChunk``."""
    factor: float
    clusters: tuple[MaterialCluster, ...]


STREAM_DTYPES = {
    ("3d", 0, 16): np.dtype([("position", "<f2", (4,)), ("lod_position", "<f2", (4,))]),
    ("3d", 0, 24): np.dtype([("position", "<f4", (3,)), ("lod_position", "<f2", (4,)), ("texcoord", "<f2", (2,))]),
    ("3d", 1, 8): np.dtype([("directions", "u1", (4,)), ("wind", "u1", (4,))]),
    ("3d", 2, 4): np.dtype([("color", "u1", (4,))]),
    ("billboard", 0, 12): np.dtype([("position", "<f2", (4,)), ("texcoord", "<f2", (2,))]),
    ("billboard", 1, 4): np.dtype([("color", "u1", (4,))]),
}


@dataclass(frozen=True, slots=True, eq=False)
class VertexStream:
    slot: int
    data: np.ndarray

    @property
    def stride(self) -> int:
        return self.data.dtype.itemsize


@dataclass(frozen=True, slots=True, eq=False)
class Geometry:
    streams: tuple[VertexStream, ...]

    @property
    def vertex_count(self) -> int:
        return len(self.streams[0].data)

    def stream(self, slot: int) -> np.ndarray:
        return next(stream.data for stream in self.streams if stream.slot == slot)

    def stream_bytes(self) -> dict[int, tuple[bytes, int]]:
        return {stream.slot: (stream.data.tobytes(), stream.stride) for stream in self.streams}


@dataclass(frozen=True, slots=True, eq=False)
class StmeshFile:
    """``unknown_45a`` holds +0x45a as stored; ``exporter_460`` the four bytes RE4 and SF6 leave at +0x460."""
    layout: BufferLayout
    lod_profile: LodProfile
    flags: int
    total_vertex_count: int
    bounds: tuple[tuple[float, float, float], tuple[float, float, float]]
    lods: tuple[Lod, ...]
    material_names: tuple[str, ...]
    wind: WindConfig
    vortexel_wind_sample_offset: tuple[float, float, float]
    billboard_side_view_count: int
    unknown_45a: int
    billboard_cross_fade: BillboardFaceCrossFadeThreshold
    lod_bvh_sources: int
    lod_transition_mask: int
    exporter_460: int
    sdf_texture_path: str | None
    geometry: Geometry | None
    billboards: Geometry | None
    indices: np.ndarray
    group_pivots: np.ndarray
    meshlets: StmeshMeshlets | None

    @property
    def is_meshlet(self) -> bool:
        return bool(self.flags & FLAG_MESHLETS)

    @property
    def lod_count(self) -> int:
        return len(self.meshlets.lods) if self.meshlets is not None else len(self.lods)


def _utf16(block: bytes, at: int) -> str:
    end = at
    while block[end:end + 2] != b"\0\0":
        end += 2
        if end >= len(block):
            raise StmeshError("a stmesh's string runs past its runtime block")
    return block[at:end].decode("utf-16-le")


def _offset(block: bytes, at: int) -> int:
    value = struct.unpack_from("<Q", block, at)[0]
    if value >= len(block):
        raise StmeshError("a stmesh's runtime block points past its end")
    return value


def _geometry(kind: str, data: bytes, vertex_at: int, chunk: bytes, at_slots: int, at_offsets: int,
              at_strides: int, count: int, span_end: int) -> tuple[Geometry | None, int]:
    if not count:
        return None, span_end
    if count > STREAMS:
        raise StmeshError(f"a stmesh has {count} vertex streams, more than {STREAMS}")
    slots = struct.unpack_from(f"<{count}I", chunk, at_slots)
    offsets = struct.unpack_from(f"<{count}I", chunk, at_offsets)
    strides = struct.unpack_from(f"<{count}I", chunk, at_strides)
    ends = (*offsets[1:], span_end)
    counts = {(end - offset) // stride if stride and (end - offset) % stride == 0 else None
              for offset, end, stride in zip(offsets, ends, strides)}
    if len(counts) != 1 or None in counts:
        raise StmeshError("a stmesh's vertex streams do not hold the same number of vertices")
    vertices = counts.pop()
    streams = []
    for slot, offset, stride in zip(slots, offsets, strides):
        dtype = STREAM_DTYPES.get((kind, slot, stride))
        if dtype is None:
            raise StmeshError(f"a stmesh's {kind} slot {slot} stream of stride {stride} has no known layout")
        streams.append(VertexStream(slot, np.frombuffer(data, dtype, vertices, vertex_at + offset).copy()))
    return Geometry(tuple(streams)), offsets[0]


def read_stmesh(data: bytes, streaming: bytes | None = None) -> StmeshFile:
    if len(data) < HEADER or data[:4] != MAGIC:
        raise StmeshError("not a SpeedTree mesh")
    reserved, runtime, buffers = struct.unpack_from("<III", data, 4)
    # Layout unknown yet: the block holds at least the smaller fixed part.
    if runtime != HEADER or buffers < runtime + BufferLayout.EARLY.fixed_block or buffers > len(data):
        raise StmeshError("a stmesh's chunks are not where its header says")
    if reserved:
        raise StmeshError("a stmesh's reserved header word is not zero")
    block = data[runtime:buffers]
    profile = LodProfile(*struct.unpack_from(LodProfile.FORMAT, block, 0))
    flags, lod_count, name_count = block[0x40], block[0x41], block[0x42]
    total_vertex_count = struct.unpack_from("<I", block, 0x44)[0]
    low, high = struct.unpack_from("<3f", block, 0x48), struct.unpack_from("<3f", block, 0x54)
    meshlet = bool(flags & FLAG_MESHLETS)
    layout = (BufferLayout.LATE if meshlet or buffers + BufferLayout.LATE == struct.unpack_from("<I", data, buffers + 8)[0]
              else BufferLayout.EARLY)
    if buffers + layout > len(data):
        raise StmeshError("a stmesh's buffer chunk runs past its end")
    chunk = data[buffers:buffers + layout]
    material_at = layout.cluster_material
    # Meshlet files have no LOD table.
    table = struct.unpack_from("<Q", block, 0x60)[0] and _offset(block, 0x60)
    lods = []
    for number in range(lod_count if table else 0):
        at = _offset(block, table + 8 * number)
        count, factor = block[at], struct.unpack_from("<f", block, at + 4)[0]
        clusters = []
        for cluster in range(count):
            start = at + 0x10 + CLUSTER * cluster
            bvh_size = struct.unpack_from("<I", block, start + 0x10)[0] if layout is BufferLayout.LATE else 0
            clusters.append(MaterialCluster(*struct.unpack_from("<4I", block, start), bvh_size,
                                            block[start + material_at], block[start + material_at + 1]))
        lods.append(Lod(factor, tuple(clusters)))
    names_at = _offset(block, 0x68)
    names = tuple(_utf16(block, _offset(block, names_at + 8 * number)) for number in range(name_count))
    if any(cluster.material_id >= name_count for lod in lods for cluster in lod.clusters):
        raise StmeshError("a stmesh's cluster names a material it does not have")
    if block[0x459]:
        raise StmeshError(f"a stmesh with {block[0x459]} high meshes is not read")
    if block[0x45b] & ~1:
        raise StmeshError("a stmesh's +0x45b holds bits past the cross fade threshold")
    sdf_texture = _utf16(block, _offset(block, 0x460)) if flags & FLAG_SDF_TEXTURE else None
    exporter_460 = struct.unpack_from("<I", block, 0x460)[0] if layout is BufferLayout.EARLY else 0
    wind = WindConfig.read(block[0x80:0x80 + WIND_SIZE])

    vertex_size, index_size, vertex_at, index_at = struct.unpack_from("<4I", chunk)
    if vertex_at + vertex_size > len(data) or index_at + index_size > len(data) or index_size % 2:
        raise StmeshError("a stmesh's vertex or index span runs past its end")
    indices = np.frombuffer(data, "<u2", index_size // 2, index_at).copy() if index_size else np.zeros(0, "<u2")
    at_slots, at_offsets, at_strides, at_bslots, at_boffsets, at_bstrides, at_counts, at_pivots = layout.fields
    count, billboard_count = struct.unpack_from("<2I", chunk, at_counts)
    billboard_start = struct.unpack_from("<I", chunk, at_boffsets)[0] if billboard_count else vertex_size
    billboards, _ = _geometry("billboard", data, vertex_at, chunk, at_bslots, at_boffsets, at_bstrides,
                              billboard_count, vertex_size)
    geometry, first = _geometry("3d", data, vertex_at, chunk, at_slots, at_offsets, at_strides, count, billboard_start)
    if geometry is not None and first:
        raise StmeshError("a stmesh's 3D vertex streams do not begin its vertex span")
    pivot_at, pivot_count = struct.unpack_from("<2I", chunk, at_pivots)
    if pivot_at + 12 * pivot_count > len(data):
        raise StmeshError("a stmesh's group pivots run past its end")
    pivots = (np.frombuffer(data, "<f4", 3 * pivot_count, pivot_at).reshape(-1, 3).copy() if pivot_count
              else np.zeros((0, 3), "<f4"))

    meshlets = None
    if meshlet:
        blob_at, blob_size = struct.unpack_from("<2I", chunk, MESHLET_SPAN)
        try:
            meshlets = read_meshlets(block, data, blob_at, blob_size, streaming)
        except MeshletError as error:
            raise StmeshError(str(error)) from error
        if lod_count != len(meshlets.lods):
            raise StmeshError("a stmesh's LOD count and its meshlet LODs differ")
    elif geometry is None:
        raise StmeshError("a stmesh has neither vertex streams nor meshlets")
    else:
        for lod in lods:
            for cluster in lod.clusters:
                drawing = billboards if cluster.batch_type == BatchType.BILLBOARDS else geometry
                if drawing is None:
                    raise StmeshError("a stmesh's billboard cluster has no billboard geometry")
                drawn = indices[cluster.start_index:cluster.start_index + cluster.index_count]
                if len(drawn) != cluster.index_count:
                    raise StmeshError("a stmesh's cluster draws past its indices")
                if cluster.index_count and int(drawn.max()) + cluster.base_vertex >= drawing.vertex_count:
                    raise StmeshError("a stmesh's cluster draws past its vertices")
    return StmeshFile(layout, profile, flags, total_vertex_count, (low, high), tuple(lods), names, wind,
                      struct.unpack_from("<3f", block, 0x74), block[0x458], block[0x45a],
                      BillboardFaceCrossFadeThreshold(block[0x45b] & 1),
                      struct.unpack_from("<I", block, 0x45c)[0] & 0xffffff, block[0x45f], exporter_460, sdf_texture,
                      geometry, billboards, indices, pivots, meshlets)


__all__ = ["BatchType", "BillboardFaceCrossFadeThreshold", "BufferLayout", "COMPONENT", "FLAG_BILLBOARD_LOD",
           "FLAG_BVH", "FLAG_MESHLETS", "FLAG_RAY_TRACING", "FLAG_SDF_TEXTURE", "FOLIAGE", "Geometry", "Lod",
           "LodProfile", "MAGIC", "MaterialCluster", "PLACEMENTS", "STREAM_DTYPES", "StmeshError", "StmeshFile",
           "VertexStream", "read_stmesh"]
