"""See docs/internals/speedtree.md, Meshlet files."""
from __future__ import annotations

import struct
from dataclasses import dataclass

import numpy as np

from utils.hex_util import align

HEADER = 0x90
BVH = 0xa0
PIVOTS = 256
_LODS = 8


class MeshletError(ValueError):
    pass


def position_dtype(level: int) -> tuple[np.dtype, int]:
    """``level`` is flags bits 12-13."""
    if level == 0:
        return np.dtype("<u2"), 3
    if level == 1:
        return np.dtype("<u4"), 1
    return np.dtype("u1"), 3


@dataclass(frozen=True, slots=True, eq=False)
class MeshletChunk:
    center: tuple[float, float, float]
    material_id: int
    part_id: int
    relative_bounds: tuple[int, ...]
    flags: int
    triangles: np.ndarray
    positions: np.ndarray
    lod_positions: np.ndarray
    texcoords: np.ndarray
    directions: np.ndarray
    wind: np.ndarray
    colors: np.ndarray
    halves: np.ndarray
    cluster_flags: int
    quantized_center: tuple[int, int, int]

    @property
    def vertex_count(self) -> int:
        return len(self.directions)

    @property
    def position_level(self) -> int:
        return (self.flags >> 12) & 3

    def _arrays(self) -> list[bytes]:
        def padded(array: np.ndarray, size: int | None = None) -> bytes:
            raw = np.ascontiguousarray(array).tobytes()
            return raw + bytes((size if size is not None else align(len(raw), 4)) - len(raw))
        head = struct.pack("<3f4B6HI", *self.center, self.vertex_count, len(self.triangles), self.material_id,
                           self.part_id, *self.relative_bounds, self.flags)
        return [head, padded(self.triangles), padded(self.positions), padded(self.lod_positions),
                padded(self.texcoords), padded(self.directions), padded(self.wind), padded(self.colors),
                padded(self.halves, 8 if self.flags & 0x60 else None)]

    def pack(self) -> bytes:
        return b"".join(self._arrays())

    @classmethod
    def read(cls, blob: bytes, at: int, end: int, cluster_flags: int,
             quantized_center: tuple[int, int, int]) -> "MeshletChunk":
        if at + 32 > end:
            raise MeshletError("a SpeedTree meshlet chunk runs past its LOD")
        center = struct.unpack_from("<3f", blob, at)
        vertices, triangles, material, part = blob[at + 12:at + 16]
        bounds = struct.unpack_from("<6H", blob, at + 16)
        flags = struct.unpack_from("<I", blob, at + 28)[0]
        cursor = at + 32

        def take(dtype, count: int, components: int, size: int | None = None) -> np.ndarray:
            nonlocal cursor
            width = np.dtype(dtype).itemsize * count * components
            stored = size if size is not None else align(width, 4)
            if cursor + stored > end:
                raise MeshletError("a SpeedTree meshlet chunk runs past its LOD")
            if any(blob[cursor + width:cursor + stored]):
                raise MeshletError("a SpeedTree meshlet chunk's padding is not zero")
            array = np.frombuffer(blob, dtype, count * components, cursor)
            cursor += stored
            return array.reshape(count, components) if components > 1 else array.copy()

        level = (flags >> 12) & 3
        position, components = position_dtype(level)
        tris = take("u1", triangles, 3)
        positions = take(position, vertices, components)
        lod_positions = take(position, vertices, components)
        texcoords = take("<f2", 1 if flags & 2 else vertices, 2)
        directions = take("u1", vertices, 4)
        wind = take("u1", 1 if flags & 0x80 else vertices, 4)
        colors = take("u1", 1 if flags & 0x10 else vertices, 4)
        halves = take("<f2", 1 if flags & 0x60 else vertices, 3, 8 if flags & 0x60 else None)
        if cursor != end:
            raise MeshletError(f"a SpeedTree meshlet chunk ends {end - cursor} bytes before the next")
        return cls(center, material, part, bounds, flags, tris.copy(), positions.copy(), lod_positions.copy(),
                   texcoords.copy(), directions.copy(), wind.copy(), colors.copy(), halves.copy(), cluster_flags,
                   quantized_center)


@dataclass(frozen=True, slots=True, eq=False)
class MeshletLod:
    """``count_metadata`` is the high 16 bits of the ``MeshletBVH`` cluster count."""
    chunks: tuple[MeshletChunk, ...]
    count_metadata: int


