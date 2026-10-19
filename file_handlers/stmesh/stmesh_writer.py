"""See docs/internals/speedtree.md, Format."""
from __future__ import annotations

import struct
from dataclasses import dataclass

lazy import numpy as np

lazy from utils.hex_util import align

lazy from .meshlets import BVH, HEADER as MESHLET_HEADER, pack_blob, pack_block_tables, pack_streaming
lazy from .stmesh_file import (CLUSTER, FLAG_SDF_TEXTURE, HEADER, MAGIC, MESHLET_SPAN, STREAMS, BufferLayout, Geometry,
                          StmeshError, StmeshFile)
lazy from .wind import SIZE as WIND_SIZE


@dataclass(frozen=True, slots=True)
class StmeshWriteResult:
    main: bytes
    streaming: bytes | None


class _Block:
    def __init__(self, fixed: int):
        self.data = bytearray(fixed)

    def append(self, raw: bytes, alignment: int = 16) -> int:
        at = align(len(self.data), alignment)
        self.data += bytes(at - len(self.data)) + raw
        return at

    def put_q(self, at: int, value: int) -> None:
        struct.pack_into("<Q", self.data, at, value)


def _lod_chunk(lod, layout: BufferLayout) -> bytes:
    raw = bytearray(0x10 + CLUSTER * len(lod.clusters))
    raw[0] = len(lod.clusters)
    struct.pack_into("<f", raw, 4, lod.factor)
    for number, cluster in enumerate(lod.clusters):
        at = 0x10 + CLUSTER * number
        struct.pack_into("<4I", raw, at, cluster.index_count, cluster.start_index, cluster.base_vertex,
                         cluster.bvh_offset)
        if layout is BufferLayout.LATE:
            struct.pack_into("<I", raw, at + 0x10, cluster.bvh_size)
        elif cluster.bvh_size:
            raise StmeshError("RE4 and SF6 clusters hold no BVH size")
        material = at + layout.cluster_material
        raw[material], raw[material + 1] = cluster.material_id, cluster.batch_type
    return raw.take_bytes()


def _streams(chunk: bytearray, geometry: Geometry | None, at_slots: int, at_offsets: int, at_strides: int,
             start: int) -> tuple[bytes, int]:
    if geometry is None:
        return b"", 0
    if len(geometry.streams) > STREAMS:
        raise StmeshError(f"a stmesh holds at most {STREAMS} vertex streams")
    counts = {len(stream.data) for stream in geometry.streams}
    if len(counts) != 1:
        raise StmeshError("a stmesh's vertex streams do not hold the same number of vertices")
    raw, at = [], start
    for number, stream in enumerate(geometry.streams):
        struct.pack_into("<I", chunk, at_slots + 4 * number, stream.slot)
        struct.pack_into("<I", chunk, at_offsets + 4 * number, at)
        struct.pack_into("<I", chunk, at_strides + 4 * number, stream.stride)
        raw.append(np.ascontiguousarray(stream.data).tobytes())
        at += len(raw[-1])
    return b"".join(raw), len(geometry.streams)


