import struct
from array import array
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Dict, List, Tuple, Optional

from utils.binary_handler import BinaryHandler
from utils.native_build import ensure_fastmesh
ensure_fastmesh()
from fastmesh import (
    unpack_normals_tangents,
    pack_normals_tangents,
    unpack_uvs,
    pack_uvs,
    unpack_colors,
    pack_colors,
)

MESH_MAGIC = 0x4853454D
MPLY_MAGIC = 0x4D504C59


class MeshMainVersion(IntEnum):
    UNKNOWN = 0
    RE7 = 1
    DMC5 = 2
    RE_RT = 3
    RE8 = 4
    SF6 = 5
    DD2 = 6
    MHWILDS = 7


def get_mesh_version(internal_version: int) -> MeshMainVersion:
    mapping = {
        386270720: MeshMainVersion.DMC5,
        21041600: MeshMainVersion.RE_RT,
        2020091500: MeshMainVersion.RE8,
        220822879: MeshMainVersion.SF6,
        230517984: MeshMainVersion.DD2,
        240704828: MeshMainVersion.MHWILDS,
    }
    return mapping.get(internal_version, MeshMainVersion.MHWILDS)


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

    format_version: MeshMainVersion = MeshMainVersion.UNKNOWN

    def read(self, h: BinaryHandler) -> bool:
        self.magic = h.read_uint32()
        if self.magic != MESH_MAGIC:
            return False
        self.version = h.read_uint32()
        self.file_size = h.read_uint32()
        self.lod_hash = h.read_uint32()
        self.format_version = get_mesh_version(self.version)

        if self.format_version < MeshMainVersion.SF6:
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
            self.streaming_info_offset = h.read_int64()
        else:
            self.flags = h.read_int16()
            self.ukn = h.read_int16()
            self.name_count = h.read_int16()
            h.skip(2)
            self.mesh_group_offset = h.read_int64()
            self.lods_offset = h.read_int64()
            self.shadow_lods_offset = h.read_int64()
            self.occluder_mesh_offset = h.read_int64()
            self.normal_recalc_offset = h.read_int64()
            self.blend_shapes_offset = h.read_int64()
            self.mesh_offset = h.read_int64()
            h.skip(8)
            self.floats_offset = h.read_int64()
            self.bounds_offset = h.read_int64()
            self.bones_offset = h.read_int64()
            self.material_indices_offset = h.read_int64()
            self.bone_indices_offset = h.read_int64()
            self.blend_shape_indices_offset = h.read_int64()
            self.streaming_info_offset = h.read_int64()
            self.name_offsets_offset = h.read_int64()
            self.vertices_offset = h.read_int64()
            h.skip(8)
        return True

    def write(self, h: BinaryHandler):
        h.write_uint32(self.magic)
        h.write_uint32(self.version)
        h.write_uint32(self.file_size)
        h.write_uint32(self.lod_hash)
        if self.format_version < MeshMainVersion.SF6:
            h.write_int16(self.flags)
            h.write_int16(self.name_count)
            h.write_int32(self.ukn)
            h.write_int64(self.lods_offset)
            h.write_int64(self.shadow_lods_offset)
            h.write_int64(self.occluder_mesh_offset)
            h.write_int64(self.bones_offset)
            h.write_int64(self.normal_recalc_offset)
            h.write_int64(self.blend_shapes_offset)
            h.write_int64(self.bounds_offset)
            h.write_int64(self.mesh_offset)
            h.write_int64(self.floats_offset)
            h.write_int64(self.material_indices_offset)
            h.write_int64(self.bone_indices_offset)
            h.write_int64(self.blend_shape_indices_offset)
            h.write_int64(self.name_offsets_offset)
            h.write_int64(self.streaming_info_offset)
        else:
            h.write_int16(self.flags)
            h.write_int16(self.ukn)
            h.write_int16(self.name_count)
            h.write_int16(0)
            h.write_int64(self.mesh_group_offset)
            h.write_int64(self.lods_offset)
            h.write_int64(self.shadow_lods_offset)
            h.write_int64(self.occluder_mesh_offset)
            h.write_int64(self.normal_recalc_offset)
            h.write_int64(self.blend_shapes_offset)
            h.write_int64(self.mesh_offset)
            h.write_int64(0)
            h.write_int64(self.floats_offset)
            h.write_int64(self.bounds_offset)
            h.write_int64(self.bones_offset)
            h.write_int64(self.material_indices_offset)
            h.write_int64(self.bone_indices_offset)
            h.write_int64(self.blend_shape_indices_offset)
            h.write_int64(self.streaming_info_offset)
            h.write_int64(self.name_offsets_offset)
            h.write_int64(self.vertices_offset)
            h.write_int64(0)


