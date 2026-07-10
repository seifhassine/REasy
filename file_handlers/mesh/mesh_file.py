import math
import struct
from array import array
from bisect import bisect_right
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Tuple, Optional

from utils.binary_handler import BinaryHandler
from utils.native_build import ensure_fastmesh
ensure_fastmesh()
from fastmesh import (
    unpack_normals_tangents,
    unpack_uvs,
    unpack_colors,
)

MESH_MAGIC = 0x4853454D
MPLY_MAGIC = 0x594C504D


class MeshMainVersion(IntEnum):
    UNKNOWN = 0
    RE7 = 1
    DMC5 = 2
    RE8 = 3
    RE_RT = 4
    RE4 = 5
    SF6 = 6
    DD2_OLD = 7
    KUNITSUGAMI = 8
    DD2 = 9
    ONIMUSHA = 10
    MHWILDS = 11
    PRAGMATA = 13
    RE9 = 14


MESH_VERSION_PAIRS = {
    (352921600, 32): MeshMainVersion.RE7,
    (386270720, 1808282334): MeshMainVersion.DMC5,
    (386270720, 1808312334): MeshMainVersion.DMC5,
    (386270720, 1902042334): MeshMainVersion.DMC5,
    (21011200, 1902042334): MeshMainVersion.DMC5,
    (21041600, 2109108288): MeshMainVersion.RE_RT,
    (21041600, 220128762): MeshMainVersion.RE_RT,
    (21041600, 2109148288): MeshMainVersion.RE_RT,
    (21061800, 2109148288): MeshMainVersion.RE_RT,
    (21091000, 2109148288): MeshMainVersion.RE_RT,
    (2020091500, 2101050001): MeshMainVersion.RE8,
    (220822879, 221108797): MeshMainVersion.RE4,
    (220705151, 230110883): MeshMainVersion.SF6,
    (230403828, 230110883): MeshMainVersion.SF6,
    (230517984, 231011879): MeshMainVersion.DD2_OLD,
    (230517984, 240423143): MeshMainVersion.DD2,
    (230727984, 240306278): MeshMainVersion.KUNITSUGAMI,
    (240704828, 240827123): MeshMainVersion.ONIMUSHA,
    (240827123, 240827123): MeshMainVersion.ONIMUSHA,
    (240704828, 241111606): MeshMainVersion.MHWILDS,
    (250203152, 250604100): MeshMainVersion.PRAGMATA,
    (250203152, 251215606): MeshMainVersion.PRAGMATA,
    (250707828, 250925211): MeshMainVersion.PRAGMATA,
    (250707828, 251121828): MeshMainVersion.PRAGMATA,
    (250904410, 250925211): MeshMainVersion.RE9,
}


def get_mesh_version(internal_version: int, file_version: int) -> MeshMainVersion:
    pair = internal_version, file_version
    if pair not in MESH_VERSION_PAIRS:
        raise ValueError(f"Unsupported MESH version pair: {pair}")
    return MESH_VERSION_PAIRS[pair]


def _checked_slice(data: bytes | bytearray, start: int, size: int, label: str) -> bytes:
    end = start + size
    if start < 0 or size < 0 or end > len(data):
        raise ValueError(f"{label} range [{start}, {end}) exceeds {len(data)} bytes")
    return bytes(memoryview(data)[start:end])


@dataclass
class Header:
    magic: int = MESH_MAGIC
    version: int = 0
    file_size: int = 0
    lod_hash: int = 0

    flags: int = 0
    name_count: int = 0
    ukn: int = 0
    lods_offset: int = 0
    shadow_lods_offset: int = 0
    occluder_mesh_offset: int = 0
    bones_offset: int = 0
    normal_recalc_offset: int = 0
    blend_shapes_offset: int = 0
    bounds_offset: int = 0
    mesh_offset: int = 0
    floats_offset: int = 0
    material_indices_offset: int = 0
    bone_indices_offset: int = 0
    blend_shape_indices_offset: int = 0
    name_offsets_offset: int = 0

    streaming_info_offset: int = 0
    mesh_group_offset: int = 0
    dd2_hash_offset: int = 0
    vertices_offset: int = 0
    sdf_path_offset: int = 0
    parts_name_hash_offset: int = 0
    buffer_headers_offset: int = 0
    reserved_meshlet0: int = 0
    meshlet_bvh_offset: int = 0
    meshlet_parts_offset: int = 0
    reserved_meshlet1: int = 0
    reserved_meshlet2: int = 0
    reserved_meshlet3: int = 0
    content_flags: int = 0
    header_pad: int = 0
    wilds_unkn1: int = 0
    wilds_unkn2: int = 0
    wilds_unkn3: int = 0
    wilds_unkn4: int = 0

    format_version: MeshMainVersion = MeshMainVersion.UNKNOWN

    def _read_meshlet_or_lod_offsets(self, h: BinaryHandler):
        if self.magic == MPLY_MAGIC:
            self.reserved_meshlet0 = h.read_int64()
            self.mesh_offset = h.read_int64()
            self.meshlet_bvh_offset = h.read_int64()
            self.meshlet_parts_offset = h.read_int64()
            self.reserved_meshlet1 = h.read_int64()
            self.reserved_meshlet2 = h.read_int64()
            self.reserved_meshlet3 = h.read_int64()
        else:
            self.lods_offset = h.read_int64()
            self.shadow_lods_offset = h.read_int64()
            self.occluder_mesh_offset = h.read_int64()
            self.normal_recalc_offset = h.read_int64()
            self.blend_shapes_offset = h.read_int64()
            self.mesh_offset = h.read_int64()
            self.mesh_group_offset = h.read_int64()

    def _read_common_mesh_offsets(self, h: BinaryHandler):
        self.floats_offset = h.read_int64()
        self.bounds_offset = h.read_int64()
        self.bones_offset = h.read_int64()
        self.material_indices_offset = h.read_int64()
        self.bone_indices_offset = h.read_int64()
        self.blend_shape_indices_offset = h.read_int64()

    def read(self, h: BinaryHandler, *, file_version: int) -> bool:
        self.magic = h.read_uint32()
        if self.magic not in (MESH_MAGIC, MPLY_MAGIC):
            return False
        self.version = h.read_uint32()
        self.file_size = h.read_uint32()
        self.lod_hash = h.read_uint32()
        self.format_version = get_mesh_version(self.version, file_version)

        if self.format_version < MeshMainVersion.RE4:
            self.flags = h.read_int16()
            self.name_count = h.read_int16()
            self.ukn = h.read_int32()
            self.lods_offset = h.read_int64()
            self.shadow_lods_offset = h.read_int64()
            self.occluder_mesh_offset = h.read_int64()
            self.bones_offset = h.read_int64()
            self.normal_recalc_offset = h.read_int64()
            self.blend_shapes_offset = h.read_int64()
            self.bounds_offset = h.read_int64()
            self.mesh_offset = h.read_int64()
            self.floats_offset = h.read_int64()
            self.material_indices_offset = h.read_int64()
            self.bone_indices_offset = h.read_int64()
            self.blend_shape_indices_offset = h.read_int64()
            self.name_offsets_offset = h.read_int64()
            self.streaming_info_offset = 0
        elif self.format_version < MeshMainVersion.ONIMUSHA:
            self.content_flags = h.read_uint32() & 0x00FFFFFF
            self.name_count = h.read_int16()
            self.header_pad = h.read_int16()
            self.buffer_headers_offset = h.read_int64()
            self._read_meshlet_or_lod_offsets(h)
            self._read_common_mesh_offsets(h)
            if self.format_version < MeshMainVersion.DD2_OLD:
                self.streaming_info_offset = h.read_int64()
                self.name_offsets_offset = h.read_int64()
            else:
                self.name_offsets_offset = h.read_int64()
                self.parts_name_hash_offset = h.read_int64()
                self.streaming_info_offset = h.read_int64()
            self.vertices_offset = h.read_int64()
            self.sdf_path_offset = h.read_int64()
        else:
            self.wilds_unkn1 = h.read_int32()
            self.name_count = h.read_int16()
            self.content_flags = h.read_uint32() & 0x00FFFFFF
            self.header_pad = h.read_int16()
            self.wilds_unkn2 = h.read_int32()
            self.wilds_unkn3 = h.read_int32()
            self.wilds_unkn4 = h.read_int32()
            self.vertices_offset = h.read_int64()
            self._read_meshlet_or_lod_offsets(h)
            self._read_common_mesh_offsets(h)
            self.name_offsets_offset = h.read_int64()
            self.streaming_info_offset = h.read_int64()
            self.sdf_path_offset = h.read_int64()
        return True