def write_stmesh(mesh: StmeshFile) -> StmeshWriteResult:
    layout = mesh.layout
    meshlet = mesh.is_meshlet
    if meshlet != (mesh.meshlets is not None):
        raise StmeshError("a stmesh's meshlet flag and meshlets disagree")
    if bool(mesh.flags & FLAG_SDF_TEXTURE) != (mesh.sdf_texture_path is not None):
        raise StmeshError("a stmesh's SDF texture flag and path disagree")
    if meshlet and layout is not BufferLayout.LATE:
        raise StmeshError("RE4 and SF6 stmesh files hold no meshlets")

    block = _Block(layout.fixed_block)
    fixed = block.data
    fixed[0:0x40] = mesh.lod_profile.pack()
    fixed[0x40], fixed[0x41], fixed[0x42] = mesh.flags, mesh.lod_count, len(mesh.material_names)
    struct.pack_into("<I3f3f", fixed, 0x44, mesh.total_vertex_count, *mesh.bounds[0], *mesh.bounds[1])
    struct.pack_into("<3f", fixed, 0x74, *mesh.vortexel_wind_sample_offset)
    fixed[0x80:0x80 + WIND_SIZE] = mesh.wind.pack()
    fixed[0x458], fixed[0x45a] = mesh.billboard_side_view_count, mesh.unknown_45a
    fixed[0x45b] = int(mesh.billboard_cross_fade)
    struct.pack_into("<I", fixed, 0x45c, mesh.lod_bvh_sources | mesh.lod_transition_mask << 24)
    if layout is BufferLayout.EARLY:
        struct.pack_into("<I", fixed, 0x460, mesh.exporter_460)
    elif mesh.exporter_460:
        raise StmeshError("only RE4 and SF6 stmesh files keep the exporter's bytes at +0x460")

    if mesh.lods:
        table = block.append(bytes(8 * len(mesh.lods)))
        block.put_q(0x60, table)
        for number, lod in enumerate(mesh.lods):
            block.put_q(table + 8 * number, block.append(_lod_chunk(lod, layout)))
    names = block.append(bytes(8 * len(mesh.material_names)))
    block.put_q(0x68, names)
    for number, name in enumerate(mesh.material_names):
        block.put_q(names + 8 * number, block.append(name.encode("utf-16-le") + b"\0\0", 1 if number else 16))
    if mesh.sdf_texture_path is not None:
        block.put_q(0x460, block.append(mesh.sdf_texture_path.encode("utf-16-le") + b"\0\0"))

    chunk = bytearray(int(layout))
    at_slots, at_offsets, at_strides, at_bslots, at_boffsets, at_bstrides, at_counts, at_pivots = layout.fields
    if meshlet:
        copy = block.append(bytes(MESHLET_HEADER))
        block.put_q(0x468, copy)
        tables_at = block.append(bytes(BVH))
        block.put_q(0x470, tables_at)
        # The blob's offset depends on the BVH tables' size, and the tables need the blob's chunk order.
        provisional = pack_block_tables(mesh.meshlets, tables_at, [0] * sum(len(l.chunks) for l in mesh.meshlets.lods))
        block.data[tables_at:] = provisional
        size = align(len(block.data))
        block.data += bytes(size - len(block.data))
        buffers = HEADER + size
        blob_at = buffers + int(layout)
        blob, order = pack_blob(mesh.meshlets, blob_at)
        block.data[copy:copy + MESHLET_HEADER] = blob[:MESHLET_HEADER]
        block.data[tables_at:tables_at + len(provisional)] = pack_block_tables(mesh.meshlets, tables_at, order)
        struct.pack_into("<2I", chunk, MESHLET_SPAN, blob_at, len(blob))
        body = blob
        streaming = pack_streaming(mesh.meshlets)
    else:
        size = align(len(block.data))
        block.data += bytes(size - len(block.data))
        buffers = HEADER + size
        vertex_at = buffers + int(layout)
        # The vertex-stream files' unused stride entries hold 4.
        struct.pack_into(f"<{STREAMS}I", chunk, at_strides, *[4] * STREAMS)
        struct.pack_into(f"<{STREAMS}I", chunk, at_bstrides, *[4] * STREAMS)
        geometry, count = _streams(chunk, mesh.geometry, at_slots, at_offsets, at_strides, 0)
        billboards, billboard_count = _streams(chunk, mesh.billboards, at_bslots, at_boffsets, at_bstrides,
                                               len(geometry))
        vertices = geometry + billboards
        struct.pack_into("<2I", chunk, at_counts, count, billboard_count)
        if billboard_count:
            struct.pack_into("<I", block.data, 0x70, len(geometry))
        index_bytes = np.ascontiguousarray(mesh.indices, "<u2").tobytes()
        index_at = align(vertex_at + len(vertices))
        struct.pack_into("<4I", chunk, 0, len(vertices), len(index_bytes), vertex_at, index_at)
        body = vertices + bytes(index_at - vertex_at - len(vertices)) + index_bytes
        if len(mesh.group_pivots):
            pivot_at = index_at + len(index_bytes)
            struct.pack_into("<2I", chunk, at_pivots, pivot_at, len(mesh.group_pivots))
            body += np.ascontiguousarray(mesh.group_pivots, "<f4").tobytes()
        streaming = None
    header = MAGIC + struct.pack("<3I", 0, HEADER, buffers)
    return StmeshWriteResult(header + bytes(block.data) + chunk.take_bytes() + body, streaming)


__all__ = ["StmeshWriteResult", "write_stmesh"]