@dataclass
class StreamingMeshEntry:
    start: int
    end: int


@dataclass
class MeshStreamingInfo:
    unkn1: int = 0
    entries: List[StreamingMeshEntry] = field(default_factory=list)
    entry_offset: int = 0

    def read(self, h: BinaryHandler) -> bool:
        entry_count = h.read_int32()
        self.unkn1 = h.read_int32()
        self.entry_offset = h.read_int64()
        with h.seek_temp(self.entry_offset):
            self.entries = [
                StreamingMeshEntry(h.read_uint32(), h.read_uint32())
                for _ in range(entry_count)
            ]
        return True

    def write(self, h: BinaryHandler):
        h.write_int32(len(self.entries))
        h.write_int32(self.unkn1)
        h.write_int64(self.entry_offset)
        cur = h.tell
        h.seek(self.entry_offset)
        for e in self.entries:
            h.write_uint32(e.start)
            h.write_uint32(e.end)
        h.seek(cur)


class VertexBufferType(IntEnum):
    Position = 0
    NormalsTangents = 1
    UV0 = 2
    UV1 = 3
    BoneWeights = 4
    Colors = 5


@dataclass
class MeshBufferItemHeader:
    type: VertexBufferType
    size: int
    offset: int


class MeshBuffer:
    def __init__(self, version: MeshMainVersion):
        self.version = version
        self.element_headers_offset = 0
        self.vertex_buffer_offset = 0
        self.face_buffer_offset = 0
        self.vertex_buffer_size = 0
        self.face_vert_buffer_header_size = 0
        self.element_count = 0
        self.element_count2 = 0
        self.ukn1 = 0
        self.ukn2 = 0
        self.blend_shape_offset = 0
        self.buffer_headers: List[MeshBufferItemHeader] = []

        self.positions: array = array("f")
        self.normals: array = array("f")
        self.tangents: array = array("f")
        self.normal_ws: array = array("B")
        self.tangent_ws: array = array("B")
        self.uv0: array = array("d")
        self.uv1: array = array("d")
        self.uv0_special: Dict[int, int] = {}
        self.uv1_special: Dict[int, int] = {}
        self.colors: array = array("B")
        self.faces: array = array("H")

    def read(self, h: BinaryHandler, *, preserve_signs: bool = False) -> bool:
        self.element_headers_offset = h.read_int64()
        self.vertex_buffer_offset = h.read_int64()
        if self.version >= MeshMainVersion.SF6:
            h.read_int64()  # uknOffset
            self.vertex_buffer_size = h.read_int32()
            # face buffer offset is stored relative to vertex_buffer_offset
            self.face_buffer_offset = h.read_int32() + self.vertex_buffer_offset
        else:
            self.face_buffer_offset = h.read_int64()
            if self.version == MeshMainVersion.RE_RT:
                self.ukn1 = h.read_int32()
                self.ukn2 = h.read_int32()
            self.vertex_buffer_size = h.read_int32()
            self.face_vert_buffer_header_size = h.read_int32()
        self.element_count = h.read_int16()
        self.element_count2 = h.read_int16()

        with h.seek_temp(self.element_headers_offset):
            self.buffer_headers = []
            for _ in range(self.element_count):
                t = VertexBufferType(h.read_int16())
                sz = h.read_int16()
                off = h.read_int32()
                self.buffer_headers.append(MeshBufferItemHeader(t, sz, off))

        base = h.data

        vert_count = 0
        for i, bh in enumerate(self.buffer_headers):
            if bh.type != VertexBufferType.Position:
                continue
            start = self.vertex_buffer_offset + bh.offset
            if i == self.element_count - 1:
                end = start + (self.vertex_buffer_size - bh.offset)
            else:
                end = self.vertex_buffer_offset + self.buffer_headers[i + 1].offset
            data = memoryview(base)[start:end]
            self.positions = array("f")
            self.positions.frombytes(data)
            vert_count = len(self.positions) // 3
            break

        for i, bh in enumerate(self.buffer_headers):
            if bh.type == VertexBufferType.Position:
                continue
            start = self.vertex_buffer_offset + bh.offset
            if i < self.element_count - 1:
                end = self.vertex_buffer_offset + self.buffer_headers[i + 1].offset
            else:
                end = start + bh.size * vert_count
            data = memoryview(base)[start:end]
            if bh.type == VertexBufferType.NormalsTangents:
                self.normals, self.normal_ws, self.tangents, self.tangent_ws = unpack_normals_tangents(data)
            elif bh.type == VertexBufferType.UV0:
                if preserve_signs:
                    shorts = array("H")
                    shorts.frombytes(data)
                    self.uv0_special = {}
                    for idx, s in enumerate(shorts):
                        is_neg_zero = s == 0x8000
                        is_nan = (s & 0x7C00) == 0x7C00 and (s & 0x03FF)
                        if is_neg_zero or is_nan:
                            self.uv0_special[idx] = s
                self.uv0 = unpack_uvs(data)
            elif bh.type == VertexBufferType.UV1:
                if preserve_signs:
                    shorts = array("H")
                    shorts.frombytes(data)
                    self.uv1_special = {}
                    for idx, s in enumerate(shorts):
                        is_neg_zero = s == 0x8000
                        is_nan = (s & 0x7C00) == 0x7C00 and (s & 0x03FF)
                        if is_neg_zero or is_nan:
                            self.uv1_special[idx] = s
                self.uv1 = unpack_uvs(data)
            elif bh.type == VertexBufferType.Colors:
                self.colors = unpack_colors(data)
        return True

    def write(self, h: BinaryHandler, *, preserve_signs: bool = False):
        h.write_int64(self.element_headers_offset)
        h.write_int64(self.vertex_buffer_offset)
        if self.version >= MeshMainVersion.SF6:
            h.write_int64(0)
            h.write_int32(self.vertex_buffer_size)
            h.write_int32(self.face_buffer_offset - self.vertex_buffer_offset)
        else:
            h.write_int64(self.face_buffer_offset)
            if self.version == MeshMainVersion.RE_RT:
                h.write_int32(self.ukn1)
                h.write_int32(self.ukn2)
            h.write_int32(self.vertex_buffer_size)
            h.write_int32(self.face_vert_buffer_header_size)
        h.write_int16(self.element_count)
        h.write_int16(self.element_count2)

        cur = h.tell
        h.seek(self.element_headers_offset)
        for bh in self.buffer_headers:
            h.write_int16(bh.type)
            h.write_int16(bh.size)
            h.write_int32(bh.offset)

        for bh in self.buffer_headers:
            start = self.vertex_buffer_offset + bh.offset
            h.seek(start)
            if bh.type == VertexBufferType.Position:
                h.write_bytes(self.positions.tobytes())
            elif bh.type == VertexBufferType.NormalsTangents:
                h.write_bytes(
                    pack_normals_tangents(
                        self.normals, self.normal_ws, self.tangents, self.tangent_ws
                    )
                )
            elif bh.type == VertexBufferType.UV0:
                buf = bytearray(pack_uvs(self.uv0))
                if preserve_signs and self.uv0_special:
                    shorts = array("H")
                    shorts.frombytes(buf)
                    for i, raw in self.uv0_special.items():
                        shorts[i] = raw
                    buf = bytearray(shorts.tobytes())
                h.write_bytes(buf)
            elif bh.type == VertexBufferType.UV1:
                buf = bytearray(pack_uvs(self.uv1))
                if preserve_signs and self.uv1_special:
                    shorts = array("H")
                    shorts.frombytes(buf)
                    for i, raw in self.uv1_special.items():
                        shorts[i] = raw
                    buf = bytearray(shorts.tobytes())
                h.write_bytes(buf)
            elif bh.type == VertexBufferType.Colors:
                h.write_bytes(pack_colors(self.colors))

        h.seek(self.face_buffer_offset)
        h.write_bytes(self.faces.tobytes())

        h.seek(cur)