@dataclass
class StreamingMeshEntry:
    start: int
    size: int


@dataclass
class MeshStreamingInfo:
    entries: List[StreamingMeshEntry] = field(default_factory=list)

    def read(self, h: BinaryHandler) -> bool:
        entry_count = h.read_int32()
        h.read_int32()
        entry_offset = h.read_int64()
        with h.seek_temp(entry_offset):
            self.entries = [
                StreamingMeshEntry(h.read_uint32(), h.read_uint32())
                for _ in range(entry_count)
            ]
        return True

class VertexBufferType(IntEnum):
    Position = 0
    NormalsTangents = 1
    UV0 = 2
    UV1 = 3
    BoneWeights = 4
    Colors = 5
    UnknownType6 = 6
    ExtraWeights = 7


@dataclass
class MeshBufferItemHeader:
    type: int
    size: int
    offset: int


@dataclass
class MeshBufferPayload:
    vertex_bytes: bytes = b""
    face_bytes: bytes = b""
    buffer_headers: List[MeshBufferItemHeader] = field(default_factory=list)
    positions: array = field(default_factory=lambda: array("f"))
    normals: array = field(default_factory=lambda: array("f"))
    tangents: array = field(default_factory=lambda: array("f"))
    normal_ws: array = field(default_factory=lambda: array("B"))
    tangent_ws: array = field(default_factory=lambda: array("B"))
    uv0: array = field(default_factory=lambda: array("d"))
    uv1: array = field(default_factory=lambda: array("d"))
    colors: array = field(default_factory=lambda: array("B"))
    faces: array = field(default_factory=lambda: array("H"))
    integer_faces: Optional[array] = None


@dataclass
class StreamingBufferHeader:
    element_headers_offset: int
    vertex_buffer_offset: int
    vertex_buffer_length: int
    unpadded_buffer_size: int
    main_vertex_element_count: int
    buffer_index: int
    vertex_elements: List[MeshBufferItemHeader]