@dataclass(frozen=True, slots=True, eq=False)
class StmeshMeshlets:
    bounds_min: tuple[float, float, float]
    bounds_max: tuple[float, float, float]
    loaded_lod: int
    resident_lod: int
    allocation_lod: int
    lod_factors: np.ndarray
    group_pivots: np.ndarray
    lods: tuple[MeshletLod, ...]
    bvh_offset: tuple[float, float, float]
    bvh_scale: float
    blob_offset: int = 0

    def chunks(self):
        for lod, entry in enumerate(self.lods):
            for chunk in entry.chunks:
                yield lod, chunk


def _header(meshlets: StmeshMeshlets, tables: list[int], ends: list[int]) -> bytes:
    lods = len(meshlets.lods)
    info = lods | meshlets.loaded_lod << 8 | meshlets.resident_lod << 16 | meshlets.allocation_lod << 24
    pad = [0] * (_LODS - lods)
    return (struct.pack("<3fI3fI", *meshlets.bounds_min, info, *meshlets.bounds_max, (1 << lods) - 1)
            + struct.pack(f"<{_LODS}I", *tables, *pad) + np.asarray(meshlets.lod_factors, "<f2").tobytes()
            + struct.pack(f"<{_LODS}I", *ends, *pad)
            + struct.pack("<4I", HEADER << 8 | (len(meshlets.group_pivots) - 1), 0, 0, 0))


def pack_blob(meshlets: StmeshMeshlets, file_offset: int) -> tuple[bytes, list[int]]:
    if len(meshlets.group_pivots) != PIVOTS:
        raise MeshletError(f"a SpeedTree meshlet blob holds {PIVOTS} group pivots")
    lods = len(meshlets.lods)
    if not 0 < lods <= _LODS:
        raise MeshletError(f"a SpeedTree mesh has 1 to {_LODS} meshlet LODs, not {lods}")
    pivots = np.ascontiguousarray(meshlets.group_pivots, "<f4").tobytes()
    tables_at = HEADER + len(pivots)
    tables, at = [], tables_at
    for lod in meshlets.lods:
        tables.append(at)
        at += 4 + 4 * len(lod.chunks)
    chunk_at: dict[tuple[int, int], int] = {}
    regions, ends = [], [0] * lods
    for number in range(lods - 1, -1, -1):
        for index, chunk in enumerate(meshlets.lods[number].chunks):
            chunk_at[number, index] = at
            regions.append(chunk.pack())
            at += len(regions[-1])
        ends[number] = file_offset + at
    table_bytes = b"".join(struct.pack(f"<I{len(lod.chunks)}I", len(lod.chunks),
                                       *(chunk_at[number, index] for index in range(len(lod.chunks))))
                           for number, lod in enumerate(meshlets.lods))
    blob = _header(meshlets, tables, ends) + pivots + table_bytes + b"".join(regions)
    order = [chunk_at[number, index] for number, lod in enumerate(meshlets.lods) for index in range(len(lod.chunks))]
    return blob, order


def pack_block_tables(meshlets: StmeshMeshlets, block_offset: int, chunk_offsets: list[int]) -> bytes:
    clusters = [chunk for _, chunk in meshlets.chunks()]
    headers_at = block_offset + BVH
    centers_at = headers_at + 16 * len(clusters)
    pad = [0] * (_LODS - len(meshlets.lods))
    counts = [len(lod.chunks) | lod.count_metadata << 16 for lod in meshlets.lods]
    firsts, total = [], 0
    for lod in meshlets.lods:
        firsts.append(16 * total)
        total += len(lod.chunks)
    bvh = (struct.pack("<2Q", headers_at, centers_at) + struct.pack("<4f", *meshlets.bvh_offset, meshlets.bvh_scale)
           + struct.pack(f"<{_LODS}I", *counts, *pad) + struct.pack(f"<{_LODS}I", *firsts, *pad) + bytes(0x40))
    headers, index_at = [], 0
    for number, (chunk, at) in enumerate(zip(clusters, chunk_offsets)):
        level = chunk.position_level
        word0 = (chunk.vertex_count | 3 * len(chunk.triangles) << 8 | chunk.cluster_flags | level << 20
                 | chunk.material_id << 23)
        vertices_at = at + 32 + align(3 * len(chunk.triangles), 4)
        headers.append(struct.pack("<4I", word0, chunk.part_id | number << 8, vertices_at, index_at))
        index_at += 6 * len(chunk.triangles)
    centers = b"".join(struct.pack("<3H", *chunk.quantized_center) for chunk in clusters)
    return bvh + b"".join(headers) + centers