class Submesh:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.material_index = 0
        self.ukn = 0
        self.streaming_offset = 0
        self.streaming_offset2 = 0
        self.ukn2 = 0
        self.indices_count = 0
        self.faces_index_offset = 0
        self.verts_index_offset = 0
        self.vert_count = 0

    def read(self, h: BinaryHandler):
        self.material_index = h.read_uint16()
        self.ukn = h.read_uint16()
        self.indices_count = h.read_int32()
        self.faces_index_offset = h.read_int32()
        self.verts_index_offset = h.read_int32()
        if self.version >= MeshMainVersion.RE_RT:
            self.streaming_offset = h.read_int32()
            self.streaming_offset2 = h.read_int32()
        if self.version >= MeshMainVersion.DD2:
            self.ukn2 = h.read_int32()

    def write(self, h: BinaryHandler):
        h.write_uint16(self.material_index)
        h.write_uint16(self.ukn)
        h.write_int32(self.indices_count)
        h.write_int32(self.faces_index_offset)
        h.write_int32(self.verts_index_offset)
        if self.version >= MeshMainVersion.RE_RT:
            h.write_int32(self.streaming_offset)
            h.write_int32(self.streaming_offset2)
        if self.version >= MeshMainVersion.DD2:
            h.write_int32(self.ukn2)