class MeshBuffer:
    def __init__(self, version: MeshMainVersion):
        self.version = version
        self.element_headers_offset = 0
        self.vertex_buffer_offset = 0
        self.face_buffer_offset = 0
        self.total_buffer_size = 0
        self.vertex_buffer_size = 0
        self.element_count = 0
        self.ukn1 = 0
        self.ukn2 = 0
        self.blend_shape_offset = 0
        self.shapekey_weight_buffer_offset = 0
        self.shapekey_weight_buffer_size = 0
        self.buffer_index_unkn = 0
        self.shadow_mesh_index_buffer_offset = 0
        self.occluder_mesh_index_buffer_offset = 0
        self.blend_shape_index_remap_offset = 0
        self.blend_shape_vertex_remap_offset = 0
        self.buffer_index = 0
        self.buffer_headers: List[MeshBufferItemHeader] = []
        self.streaming_buffer_headers: Dict[int, StreamingBufferHeader] = {}
        self.streaming_buffer_count = 0
        self.buffer_payloads: Dict[int, MeshBufferPayload] = {}
        self.index_counts_by_buffer: Dict[int, int] = {}

        self.positions: array = array("f")
        self.normals: array = array("f")
        self.tangents: array = array("f")
        self.normal_ws: array = array("B")
        self.tangent_ws: array = array("B")
        self.uv0: array = array("d")
        self.uv1: array = array("d")
        self.colors: array = array("B")
        self.faces: array = array("H")
        self.integer_faces: Optional[array] = None
        self.has_32bit_indices = False

    def _decode_payload(
        self,
        payload: MeshBufferPayload,
        *,
        has_32bit_indices: bool = False,
    ) -> None:
        vert_count = 0
        for i, bh in enumerate(payload.buffer_headers):
            if bh.type != VertexBufferType.Position:
                continue
            start = bh.offset
            if i == len(payload.buffer_headers) - 1:
                end = len(payload.vertex_bytes)
            else:
                end = payload.buffer_headers[i + 1].offset
            data = memoryview(payload.vertex_bytes)[start:end]
            payload.positions = array("f")
            payload.positions.frombytes(data)
            vert_count = len(payload.positions) // 3
            break

        for i, bh in enumerate(payload.buffer_headers):
            if bh.type == VertexBufferType.Position:
                continue
            start = bh.offset
            if i < len(payload.buffer_headers) - 1:
                end = payload.buffer_headers[i + 1].offset
            else:
                end = start + bh.size * vert_count
            data = memoryview(payload.vertex_bytes)[start:end]
            if bh.type == VertexBufferType.NormalsTangents:
                payload.normals, payload.normal_ws, payload.tangents, payload.tangent_ws = unpack_normals_tangents(data)
            elif bh.type == VertexBufferType.UV0:
                payload.uv0 = unpack_uvs(data)
            elif bh.type == VertexBufferType.UV1:
                payload.uv1 = unpack_uvs(data)
            elif bh.type == VertexBufferType.Colors:
                payload.colors = unpack_colors(data)

        if has_32bit_indices:
            payload.integer_faces = array("I")
            payload.integer_faces.frombytes(payload.face_bytes)
            payload.faces = array("H")
        else:
            payload.faces = array("H")
            payload.faces.frombytes(payload.face_bytes)
            payload.integer_faces = None

    def _read_streaming_buffer_headers(
        self,
        h: BinaryHandler,
        streaming_info: MeshStreamingInfo,
        header_base: int,
    ) -> None:
        if self.version < MeshMainVersion.SF6:
            return
        header_size = 80 if self.version >= MeshMainVersion.PRAGMATA else 64
        header_pos = header_base + header_size
        for _stream_entry in streaming_info.entries:
            if self.version >= MeshMainVersion.MHWILDS and _stream_entry.size <= 0:
                continue
            h.seek(header_pos)
            element_headers_offset = h.read_int64()
            vertex_buffer_offset = h.read_int64()
            h.read_int64()
            unpadded_buffer_size = h.read_int32()
            vertex_buffer_length = h.read_int32()
            main_vertex_element_count = h.read_int16()
            h.read_int16()
            if self.version >= MeshMainVersion.PRAGMATA:
                h.read_int32()
                h.read_int32()
                h.read_int32()
                h.read_int32()
            h.read_int32()
            h.read_int32()
            if self.version >= MeshMainVersion.MHWILDS:
                h.read_int32()
                h.read_int32()
                h.read_int32()
                h.read_int32()
                buffer_index = h.read_int32()
            else:
                h.read_int32()
                h.read_int32()
                buffer_index = h.read_int32()
                h.read_int64()
            vertex_elements: List[MeshBufferItemHeader] = []
            with h.seek_temp(element_headers_offset):
                for _ in range(main_vertex_element_count):
                    raw_type = h.read_int16()
                    try:
                        t = VertexBufferType(raw_type)
                    except ValueError:
                        t = raw_type
                    sz = h.read_int16()
                    off = h.read_int32()
                    vertex_elements.append(MeshBufferItemHeader(t, sz, off))
            self.streaming_buffer_headers[buffer_index] = StreamingBufferHeader(
                element_headers_offset=element_headers_offset,
                vertex_buffer_offset=vertex_buffer_offset,
                vertex_buffer_length=vertex_buffer_length,
                unpadded_buffer_size=unpadded_buffer_size,
                main_vertex_element_count=main_vertex_element_count,
                buffer_index=buffer_index,
                vertex_elements=vertex_elements,
            )
            header_pos += header_size

    def finalize_payloads(self) -> None:
        for buffer_index, payload in self.buffer_payloads.items():
            self._decode_payload(
                payload,
                has_32bit_indices=self.has_32bit_indices,
            )
            if buffer_index == 0:
                self.positions = payload.positions
                self.normals = payload.normals
                self.tangents = payload.tangents
                self.normal_ws = payload.normal_ws
                self.tangent_ws = payload.tangent_ws
                self.uv0 = payload.uv0
                self.uv1 = payload.uv1
                self.colors = payload.colors
                self.faces = payload.faces
                self.integer_faces = payload.integer_faces

    def read(
        self,
        h: BinaryHandler,
        *,
        streaming_info: Optional[MeshStreamingInfo] = None,
        streaming_data: Optional[bytes] = None,
    ) -> bool:
        header_base = h.tell
        self.streaming_buffer_count = (
            len(streaming_info.entries) if streaming_info is not None else 0
        )
        self.element_headers_offset = h.read_int64()
        self.vertex_buffer_offset = h.read_int64()
        if self.version >= MeshMainVersion.RE4:
            self.shapekey_weight_buffer_offset = h.read_int64()
            self.total_buffer_size = h.read_int32()
            self.vertex_buffer_size = h.read_int32()
            self.face_buffer_offset = self.vertex_buffer_offset + self.vertex_buffer_size
        else:
            self.face_buffer_offset = h.read_int64()
            if self.version == MeshMainVersion.RE_RT:
                h.read_int64()  # rtPadding
            self.total_buffer_size = h.read_int32()
            h.read_int32()
            self.vertex_buffer_size = self.face_buffer_offset - self.vertex_buffer_offset
        self.element_count = h.read_int16()
        h.read_int16()
        if self.version >= MeshMainVersion.PRAGMATA:
            self.shapekey_weight_buffer_size = h.read_int32()
            self.ukn1 = h.read_int32()
            self.ukn2 = h.read_int32()
            h.read_int32()
        if self.version >= MeshMainVersion.RE4:
            self.shadow_mesh_index_buffer_offset = h.read_int32()
            self.occluder_mesh_index_buffer_offset = h.read_int32()
            if self.version >= MeshMainVersion.MHWILDS:
                self.blend_shape_index_remap_offset = h.read_int32()
                self.blend_shape_vertex_remap_offset = h.read_int32()
                self.blend_shape_offset = h.read_int32()
                h.read_int32()
                self.buffer_index = h.read_int32()
            else:
                self.blend_shape_offset = h.read_int32()
                self.buffer_index_unkn = h.read_int32()
                self.buffer_index = h.read_int32()
                h.read_int64()
        else:
            self.shadow_mesh_index_buffer_offset = h.read_int32()
            self.occluder_mesh_index_buffer_offset = h.read_int32()
            self.blend_shape_offset = h.read_int32()

        with h.seek_temp(self.element_headers_offset):
            self.buffer_headers = []
            for _ in range(self.element_count):
                raw_type = h.read_int16()
                try:
                    t = VertexBufferType(raw_type)
                except ValueError:
                    t = raw_type
                sz = h.read_int16()
                off = h.read_int32()
                self.buffer_headers.append(MeshBufferItemHeader(t, sz, off))
        if streaming_info is not None and streaming_data is not None:
            self._read_streaming_buffer_headers(h, streaming_info, header_base)

        base = h.data
        self.buffer_payloads[0] = MeshBufferPayload(
            vertex_bytes=bytes(memoryview(base)[self.vertex_buffer_offset:self.vertex_buffer_offset + self.vertex_buffer_size]),
            face_bytes=b"",
            buffer_headers=list(self.buffer_headers),
        )
        if streaming_info is not None and streaming_data is not None:
            for buffer_idx, header in self.streaming_buffer_headers.items():
                if buffer_idx <= 0 or buffer_idx - 1 >= len(streaming_info.entries):
                    raise ValueError(
                        f"Streaming buffer index {buffer_idx} has no matching entry"
                    )
                stream_entry = streaming_info.entries[buffer_idx - 1]
                if not 0 <= header.vertex_buffer_length <= header.unpadded_buffer_size:
                    raise ValueError(
                        f"Invalid streaming vertex length for buffer {buffer_idx}"
                    )
                payload = _checked_slice(
                    streaming_data,
                    stream_entry.start,
                    header.unpadded_buffer_size,
                    f"Streaming buffer {buffer_idx}",
                )
                self.buffer_payloads[buffer_idx] = MeshBufferPayload(
                    vertex_bytes=payload[:header.vertex_buffer_length],
                    face_bytes=payload[header.vertex_buffer_length:],
                    buffer_headers=list(header.vertex_elements),
                )
        return True