def pack_streaming(meshlets: StmeshMeshlets) -> bytes:
    return b"".join(np.ascontiguousarray(chunk.triangles, "<u2").tobytes() for _, chunk in meshlets.chunks())


_CLUSTER_FLAGS = 0x804f0000


def read_meshlets(block: bytes, data: bytes, blob_at: int, blob_size: int,
                  streaming: bytes | None = None) -> StmeshMeshlets:
    if blob_at + blob_size > len(data) or blob_size < HEADER:
        raise MeshletError("a stmesh's meshlet blob runs past its end")
    blob = data[blob_at:blob_at + blob_size]
    copy_at = struct.unpack_from("<Q", block, 0x468)[0]
    if block[copy_at:copy_at + HEADER] != blob[:HEADER]:
        raise MeshletError("a stmesh's meshlet header copy differs from its blob's")
    bounds_min = struct.unpack_from("<3f", blob, 0)
    info = struct.unpack_from("<I", blob, 0xc)[0]
    bounds_max = struct.unpack_from("<3f", blob, 0x10)
    lods = info & 0xff
    if not 0 < lods <= _LODS or struct.unpack_from("<I", blob, 0x1c)[0] != (1 << lods) - 1:
        raise MeshletError("a stmesh's meshlet header names no valid LODs")
    tables = struct.unpack_from(f"<{_LODS}I", blob, 0x20)
    factors = np.frombuffer(blob, "<f2", 16, 0x40).copy()
    extension = struct.unpack_from("<4I", blob, 0x80)
    if extension != (HEADER << 8 | (PIVOTS - 1), 0, 0, 0):
        raise MeshletError("a stmesh's meshlet header extension is not the 256 group pivots after it")
    pivots = np.frombuffer(blob, "<f4", 3 * PIVOTS, HEADER).reshape(PIVOTS, 3).copy()
    bvh_at = struct.unpack_from("<Q", block, 0x470)[0]
    headers_at, centers_at = struct.unpack_from("<2Q", block, bvh_at)
    bvh_offset = struct.unpack_from("<3f", block, bvh_at + 0x10)
    bvh_scale = struct.unpack_from("<f", block, bvh_at + 0x1c)[0]
    counts = struct.unpack_from(f"<{_LODS}I", block, bvh_at + 0x20)
    ends = struct.unpack_from(f"<{_LODS}I", blob, 0x60)
    starts = []
    for number in range(lods):
        count = struct.unpack_from("<I", blob, tables[number])[0]
        starts.append(struct.unpack_from(f"<{count}I", blob, tables[number] + 4))
        if count != counts[number] & 0xffff:
            raise MeshletError("a stmesh's meshlet LOD table and MeshletBVH count differ")
    result, cluster = [], 0
    for number in range(lods):
        chunk_ends = list(starts[number][1:]) + [ends[number] - blob_at]
        chunks = []
        for at, end in zip(starts[number], chunk_ends):
            word0 = struct.unpack_from("<I", block, headers_at + 16 * cluster)[0]
            center = struct.unpack_from("<3H", block, centers_at + 6 * cluster)
            chunks.append(MeshletChunk.read(blob, at, end, word0 & _CLUSTER_FLAGS, center))
            cluster += 1
        result.append(MeshletLod(tuple(chunks), counts[number] >> 16))
    meshlets = StmeshMeshlets(bounds_min, bounds_max, (info >> 8) & 0xff, (info >> 16) & 0xff, info >> 24, factors,
                              pivots, tuple(result), bvh_offset, bvh_scale, blob_at)
    if streaming is not None and pack_streaming(meshlets) != streaming:
        raise MeshletError("a stmesh's streaming file is not its clusters' triangles")
    return meshlets


__all__ = ["BVH", "HEADER", "MeshletChunk", "MeshletError", "MeshletLod", "PIVOTS", "StmeshMeshlets", "pack_blob",
           "pack_block_tables", "pack_streaming", "position_dtype", "read_meshlets"]