class MeshGroup:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.group_id = 0
        self.submesh_count = 0
        self.vertex_count = 0
        self.face_count = 0
        self.submeshes: List[Submesh] = []
        self.mesh_vertex_offset = 0
        self.offset = 0
        self.unk_bytes = b""

    def read(self, h: BinaryHandler):
        self.offset = h.tell
        self.group_id = h.read_uint8()
        self.submesh_count = h.read_uint8()
        self.unk_bytes = h.read_bytes(6)
        self.vertex_count = h.read_int32()
        self.face_count = h.read_int32()
        for _ in range(self.submesh_count):
            sm = Submesh(self.buffer, self.version)
            sm.read(h)
            self.submeshes.append(sm)
        for i, sm in enumerate(self.submeshes):
            if i < self.submesh_count - 1:
                sm.vert_count = self.submeshes[i + 1].verts_index_offset - sm.verts_index_offset
            else:
                sm.vert_count = self.vertex_count - (sm.verts_index_offset - self.mesh_vertex_offset)

    def write(self, h: BinaryHandler):
        h.seek(self.offset)
        h.write_uint8(self.group_id)
        h.write_uint8(self.submesh_count)
        if self.unk_bytes:
            h.write_bytes(self.unk_bytes)
        else:
            h.write_bytes(b"\x00" * 6)
        h.write_int32(self.vertex_count)
        h.write_int32(self.face_count)
        for sm in self.submeshes:
            sm.write(h)


class MeshLOD:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.mesh_groups: List[MeshGroup] = []
        self.lod_factor = 0.0
        self.vertex_format = 0
        self.header_offset = 0
        self.mesh_offsets: List[int] = []

    def read(self, h: BinaryHandler):
        base = h.data
        mesh_count = h.read_uint8()
        self.vertex_format = h.read_uint8()
        h.skip(2)
        self.lod_factor = h.read_float()
        self.header_offset = h.read_int64()
        with h.seek_temp(self.header_offset):
            self.mesh_offsets = [h.read_int64() for _ in range(mesh_count)]
        vert_offset = 0
        total_indices = 0
        for off in self.mesh_offsets:
            h.seek(off)
            mg = MeshGroup(self.buffer, self.version)
            mg.mesh_vertex_offset = vert_offset
            mg.read(h)
            self.mesh_groups.append(mg)
            vert_offset += mg.vertex_count
            total_indices += mg.face_count
        start = self.buffer.face_buffer_offset
        size = total_indices * 2
        mv = memoryview(base)[start:start + size]
        self.buffer.faces = array("H")
        self.buffer.faces.frombytes(mv)

    def write(self, h: BinaryHandler):
        h.write_uint8(len(self.mesh_groups))
        h.write_uint8(self.vertex_format)
        h.write_uint16(0)
        h.write_float(self.lod_factor)
        h.write_int64(self.header_offset)
        cur = h.tell
        h.seek(self.header_offset)
        for off in self.mesh_offsets:
            h.write_int64(off)
        h.seek(cur)
        for mg in self.mesh_groups:
            mg.write(h)