class Submesh:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.material_index = 0
        self.is_quad = 0
        self.buffer_index = 0
        self.reserve = 0
        self.streaming_offset = 0
        self.streaming_offset2 = 0
        self.ukn1 = 0
        self.ukn2 = 0
        self.indices_count = 0
        self.faces_index_offset = 0
        self.verts_index_offset = 0
        self.vert_count = 0

    def read(self, h: BinaryHandler):
        if self.version < MeshMainVersion.RE4:
            self.material_index = h.read_uint16()
            self.reserve = h.read_uint16()
        else:
            self.material_index = h.read_uint8()
            self.is_quad = h.read_uint8()
            if self.version >= MeshMainVersion.SF6:
                self.buffer_index = h.read_uint8()
                self.reserve = h.read_uint8()
            else:
                self.reserve = h.read_uint16()
            if self.version >= MeshMainVersion.ONIMUSHA:
                self.ukn1 = h.read_int32()
        self.indices_count = h.read_int32()
        self.faces_index_offset = h.read_int32()
        self.verts_index_offset = h.read_int32()
        if self.version >= MeshMainVersion.RE8:
            self.streaming_offset = h.read_int32()
            self.streaming_offset2 = h.read_int32()
        if self.version >= MeshMainVersion.DD2:
            self.ukn2 = h.read_int32()
        if min(self.indices_count, self.faces_index_offset, self.verts_index_offset) < 0:
            raise ValueError("Negative submesh count or offset")

class MeshGroup:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.group_id = 0
        self.vertex_count = 0
        self.face_count = 0
        self.submeshes: List[Submesh] = []

    def read(
        self,
        h: BinaryHandler,
        *,
        buffer_vertex_offsets: Dict[int, int],
    ):
        self.group_id = h.read_uint8()
        submesh_count = h.read_uint8()
        h.skip(6)
        self.vertex_count = h.read_int32()
        self.face_count = h.read_int32()
        if min(self.vertex_count, self.face_count) < 0:
            raise ValueError("Negative mesh-group count")
        for _ in range(submesh_count):
            sm = Submesh(self.buffer, self.version)
            sm.read(h)
            self.submeshes.append(sm)

        next_offsets_by_buffer: Dict[int, int] = {}
        for sm in reversed(self.submeshes):
            buffer_index = sm.buffer_index
            vertex_end = next_offsets_by_buffer.get(
                buffer_index,
                buffer_vertex_offsets.get(buffer_index, 0) + self.vertex_count,
            )
            sm.vert_count = vertex_end - sm.verts_index_offset
            if sm.vert_count < 0:
                raise ValueError(
                    f"Invalid submesh vertex span [{sm.verts_index_offset}, {vertex_end})"
                )
            next_offsets_by_buffer[buffer_index] = sm.verts_index_offset

        for buffer_index in next_offsets_by_buffer:
            buffer_vertex_offsets[buffer_index] = (
                buffer_vertex_offsets.get(buffer_index, 0) + self.vertex_count
            )

class MeshLOD:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.mesh_groups: List[MeshGroup] = []
        self.lod_factor = 0.0
        self.vertex_format = 0
        self.lod_level = 0
        self.reserve = 0

    def read(
        self,
        h: BinaryHandler,
        *,
        buffer_vertex_offsets: Dict[int, int],
    ):
        mesh_count = h.read_uint8()
        self.vertex_format = h.read_uint8()
        if self.version >= MeshMainVersion.SF6:
            self.lod_level = h.read_uint8()
            self.reserve = h.read_uint8()
        else:
            self.reserve = h.read_uint16()
        self.lod_factor = h.read_float()
        header_offset = h.read_int64()
        with h.seek_temp(header_offset):
            mesh_offsets = [h.read_int64() for _ in range(mesh_count)]
        for off in mesh_offsets:
            if off <= 0:
                continue
            h.seek(off)
            mg = MeshGroup(self.buffer, self.version)
            mg.read(h, buffer_vertex_offsets=buffer_vertex_offsets)
            self.mesh_groups.append(mg)
            for sm in mg.submeshes:
                index_end = sm.faces_index_offset + sm.indices_count
                self.buffer.index_counts_by_buffer[sm.buffer_index] = max(
                    self.buffer.index_counts_by_buffer.get(sm.buffer_index, 0),
                    index_end,
                )

class MeshData:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.lods: List[MeshLOD] = []
        self.material_count = 0
        self.uv_count = 0
        self.skin_weight_count = 0
        self.total_mesh_count = 0
        self.integer_faces = 0
        self.shared_lod_bits = 0
        self.ukn1 = 0
        self.bounding_sphere = (0.0, 0.0, 0.0, 0.0)
        self.bounding_box = (
            (0.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 0.0),
        )

    def read(self, h: BinaryHandler, *, read_all_lods: bool = False):
        lod_count = h.read_uint8()
        self.material_count = h.read_uint8()
        self.uv_count = h.read_uint8()
        self.skin_weight_count = h.read_uint8()
        self.total_mesh_count = h.read_int16()
        self.integer_faces = h.read_uint8()
        self.shared_lod_bits = h.read_uint8()
        self.buffer.has_32bit_indices = bool(self.integer_faces)
        if self.version <= MeshMainVersion.DMC5:
            self.ukn1 = h.read_int64()
        self.bounding_sphere = h.read_vec4()
        if self.version >= MeshMainVersion.RE4:
            self.bounding_box = tuple(
                (*h.read_vec3(), float(h.read_uint32())) for _ in range(2)
            )
        else:
            self.bounding_box = (h.read_vec4(), h.read_vec4())
        lod_offsets_start = h.read_int64()
        with h.seek_temp(lod_offsets_start):
            lod_offsets = [h.read_int64() for _ in range(lod_count)]

        parse_all_lods = read_all_lods or self.buffer.streaming_buffer_count > 0
        lod_targets = lod_offsets if parse_all_lods else lod_offsets[:1]
        self.buffer.index_counts_by_buffer = {}
        buffer_vertex_offsets: Dict[int, int] = {}
        lods_by_offset: Dict[int, MeshLOD] = {}
        for lod_index, off in enumerate(lod_targets):
            if off <= 0:
                continue
            lod = lods_by_offset.get(off)
            if lod is None:
                h.seek(off)
                lod = MeshLOD(self.buffer, self.version)
                lod.read(h, buffer_vertex_offsets=buffer_vertex_offsets)
                lods_by_offset[off] = lod
            if read_all_lods or lod_index == 0:
                self.lods.append(lod)

        missing_buffers = (
            {0, *self.buffer.index_counts_by_buffer}
            - self.buffer.buffer_payloads.keys()
        )
        if missing_buffers:
            raise ValueError(f"Missing mesh buffers: {sorted(missing_buffers)}")

        index_size = 4 if self.buffer.has_32bit_indices else 2
        resident_index_count = (
            self.buffer.index_counts_by_buffer.get(0, 0)
            if self.buffer.streaming_buffer_count
            else sum(self.buffer.index_counts_by_buffer.values())
        )
        resident_start = self.buffer.face_buffer_offset
        self.buffer.buffer_payloads[0].face_bytes = _checked_slice(
            h.data,
            resident_start,
            resident_index_count * index_size,
            "Resident indices",
        )

        for buffer_index, index_count in self.buffer.index_counts_by_buffer.items():
            if buffer_index == 0:
                continue
            payload = self.buffer.buffer_payloads[buffer_index]
            required_size = index_count * index_size
            if len(payload.face_bytes) < required_size:
                raise ValueError(
                    f"Mesh buffer {buffer_index} needs {required_size} index bytes"
                )
            payload.face_bytes = payload.face_bytes[:required_size]

        self.buffer.finalize_payloads()

@dataclass
class MPLYChunkFlags:
    raw: int
    is_meshlet_compressed_normal: bool
    is_meshlet_compressed_texcoord1: bool
    is_meshlet_compressed_texcoord2: bool
    is_meshlet_compressed_texcoord3: bool
    is_meshlet_compressed_vertex_color: bool
    is_meshlet_no_tangent: bool
    is_meshlet_use_skinned: bool
    is_meshlet_compressed_skinned: bool
    is_meshlet_use_vertex_color: bool
    is_meshlet_use_texcoord2: bool
    is_meshlet_use_texcoord3: bool
    has_tangent_bits_block: bool
    use_32bit_pos: bool
    use_24bit_pos: bool

    @classmethod
    def read(cls, raw: int, version: MeshMainVersion) -> "MPLYChunkFlags":
        # DD2-family flags leave bit 15 unused.  The same bit denotes an
        # inline per-vertex tangent mask in the later/alternate layouts.
        has_tangent_bits_block = (
            version
            not in (
                MeshMainVersion.DD2_OLD,
                MeshMainVersion.KUNITSUGAMI,
                MeshMainVersion.DD2,
            )
            and bool(raw & (1 << 15))
        )
        return cls(
            raw=raw,
            is_meshlet_compressed_normal=bool(raw & (1 << 0)),
            is_meshlet_compressed_texcoord1=bool(raw & (1 << 1)),
            is_meshlet_compressed_texcoord2=bool(raw & (1 << 2)),
            is_meshlet_compressed_texcoord3=bool(raw & (1 << 3)),
            is_meshlet_compressed_vertex_color=bool(raw & (1 << 4)),
            is_meshlet_no_tangent=bool(raw & (1 << (5 if version >= MeshMainVersion.PRAGMATA else 6))),
            is_meshlet_use_skinned=bool(raw & (1 << (6 if version >= MeshMainVersion.PRAGMATA else 11))),
            is_meshlet_compressed_skinned=bool(raw & (1 << (7 if version >= MeshMainVersion.PRAGMATA else 5))),
            is_meshlet_use_vertex_color=bool(raw & (1 << 8)),
            is_meshlet_use_texcoord2=bool(raw & (1 << 9)),
            is_meshlet_use_texcoord3=bool(raw & (1 << 10)),
            has_tangent_bits_block=has_tangent_bits_block,
            use_32bit_pos=bool(raw & (1 << 12)),
            use_24bit_pos=bool(raw & (1 << 13)),
        )


@dataclass
class MPLYChunk:
    vert_count: int
    positions: List[Tuple[float, float, float]]
    faces: List[int]
    normals: List[Tuple[float, float, float]]
    colors: List[int]


@dataclass
class MPLYClusterHeader:
    vertex_count: int
    index_count: int
    material_id: int
    index_offset_bytes: int