class MeshData:
    def __init__(self, buffer: MeshBuffer, version: MeshMainVersion):
        self.buffer = buffer
        self.version = version
        self.lods: List[MeshLOD] = []
        self.lod_count = 0
        self.material_count = 0
        self.uv_count = 0
        self.skin_weight_count = 0
        self.total_mesh_count = 0
        self.ukn1 = 0
        self.bounding_sphere = (0.0, 0.0, 0.0, 0.0)
        self.bounding_box = (
            (0.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 0.0),
        )
        self.lod_offsets_start = 0
        self.lod_offsets: List[int] = []
        self._read_all_lods = False

    def read(self, h: BinaryHandler, *, read_all_lods: bool = False):
        self.lod_count = h.read_uint8()
        self.material_count = h.read_uint8()
        self.uv_count = h.read_uint8()
        self.skin_weight_count = h.read_uint8()
        self.total_mesh_count = h.read_int32()
        if self.version <= MeshMainVersion.DMC5:
            self.ukn1 = h.read_int64()
        self.bounding_sphere = (
            h.read_float(),
            h.read_float(),
            h.read_float(),
            h.read_float(),
        )
        self.bounding_box = (
            (
                h.read_float(),
                h.read_float(),
                h.read_float(),
                h.read_float(),
            ),
            (
                h.read_float(),
                h.read_float(),
                h.read_float(),
                h.read_float(),
            ),
        )
        self.lod_offsets_start = h.read_int64()
        with h.seek_temp(self.lod_offsets_start):
            self.lod_offsets = [h.read_int64() for _ in range(self.lod_count)]

        lod_targets = self.lod_offsets if read_all_lods else self.lod_offsets[:1]
        for off in lod_targets:
            h.seek(off)
            lod = MeshLOD(self.buffer, self.version)
            lod.read(h)
            self.lods.append(lod)

        self._read_all_lods = read_all_lods

    def write(self, h: BinaryHandler):
        h.write_uint8(self.lod_count)
        h.write_uint8(self.material_count)
        h.write_uint8(self.uv_count)
        h.write_uint8(self.skin_weight_count)
        h.write_int32(self.total_mesh_count)
        if self.version <= MeshMainVersion.DMC5:
            h.write_int64(self.ukn1)
        for v in self.bounding_sphere:
            h.write_float(v)
        for v in self.bounding_box[0]:
            h.write_float(v)
        for v in self.bounding_box[1]:
            h.write_float(v)
        h.write_int64(self.lod_offsets_start)
        cur = h.tell
        h.seek(self.lod_offsets_start)
        for off in self.lod_offsets:
            h.write_int64(off)
        h.seek(cur)
        for lod, off in zip(self.lods, self.lod_offsets):
            h.seek(off)
            lod.write(h)


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
        self.blend_shape_indices: List[int] = []

        self._raw: bytes = b""

    def read(self, data: bytes, *, read_extras: bool = False, preserve_signs: bool = False) -> bool:
        self._raw = data
        h = BinaryHandler(data)
        if not self.header.read(h):
            raise ValueError("Not a mesh file")

        if self.header.streaming_info_offset:
            h.seek(self.header.streaming_info_offset)
            self.streaming_info = MeshStreamingInfo()
            self.streaming_info.read(h)

        if self.header.mesh_offset:
            h.seek(self.header.mesh_offset)
            self.mesh_buffer = MeshBuffer(self.header.format_version)
            self.mesh_buffer.read(h, preserve_signs=preserve_signs)

        if self.header.lods_offset and self.mesh_buffer:
            h.seek(self.header.lods_offset)
            md = MeshData(self.mesh_buffer, self.header.format_version)
            md.read(h, read_all_lods=read_extras)
            self.meshes.append(md)

        if read_extras:
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
            offsets = [o for o in offsets if o > 0]
            offsets.sort()
            offsets.append(self.header.file_size)

            def section_size(start: int) -> int:
                for o in offsets:
                    if o > start:
                        return o - start
                return 0

            if self.header.shadow_lods_offset:
                size = section_size(self.header.shadow_lods_offset)
                h.seek(self.header.shadow_lods_offset)
                self.shadow_lod_bytes = h.read_bytes(size)
            if self.header.occluder_mesh_offset:
                size = section_size(self.header.occluder_mesh_offset)
                h.seek(self.header.occluder_mesh_offset)
                self.occluder_mesh_bytes = h.read_bytes(size)
            if self.header.bones_offset:
                size = section_size(self.header.bones_offset)
                h.seek(self.header.bones_offset)
                self.bones_bytes = h.read_bytes(size)
            if self.header.normal_recalc_offset:
                size = section_size(self.header.normal_recalc_offset)
                h.seek(self.header.normal_recalc_offset)
                self.normal_recalc_bytes = h.read_bytes(size)
            if self.header.blend_shapes_offset:
                size = section_size(self.header.blend_shapes_offset)
                h.seek(self.header.blend_shapes_offset)
                self.blend_shape_bytes = h.read_bytes(size)
            if self.header.bounds_offset:
                size = section_size(self.header.bounds_offset)
                h.seek(self.header.bounds_offset)
                self.bounds_bytes = h.read_bytes(size)
            if self.header.floats_offset:
                size = section_size(self.header.floats_offset)
                h.seek(self.header.floats_offset)
                self.float_bytes = h.read_bytes(size)
            if self.header.bone_indices_offset:
                size = section_size(self.header.bone_indices_offset)
                start = self.header.bone_indices_offset
                fmt = '<{}H'.format(size // 2)
                self.bone_indices = list(struct.unpack_from(fmt, data, start))
            if self.header.blend_shape_indices_offset:
                size = section_size(self.header.blend_shape_indices_offset)
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

    def build(self, *, preserve_signs: bool = False) -> bytes:
        base = bytearray(self._raw) if self._raw else bytearray(self.header.file_size or 0)
        handler = BinaryHandler(base)
        self.header.write(handler)
        if self.header.streaming_info_offset and self.streaming_info:
            handler.seek(self.header.streaming_info_offset)
            self.streaming_info.write(handler)
        if self.header.mesh_offset and self.mesh_buffer:
            handler.seek(self.header.mesh_offset)
            self.mesh_buffer.write(handler, preserve_signs=preserve_signs)
        if (
            self.header.lods_offset
            and self.meshes
            and getattr(self.meshes[0], "_read_all_lods", False)
        ):
            handler.seek(self.header.lods_offset)
            self.meshes[0].write(handler)
        if self.header.shadow_lods_offset and self.shadow_lod_bytes:
            handler.seek(self.header.shadow_lods_offset)
            handler.write_bytes(self.shadow_lod_bytes)
        if self.header.occluder_mesh_offset and self.occluder_mesh_bytes:
            handler.seek(self.header.occluder_mesh_offset)
            handler.write_bytes(self.occluder_mesh_bytes)
        if self.header.bones_offset and self.bones_bytes:
            handler.seek(self.header.bones_offset)
            handler.write_bytes(self.bones_bytes)
        if self.header.normal_recalc_offset and self.normal_recalc_bytes:
            handler.seek(self.header.normal_recalc_offset)
            handler.write_bytes(self.normal_recalc_bytes)
        if self.header.blend_shapes_offset and self.blend_shape_bytes:
            handler.seek(self.header.blend_shapes_offset)
            handler.write_bytes(self.blend_shape_bytes)
        if self.header.bounds_offset and self.bounds_bytes:
            handler.seek(self.header.bounds_offset)
            handler.write_bytes(self.bounds_bytes)
        if self.header.floats_offset and self.float_bytes:
            handler.seek(self.header.floats_offset)
            handler.write_bytes(self.float_bytes)
        if self.header.material_indices_offset and self.material_indices:
            handler.seek(self.header.material_indices_offset)
            handler.write_bytes(struct.pack('<{}h'.format(len(self.material_indices)), *self.material_indices))
        if self.header.bone_indices_offset and self.bone_indices:
            handler.seek(self.header.bone_indices_offset)
            handler.write_bytes(struct.pack('<{}H'.format(len(self.bone_indices)), *self.bone_indices))
        if self.header.blend_shape_indices_offset and self.blend_shape_indices:
            handler.seek(self.header.blend_shape_indices_offset)
            handler.write_bytes(struct.pack('<{}H'.format(len(self.blend_shape_indices)), *self.blend_shape_indices))
        if self.header.name_offsets_offset and self.names:
            handler.seek(self.header.name_offsets_offset)
            for off in self.name_offsets:
                handler.write_int64(off)
            for name, off in zip(self.names, self.name_offsets):
                handler.seek(off)
                handler.write_string(name)
        self.header.file_size = len(handler.data)
        handler.seek(0)
        self.header.write(handler)
        return bytes(handler.data)