class MPLYParser:
    def __init__(self, header: Header, data: bytes, streaming_data: Optional[bytes] = None):
        self.header = header
        self.data = data
        self.streaming_data = streaming_data
        self.h = BinaryHandler(data)

    @staticmethod
    def _position_size(flags: MPLYChunkFlags) -> int:
        if flags.use_32bit_pos:
            return 4
        if flags.use_24bit_pos:
            return 3
        return 6

    @staticmethod
    def _shared_count(is_compressed: bool, vert_count: int) -> int:
        return 1 if is_compressed else vert_count

    @staticmethod
    def _aligned(pos: int, size: int = 4) -> int:
        return pos + ((size - (pos % size)) % size)

    @staticmethod
    def _decode_normal_block(data: bytes, flags: MPLYChunkFlags, vert_count: int) -> List[Tuple[float, float, float]]:
        stride = 4 if flags.is_meshlet_no_tangent else 8
        shared_count = 1 if flags.is_meshlet_compressed_normal else vert_count
        entries: List[Tuple[float, float, float]] = []
        for i in range(shared_count):
            x, y, z, _ = struct.unpack_from("<4b", data, i * stride)
            length = math.sqrt(x * x + y * y + z * z) or 1.0
            entries.append((x / length, y / length, z / length))
        if shared_count == 1 and vert_count > 1:
            entries *= vert_count
        return entries

    @staticmethod
    def _decode_color_block(data: bytes, is_compressed: bool, vert_count: int) -> List[int]:
        count = 1 if is_compressed else vert_count
        colors = list(data[:count * 4])
        if count == 1 and vert_count > 1:
            colors *= vert_count
        return colors

    @staticmethod
    def _decode_positions(
        data: bytes,
        flags: MPLYChunkFlags,
        center: Tuple[float, float, float],
        relative_aabb: Tuple[float, float, float, float, float, float],
        version: MeshMainVersion,
    ) -> List[Tuple[float, float, float]]:
        if flags.use_24bit_pos:
            raw_positions = [struct.unpack_from("<3B", data, i * 3) for i in range(len(data) // 3)]
            pos_array = [(x / 255.0, y / 255.0, z / 255.0) for x, y, z in raw_positions]
        elif flags.use_32bit_pos:
            inv_10bit = 1.0 / 1023.0
            pos_array = []
            for i in range(len(data) // 4):
                packed = struct.unpack_from("<I", data, i * 4)[0]
                pos_array.append((
                    (packed & 0x3FF) * inv_10bit,
                    ((packed >> 10) & 0x3FF) * inv_10bit,
                    ((packed >> 20) & 0x3FF) * inv_10bit,
                ))
        else:
            raw_positions = [struct.unpack_from("<3H", data, i * 6) for i in range(len(data) // 6)]
            pos_array = [(x / 65535.0, y / 65535.0, z / 65535.0) for x, y, z in raw_positions]

        if version < MeshMainVersion.MHWILDS:
            scale_byte = (flags.raw >> 16) & 0xFF
            scale = math.ldexp(1.0, scale_byte - 127)
            return [
                (
                    (x - 0.5) * scale + center[0],
                    (y - 0.5) * scale + center[1],
                    (z - 0.5) * scale + center[2],
                )
                for x, y, z in pos_array
            ]

        # Credit: shadowcookie for the compressed position decode.
        div_byte = (flags.raw >> 24) & 0xFF
        mult_byte = (flags.raw >> 16) & 0xFF
        scale = math.ldexp(1.0, div_byte - 127)
        offset = math.ldexp(1.0, mult_byte - div_byte)

        offset_terms = (
            ((relative_aabb[0] / 65535.0) - 0.5) * offset,
            ((relative_aabb[2] / 65535.0) - 0.5) * offset,
            ((relative_aabb[4] / 65535.0) - 0.5) * offset,
        )

        if flags.use_24bit_pos:
            quant_scale = 1.0 / 256.0
        elif flags.use_32bit_pos:
            quant_scale = 1.0 / 64.0
        else:
            quant_scale = 1.0
        
        return [
            (
                (((x - 0.5) * quant_scale) + offset_terms[0]) * scale + center[0],
                (((y - 0.5) * quant_scale) + offset_terms[1]) * scale + center[1],
                (((z - 0.5) * quant_scale) + offset_terms[2]) * scale + center[2],
            )
            for x, y, z in pos_array
        ]

    def _read_meshlet_layout(self) -> Tuple[int, int, List[int]]:
        self.h.seek(self.header.mesh_offset)
        blob_base = self.h.read_int64()
        self.h.skip(12)
        lod_num = self.h.read_uint32() & 0xFF
        self.h.skip(12)
        self.h.read_uint32()
        lod_offsets = [self.h.read_uint32() for _ in range(8)]
        if self.header.format_version >= MeshMainVersion.MHWILDS:
            self.h.skip(64 + 16)
        else:
            self.h.skip(64)
        blob_size = self.h.read_uint32()
        self.h.read_uint32()
        if lod_num > 8 or blob_base < 0 or blob_base + blob_size > len(self.data):
            raise ValueError("Invalid MPLY meshlet layout")
        return blob_base, blob_size, lod_offsets[:lod_num]

    def _parse_chunk(self, start: int, end: int) -> MPLYChunk:
        if not 0 <= start < end <= len(self.data):
            raise ValueError(f"Invalid MPLY chunk range [{start}, {end})")
        self.h.seek(start)
        center = tuple(self.h.read_vec3())
        vert_count = self.h.read_uint8()
        face_count = self.h.read_uint8()
        self.h.skip(2)
        relative_aabb = [self.h.read_uint16() for _ in range(6)]
        flags = MPLYChunkFlags.read(self.h.read_uint32(), self.header.format_version)

        faces = list(self.h.read_bytes(face_count * 3))
        self.h.seek(self._aligned(self.h.tell))

        positions = self._decode_positions(
            self.h.read_bytes(vert_count * self._position_size(flags)),
            flags,
            center,
            tuple(relative_aabb),
            self.header.format_version,
        )
        self.h.seek(self._aligned(self.h.tell))

        normal_count = self._shared_count(flags.is_meshlet_compressed_normal, vert_count)
        normal_stride = 4 if flags.is_meshlet_no_tangent else 8
        normals = self._decode_normal_block(self.h.read_bytes(normal_count * normal_stride), flags, vert_count)

        if flags.has_tangent_bits_block and (self.header.format_version < MeshMainVersion.PRAGMATA or flags.is_meshlet_no_tangent):
            self.h.skip(((vert_count + 31) // 32) * 4)

        uv0_count = self._shared_count(flags.is_meshlet_compressed_texcoord1, vert_count)
        self.h.skip(uv0_count * 4)
        if flags.is_meshlet_use_texcoord2:
            self.h.skip(self._shared_count(flags.is_meshlet_compressed_texcoord2, vert_count) * 4)
        if flags.is_meshlet_use_texcoord3:
            self.h.skip(self._shared_count(flags.is_meshlet_compressed_texcoord3, vert_count) * 4)

        colors: List[int] = []
        if flags.is_meshlet_use_vertex_color:
            color_count = self._shared_count(flags.is_meshlet_compressed_vertex_color, vert_count)
            colors = self._decode_color_block(self.h.read_bytes(color_count * 4), flags.is_meshlet_compressed_vertex_color, vert_count)
        if flags.is_meshlet_use_skinned:
            self.h.skip(self._shared_count(flags.is_meshlet_compressed_skinned, vert_count) * 16)

        if self.h.tell > end:
            raise ValueError(f"MPLY chunk data exceeds its boundary at {end}")
        self.h.seek(end)
        return MPLYChunk(vert_count, positions, faces, normals, colors)

    def _read_cluster_headers(self) -> List[List[MPLYClusterHeader]]:
        if not self.header.meshlet_bvh_offset:
            raise ValueError("MPLY has no cluster headers")
        self.h.seek(self.header.meshlet_bvh_offset + 32)
        counts = []
        for _ in range(8):
            if self.header.format_version == MeshMainVersion.KUNITSUGAMI or self.header.format_version >= MeshMainVersion.MHWILDS:
                counts.append(self.h.read_uint16())
                self.h.skip(2)
            else:
                counts.append(self.h.read_uint32())
        cluster_offsets = [self.h.read_uint32() for _ in range(8)]
        self.h.skip(32)
        gpu_cluster_headers = struct.unpack_from("<Q", self.data, self.header.meshlet_bvh_offset)[0]
        lod_headers: List[List[MPLYClusterHeader]] = []
        for lod_index, count in enumerate(counts):
            headers: List[MPLYClusterHeader] = []
            if count:
                self.h.seek(gpu_cluster_headers + cluster_offsets[lod_index])
                for _ in range(count):
                    word0 = self.h.read_uint32()
                    self.h.read_uint32()
                    self.h.read_uint32()
                    index_offset = self.h.read_uint32()
                    headers.append(MPLYClusterHeader(
                        vertex_count=word0 & 0xFF,
                        index_count=(word0 >> 8) & 0x1FF,
                        material_id=(word0 >> 24) & 0xFF,
                        index_offset_bytes=index_offset,
                    ))
            lod_headers.append(headers)
        return lod_headers

    def _read_streaming_faces(
        self,
        cluster_header: MPLYClusterHeader,
    ) -> Optional[List[int]]:
        if self.streaming_data is None or cluster_header.index_count == 0:
            return None
        start = cluster_header.index_offset_bytes
        data = _checked_slice(
            self.streaming_data,
            start,
            cluster_header.index_count * 2,
            "MPLY streaming indices",
        )
        return list(struct.unpack(f"<{cluster_header.index_count}H", data))

    def parse(self) -> Tuple[MeshBuffer, List[MeshData]]:
        blob_base, blob_size, lod_offsets = self._read_meshlet_layout()
        cluster_headers = self._read_cluster_headers()
        mesh_buffer = MeshBuffer(self.header.format_version)
        lods: List[MeshLOD] = []
        all_positions = array("f")
        all_normals = array("f")
        all_colors = array("B")
        all_faces = array("I")
        vertex_base = 0

        lod_chunk_offsets: List[Optional[List[int]]] = []
        all_chunk_offsets: List[int] = []
        for lod_offset in lod_offsets:
            if lod_offset == 0:
                lod_chunk_offsets.append(None)
                continue
            self.h.seek(blob_base + lod_offset)
            offsets = [self.h.read_uint32() for _ in range(self.h.read_uint32())]
            lod_chunk_offsets.append(offsets)
            all_chunk_offsets.extend(offsets)

        sorted_boundaries = sorted({off for off in all_chunk_offsets if off} | {off for off in lod_offsets if off} | {blob_size})
        for lod_index, chunk_offsets in enumerate(lod_chunk_offsets):
            if chunk_offsets is None:
                continue
            lod = MeshLOD(mesh_buffer, self.header.format_version)
            lod.lod_level = lod_index
            group = MeshGroup(mesh_buffer, self.header.format_version)
            lod_cluster_headers = cluster_headers[lod_index]
            if len(lod_cluster_headers) != len(chunk_offsets):
                raise ValueError(
                    f"MPLY LOD {lod_index}: {len(chunk_offsets)} chunks, "
                    f"{len(lod_cluster_headers)} cluster headers"
                )
            for chunk_index, (chunk_offset, cluster_header) in enumerate(
                zip(chunk_offsets, lod_cluster_headers)
            ):
                start = blob_base + chunk_offset
                end = blob_base + sorted_boundaries[
                    bisect_right(sorted_boundaries, chunk_offset)
                ]
                chunk = self._parse_chunk(start, end)
                if chunk.vert_count != cluster_header.vertex_count:
                    raise ValueError(
                        f"MPLY LOD {lod_index} chunk {chunk_index}: "
                        f"{chunk.vert_count}/{cluster_header.vertex_count} vertices"
                    )
                streaming_faces = self._read_streaming_faces(cluster_header)
                if streaming_faces is not None:
                    chunk.faces = streaming_faces
                all_positions.extend(v for pos in chunk.positions for v in pos)
                all_normals.extend(v for normal in chunk.normals for v in normal)
                all_colors.extend(chunk.colors or [255] * (4 * chunk.vert_count))
                face_offset = len(all_faces)
                all_faces.extend(chunk.faces)
                sm = Submesh(mesh_buffer, self.header.format_version)
                sm.material_index = cluster_header.material_id
                sm.indices_count = len(chunk.faces)
                sm.faces_index_offset = face_offset
                sm.verts_index_offset = vertex_base
                sm.vert_count = chunk.vert_count
                group.submeshes.append(sm)
                group.vertex_count += chunk.vert_count
                group.face_count += len(chunk.faces)
                vertex_base += chunk.vert_count
            lod.mesh_groups.append(group)
            lods.append(lod)

        mesh_buffer.positions = all_positions
        mesh_buffer.normals = all_normals
        mesh_buffer.colors = all_colors
        mesh_buffer.faces = array("H")
        mesh_buffer.integer_faces = all_faces
        mesh_buffer.has_32bit_indices = True
        mesh_buffer.buffer_payloads[0] = MeshBufferPayload(
            positions=all_positions,
            normals=all_normals,
            colors=all_colors,
            faces=array("H"),
            integer_faces=all_faces,
        )
        mesh_data = MeshData(mesh_buffer, self.header.format_version)
        mesh_data.lods = lods
        mesh_data.material_count = self.header.name_count
        return mesh_buffer, [mesh_data]

@dataclass
class JointNode:
    index: int
    parent_index: int
    sibling_index: int
    child_index: int
    symmetry_index: int
    use_secondary_weight: int
    reserved: bytes = b"\x00" * 5

class MeshFile:
    def __init__(self):
        self.header = Header()
        self.streaming_info: Optional[MeshStreamingInfo] = None
        self.mesh_buffer: Optional[MeshBuffer] = None
        self.meshes: List[MeshData] = []
        self.material_names: List[str] = []
        self.names: List[str] = []
        self.name_offsets: List[int] = []
        self.material_indices: List[int] = []

        self.shadow_lod_bytes: bytes = b""
        self.occluder_mesh_bytes: bytes = b""
        self.bones_bytes: bytes = b""
        self.normal_recalc_bytes: bytes = b""
        self.blend_shape_bytes: bytes = b""
        self.bounds_bytes: bytes = b""
        self.float_bytes: bytes = b""
        self.bone_indices: List[int] = []        
        self.bone_remap_indices: List[int] = []
        self.bones: List["JointNode"] = []
        self.local_matrices: List[List[float]] = []
        self.world_matrices: List[List[float]] = []
        self.inverse_bind_matrices: List[List[float]] = []
        self.joint_count: int = 0
        self.bone_remap_count: int = 0
        self.blend_shape_indices: List[int] = []
        self.streaming_data_loaded = False
        self.streaming_buffer_count = 0
        
    def _section_size(self, start: int, sorted_offsets: List[int]) -> int:
        for offset in sorted_offsets:
            if offset > start:
                return offset - start
        return 0

    def _collect_section_offsets(self) -> List[int]:
        offsets = [
            self.header.lods_offset,
            self.header.shadow_lods_offset,
            self.header.occluder_mesh_offset,
            self.header.bones_offset,
            self.header.normal_recalc_offset,
            self.header.blend_shapes_offset,
            self.header.bounds_offset,
            self.header.mesh_offset,
            self.header.floats_offset,
            self.header.material_indices_offset,
            self.header.bone_indices_offset,
            self.header.blend_shape_indices_offset,
            self.header.name_offsets_offset,
            self.header.streaming_info_offset,
        ]
        offsets = [offset for offset in offsets if offset > 0]
        offsets.sort()
        offsets.append(self.header.file_size)
        return offsets

    def _read_joint_nodes(self, h: BinaryHandler, offset: int, count: int) -> List["JointNode"]:
        with h.seek_temp(offset):
            nodes: List[JointNode] = []
            for _ in range(count):
                node = JointNode(
                    index=h.read_int16(),
                    parent_index=h.read_int16(),
                    sibling_index=h.read_int16(),
                    child_index=h.read_int16(),
                    symmetry_index=h.read_int16(),
                    use_secondary_weight=h.read_uint8(),
                    reserved=h.read_bytes(5),
                )
                nodes.append(node)
            return nodes

    def _read_matrices(self, h: BinaryHandler, offset: int, count: int) -> List[List[float]]:
        with h.seek_temp(offset):
            return [h.read_matrix4x4() for _ in range(count)]

    def _parse_bones(self, h: BinaryHandler):
        self.bones = []
        self.bone_remap_indices = []
        self.local_matrices = []
        self.world_matrices = []
        self.inverse_bind_matrices = []
        if not self.header.bones_offset:
            return

        with h.seek_temp(self.header.bones_offset):
            self.joint_count = h.read_int32()
            self.bone_remap_count = h.read_int32()
            h.read_int32()
            h.read_int32()
            hierarchy_offset = h.read_int64()
            local_matrix_offset = h.read_int64()
            world_matrix_offset = h.read_int64()
            inv_world_matrix_offset = h.read_int64()

            if self.bone_remap_count > 0:
                self.bone_remap_indices = [h.read_int16() for _ in range(self.bone_remap_count)]
                h.align(8)

            self.bones = self._read_joint_nodes(h, hierarchy_offset, self.joint_count)
            self.local_matrices = self._read_matrices(h, local_matrix_offset, self.joint_count)
            self.world_matrices = self._read_matrices(h, world_matrix_offset, self.joint_count)
            self.inverse_bind_matrices = self._read_matrices(h, inv_world_matrix_offset, self.joint_count)

    def _parse_bone_indices(self, data: bytes, sorted_offsets: List[int]):
        self.bone_indices = []
        if not self.header.bone_indices_offset or self.joint_count <= 0:
            return
        size = self.joint_count * 2
        if self._section_size(self.header.bone_indices_offset, sorted_offsets) < size:
            raise ValueError("Bone-index section is too small")
        raw = _checked_slice(data, self.header.bone_indices_offset, size, "Bone indices")
        self.bone_indices = list(struct.unpack(f'<{self.joint_count}H', raw))

    def read(
        self,
        data: bytes,
        *,
        file_version: int,
        read_extras: bool = False,
        streaming_data: Optional[bytes] = None,
    ) -> bool:
        self.joint_count = 0
        self.bone_remap_count = 0
        h = BinaryHandler(data, file_version=file_version)
        if not self.header.read(h, file_version=file_version):
            raise ValueError("Not a mesh file")

        if self.header.streaming_info_offset:
            h.seek(self.header.streaming_info_offset)
            self.streaming_info = MeshStreamingInfo()
            self.streaming_info.read(h)
            self.streaming_buffer_count = len(self.streaming_info.entries)
            self.streaming_data_loaded = bool(streaming_data) and self.streaming_buffer_count > 0

        if self.header.magic == MPLY_MAGIC and self.header.mesh_offset:
            self.mesh_buffer, self.meshes = MPLYParser(self.header, data, streaming_data=streaming_data).parse()
        elif self.header.mesh_offset:
            h.seek(self.header.mesh_offset)
            self.mesh_buffer = MeshBuffer(self.header.format_version)
            self.mesh_buffer.read(
                h,
                streaming_info=self.streaming_info,
                streaming_data=streaming_data,
            )

        if self.header.lods_offset and self.mesh_buffer:
            h.seek(self.header.lods_offset)
            md = MeshData(self.mesh_buffer, self.header.format_version)
            md.read(h, read_all_lods=read_extras)
            self.meshes.append(md)

        sorted_offsets = self._collect_section_offsets()
        self._parse_bones(h)
        self._parse_bone_indices(data, sorted_offsets)
        
        if read_extras:
            if self.header.shadow_lods_offset:
                size = self._section_size(self.header.shadow_lods_offset, sorted_offsets)
                h.seek(self.header.shadow_lods_offset)
                self.shadow_lod_bytes = h.read_bytes(size)
            if self.header.occluder_mesh_offset:                
                size = self._section_size(self.header.occluder_mesh_offset, sorted_offsets)
                h.seek(self.header.occluder_mesh_offset)
                self.occluder_mesh_bytes = h.read_bytes(size)
            if self.header.bones_offset:
                size = self._section_size(self.header.bones_offset, sorted_offsets)
                h.seek(self.header.bones_offset)
                self.bones_bytes = h.read_bytes(size)
            if self.header.normal_recalc_offset:
                size = self._section_size(self.header.normal_recalc_offset, sorted_offsets)
                h.seek(self.header.normal_recalc_offset)
                self.normal_recalc_bytes = h.read_bytes(size)
            if self.header.blend_shapes_offset:
                size = self._section_size(self.header.blend_shapes_offset, sorted_offsets)
                h.seek(self.header.blend_shapes_offset)
                self.blend_shape_bytes = h.read_bytes(size)
            if self.header.bounds_offset:
                size = self._section_size(self.header.bounds_offset, sorted_offsets)
                h.seek(self.header.bounds_offset)
                self.bounds_bytes = h.read_bytes(size)
            if self.header.floats_offset:
                size = self._section_size(self.header.floats_offset, sorted_offsets)
                h.seek(self.header.floats_offset)
                self.float_bytes = h.read_bytes(size)
            if self.header.blend_shape_indices_offset:
                size = self._section_size(self.header.blend_shape_indices_offset, sorted_offsets)
                start = self.header.blend_shape_indices_offset
                fmt = '<{}H'.format(size // 2)
                self.blend_shape_indices = list(struct.unpack_from(fmt, data, start))

        if self.header.name_offsets_offset:
            start = self.header.name_offsets_offset
            h.seek(start)
            self.name_offsets = [h.read_int64() for _ in range(self.header.name_count)]
            for off in self.name_offsets:
                with h.seek_temp(off):
                    self.names.append(h.read_string())
            if self.header.material_indices_offset and self.meshes:
                h.seek(self.header.material_indices_offset)
                mcount = self.meshes[0].material_count
                self.material_indices = [h.read_int16() for _ in range(mcount)]
                for idx in self.material_indices:
                    if 0 <= idx < len(self.names):
                        self.material_names.append(self.names[idx])

        return True
