import math
import struct
from array import array
from bisect import bisect_right
from copy import copy
from dataclasses import dataclass, field, fields, replace
from enum import Enum, IntEnum
from importlib import import_module
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterable,
    List,
    Optional,
    Sequence,
    Tuple,
)

import numpy as np

from utils.binary_handler import BinaryHandler
from utils.hash_util import murmur3_hash_utf16le
from utils.native_build import ensure_fastmesh
from .profiles import (
    LegacyLightMapLayout,
    MeshBufferLayout,
    MeshContentFlagLayout,
    MeshGameProfile,
    MeshHeaderLayout,
    MeshMainVersion,
    MeshletLodFactorEncoding,
    NormalRecalcLayout,
    coerce_mesh_profile,
    mesh_profile_for_versions,
)
from .models import MESH_MAGIC, MPLY_MAGIC, MeshAABB
from .contract_utils import split_auxiliary_layouts

if TYPE_CHECKING:
    from .blend_shape import BlendShapeData
    from .normal_recalc import NormalRecalcData


_fastmesh = ensure_fastmesh()
_native_unpack_normals_tangents = _fastmesh.unpack_normals_tangents
unpack_colors = _fastmesh.unpack_colors


def unpack_normals_tangents(data):
    normals, normal_ws, tangents, tangent_ws = _native_unpack_normals_tangents(
        data
    )
    # D3D clamps -128/127 to -1.0.
    for vectors in (normals, tangents):
        values = np.frombuffer(vectors, dtype=np.float32)
        np.maximum(values, -1.0, out=values)
    return normals, normal_ws, tangents, tangent_ws


_CANONICAL_CONTRACTS = {
    "mesh": ("canonical_contract", "assert_complete_mesh_{}"),
    "dd2": ("dd2_contract", "assert_dd2_{}"),
    "dd2_updated": ("dd2_updated_contract", "assert_dd2_updated_{}"),
    "mhwilds": ("mhwilds_contract", "assert_mhwilds_{}"),
    "re9": ("re9_contract", "assert_re9_{}"),
    "pragmata": ("pragmata_contract", "assert_pragmata_{}"),
    "onimusha_wots": ("onimusha_wots_contract", "assert_onimusha_wots_{}"),
}


class MeshLoadMode(str, Enum):
    LIGHT = "light"
    FULL = "full"

    @classmethod
    def coerce(cls, value: "MeshLoadMode | str") -> "MeshLoadMode":
        if isinstance(value, cls):
            return value
        try:
            return cls(str(value).strip().lower())
        except ValueError as exc:
            raise ValueError(f"Unknown mesh load mode: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class MeshSemanticNote:
    section: str
    field: str
    message: str


@dataclass(frozen=True, slots=True)
class MeshContentFlags:
    raw: int
    is_skinning: bool
    has_joint: bool
    has_blend_shape: bool
    has_vertex_group: bool
    quad_enable: bool
    streaming_bvh: bool
    has_tertiary_uv: bool
    has_lightmap_uv: bool
    has_vertex_color: bool
    solved_offset: bool
    buffer_count: int
    high_mesh_count: Optional[int]
    use_ray_tracing_vertex_animation: bool
    use_sdf: bool
    enable_rebraiding: int
    merge_static_mesh_bvh: bool
    prefer_opaque: int
    prefer_shadow_cast: int
    prefer_alpha_mask: int
    reserved_bits: int

    @classmethod
    def from_raw(cls, raw: int, profile: MeshGameProfile, *, header_buffer_count: int = 0) -> "MeshContentFlags":
        layout = profile.content_flag_layout
        buffer_count_includes_resident = profile.content_buffer_count_includes_resident
        raw &= 0x00FFFFFF
        re8_layout = layout is MeshContentFlagLayout.RE8
        sf6_layout = layout is MeshContentFlagLayout.SF6
        wilds_layout = layout is MeshContentFlagLayout.WILDS
        separate_count = layout is MeshContentFlagLayout.DD2_UPDATED
        lightmap_layout = sf6_layout or wilds_layout or separate_count
        bit6 = bool(raw & (1 << 6))
        return cls(
            raw=raw,
            is_skinning=bool(raw & (1 << 0)),
            has_joint=bool(raw & (1 << 1)),
            has_blend_shape=bool(raw & (1 << 2)),
            has_vertex_group=bool(raw & (1 << 3)),
            quad_enable=bool(raw & (1 << 4)),
            streaming_bvh=bool(raw & (1 << 5)),
            has_tertiary_uv=bit6 and not lightmap_layout,
            has_lightmap_uv=bit6 and lightmap_layout,
            has_vertex_color=bool(raw & (1 << 7)) and not re8_layout,
            solved_offset=bool(raw & (1 << 8)),
            buffer_count=header_buffer_count if separate_count else (raw >> 9) & 0x7,
            # 3-bit counter: Wilds' seven external buffers plus the resident one wrap to zero.
            high_mesh_count=(
                header_buffer_count - 1 if separate_count else
                (((raw >> 9) & 0x7) - 1) & 0x7
                if buffer_count_includes_resident
                else (((raw >> 9) & 0x7) if wilds_layout else None)
            ),
            use_ray_tracing_vertex_animation=bool(raw & (1 << (9 if separate_count else 12))),
            use_sdf=bool(raw & (1 << (10 if separate_count else 13))),
            enable_rebraiding=(raw >> (11 if separate_count else 14)) & 0x3,
            merge_static_mesh_bvh=bool(raw & (1 << 16)),
            prefer_opaque=(
                int(bool(raw & (1 << 17)))
                if sf6_layout
                else (raw >> 17) & 0x3
            ),
            prefer_shadow_cast=(
                int(bool(raw & (1 << 18)))
                if sf6_layout
                else (raw >> 19) & 0x3
            ),
            prefer_alpha_mask=(
                int(bool(raw & (1 << 19)))
                if sf6_layout
                else (raw >> 21) & 0x3
            ),
            reserved_bits=(
                raw & 0x80E000 if separate_count else
                (raw >> 20) & 0xF
                if sf6_layout
                else (
                    int(bool(raw & (1 << 7)))
                    if re8_layout
                    else int(bool(raw & (1 << 23)))
                )
            ),
        )


def _decode_faces(payload: "MeshBufferPayload", use_32bit: bool) -> None:
    if use_32bit:
        payload.integer_faces = array("I")
        payload.integer_faces.frombytes(payload.face_bytes)
        payload.faces = array("H")
    else:
        payload.faces = array("H")
        payload.faces.frombytes(payload.face_bytes)
        payload.integer_faces = None


def _checked_slice(data: bytes | bytearray, start: int, size: int, label: str) -> bytes:
    return bytes(_checked_view(data, start, size, label))


def _checked_view(
    data: bytes | bytearray | memoryview,
    start: int,
    size: int,
    label: str,
) -> memoryview:
    end = start + size
    if start < 0 or size < 0 or end > len(data):
        raise ValueError(f"{label} range [{start}, {end}) exceeds {len(data)} bytes")
    return memoryview(data)[start:end]


def _unpack_raw_uvs(data) -> array:
    values = np.frombuffer(data, dtype="<f2").astype(np.float64)
    result = array("d")
    result.frombytes(values.tobytes())
    return result


@dataclass
class Header:
    magic: int = MESH_MAGIC
    version: int = 0
    file_size: int = 0
    lod_hash: int = 0

    flags: int = 0
    legacy_flags: int = 0
    legacy_solved_offset: int = 0
    legacy_light_map_raw: int = 0
    light_map_reserved: int = 0
    light_map_used: int = 0
    light_map_width: int = 0
    light_map_height: int = 0
    light_map_metadata_raw: int = 0
    light_map_padding: int = 0
    name_count: int = 0
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
    # The seventh post-buffer pointer in RE4+ MESH headers is the BlendShapeBoneModifier layout, not a mesh-group table.
    blend_shape_bone_modifier_offset: int = 0
    dd2_hash_offset: int = 0
    vertices_offset: int = 0
    on_memory_size: int = 0
    sdf_path_offset: int = 0
    parts_name_hash_offset: int = 0
    buffer_headers_offset: int = 0
    micro_polygon_reserved_offset: int = 0
    meshlet_layout_offset: int = 0
    buffer_layout_offset: int = 0
    meshlet_bvh_offset: int = 0
    meshlet_parts_offset: int = 0
    content_flags: int = 0
    content_flags_storage_raw: int = 0
    decoded_content_flags: Optional[MeshContentFlags] = None
    header_pad: int = 0
    buffer_count: int = 0
    header_reserved_dwords: Tuple[int, int, int] = (0, 0, 0)

    format_version: MeshMainVersion = MeshMainVersion.UNKNOWN
    profile: Optional[MeshGameProfile] = None

    @property
    def gpu_buffer_base_offset(self) -> int:
        return self.vertices_offset

    def _read_light_map_metadata(self, h: BinaryHandler) -> None:
        self.light_map_metadata_raw = h.read_uint32()
        self._set_light_map(self.light_map_metadata_raw)

    def _set_light_map(self, raw: int) -> None:
        self.light_map_reserved = raw & 0x1
        self.light_map_used = (raw >> 1) & 0x1
        self.light_map_width = (raw >> 2) & 0x7FFF
        self.light_map_height = (raw >> 17) & 0x7FFF

    def _read_meshlet_or_lod_offsets(self, h: BinaryHandler):
        if self.magic == MPLY_MAGIC:
            self.micro_polygon_reserved_offset = h.read_int64()
            self.meshlet_layout_offset = h.read_int64()
            self.mesh_offset = self.meshlet_layout_offset
            self.meshlet_bvh_offset = h.read_int64()
            self.meshlet_parts_offset = h.read_int64()
            self.blend_shapes_offset = h.read_int64()
            self.buffer_layout_offset = h.read_int64()
            self.blend_shape_bone_modifier_offset = h.read_int64()
        else:
            self.lods_offset = h.read_int64()
            self.shadow_lods_offset = h.read_int64()
            self.occluder_mesh_offset = h.read_int64()
            self.normal_recalc_offset = h.read_int64()
            self.blend_shapes_offset = h.read_int64()
            self.buffer_layout_offset = h.read_int64()
            self.mesh_offset = self.buffer_layout_offset
            self.blend_shape_bone_modifier_offset = h.read_int64()

    def _read_common_mesh_offsets(self, h: BinaryHandler):
        self.floats_offset = h.read_int64()
        self.bounds_offset = h.read_int64()
        self.bones_offset = h.read_int64()
        self.material_indices_offset = h.read_int64()
        self.bone_indices_offset = h.read_int64()
        self.blend_shape_indices_offset = h.read_int64()

    def read(
        self,
        h: BinaryHandler,
        *,
        file_version: int,
    ) -> bool:
        self.magic = h.read_uint32()
        if self.magic not in (MESH_MAGIC, MPLY_MAGIC):
            return False
        self.version = h.read_uint32()
        self.file_size = h.read_uint32()
        self.lod_hash = h.read_uint32()
        self.profile = mesh_profile_for_versions(self.version, file_version)
        self.format_version = self.profile.format_version

        if self.profile.header_layout is MeshHeaderLayout.LEGACY:
            self.legacy_flags = h.read_uint8()
            self.legacy_solved_offset = h.read_uint8()
            self.flags = self.legacy_flags | (self.legacy_solved_offset << 8)
            if self.profile.legacy_flags_are_content_flags:
                self.decoded_content_flags = MeshContentFlags.from_raw(self.legacy_flags, self.profile)
            self.name_count = h.read_uint16()
            self.legacy_light_map_raw = h.read_uint32()
            if self.profile.legacy_light_map_layout is LegacyLightMapLayout.RE8:
                self._set_light_map(self.legacy_light_map_raw)
            else:
                self.light_map_reserved = self.legacy_light_map_raw & 0xFF
                self.light_map_used = (self.legacy_light_map_raw >> 8) & 0xFF
                self.light_map_width = (self.legacy_light_map_raw >> 16) & 0xFF
                self.light_map_height = (self.legacy_light_map_raw >> 24) & 0xFF
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
        elif self.profile.header_layout in {
            MeshHeaderLayout.RE4,
            MeshHeaderLayout.DD2,
        }:
            self.content_flags_storage_raw = h.read_uint32()
            self.content_flags = self.content_flags_storage_raw & 0x00FFFFFF
            self.decoded_content_flags = MeshContentFlags.from_raw(self.content_flags, self.profile)
            self.name_count = h.read_uint16()
            self.header_pad = h.read_uint16()
            if self.profile.header_layout is MeshHeaderLayout.DD2:
                self._read_light_map_metadata(h)
                self.light_map_padding = h.read_uint32()
            else:
                self.buffer_headers_offset = h.read_int64()
            self._read_meshlet_or_lod_offsets(h)
            self._read_common_mesh_offsets(h)
            if self.profile.header_layout is MeshHeaderLayout.RE4:
                self.streaming_info_offset = h.read_int64()
                self.name_offsets_offset = h.read_int64()
            else:
                self.name_offsets_offset = h.read_int64()
                self.parts_name_hash_offset = h.read_int64()
                self.streaming_info_offset = h.read_int64()
            if self.profile.header_layout is MeshHeaderLayout.DD2:
                self.on_memory_size = h.read_uint64()
                self.vertices_offset = self.on_memory_size
            else:
                self.vertices_offset = h.read_int64()
            self.sdf_path_offset = h.read_int64()
        else:
            # Wilds moves the RE8/DD2 light-map metadata dword ahead of stringCount.
            self._read_light_map_metadata(h)
            self.name_count = h.read_uint16()
            self.content_flags_storage_raw = h.read_uint32()
            self.content_flags = self.content_flags_storage_raw & 0x00FFFFFF
            count_storage = h.read_uint16()
            count_bits = self.profile.header_buffer_count_bits
            self.buffer_count = count_storage & ((1 << count_bits) - 1)
            self.header_pad = count_storage >> count_bits
            self.decoded_content_flags = MeshContentFlags.from_raw(
                self.content_flags, self.profile, header_buffer_count=self.buffer_count)
            self.header_reserved_dwords = (h.read_int32(), h.read_int32(), h.read_int32())
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
    pad: int = 0
    entries_offset: int = 0

    def read(self, h: BinaryHandler) -> bool:
        entry_count = h.read_int32()
        self.pad = h.read_int32()
        self.entries_offset = h.read_int64()
        if not 0 <= entry_count <= 4096:
            raise ValueError(
                f"StreamingInfo entry count {entry_count} is outside 0..4096"
            )
        if self.pad:
            raise ValueError("StreamingInfo.pad must be zero")
        if entry_count and self.entries_offset <= 0:
            raise ValueError("StreamingInfo has entries but no entries pointer")
        with h.seek_temp(self.entries_offset):
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
    UV2 = 6
    ExtraWeights = 7


class MPMRLodStrategy(IntEnum):
    ScreenArea = 0
    Distance = 1


@dataclass(frozen=True, slots=True)
class MPLYRuntimeSettings:
    lod_factors: Tuple[float, ...] = ()
    meshlet_raster_switch_threshold: int = 0
    lod_strategy: MPMRLodStrategy = MPMRLodStrategy.ScreenArea


@dataclass
class MeshBufferItemHeader:
    type: int
    size: int
    offset: int


@dataclass(frozen=True, slots=True)
class VertexAttributeStream:
    type: int
    stride: int
    offset: int
    data: memoryview
    native_stored_byte_length: int
    implicit_zero_suffix_byte_length: int

    @property
    def semantic(self) -> str:
        try:
            return VertexBufferType(self.type).name
        except ValueError:
            return f"Unknown_{self.type}"


@dataclass(frozen=True)
class SkinWeightBuffer:
    influence_count: int
    deform_indices: array
    weights: array
    vertex_count: int
    declared_influence_count: int = 0
    native_weight_capacity: int = 8
    packed_index_word_count: int = 2
    stored_weight_count: int = 0
    trailing_weight_bytes: bytes = b""
    implicit_last_weight: bool = False
    runtime_ignored_weight_bytes: bytes = b""
    explicit_sixth_weight_bytes: bytes = b""
    unused_seventh_weight_bytes: bytes = b""
    unused_eighth_weight_bytes: bytes = b""
    packed_index_reserved_bits: bytes = b""

    def __post_init__(self) -> None:
        if self.influence_count <= 0:
            raise ValueError("skin-weight influence count must be positive")
        if self.vertex_count < 0:
            raise ValueError("skin-weight vertex count must be nonnegative")
        if len(self.deform_indices) != self.vertex_count * self.influence_count:
            raise ValueError(
                "skin-weight indices do not match the topology-owned vertex count"
            )
        if len(self.weights) != len(self.deform_indices):
            raise ValueError("skin-weight values do not match their indices")
        declared_influence_count = (
            self.declared_influence_count or self.influence_count
        )
        if declared_influence_count < self.influence_count:
            raise ValueError(
                "declared skin-weight count is smaller than the decoded layout"
            )
        object.__setattr__(
            self, "declared_influence_count", declared_influence_count
        )
        stored_weight_count = self.stored_weight_count or self.influence_count
        if not 0 < stored_weight_count <= self.influence_count:
            raise ValueError("stored skin-weight count is invalid")
        if self.native_weight_capacity < stored_weight_count:
            raise ValueError("native skin-weight capacity is invalid")
        if self.packed_index_word_count < 0:
            raise ValueError("packed skin-index word count is invalid")
        object.__setattr__(self, "stored_weight_count", stored_weight_count)
        if self.implicit_last_weight:
            if stored_weight_count != self.influence_count - 1:
                raise ValueError(
                    "implicit final skin weights require one fewer stored lane"
                )
        if self.implicit_last_weight or self.runtime_ignored_weight_bytes:
            ignored_count = self.native_weight_capacity - stored_weight_count
            if len(self.runtime_ignored_weight_bytes) != (
                self.vertex_count * ignored_count
            ):
                raise ValueError(
                    "runtime-ignored skin bytes do not match their vertices"
                )
        for label, raw, stride in (
            ("explicit sixth joint weight", self.explicit_sixth_weight_bytes, 1),
            ("unused seventh joint weight", self.unused_seventh_weight_bytes, 1),
            ("unused eighth joint weight", self.unused_eighth_weight_bytes, 1),
            (
                "packed-index reserved field",
                self.packed_index_reserved_bits,
                self.packed_index_word_count,
            ),
        ):
            if raw and len(raw) != self.vertex_count * stride:
                raise ValueError(f"{label} bytes do not match their vertices")


SKIN_WEIGHT_STREAMS = ("skin_weights", "extra_skin_weights", "secondary_skin_weights", "additional_vertex_weights")


@dataclass
class MeshBufferPayload:
    source_offset: int = 0
    vertex_count: int = 0
    vertex_bytes: bytes = b""
    face_bytes: bytes = b""
    buffer_headers: List[MeshBufferItemHeader] = field(default_factory=list)
    declared_vertex_semantics: tuple[int, ...] = ()
    is_auxiliary_geometry: bool = False
    auxiliary_vertex_planes_may_omit_zero_suffix: bool = False
    attribute_streams: List[VertexAttributeStream] = field(default_factory=list)
    unknown_attributes: Dict[int, List[memoryview]] = field(default_factory=dict)
    positions: array = field(default_factory=lambda: array("f"))
    normals: array = field(default_factory=lambda: array("f"))
    tangents: array = field(default_factory=lambda: array("f"))
    normal_ws: array = field(default_factory=lambda: array("B"))
    tangent_ws: array = field(default_factory=lambda: array("B"))
    uv0: array = field(default_factory=lambda: array("d"))
    uv1: array = field(default_factory=lambda: array("d"))
    uv2: array = field(default_factory=lambda: array("d"))
    colors: array = field(default_factory=lambda: array("B"))
    faces: array = field(default_factory=lambda: array("H"))
    integer_faces: Optional[array] = None
    skin_weights: Optional[SkinWeightBuffer] = None
    extra_skin_weights: Optional[SkinWeightBuffer] = None
    skin_weight_continuation_bytes: bytes = b""
    secondary_skin_weights: Optional[SkinWeightBuffer] = None
    secondary_skin_weight_bytes: bytes = b""
    additional_vertex_weights: Optional[SkinWeightBuffer] = None
    additional_vertex_weight_bytes: bytes = b""
    normal_recalc_local_index_buffer_offset: int = 0
    normal_recalc_redirected_list_offset: int = 0
    normal_recalc_payload_end: int = 0
    normal_recalc_face_words: tuple[int, ...] = ()
    normal_recalc_redirect_words: tuple[int, ...] = ()
    normal_recalc_runtime_ignored_work_padding_words: tuple[int, ...] = ()
    normal_recalc_runtime_ignored_capacity_slack: bytes = b""

    @property
    def index_values(self) -> array:
        return self.integer_faces if self.integer_faces is not None else self.faces

    @property
    def normal_w(self) -> array:
        return _decode_snorm8_array(self.normal_ws)

    @property
    def pivot_indices(self) -> array:
        return decode_local_pivot_indices(self.normal_ws)

    @property
    def tangent_handedness(self) -> array:
        return _decode_snorm8_array(self.tangent_ws)


def _attribute_view(
    payload: MeshBufferPayload,
    header: MeshBufferItemHeader,
    vertex_count: int,
) -> tuple[memoryview, int]:
    if header.size <= 0:
        raise ValueError(
            f"Vertex attribute {int(header.type)} has invalid stride {header.size}"
        )
    required_byte_length = header.size * vertex_count
    label = f"Vertex attribute {int(header.type)}"
    if not (
        payload.is_auxiliary_geometry
        and payload.auxiliary_vertex_planes_may_omit_zero_suffix
    ):
        return (
            _checked_view(
                payload.vertex_bytes,
                header.offset,
                required_byte_length,
                label,
            ),
            required_byte_length,
        )

    if not 0 <= int(header.offset) <= len(payload.vertex_bytes):
        raise ValueError(
            f"{label} starts outside its auxiliary vertex-buffer region"
        )
    following_offsets = [
        int(item.offset)
        for item in payload.buffer_headers
        if int(item.offset) > int(header.offset)
    ]
    physical_end = min(
        following_offsets,
        default=len(payload.vertex_bytes),
    )
    physical_end = min(physical_end, len(payload.vertex_bytes))
    native_stored_byte_length = min(
        required_byte_length,
        max(0, physical_end - int(header.offset)),
    )
    if (
        native_stored_byte_length < required_byte_length
        and native_stored_byte_length % int(header.size)
    ):
        raise ValueError(
            f"{label} sparse physical prefix is not record-aligned: "
            f"{native_stored_byte_length} bytes at stride {header.size}"
        )
    stored = _checked_view(
        payload.vertex_bytes,
        header.offset,
        native_stored_byte_length,
        label,
    )
    missing_byte_length = required_byte_length - native_stored_byte_length
    if not missing_byte_length:
        return stored, native_stored_byte_length
    logical = bytes(stored) + bytes(missing_byte_length)
    return memoryview(logical), native_stored_byte_length


def _decode_position_payload(
    payload: MeshBufferPayload,
    vertex_count: Optional[int] = None,
) -> int:
    position_headers = [
        header
        for header in payload.buffer_headers
        if header.type == VertexBufferType.Position
    ]
    if payload.buffer_headers and vertex_count is None:
        raise ValueError(
            "Vertex attributes require a topology-owned vertex count; "
            "attribute byte spans are not a semantic count source"
        )
    if (
        vertex_count
        and payload.buffer_headers
        and len(position_headers) != 1
    ):
        raise ValueError(
            "A nonempty vertex topology requires exactly one Position stream"
        )
    if not position_headers:
        payload.vertex_count = int(vertex_count or 0)
        return payload.vertex_count
    header = position_headers[0]
    if header.size != 12:
        raise ValueError(f"Position stream has unsupported stride {header.size}")
    count = int(vertex_count or 0)
    data, _native_stored_byte_length = _attribute_view(payload, header, count)
    payload.positions = array("f")
    payload.positions.frombytes(data)
    payload.vertex_count = count
    return count


def _decode_snorm8_array(values: array) -> array:
    decoded = array("f")
    if not values:
        return decoded
    signed = np.frombuffer(values, dtype=np.uint8).view(np.int8)
    floats = np.maximum(signed.astype(np.float32) / 127.0, -1.0)
    decoded.frombytes(floats.astype("<f4", copy=False).tobytes())
    return decoded


def decode_local_pivot_indices(values: array) -> array:
    decoded = array("H")
    if not values:
        return decoded
    raw = np.frombuffer(values, dtype=np.uint8)
    signed = raw.view(np.int8).astype(np.float32)
    snorm = np.maximum(signed / np.float32(127.0), np.float32(-1.0))
    restored = np.where(
        snorm < 0.0,
        snorm * np.float32(127.5) + np.float32(255.5),
        snorm * np.float32(127.5),
    )
    indices = np.trunc(restored).astype("<u2")
    decoded.frombytes(indices.tobytes())
    return decoded


def _decode_skin_weights(
    data: memoryview,
    vertex_count: int,
    profile: MeshGameProfile | MeshMainVersion | int,
) -> SkinWeightBuffer:
    profile = coerce_mesh_profile(profile)
    influence_count = profile.skin_weight_influence_count
    expected_size = vertex_count * 16
    if len(data) != expected_size:
        raise ValueError(
            f"Skin-weight stream has {len(data)} bytes; expected {expected_size}"
        )
    records = np.frombuffer(data, dtype=np.uint8).reshape(vertex_count, 16)
    if influence_count == 8:
        decoded_indices = records[:, :8].astype(np.uint16)
        packed_index_reserved_bits = b""
    else:
        decoded_indices, packed_tags = _decode_packed_10bit_indices(records)
        packed_index_reserved_bits = packed_tags.tobytes()
    deform_indices = array("H")
    deform_indices.frombytes(decoded_indices.astype("<u2", copy=False).tobytes())
    decoded_weights = np.zeros((vertex_count, influence_count), dtype=np.float32)
    decoded_weights[:] = (
        records[:, 8 : 8 + influence_count].astype(np.float32) / 255.0
    )
    implicit_last_weight = bool(profile.skin_weight_uses_implicit_last_weight)
    stored_weight_count = influence_count - 1 if implicit_last_weight else influence_count
    runtime_ignored_weight_bytes = b""
    if implicit_last_weight:
        runtime_ignored_weight_bytes = records[:, 8 + stored_weight_count : 16].tobytes()
        stored_units = np.sum(
            records[:, 8 : 8 + stored_weight_count].astype(np.int16),
            axis=1,
            dtype=np.int16,
        )
        decoded_weights[:, influence_count - 1] = (
            np.int16(255) - stored_units
        ).astype(np.float32) / 255.0
    weights = array("f")
    weights.frombytes(decoded_weights.astype("<f4", copy=False).tobytes())
    trailing_weight_bytes = (
        records[:, 14:16].tobytes() if influence_count == 6 else b""
    )
    return SkinWeightBuffer(
        influence_count=influence_count,
        deform_indices=deform_indices,
        weights=weights,
        vertex_count=vertex_count,
        declared_influence_count=influence_count,
        native_weight_capacity=8,
        packed_index_word_count=(0 if influence_count == 8 else 2),
        stored_weight_count=stored_weight_count,
        trailing_weight_bytes=trailing_weight_bytes,
        implicit_last_weight=implicit_last_weight,
        runtime_ignored_weight_bytes=runtime_ignored_weight_bytes,
        explicit_sixth_weight_bytes=(
            records[:, 13].tobytes()
            if influence_count == 6 and implicit_last_weight
            else b""
        ),
        unused_seventh_weight_bytes=(
            records[:, 14].tobytes() if influence_count == 6 else b""
        ),
        unused_eighth_weight_bytes=(
            records[:, 15].tobytes() if influence_count == 6 else b""
        ),
        packed_index_reserved_bits=packed_index_reserved_bits,
    )


def _decode_packed_10bit_indices(
    records: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    packed = records[:, :8].copy().view("<u4").reshape(len(records), 2)
    decoded = np.empty((len(records), 6), dtype=np.uint16)
    decoded[:, 0] = packed[:, 0] & 0x3FF
    decoded[:, 1] = (packed[:, 0] >> 10) & 0x3FF
    decoded[:, 2] = (packed[:, 0] >> 20) & 0x3FF
    decoded[:, 3] = packed[:, 1] & 0x3FF
    decoded[:, 4] = (packed[:, 1] >> 10) & 0x3FF
    decoded[:, 5] = (packed[:, 1] >> 20) & 0x3FF
    tags = np.column_stack((packed[:, 0] >> 30, packed[:, 1] >> 30)).astype(
        np.uint8
    )
    return decoded, tags


def _decode_mhwilds_skin_weights(
    primary_data: memoryview,
    continuation_data: memoryview | None,
    vertex_count: int,
    declared_influence_count: int,
) -> SkinWeightBuffer:
    expected_size = vertex_count * 16
    if len(primary_data) != expected_size:
        raise ValueError(
            f"Wilds primary skin stream has {len(primary_data)} bytes; "
            f"expected {expected_size}"
        )
    primary = np.frombuffer(primary_data, dtype=np.uint8).reshape(
        vertex_count, 16
    )
    primary_indices, primary_tags = _decode_packed_10bit_indices(primary)

    if declared_influence_count == 12:
        if continuation_data is None:
            raise ValueError(
                "Wilds 12-influence skinning requires a type-7 continuation stream"
            )
        if len(continuation_data) != expected_size:
            raise ValueError(
                f"Wilds skin continuation has {len(continuation_data)} bytes; "
                f"expected {expected_size}"
            )
        continuation = np.frombuffer(
            continuation_data, dtype=np.uint8
        ).reshape(vertex_count, 16)
        continuation_indices, continuation_tags = _decode_packed_10bit_indices(
            continuation
        )
        decoded_indices = np.concatenate(
            (primary_indices, continuation_indices), axis=1
        )
        explicit_units = np.concatenate(
            (primary[:, 8:16], continuation[:, 8:11]), axis=1
        )
        decoded_weights = np.empty((vertex_count, 12), dtype=np.float32)
        decoded_weights[:, :11] = explicit_units.astype(np.float32) / 255.0
        residual = np.int16(255) - np.sum(
            explicit_units.astype(np.int16), axis=1, dtype=np.int16
        )
        decoded_weights[:, 11] = residual.astype(np.float32) / 255.0
        ignored = continuation[:, 11:16].tobytes()
        packed_tags = np.concatenate(
            (primary_tags, continuation_tags), axis=1
        ).tobytes()
        stored_weight_count = 11
        native_weight_capacity = 16
        implicit_last_weight = True
        trailing = ignored
    elif declared_influence_count in (6, 8):
        if continuation_data is not None:
            raise ValueError(
                f"Wilds {declared_influence_count}-weight skinning must not "
                "have a type-7 continuation stream"
            )
        # SkinWeightCount_8 is an alias of SkinWeightCount_6 in all shipped SDF targets.
        decoded_indices = primary_indices
        explicit_units = primary[:, 8:13]
        decoded_weights = np.empty((vertex_count, 6), dtype=np.float32)
        decoded_weights[:, :5] = explicit_units.astype(np.float32) / 255.0
        residual = np.int16(255) - np.sum(
            explicit_units.astype(np.int16), axis=1, dtype=np.int16
        )
        decoded_weights[:, 5] = residual.astype(np.float32) / 255.0
        ignored = primary[:, 13:16].tobytes()
        packed_tags = primary_tags.tobytes()
        stored_weight_count = 5
        native_weight_capacity = 8
        implicit_last_weight = True
        trailing = primary[:, 14:16].tobytes()
    elif declared_influence_count == 4:
        if continuation_data is not None:
            raise ValueError(
                "Wilds 4-weight skinning must not have a type-7 continuation stream"
            )
        # Despite its name, the shader loads three indices and three UNORM weights normalized by their sum.
        decoded_indices = primary_indices[:, :3]
        units = primary[:, 8:11].astype(np.float32)
        totals = np.sum(units, axis=1, keepdims=True)
        decoded_weights = np.divide(
            units,
            totals,
            out=np.zeros_like(units),
            where=totals != 0.0,
        )
        ignored = primary[:, 11:16].tobytes()
        packed_tags = primary_tags.tobytes()
        stored_weight_count = 3
        native_weight_capacity = 8
        implicit_last_weight = False
        trailing = ignored
    else:
        raise ValueError(
            "Wilds SkinWeightCount selects unsupported layout "
            f"{declared_influence_count}"
        )

    deform_indices = array("H")
    deform_indices.frombytes(
        decoded_indices.astype("<u2", copy=False).tobytes()
    )
    weights = array("f")
    weights.frombytes(decoded_weights.astype("<f4", copy=False).tobytes())
    effective_influence_count = int(decoded_indices.shape[1])
    return SkinWeightBuffer(
        influence_count=effective_influence_count,
        deform_indices=deform_indices,
        weights=weights,
        vertex_count=vertex_count,
        declared_influence_count=declared_influence_count,
        native_weight_capacity=native_weight_capacity,
        packed_index_word_count=(4 if declared_influence_count == 12 else 2),
        stored_weight_count=stored_weight_count,
        trailing_weight_bytes=trailing,
        implicit_last_weight=implicit_last_weight,
        runtime_ignored_weight_bytes=ignored,
        explicit_sixth_weight_bytes=(
            primary[:, 13].tobytes()
            if declared_influence_count in (6, 8)
            else b""
        ),
        unused_seventh_weight_bytes=(
            primary[:, 14].tobytes()
            if declared_influence_count in (6, 8)
            else b""
        ),
        unused_eighth_weight_bytes=(
            primary[:, 15].tobytes()
            if declared_influence_count in (6, 8)
            else b""
        ),
        packed_index_reserved_bits=packed_tags,
    )


def _decode_vertex_attributes(
    payload: MeshBufferPayload,
    vert_count: int,
    profile: MeshGameProfile | MeshMainVersion | int,
    skin_weight_influence_count: int | None = None,
) -> None:
    profile = coerce_mesh_profile(profile)
    payload.declared_vertex_semantics = tuple(
        int(header.type) for header in payload.buffer_headers
    )
    payload.attribute_streams = []
    payload.unknown_attributes = {}
    wilds_primary: memoryview | None = None
    wilds_continuation: memoryview | None = None
    for header in payload.buffer_headers:
        data_view, native_stored_byte_length = _attribute_view(
            payload,
            header,
            vert_count,
        )
        payload.attribute_streams.append(
            VertexAttributeStream(
                type=int(header.type),
                stride=header.size,
                offset=header.offset,
                data=data_view,
                native_stored_byte_length=native_stored_byte_length,
                implicit_zero_suffix_byte_length=(
                    len(data_view) - native_stored_byte_length
                ),
            )
        )
        if header.type == VertexBufferType.Position:
            continue
        if header.type == VertexBufferType.NormalsTangents:
            if header.size != 8:
                raise ValueError(
                    f"Normal/tangent stream has unsupported stride {header.size}"
                )
            payload.normals, payload.normal_ws, payload.tangents, payload.tangent_ws = (
                unpack_normals_tangents(data_view)
            )
        elif header.type in (VertexBufferType.UV0, VertexBufferType.UV1, VertexBufferType.UV2):
            name = VertexBufferType(header.type).name
            if header.size != 4:
                raise ValueError(f"{name} stream has unsupported stride {header.size}")
            setattr(payload, name.lower(), _unpack_raw_uvs(data_view))
        elif header.type == VertexBufferType.Colors:
            if header.size != 4:
                raise ValueError(
                    f"Vertex-color stream has unsupported stride {header.size}"
                )
            payload.colors = unpack_colors(data_view)
        elif header.type == VertexBufferType.BoneWeights and header.size == 16:
            if profile.skin_weight_count_selects_influence_layout:
                wilds_primary = data_view
            else:
                payload.skin_weights = _decode_skin_weights(data_view, vert_count, profile)
        elif header.type == VertexBufferType.ExtraWeights and header.size == 16:
            if profile.skin_weight_count_selects_influence_layout:
                wilds_continuation = data_view
                payload.skin_weight_continuation_bytes = bytes(data_view)
            else:
                payload.extra_skin_weights = _decode_skin_weights(data_view, vert_count, profile)
        else:
            payload.unknown_attributes.setdefault(int(header.type), []).append(
                data_view
            )
    if wilds_primary is not None:
        if skin_weight_influence_count not in (4, 6, 8, 12):
            raise ValueError(
                "Wilds skin stream has no valid MeshData SkinWeightCount"
            )
        payload.skin_weights = _decode_mhwilds_skin_weights(
            wilds_primary,
            wilds_continuation,
            vert_count,
            skin_weight_influence_count,
        )
        payload.extra_skin_weights = None
    elif wilds_continuation is not None:
        raise ValueError(
            "Wilds type-7 skin continuation has no primary type-4 stream"
        )


@dataclass
class StreamingBufferHeader:
    entry_index: int
    element_headers_offset: int
    vertex_buffer_offset: int
    vertex_buffer_length: int
    unpadded_buffer_size: int
    main_vertex_element_count: int
    total_vertex_element_count: int
    shapekey_weight_buffer_offset: int
    shadow_mesh_index_buffer_offset: int
    occluder_mesh_index_buffer_offset: int
    blend_shape_offset: int
    buffer_index_unkn: int
    unknown_struct: int
    buffer_index: int
    vertex_elements: List[MeshBufferItemHeader]
    extra_vertex_elements: List[MeshBufferItemHeader]
    normal_recalc_local_index_buffer_offset: int = 0
    normal_recalc_redirected_list_offset: int = 0
    wilds_reserved: int = 0
    shapekey_weight_buffer_size: int = 0
    extended_prefix_reserved_dwords: Tuple[int, int, int] = (0, 0, 0)

    @property
    def element_list_offset(self) -> int:
        return self.element_headers_offset

    @property
    def universal_buffer_size(self) -> int:
        return self.unpadded_buffer_size

    @property
    def index_buffer_offset_size(self) -> int:
        return self.vertex_buffer_length

    @property
    def additional_vertex_weights_offset(self) -> int:
        return self.shapekey_weight_buffer_offset

    @property
    def additional_vertex_weights_size(self) -> int:
        return self.buffer_index_unkn

    @property
    def blend_shape_vertex_buffer_offset(self) -> int:
        return self.blend_shape_offset & 0xFFFFFFFF

    @property
    def chunk(self) -> int:
        return self.buffer_index

    @property
    def reserved(self) -> int:
        return self.unknown_struct


def _streaming_alias_layout_signature(
    header: StreamingBufferHeader,
) -> Tuple[object, ...]:
    return (
        int(header.vertex_buffer_length),
        int(header.unpadded_buffer_size),
        int(header.main_vertex_element_count),
        int(header.total_vertex_element_count),
        int(header.shapekey_weight_buffer_offset),
        int(header.shadow_mesh_index_buffer_offset),
        int(header.occluder_mesh_index_buffer_offset),
        int(header.blend_shape_offset),
        int(header.buffer_index_unkn),
        int(header.unknown_struct),
        int(header.normal_recalc_local_index_buffer_offset),
        int(header.normal_recalc_redirected_list_offset),
        int(header.wilds_reserved),
        int(header.shapekey_weight_buffer_size),
        tuple(int(value) for value in header.extended_prefix_reserved_dwords),
        tuple(
            (int(item.type), int(item.size), int(item.offset))
            for item in header.vertex_elements
        ),
        tuple(
            (int(item.type), int(item.size), int(item.offset))
            for item in header.extra_vertex_elements
        ),
    )


def _read_element_headers(
    h: BinaryHandler,
    offset: int,
    main_count: int,
    total_count: int,
) -> tuple[List[MeshBufferItemHeader], List[MeshBufferItemHeader]]:
    items: List[MeshBufferItemHeader] = []
    with h.seek_temp(offset):
        for _ in range(total_count):
            raw_type = h.read_uint16()
            try:
                element_type = VertexBufferType(raw_type)
            except ValueError:
                element_type = raw_type
            items.append(MeshBufferItemHeader(element_type, h.read_uint16(), h.read_uint32()))
    return items[:main_count], items[main_count:]


class MeshBuffer:
    def __init__(self, profile: MeshGameProfile | MeshMainVersion | int):
        self.profile = coerce_mesh_profile(profile)
        self.element_headers_offset = 0
        self.element_list_offset = 0
        self.vertex_buffer_offset = 0
        self.universal_offset = 0
        self.index_buffer_offset = 0
        self.face_buffer_offset = 0
        self.total_buffer_size = 0
        self.universal_buffer_size = 0
        self.vertex_buffer_size = 0
        self.index_buffer_offset_size = 0
        self.index_buffer_size = 0
        self.element_count = 0
        self.total_element_count = 0
        self.ukn1 = 0
        self.ukn2 = 0
        self.ukn3 = 0
        self.rt_padding = 0
        self.legacy_secondary_buffer_size = 0
        self.blend_shape_offset = 0
        self.normal_recalc_local_index_buffer_offset = 0
        self.normal_recalc_redirected_list_offset = 0
        self.wilds_reserved = 0
        self.blend_shape_vertex_buffer_offset = 0
        self.shapekey_weight_buffer_offset = 0
        self.shapekey_weight_buffer_size = 0
        self.additional_vertex_weights_offset = 0
        self.additional_vertex_weights_size = 0
        self.chunk = 0
        self.reserved = 0
        self.buffer_index_unkn = 0
        self.unknown_struct = 0
        self.shadow_mesh_index_buffer_offset = 0
        self.occluder_mesh_index_buffer_offset = 0
        self.buffer_index = 0
        self.buffer_headers: List[MeshBufferItemHeader] = []
        self.extra_buffer_headers: List[MeshBufferItemHeader] = []
        self.streaming_buffer_headers: Dict[int, StreamingBufferHeader] = {}
        self.streaming_payload_aliases: Dict[int, int] = {}
        self.streaming_buffer_count = 0
        self.buffer_payloads: Dict[int, MeshBufferPayload] = {}
        self.index_counts_by_buffer: Dict[int, int] = {}
        self.vertex_counts_by_buffer: Dict[int, int] = {}
        self._streaming_info: Optional[MeshStreamingInfo] = None
        self._streaming_data: Optional[bytes] = None
        self._streaming_data_loader: Optional[Callable[[], Optional[bytes]]] = None
        self.has_32bit_indices = False
        self.meshlet_layout: Optional["MPLYMeshletLayout"] = None
        self.mply_runtime_settings: Optional[MPLYRuntimeSettings] = None
        self.meshlet_bvh: Optional["MPLYMeshletBVH"] = None
        self.meshlet_parts_layout: Optional["MPLYMeshletPartsLayout"] = None
        self.mply_group_pivots: Optional[GroupPivotData] = None
        self.mply_cluster_headers: List["MPLYClusterHeader"] = []
        self.mply_chunk_flags: List["MPLYChunkFlags"] = []
        self.mply_chunks: List["MPLYChunk"] = []

    @property
    def extended_prefix_reserved_dwords(self) -> Tuple[int, int, int]:
        return (int(self.ukn1), int(self.ukn2), int(self.ukn3))

    def _decode_payload(
        self,
        payload: MeshBufferPayload,
        *,
        vertex_count: Optional[int] = None,
        has_32bit_indices: bool = False,
        skin_weight_influence_count: int | None = None,
    ) -> None:
        vert_count = _decode_position_payload(payload, vertex_count)
        _decode_vertex_attributes(
            payload,
            vert_count,
            self.profile,
            skin_weight_influence_count,
        )
        _decode_faces(payload, has_32bit_indices)

    def _read_streaming_buffer_headers(
        self,
        h: BinaryHandler,
        streaming_info: MeshStreamingInfo,
        header_base: int,
    ) -> None:
        if not self.profile.supports_streaming_buffers:
            return
        header_size = self.profile.streaming_header_size
        header_pos = header_base + header_size
        for entry_index, _stream_entry in enumerate(streaming_info.entries):
            if self.profile.skip_empty_stream_entries and _stream_entry.size <= 0:
                continue
            h.seek(header_pos)
            element_headers_offset = h.read_int64()
            vertex_buffer_offset = h.read_int64()
            shapekey_weight_buffer_offset = h.read_int64()
            if (
                self.profile.shape_key_weight_pointer_is_corpus_zero
                and shapekey_weight_buffer_offset
            ):
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} shape-key weight pointer must be zero"
                )
            unpadded_buffer_size = h.read_uint32()
            vertex_buffer_length = h.read_uint32()
            main_vertex_element_count = h.read_uint16()
            total_vertex_element_count = h.read_uint16()
            if vertex_buffer_offset != _stream_entry.start:
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} payload offset {vertex_buffer_offset} does "
                    f"not match StreamingInfo start {_stream_entry.start}"
                )
            if unpadded_buffer_size != _stream_entry.size:
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} payload size {unpadded_buffer_size} does "
                    f"not match StreamingInfo size {_stream_entry.size}"
                )
            if vertex_buffer_length > unpadded_buffer_size:
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} vertex prefix exceeds its payload"
                )
            if total_vertex_element_count < main_vertex_element_count:
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} element counts are inconsistent"
                )
            shapekey_weight_buffer_size = 0
            extended_prefix_reserved_dwords = (0, 0, 0)
            if self.profile.streaming_header_has_extended_prefix:
                shapekey_weight_buffer_size = h.read_int32()
                extended_prefix_reserved_dwords = (
                    h.read_int32(),
                    h.read_int32(),
                    h.read_int32(),
                )
            shadow_mesh_index_buffer_offset = h.read_uint32()
            occluder_mesh_index_buffer_offset = h.read_uint32()
            normal_recalc_local_index_buffer_offset = 0
            normal_recalc_redirected_list_offset = 0
            wilds_reserved = 0
            if self.profile.streaming_header_has_wilds_tail:
                normal_recalc_local_index_buffer_offset = h.read_int32()
                normal_recalc_redirected_list_offset = h.read_int32()
                blend_shape_offset = h.read_int32()
                wilds_reserved = h.read_int32()
                buffer_index = h.read_int32()
                buffer_index_unkn = 0
                unknown_struct = 0
            else:
                blend_shape_offset = h.read_int32()
                buffer_index_unkn = h.read_uint32()
                buffer_index = h.read_uint32()
                unknown_struct = h.read_uint64()
            if self.profile.streaming_buffer_tail_is_corpus_zero and (
                buffer_index_unkn or unknown_struct
            ):
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} reserved tail must be zero"
                )
            if self.profile.supports_additional_vertex_weights and (
                shapekey_weight_buffer_offset or buffer_index_unkn
                or (self.profile.additional_vertex_weights_size_in_modern_tail
                    and wilds_reserved)
            ):
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} declares a corpus-absent additional "
                    "vertex-weight stream"
                )
            if (
                self.profile.streaming_buffer_index_is_one_based
                and buffer_index != entry_index + 1
            ):
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{entry_index} must use one-based buffer index "
                    f"{entry_index + 1}, got {buffer_index}"
                )
            if buffer_index in self.streaming_buffer_headers:
                raise ValueError(
                    f"Duplicate streaming buffer index {buffer_index}"
                )
            vertex_elements, extra_vertex_elements = _read_element_headers(
                h, element_headers_offset, main_vertex_element_count, total_vertex_element_count,
            )
            if (
                self.profile.ordinary_vertex_layout_has_no_auxiliary_elements
                and extra_vertex_elements
            ):
                raise ValueError(
                    f"{self.profile.display_name} streaming buffer "
                    f"{buffer_index} declares auxiliary vertex elements; "
                    "the profiled layout has only the topology-owned main set"
                )
            self.streaming_buffer_headers[buffer_index] = StreamingBufferHeader(
                entry_index=entry_index,
                element_headers_offset=element_headers_offset,
                vertex_buffer_offset=vertex_buffer_offset,
                vertex_buffer_length=vertex_buffer_length,
                unpadded_buffer_size=unpadded_buffer_size,
                main_vertex_element_count=main_vertex_element_count,
                total_vertex_element_count=total_vertex_element_count,
                shapekey_weight_buffer_offset=shapekey_weight_buffer_offset,
                shadow_mesh_index_buffer_offset=shadow_mesh_index_buffer_offset,
                occluder_mesh_index_buffer_offset=occluder_mesh_index_buffer_offset,
                blend_shape_offset=blend_shape_offset,
                buffer_index_unkn=buffer_index_unkn,
                unknown_struct=unknown_struct,
                buffer_index=buffer_index,
                vertex_elements=vertex_elements,
                extra_vertex_elements=extra_vertex_elements,
                normal_recalc_local_index_buffer_offset=(
                    normal_recalc_local_index_buffer_offset
                ),
                normal_recalc_redirected_list_offset=(
                    normal_recalc_redirected_list_offset
                ),
                wilds_reserved=wilds_reserved,
                shapekey_weight_buffer_size=shapekey_weight_buffer_size,
                extended_prefix_reserved_dwords=(
                    extended_prefix_reserved_dwords
                ),
            )
            if unpadded_buffer_size == 0:
                self.buffer_payloads[buffer_index] = MeshBufferPayload(
                    source_offset=vertex_buffer_offset,
                    vertex_count=0,
                    buffer_headers=list(vertex_elements),
                    declared_vertex_semantics=tuple(int(item.type) for item in vertex_elements),
                )
            header_pos += header_size

        range_owners: Dict[Tuple[int, int], int] = {}
        for buffer_index, stream_header in sorted(
            self.streaming_buffer_headers.items()
        ):
            entry = streaming_info.entries[int(stream_header.entry_index)]
            allocation = (int(entry.start), int(entry.size))
            owner_index = range_owners.get(allocation)
            if owner_index is None:
                range_owners[allocation] = int(buffer_index)
                continue
            owner = self.streaming_buffer_headers[owner_index]
            if _streaming_alias_layout_signature(
                stream_header
            ) != _streaming_alias_layout_signature(owner):
                raise ValueError(
                    f"{self.profile.display_name} streaming buffers "
                    f"{owner_index} and {buffer_index} alias one allocation "
                    "with different runtime layouts"
                )
            self.streaming_payload_aliases[int(buffer_index)] = owner_index

    def ensure_streaming_payloads(self, buffer_indices: Iterable[int]) -> None:
        requested = {
            int(index)
            for index in buffer_indices
            if int(index) != 0 and int(index) not in self.buffer_payloads
        }
        if not requested:
            return
        if self._streaming_data is None and self._streaming_data_loader is not None:
            self._streaming_data = self._streaming_data_loader()
        if self._streaming_info is None or self._streaming_data is None:
            return
        for buffer_idx in sorted(requested):
            header = self.streaming_buffer_headers.get(buffer_idx)
            if header is None:
                raise ValueError(f"Streaming buffer {buffer_idx} has no header")
            payload = _checked_slice(
                self._streaming_data,
                header.vertex_buffer_offset,
                header.unpadded_buffer_size,
                f"Streaming buffer {buffer_idx}",
            )
            self.buffer_payloads[buffer_idx] = MeshBufferPayload(
                source_offset=header.vertex_buffer_offset,
                vertex_bytes=payload[:header.vertex_buffer_length],
                face_bytes=payload[header.vertex_buffer_length:],
                buffer_headers=list(header.vertex_elements),
                declared_vertex_semantics=tuple(
                    int(item.type) for item in header.vertex_elements
                ),
            )

    def finalize_payloads(self, *, skin_weight_influence_count: int | None = None) -> None:
        for buffer_index, payload in self.buffer_payloads.items():
            self._decode_payload(
                payload,
                vertex_count=self.vertex_counts_by_buffer.get(buffer_index),
                has_32bit_indices=self.has_32bit_indices,
                skin_weight_influence_count=skin_weight_influence_count,
            )

    def _read_blend_shape_offset(self, h: BinaryHandler) -> None:
        # Relative to ``universal_offset``.
        self.blend_shape_offset = h.read_int32()
        self.blend_shape_vertex_buffer_offset = self.blend_shape_offset & 0xFFFFFFFF

    def read(
        self,
        h: BinaryHandler,
        *,
        streaming_info: Optional[MeshStreamingInfo] = None,
        streaming_data: Optional[bytes] = None,
        streaming_data_loader: Optional[Callable[[], Optional[bytes]]] = None,
    ) -> bool:
        header_base = h.tell
        self.streaming_buffer_count = (
            len(streaming_info.entries) if streaming_info is not None else 0
        )
        self._streaming_info = streaming_info
        self._streaming_data = streaming_data
        self._streaming_data_loader = streaming_data_loader
        self.element_headers_offset = h.read_int64()
        self.element_list_offset = self.element_headers_offset
        self.vertex_buffer_offset = h.read_int64()
        self.universal_offset = self.vertex_buffer_offset
        if (
            self.profile.has_shape_key_weight_pointer
            or self.profile.supports_additional_vertex_weights
        ):
            self.shapekey_weight_buffer_offset = h.read_int64()
            if self.profile.supports_additional_vertex_weights:
                self.additional_vertex_weights_offset = (
                    self.shapekey_weight_buffer_offset
                )
            if (
                self.profile.shape_key_weight_pointer_is_corpus_zero
                and self.shapekey_weight_buffer_offset
            ):
                raise ValueError(
                    f"{self.profile.display_name} shape-key weight pointer "
                    "must be zero"
                )
            if self.profile.buffer_layout is MeshBufferLayout.DD2:
                self.total_buffer_size = h.read_uint32()
                self.vertex_buffer_size = h.read_uint32()
                self.universal_buffer_size = self.total_buffer_size
                self.index_buffer_offset_size = self.vertex_buffer_size
            else:
                self.total_buffer_size = h.read_int32()
                self.vertex_buffer_size = h.read_int32()
            if (
                self.total_buffer_size < 0
                or self.vertex_buffer_size < 0
                or self.vertex_buffer_size > self.total_buffer_size
            ):
                raise ValueError(
                    "GPU buffer total/vertex-prefix sizes are inconsistent: "
                    f"{self.total_buffer_size}/{self.vertex_buffer_size}"
                )
            self.index_buffer_offset = self.vertex_buffer_offset + self.vertex_buffer_size
            self.face_buffer_offset = self.index_buffer_offset
            self.index_buffer_size = (
                self.total_buffer_size - self.vertex_buffer_size
            )
        else:
            self.index_buffer_offset = h.read_int64()
            self.face_buffer_offset = self.index_buffer_offset
            if self.profile.has_rt_buffer_padding:
                self.rt_padding = h.read_int64()
            if self.profile.has_split_legacy_buffer_sizes:
                self.vertex_buffer_size = h.read_uint32()
                self.index_buffer_size = h.read_uint32()
                self.total_buffer_size = (
                    self.vertex_buffer_size + self.index_buffer_size
                )
            elif self.profile.legacy_buffer_sizes_are_vertex_and_index:
                self.vertex_buffer_size = h.read_int32()
                self.index_buffer_size = h.read_int32()
                self.total_buffer_size = (
                    self.vertex_buffer_size + self.index_buffer_size
                )
                if self.vertex_buffer_size < 0 or self.index_buffer_size < 0:
                    raise ValueError("Legacy GPU buffer sizes must be nonnegative")
                pointer_span = self.face_buffer_offset - self.vertex_buffer_offset
                if pointer_span != self.vertex_buffer_size:
                    raise ValueError(
                        "Legacy vertex-buffer size does not match the vertex/index "
                        f"pointer span ({self.vertex_buffer_size} != {pointer_span})"
                    )
            else:
                self.total_buffer_size = h.read_int32()
                self.legacy_secondary_buffer_size = h.read_int32()
                self.vertex_buffer_size = (
                    self.face_buffer_offset - self.vertex_buffer_offset
                )
        self.element_count = h.read_uint16()
        self.total_element_count = h.read_uint16()
        if self.profile.has_shape_key_weight_size:
            self.shapekey_weight_buffer_size = h.read_int32()
            self.ukn1 = h.read_int32()
            self.ukn2 = h.read_int32()
            self.ukn3 = h.read_int32()
        if self.profile.has_auxiliary_index_offsets:
            self.shadow_mesh_index_buffer_offset = h.read_uint32()
            self.occluder_mesh_index_buffer_offset = h.read_uint32()
            if self.profile.has_wilds_buffer_tail:
                self.normal_recalc_local_index_buffer_offset = h.read_int32()
                self.normal_recalc_redirected_list_offset = h.read_int32()
                self.blend_shape_offset = h.read_int32()
                if self.profile.additional_vertex_weights_size_in_modern_tail:
                    self.additional_vertex_weights_size = h.read_uint32()
                else:
                    self.wilds_reserved = h.read_int32()
                self.buffer_index = h.read_int32()
            else:
                self._read_blend_shape_offset(h)
                self.buffer_index_unkn = h.read_uint32()
                self.buffer_index = h.read_uint32()
                self.unknown_struct = h.read_uint64()
        else:
            self.shadow_mesh_index_buffer_offset = h.read_uint32()
            self.occluder_mesh_index_buffer_offset = h.read_uint32()
            self._read_blend_shape_offset(h)

        if self.profile.buffer_layout is MeshBufferLayout.DD2:
            self.additional_vertex_weights_size = self.buffer_index_unkn
            self.chunk = self.buffer_index
            self.reserved = self.unknown_struct

        if self.profile.resident_buffer_tail_is_corpus_zero and any(
            (
                self.buffer_index_unkn,
                self.buffer_index,
                self.unknown_struct,
            )
        ):
            raise ValueError(
                f"{self.profile.display_name} resident buffer tail must be zero"
            )

        if self.element_count < 0 or self.total_element_count < self.element_count:
            raise ValueError(
                f"Invalid mesh-buffer element counts "
                f"{self.element_count}/{self.total_element_count}"
            )
        self.buffer_headers, self.extra_buffer_headers = _read_element_headers(
            h, self.element_headers_offset, self.element_count, self.total_element_count,
        )
        if (
            self.profile.ordinary_vertex_layout_has_no_auxiliary_elements
            and self.extra_buffer_headers
        ):
            raise ValueError(
                f"{self.profile.display_name} resident buffer declares "
                "auxiliary vertex elements; the profiled layout has only the "
                "topology-owned main set"
            )
        if streaming_info is not None:
            self._read_streaming_buffer_headers(h, streaming_info, header_base)

        base = h.data
        self.buffer_payloads[0] = MeshBufferPayload(
            source_offset=self.vertex_buffer_offset,
            vertex_bytes=_checked_slice(
                base,
                self.vertex_buffer_offset,
                self.vertex_buffer_size,
                "Resident vertices",
            ),
            face_bytes=b"",
            buffer_headers=list(self.buffer_headers),
            declared_vertex_semantics=tuple(
                int(item.type) for item in self.buffer_headers
            ),
        )
        return True

class _MeshBufferOwned:
    def __init__(self, buffer: MeshBuffer) -> None:
        self.buffer = buffer

    @property
    def profile(self) -> MeshGameProfile:
        return self.buffer.profile


class Submesh(_MeshBufferOwned):
    def __init__(self, buffer: MeshBuffer):
        super().__init__(buffer)
        self.material_index = 0
        # Its mesh group's ID.
        self.part_id = 0
        self.is_quad = 0
        self.buffer_index = 0
        self.reserve = 0
        self.streaming_offset = 0
        self.streaming_offset2 = 0
        self.ukn1 = 0
        self.wilds_tail = 0
        self.runtime_ignored_tail_residue = 0
        self.indices_count = 0
        self.faces_index_offset = 0
        self.verts_index_offset = 0
        self.vert_count = 0
        self.file_offset = 0

    @property
    def runtime_ignored_reserve03(self) -> int:
        return int(self.reserve)

    @runtime_ignored_reserve03.setter
    def runtime_ignored_reserve03(self, value: int) -> None:
        self.reserve = int(value)

    @property
    def reserved_dword04(self) -> int:
        return int(self.ukn1)

    @reserved_dword04.setter
    def reserved_dword04(self, value: int) -> None:
        self.ukn1 = int(value)

    # The engine's names of fields above.
    material_id = property(lambda self: self.material_index)
    start_index_location = property(lambda self: self.faces_index_offset)
    base_vertex_location = property(lambda self: self.verts_index_offset)
    streaming_offset_bytes = property(lambda self: self.streaming_offset)
    streaming_platform_specific_offset_bytes = property(lambda self: self.streaming_offset2)

    @property
    def allocated_index_count(self) -> int:
        count = int(self.indices_count)
        if (
            self.profile.submesh_index_ranges_are_even_aligned
        ):
            return (count + 1) & ~1
        return count

    @property
    def index_padding_count(self) -> int:
        return self.allocated_index_count - int(self.indices_count)

    def read(self, h: BinaryHandler):
        self.file_offset = h.tell
        if not self.profile.submesh_has_modern_material:
            self.material_index = h.read_uint16()
            self.reserve = h.read_uint16()
        else:
            self.material_index = h.read_uint8()
            self.is_quad = h.read_uint8()
            if self.profile.submesh_has_buffer_index:
                self.buffer_index = h.read_uint8()
                self.reserve = h.read_uint8()
            else:
                self.reserve = h.read_uint16()
            if self.profile.submesh_has_ukn1:
                self.ukn1 = h.read_int32()
        self.indices_count = h.read_uint32()
        self.faces_index_offset = h.read_uint32()
        self.verts_index_offset = h.read_int32()
        if self.profile.submesh_has_streaming_offsets:
            self.streaming_offset = h.read_int32()
            self.streaming_offset2 = h.read_int32()
        if self.profile.submesh_has_wilds_tail:
            self.wilds_tail = h.read_uint32()
        elif self.profile.submesh_has_runtime_ignored_tail_residue:
            self.runtime_ignored_tail_residue = h.read_uint32()
        if min(self.indices_count, self.faces_index_offset, self.verts_index_offset) < 0:
            raise ValueError("Negative submesh count or offset")

class MeshGroup(_MeshBufferOwned):
    def __init__(self, buffer: MeshBuffer):
        super().__init__(buffer)
        self.group_id = 0
        self.cluster_count = 0
        self.vertex_count = 0
        self.face_count = 0
        self.reserve = b""
        self.file_offset = 0
        self.submeshes: List[Submesh] = []

    @property
    def index_span_count(self) -> int:
        return int(self.face_count)

    # The engine's name of the group's ID.
    part_id = property(lambda self: self.group_id)

    @property
    def drawable_index_count(self) -> int:
        return sum(int(submesh.indices_count) for submesh in self.submeshes)

    def read(
        self,
        h: BinaryHandler,
        *,
        buffer_vertex_offsets: Dict[int, int],
        allow_initial_vertex_offset: bool = False,
    ):
        self.file_offset = h.tell
        self.group_id = h.read_uint8()
        submesh_count = h.read_uint8()
        self.cluster_count = submesh_count
        self.reserve = h.read_bytes(6)
        self.vertex_count = h.read_uint32()
        self.face_count = h.read_uint32()
        for _ in range(submesh_count):
            sm = Submesh(self.buffer)
            sm.read(h)
            sm.part_id = self.group_id
            self.submeshes.append(sm)

        if self.profile.submesh_has_buffer_index and self.submeshes:
            indices = {submesh.buffer_index for submesh in self.submeshes}
            if len(indices) != 1:
                raise ValueError(
                    f"Mesh group {self.group_id} mixes streaming buffers "
                    f"{sorted(indices)}"
                )

        initial_offsets_by_buffer: Dict[int, int] = {}
        for sm in self.submeshes:
            initial_offsets_by_buffer[sm.buffer_index] = min(
                sm.verts_index_offset,
                initial_offsets_by_buffer.get(
                    sm.buffer_index,
                    sm.verts_index_offset,
                ),
            )

        next_offsets_by_buffer: Dict[int, int] = {}
        for sm in reversed(self.submeshes):
            buffer_index = sm.buffer_index
            group_start = (
                initial_offsets_by_buffer[buffer_index]
                if allow_initial_vertex_offset
                else buffer_vertex_offsets.get(buffer_index, 0)
            )
            vertex_end = next_offsets_by_buffer.get(
                buffer_index,
                group_start + self.vertex_count,
            )
            sm.vert_count = vertex_end - sm.verts_index_offset
            if sm.vert_count < 0:
                raise ValueError(
                    f"Invalid submesh vertex span [{sm.verts_index_offset}, {vertex_end})"
                )
            next_offsets_by_buffer[buffer_index] = sm.verts_index_offset

        for buffer_index in next_offsets_by_buffer:
            group_start = (
                initial_offsets_by_buffer[buffer_index]
                if allow_initial_vertex_offset
                else buffer_vertex_offsets.get(buffer_index, 0)
            )
            buffer_vertex_offsets[buffer_index] = max(
                buffer_vertex_offsets.get(buffer_index, 0),
                group_start + self.vertex_count,
            )

class MeshLOD(_MeshBufferOwned):
    def __init__(self, buffer: MeshBuffer):
        super().__init__(buffer)
        self.mesh_groups: List[MeshGroup] = []
        self.part_count = 0
        self.lod_factor = 0.0
        self.vertex_format = 0
        self.lod_level = 0
        self.reserve = 0
        self.file_offset = 0
        self.group_offsets_offset = 0
        self.group_offsets: List[int] = []

    def read(
        self,
        h: BinaryHandler,
        *,
        buffer_vertex_offsets: Dict[int, int],
        track_index_counts: bool = True,
        allow_initial_vertex_offset: bool = False,
    ):
        self.file_offset = h.tell
        mesh_count = h.read_uint8()
        self.part_count = mesh_count
        self.vertex_format = h.read_uint8()
        if self.profile.lod_has_level:
            self.lod_level = h.read_uint8()
            self.reserve = h.read_uint8()
        else:
            self.reserve = h.read_uint16()
        self.lod_factor = h.read_float()
        self.group_offsets_offset = h.read_int64()
        with h.seek_temp(self.group_offsets_offset):
            mesh_offsets = [h.read_int64() for _ in range(mesh_count)]
        self.group_offsets = mesh_offsets
        for off in mesh_offsets:
            if off <= 0:
                continue
            h.seek(off)
            mg = MeshGroup(self.buffer)
            mg.read(
                h,
                buffer_vertex_offsets=buffer_vertex_offsets,
                allow_initial_vertex_offset=allow_initial_vertex_offset,
            )
            self.mesh_groups.append(mg)
            if track_index_counts:
                for sm in mg.submeshes:
                    index_end = (
                        sm.faces_index_offset + sm.allocated_index_count
                    )
                    self.buffer.index_counts_by_buffer[sm.buffer_index] = max(
                        self.buffer.index_counts_by_buffer.get(sm.buffer_index, 0),
                        index_end,
                    )

def _init_lod_layout_fields(layout: object) -> None:
    layout.lods = []
    layout.lod_offsets = []
    layout.lod_offsets_offset = 0
    layout.material_count = 0
    layout.total_cluster_count = 0
    layout.uv_count = 0
    layout.skin_weight_count = 0
    layout.skin_weight_count_raw = 0
    layout.skin_weight_count_code = 0
    layout.skin_weight_influence_count = 0
    layout.skin_weight_runtime_ignored_tag = 0
    layout.total_mesh_count = 0
    layout.max_draw_call_count = 0
    layout.has_32bit_index_buffer = False
    layout.reserve = 0


def _read_lod_layout_header(layout: "MeshData | ShadowMeshData", h: BinaryHandler) -> int:
    lod_count = h.read_uint8()
    layout.total_cluster_count = h.read_uint8()
    layout.material_count = layout.total_cluster_count
    layout.uv_count = h.read_uint8()
    layout.skin_weight_count_raw = layout.skin_weight_count = h.read_uint8()
    layout.skin_weight_count_code = layout.skin_weight_count_raw & 0x7
    layout.skin_weight_influence_count = (
        layout.profile.mesh_data_skin_weight_influence_count(layout.skin_weight_count_raw)
    )
    # Native consumers mask SkinWeightCount with 7; the upper bits are a serializer tag (RE4/DD2 0x10, SF6 0x08).
    layout.skin_weight_runtime_ignored_tag = layout.skin_weight_count_raw & ~0x7
    layout.max_draw_call_count = layout.total_mesh_count = h.read_uint16()
    layout.has_32bit_index_buffer = bool(h.read_uint8())
    layout.reserve = h.read_uint8()
    return lod_count


class MeshData(_MeshBufferOwned):
    def __init__(self, buffer: MeshBuffer):
        super().__init__(buffer)
        _init_lod_layout_fields(self)
        self.skin_weight_flags = 0
        self.integer_faces = 0
        self.shared_lod_bits = 0
        self.aabb_min_padding = 0
        self.chunk = 0
        self.aabb_max_padding = 0
        self.ukn1 = 0
        self.vertex_counts_by_buffer: Dict[int, int] = {}
        self.index_counts_by_buffer: Dict[int, int] = {}
        self.bounding_sphere = (0.0, 0.0, 0.0, 0.0)
        self.bounding_box = (
            (0.0, 0.0, 0.0, 0.0),
            (0.0, 0.0, 0.0, 0.0),
        )

    def read(
        self,
        h: BinaryHandler,
        *,
        read_all_lods: bool = False,
        lightweight: bool = False,
    ):
        lod_count = _read_lod_layout_header(self, h)
        self.skin_weight_flags = self.skin_weight_runtime_ignored_tag
        self.integer_faces = int(self.has_32bit_index_buffer)
        self.shared_lod_bits = self.reserve
        self.buffer.has_32bit_indices = self.has_32bit_index_buffer
        if self.profile.mesh_data_has_legacy_unknown:
            self.ukn1 = h.read_int64()
        self.bounding_sphere = h.read_vec4()
        if self.profile.mesh_data_has_modern_bounds:
            minimum = h.read_vec3()
            self.aabb_min_padding = h.read_uint32()
            maximum = h.read_vec3()
            packed_max = h.read_uint32()
            self.chunk = packed_max & 0xFF
            self.aabb_max_padding = packed_max >> 8
            self.bounding_box = (
                (*minimum, float(self.aabb_min_padding)),
                (*maximum, float(packed_max)),
            )
        else:
            self.bounding_box = (h.read_vec4(), h.read_vec4())
        self.lod_offsets_offset = h.read_int64()
        with h.seek_temp(self.lod_offsets_offset):
            lod_offsets = [h.read_int64() for _ in range(lod_count)]
        self.lod_offsets = lod_offsets

        parse_all_lods = (
            read_all_lods
            or (self.buffer.streaming_buffer_count > 0 and not lightweight)
        )
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
                lod = MeshLOD(self.buffer)
                lod.read(h, buffer_vertex_offsets=buffer_vertex_offsets)
                lods_by_offset[off] = lod
            if read_all_lods or lod_index == 0:
                self.lods.append(lod)

        self.vertex_counts_by_buffer = {
            index: 0 for index, header in self.buffer.streaming_buffer_headers.items()
            if header.unpadded_buffer_size == 0
        }
        self.vertex_counts_by_buffer.update(buffer_vertex_offsets)
        self.index_counts_by_buffer = self.buffer.index_counts_by_buffer
        self.buffer.vertex_counts_by_buffer = self.vertex_counts_by_buffer
        self.buffer.ensure_streaming_payloads(
            self.buffer.index_counts_by_buffer.keys()
        )

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

        if lightweight:
            self.buffer.buffer_payloads = {
                index: payload for index, payload in self.buffer.buffer_payloads.items()
                if index in self.vertex_counts_by_buffer
            }
        self.buffer.finalize_payloads(
            skin_weight_influence_count=(
                self.skin_weight_influence_count
                if self.profile.skin_weight_count_selects_influence_layout
                else None
            ),
        )


@dataclass
class GroupPivotData:
    buffer_size: int = 0
    reserve: int = 0
    buffer_offset: int = 0
    pivots: List[Tuple[float, float, float]] = field(default_factory=list)
    raw: bytes = b""


@dataclass
class BoundingBoxData:
    count: int = 0
    reserve: int = 0
    boxes_offset: int = 0
    boxes: List[MeshAABB] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class MeshSectionRecord:
    name: str
    offset: int
    size: int
    raw: bytes


def _geometry_spans(
    groups: Iterable[MeshGroup],
) -> tuple[Dict[int, int], Dict[int, int], Dict[int, int], int, int]:
    vertex_bases: Dict[int, int] = {}
    vertex_ends: Dict[int, int] = {}
    index_counts: Dict[int, int] = {}
    for group in groups:
        for submesh in group.submeshes:
            buffer_index = int(submesh.buffer_index)
            vertex_start = int(submesh.verts_index_offset)
            vertex_end = vertex_start + int(submesh.vert_count)
            vertex_bases[buffer_index] = min(
                vertex_start,
                vertex_bases.get(buffer_index, vertex_start),
            )
            vertex_ends[buffer_index] = max(
                vertex_end,
                vertex_ends.get(buffer_index, vertex_end),
            )
            index_counts[buffer_index] = max(
                index_counts.get(buffer_index, 0),
                submesh.faces_index_offset + submesh.allocated_index_count,
            )
    vertex_counts = {
        buffer_index: vertex_ends[buffer_index] - vertex_base
        for buffer_index, vertex_base in vertex_bases.items()
    }
    return (
        vertex_counts,
        vertex_bases,
        index_counts,
        sum(vertex_counts.values()),
        sum(index_counts.values()),
    )


class _SecondaryGeometry(_MeshBufferOwned):
    def __init__(self, buffer: MeshBuffer):
        super().__init__(buffer)
        self.vertex_count = 0
        self.index_count = 0
        self.vertex_counts_by_buffer: Dict[int, int] = {}
        self.vertex_bases_by_buffer: Dict[int, int] = {}
        self.index_counts_by_buffer: Dict[int, int] = {}
        self.payloads: Dict[int, MeshBufferPayload] = {}
        self.shares_main_payload = False

    def _set_geometry_spans(self, groups: Iterable["MeshGroup"]) -> None:
        (
            self.vertex_counts_by_buffer,
            self.vertex_bases_by_buffer,
            self.index_counts_by_buffer,
            self.vertex_count,
            self.index_count,
        ) = _geometry_spans(groups)


class ShadowMeshData(_SecondaryGeometry):
    def __init__(self, buffer: MeshBuffer):
        super().__init__(buffer)
        _init_lod_layout_fields(self)
        self.dmc5_pad = 0
        self.reserved_slots: Tuple[int, ...] = ()

    @property
    def geometry_lods(self) -> tuple[MeshLOD, ...]:
        return tuple(self.lods)

    def read(self, h: BinaryHandler, *, read_all_lods: bool = True) -> None:
        lod_count = _read_lod_layout_header(self, h)
        if self.profile.shadow_has_legacy_pad:
            self.dmc5_pad = h.read_int64()
        self.lod_offsets_offset = h.read_int64()
        if self.profile.shadow_has_reserved_slots:
            self.reserved_slots = tuple(h.read_int64() for _ in range(6))
        if lod_count < 0 or lod_count > 128:
            raise ValueError(f"Invalid shadow LOD count {lod_count}")
        if lod_count and self.lod_offsets_offset <= 0:
            raise ValueError("Shadow mesh has no LOD-offset table")
        if lod_count:
            with h.seek_temp(self.lod_offsets_offset):
                self.lod_offsets = [h.read_int64() for _ in range(lod_count)]
        targets = self.lod_offsets if read_all_lods else self.lod_offsets[:1]
        offsets: Dict[int, int] = {}
        lods_by_offset: Dict[int, MeshLOD] = {}
        for lod_index, lod_offset in enumerate(targets):
            if lod_offset <= 0:
                continue
            lod = lods_by_offset.get(lod_offset)
            if lod is None:
                h.seek(lod_offset)
                lod = MeshLOD(self.buffer)
                lod.read(
                    h,
                    buffer_vertex_offsets=offsets,
                    track_index_counts=False,
                    allow_initial_vertex_offset=True,
                )
                lods_by_offset[lod_offset] = lod
            self.lods.append(lod)
        self._set_geometry_spans(group for lod in self.lods for group in lod.mesh_groups)


class OccluderMeshData(_SecondaryGeometry):
    def __init__(self, buffer: MeshBuffer):
        super().__init__(buffer)
        self.part_count = 0
        self.vertex_format = 0
        self.lod_level = 0
        self.reserve = 0
        self.lod_factor = 0.0
        self.part_offsets: List[int] = []
        self.part_offsets_offset = 0
        self.mesh_groups: List[MeshGroup] = []

    @property
    def geometry_lods(self) -> tuple["OccluderMeshData", ...]:
        return (self,)

    def read(self, h: BinaryHandler) -> None:
        self.part_count = h.read_uint8()
        self.vertex_format = h.read_uint8()
        self.lod_level = h.read_uint8()
        self.reserve = h.read_uint8()
        self.lod_factor = h.read_float()
        self.part_offsets_offset = h.read_int64()
        if self.part_count and self.part_offsets_offset <= 0:
            raise ValueError("Occluder mesh has no part-offset table")
        if self.part_count:
            with h.seek_temp(self.part_offsets_offset):
                self.part_offsets = [
                    h.read_int64() for _ in range(self.part_count)
                ]
        vertex_offsets: Dict[int, int] = {}
        for part_offset in self.part_offsets:
            if part_offset <= 0:
                continue
            h.seek(part_offset)
            group = MeshGroup(self.buffer)
            group.read(
                h,
                buffer_vertex_offsets=vertex_offsets,
                allow_initial_vertex_offset=True,
            )
            self.mesh_groups.append(group)
        self._set_geometry_spans(self.mesh_groups)


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
    tangent_handedness_mode: int
    position_compress_level: int
    relative_aabb_scale_exponent: int
    position_scale_exponent: int
    shader_unused_upper_byte: Optional[int]
    shader_unused_low_bits: int
    shader_unused_producer_bit7: bool
    shader_unused_bits14_15: int

    @property
    def producer_reserved_bit11(self) -> bool:
        return bool(self.raw & (1 << 11))

    @property
    def has_skin_stream(self) -> bool:
        return self.is_meshlet_use_skinned or self.is_meshlet_compressed_skinned

    @property
    def skin_stream_is_shared(self) -> bool:
        return self.is_meshlet_compressed_skinned

    @property
    def use_32bit_pos(self) -> bool:
        return self.position_compress_level == 1

    @property
    def use_24bit_pos(self) -> bool:
        return self.position_compress_level >= 2

    @classmethod
    def read(
        cls,
        raw: int,
        profile: MeshGameProfile | MeshMainVersion | int,
    ) -> "MPLYChunkFlags":
        profile = coerce_mesh_profile(profile)
        tangent_handedness_mode = (raw >> 14) & 0x3
        has_tangent_bits_block = (
            tangent_handedness_mode == 2
            if profile.meshlet_uses_mhwilds_tangent_handedness
            else (
                not profile.meshlet_uses_legacy_flag_bits
                and bool(raw & (1 << 15))
            )
        )
        position_compress_level = (raw >> 12) & 0x3
        if profile.meshlet_uses_legacy_flag_bits:
            shader_unused_low_bits = raw & (
                (1 << 7) | (1 << 14) | (1 << 15)
            )
        elif profile.meshlet_uses_pragmata_flag_bits:
            shader_unused_low_bits = raw & (
                (1 << 11)
                | (
                    0
                    if profile.meshlet_uses_mhwilds_tangent_handedness
                    else (1 << 14)
                )
            )
        else:
            shader_unused_low_bits = raw & (1 << 7)
        low_scale_exponent = (raw >> 16) & 0xFF
        upper_scale_exponent = (raw >> 24) & 0xFF
        dual_exponent_codec = profile.meshlet_uses_dual_exponent_position_decode
        return cls(
            raw=raw,
            is_meshlet_compressed_normal=bool(raw & (1 << 0)),
            is_meshlet_compressed_texcoord1=bool(raw & (1 << 1)),
            is_meshlet_compressed_texcoord2=bool(raw & (1 << 2)),
            is_meshlet_compressed_texcoord3=bool(raw & (1 << 3)),
            is_meshlet_compressed_vertex_color=bool(raw & (1 << 4)),
            is_meshlet_no_tangent=bool(
                raw
                & (1 << (5 if profile.meshlet_uses_pragmata_flag_bits else 6))
            ),
            is_meshlet_use_skinned=bool(
                raw
                & (1 << (6 if profile.meshlet_uses_pragmata_flag_bits else 11))
            ),
            is_meshlet_compressed_skinned=bool(
                raw
                & (1 << (7 if profile.meshlet_uses_pragmata_flag_bits else 5))
            ),
            is_meshlet_use_vertex_color=bool(raw & (1 << 8)),
            is_meshlet_use_texcoord2=bool(raw & (1 << 9)),
            is_meshlet_use_texcoord3=bool(raw & (1 << 10)),
            has_tangent_bits_block=has_tangent_bits_block,
            tangent_handedness_mode=tangent_handedness_mode,
            position_compress_level=position_compress_level,
            relative_aabb_scale_exponent=low_scale_exponent,
            position_scale_exponent=(
                upper_scale_exponent if dual_exponent_codec else low_scale_exponent
            ),
            shader_unused_upper_byte=(
                None if dual_exponent_codec else upper_scale_exponent
            ),
            shader_unused_low_bits=shader_unused_low_bits,
            shader_unused_producer_bit7=bool(
                raw & (1 << 7)
                and not profile.meshlet_uses_pragmata_flag_bits
            ),
            shader_unused_bits14_15=(raw >> 14) & 0x3,
        )


@dataclass
class MPLYClusterAABB:
    center: Tuple[float, float, float]
    half_extent: Tuple[float, float, float]

    @property
    def minimum(self) -> Tuple[float, float, float]:
        return tuple(
            value - extent
            for value, extent in zip(self.center, self.half_extent)
        )

    @property
    def maximum(self) -> Tuple[float, float, float]:
        return tuple(
            value + extent
            for value, extent in zip(self.center, self.half_extent)
        )


@dataclass(frozen=True)
class MPLYClusterCullingAABB:
    quantized_center: Tuple[int, int, int]
    scale_exponent: int
    position_compress_level: int
    center: Tuple[float, float, float]
    half_extent: float

    @property
    def minimum(self) -> Tuple[float, float, float]:
        return tuple(value - self.half_extent for value in self.center)

    @property
    def maximum(self) -> Tuple[float, float, float]:
        return tuple(value + self.half_extent for value in self.center)


# Meshlet chunk header: center, vertex/face counts, material and part IDs, relative AABB, flags.
_MPLY_CHUNK_HEADER = struct.Struct("<3f4B6HI")

_ARRAY_DTYPES = {"f": np.float32, "d": np.float64, "B": np.uint8, "H": np.uint16, "I": np.uint32}

MPLY_VECTOR_PLANES = {"positions": 3, "normals": 3, "tangents": 3, "uv0": 2, "uv1": 2, "uv2": 2}
MPLY_INTEGER_PLANES = ("faces", "colors", "normal_ws", "tangent_ws", "tangent_mask_bits")
_MPLY_PLANES = frozenset((*MPLY_VECTOR_PLANES, *MPLY_INTEGER_PLANES))


def mply_plane(name: str, values: object) -> np.ndarray:
    """Integer planes keep a non-integer input's own type, so the writer can still reject it."""
    width = MPLY_VECTOR_PLANES.get(name)
    if width is not None:
        array_value = np.array(values, dtype=np.float64)
        return array_value.reshape(0, width) if array_value.size == 0 else array_value
    array_value = np.array(values)
    if array_value.size == 0:
        return np.zeros(0, np.int64)
    return array_value.astype(np.int64) if array_value.dtype.kind in "iub" else array_value


def _mply_planes_equal(left: np.ndarray, right: object) -> bool:
    right = np.asarray(right)
    return left.shape == right.shape and bool(np.array_equal(left, right))


@dataclass(eq=False)
class MPLYChunk:
    vert_count: int
    positions: np.ndarray
    faces: np.ndarray
    normals: np.ndarray
    uv0: np.ndarray
    uv1: np.ndarray
    uv2: np.ndarray
    colors: np.ndarray
    normal_ws: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    tangents: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    tangent_ws: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    material_id: int = 0
    part_id: int = 0
    flags: Optional[MPLYChunkFlags] = None
    skin_record_bytes: bytes = b""
    skin_weights: Optional[SkinWeightBuffer] = None
    skin_is_shared: bool = False
    cluster_aabb: Optional[MPLYClusterAABB] = None
    tangent_mask_bytes: bytes = b""
    tangent_mask_bits: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int64))
    face_alignment_padding: bytes = b""
    position_alignment_padding: bytes = b""
    trailing_payload: bytes = b""
    lod_index: int = 0
    no_ray_tracing: bool = False
    alpha_test: bool = False
    transparent: bool = False
    meshlet_material_flag_extension: bool = False

    def __setattr__(self, name: str, value: object) -> None:
        if name in _MPLY_PLANES:
            if not (isinstance(value, np.ndarray) and value.flags.writeable and value.dtype.kind in "fi"
                    and (value.ndim == 2) == (name in MPLY_VECTOR_PLANES)
                    and (value.dtype == np.float64 if name in MPLY_VECTOR_PLANES else value.dtype == np.int64)):
                value = mply_plane(name, value)
        object.__setattr__(self, name, value)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, MPLYChunk):
            return NotImplemented
        for item in fields(self):
            left, right = getattr(self, item.name), getattr(other, item.name)
            if isinstance(left, np.ndarray):
                if not _mply_planes_equal(left, right):
                    return False
            elif left != right:
                return False
        return True

    __hash__ = None


@dataclass
class MPLYClusterHeader:
    vertex_count: int
    index_count: int
    material_id: int
    index_offset_bytes: int
    part_id: int = 0
    no_ray_tracing: bool = False
    alpha_test: bool = False
    transparent: bool = False
    position_compress_level: int = 0
    meshlet_material_flag_extension: bool = False
    reserved_bit31: bool = False
    # Not a byte offset: the 24-bit value indexes MeshletBVH.QuantizeAABBCenter.
    aabb_center_offset: int = 0
    culling_aabb: Optional[MPLYClusterCullingAABB] = None

    @property
    def aabb_center_index(self) -> int:
        return self.aabb_center_offset


@dataclass(frozen=True)
class MPLYMeshletHeader:
    min_aabb: Tuple[float, float, float]
    lod_num: int
    loaded_lod: int
    resident_lod: int
    allocation_lod: int
    max_aabb: Tuple[float, float, float]
    valid_lod_bits: int
    lod_cluster_offsets: Tuple[int, ...]
    lod_factor_or_cpu_cluster_offset_size_words: Tuple[int, ...]
    bindless_geometry_or_cpu_cluster_size_words: Tuple[int, ...]
    lod_factor_encoding: MeshletLodFactorEncoding = (
        MeshletLodFactorEncoding.FLOAT32
    )

    @property
    def lod_factors(self) -> Tuple[float, ...]:
        if self.lod_factor_encoding is MeshletLodFactorEncoding.CPU_UNUSED:
            return ()
        if self.lod_factor_encoding is MeshletLodFactorEncoding.PACKED_BINARY16:
            return self.packed_binary16_lod_factor_lanes
        return tuple(
            struct.unpack("<f", struct.pack("<I", word))[0]
            for word in self.lod_factor_or_cpu_cluster_offset_size_words
        )

    @property
    def packed_binary16_lod_factor_lanes(self) -> Tuple[float, ...]:
        if (
            self.lod_factor_encoding
            is not MeshletLodFactorEncoding.PACKED_BINARY16
        ):
            return ()
        raw = struct.pack(
            "<4I", *self.lod_factor_or_cpu_cluster_offset_size_words[:4]
        )
        return tuple(float(value) for value in struct.unpack("<8e", raw))

    @property
    def serialized_meshlet_raster_switch_threshold(self) -> int:
        if self.lod_factor_encoding is not MeshletLodFactorEncoding.PACKED_BINARY16:
            return 0
        return int(self.lod_factor_or_cpu_cluster_offset_size_words[4]) & 0x7

    @property
    def serialized_mpmr_lod_strategy(self) -> MPMRLodStrategy:
        if self.lod_factor_encoding is not MeshletLodFactorEncoding.PACKED_BINARY16:
            return MPMRLodStrategy.ScreenArea
        value = (
            int(self.lod_factor_or_cpu_cluster_offset_size_words[4]) >> 3
        ) & 0x1
        return MPMRLodStrategy(value)

    @property
    def serialized_lod_control_reserved_bits(self) -> int:
        if self.lod_factor_encoding is not MeshletLodFactorEncoding.PACKED_BINARY16:
            return 0
        return int(self.lod_factor_or_cpu_cluster_offset_size_words[4]) & ~0xF

    @property
    def serialized_meshlet_raster_switch_triangle_count(self) -> int:
        return (self.serialized_meshlet_raster_switch_threshold + 1) * 16

    @property
    def serialized_lod_payload_reserved_words(self) -> Tuple[int, ...]:
        if self.lod_factor_encoding is not MeshletLodFactorEncoding.PACKED_BINARY16:
            return ()
        return self.lod_factor_or_cpu_cluster_offset_size_words[5:8]

    @property
    def active_lod_factors(self) -> Tuple[float, ...]:
        return self.lod_factors[: self.lod_num]

    @property
    def bindless_geometry_offsets(self) -> Tuple[int, ...]:
        return self.bindless_geometry_or_cpu_cluster_size_words

    @property
    def serialized_cpu_unused_words(self) -> Tuple[int, ...]:
        return self.lod_factor_or_cpu_cluster_offset_size_words

    @property
    def serialized_lod_end_offsets(self) -> Tuple[int, ...]:
        return self.bindless_geometry_or_cpu_cluster_size_words


def _mply_pivot_count(serialized_count: int, blob_relative_offset: int) -> int:
    return 256 if blob_relative_offset and serialized_count == 0 else serialized_count


@dataclass(frozen=True)
class MPLYGroupPivotInfo:
    serialized_count: int
    blob_relative_offset: int
    reserve_u16: int
    reserve_u32: Tuple[int, int, int]
    pivots: Tuple[Tuple[float, float, float], ...]
    layout_mirror_matches_blob: bool
    header_blob_relative_offset: int = 0
    header_offset_matches_internal: bool = False
    skin_weight_count_raw: int = 0

    @property
    def pivot_count(self) -> int:
        return _mply_pivot_count(self.serialized_count, self.blob_relative_offset)

    @property
    def present(self) -> bool:
        return bool(self.blob_relative_offset and self.pivot_count)


@dataclass(frozen=True)
class MPLYLodRegion:
    lod_index: int
    table_relative_offset: int
    table_absolute_offset: int
    region_end_absolute: int
    cluster_count: int
    chunk_relative_offsets: Tuple[int, ...]


@dataclass(frozen=True)
class MPLYMeshletLayout:
    gpu_meshlet_offset: int
    header: MPLYMeshletHeader
    extension_words: Tuple[int, ...]
    gpu_data_size: int
    reserved: int
    gpu_header_matches_resident: bool
    group_pivot_info: Optional[MPLYGroupPivotInfo] = None
    lod_regions: Tuple[MPLYLodRegion, ...] = ()
    # The blob as the engine's geometry buffer holds it, except for the resident LOD the runtime sets in its header.
    gpu_data: bytes = field(default=b"", repr=False, compare=False)


@dataclass(frozen=True)
class MPLYMeshletBVH:
    gpu_cluster_headers: int
    gpu_cluster_quantize_offset: int
    offset: Tuple[float, float, float]
    scale: float
    cluster_counts: Tuple[int, ...]
    cluster_count_metadata: Tuple[int, ...]
    cluster_offsets: Tuple[int, ...]
    meshlet_bvh_offsets: Tuple[int, ...]
    quantized_aabb_centers: Tuple[Tuple[int, int, int], ...]
    scale_exponent_is_signed: bool = False


    @property
    def cluster_count_reserved_bytes(self) -> Tuple[int, ...]:
        return tuple(value & 0xFF for value in self.cluster_count_metadata)

    @property
    def cluster_bounds_scale_exponents(self) -> Tuple[int, ...]:
        raw = self.cluster_bounds_scale_exponent_raw_bytes
        if not self.scale_exponent_is_signed:
            return raw
        return tuple(value - 0x100 if value & 0x80 else value for value in raw)

    @property
    def cluster_bounds_scale_exponent_raw_bytes(self) -> Tuple[int, ...]:
        return tuple((value >> 8) & 0xFF for value in self.cluster_count_metadata)

    @property
    def cluster_bounds_quantization_units(self) -> Tuple[float, ...]:
        return tuple(
            math.ldexp(1.0, -16 - exponent)
            for exponent in self.cluster_bounds_scale_exponents
        )

    def decode_cluster_culling_aabb(
        self,
        lod_index: int,
        cluster: MPLYClusterHeader,
    ) -> MPLYClusterCullingAABB:
        if not 0 <= lod_index < len(self.cluster_counts):
            raise IndexError(f"MPLY LOD index {lod_index} is out of range")
        center_index = int(cluster.aabb_center_index)
        if not 0 <= center_index < len(self.quantized_aabb_centers):
            raise IndexError(
                f"MPLY AABB-center index {center_index} is out of range"
            )
        level = int(cluster.position_compress_level)
        maximum_delta = (65535, 1023, 255, 255)[level]
        exponent = self.cluster_bounds_scale_exponents[lod_index]
        normalized_half_extent = math.ldexp(
            float(maximum_delta),
            -16 - exponent,
        )
        quantized = self.quantized_aabb_centers[center_index]
        center = tuple(
            origin + float(value) * self.scale / 65536.0
            for origin, value in zip(self.offset, quantized)
        )
        return MPLYClusterCullingAABB(
            quantized_center=quantized,
            scale_exponent=exponent,
            position_compress_level=level,
            center=center,
            half_extent=abs(float(self.scale)) * normalized_half_extent,
        )


@dataclass(frozen=True)
class MPLYMeshletPartsLayout:
    parts_indices_offset: int
    parts_num: int
    reserve: int
    parts_indices_raw: bytes
    alignment_padding: bytes = b""

    @property
    def parts_indices(self) -> Tuple[int, ...]:
        return tuple(self.parts_indices_raw)


class MPLYParser:
    def __init__(self, header: Header, data: bytes, streaming_data: Optional[bytes] = None):
        self.header = header
        self.profile = header.profile
        self.data = data
        self.streaming_data = streaming_data
        self.h = BinaryHandler(data)
        self.layout: Optional[MPLYMeshletLayout] = None
        self.bvh: Optional[MPLYMeshletBVH] = None
        self.group_pivots: Optional[GroupPivotData] = None

    @staticmethod
    def _position_size(flags: MPLYChunkFlags) -> int:
        if flags.position_compress_level == 1:
            return 4
        if flags.position_compress_level >= 2:
            return 3
        return 6

    @staticmethod
    def _shared_count(is_compressed: bool, vert_count: int) -> int:
        return 1 if is_compressed else vert_count

    @staticmethod
    def _aligned(pos: int, size: int = 4) -> int:
        return pos + ((size - (pos % size)) % size)

    @staticmethod
    def _decode_normal_block(
        data: bytes,
        flags: MPLYChunkFlags,
        vert_count: int,
    ) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        stride = 4 if flags.is_meshlet_no_tangent else 8
        shared_count = 1 if flags.is_meshlet_compressed_normal else vert_count
        expected_size = shared_count * stride
        if len(data) != expected_size:
            raise ValueError(
                "MPLY normal/tangent block size does not match its flag-owned "
                f"record count ({len(data)} != {expected_size})"
            )
        raw = np.frombuffer(data, np.int8).reshape(shared_count, stride)
        normals = np.maximum(raw[:, :3].astype(np.float64) / 127.0, -1.0)
        normal_ws = raw[:, 3].view(np.uint8).astype(np.int64)
        if flags.is_meshlet_no_tangent:
            tangents = np.ones((shared_count, 3))
            tangent_ws = np.full(shared_count, 127, np.int64)
        else:
            tangents = np.maximum(raw[:, 4:7].astype(np.float64) / 127.0, -1.0)
            tangent_ws = raw[:, 7].view(np.uint8).astype(np.int64)
        if shared_count == 1 and vert_count > 1:
            normals, normal_ws, tangents, tangent_ws = (
                np.repeat(plane, vert_count, axis=0) for plane in (normals, normal_ws, tangents, tangent_ws)
            )
        return normals, normal_ws, tangents, tangent_ws

    @staticmethod
    def _decode_mhwilds_tangent_handedness(
        flags: MPLYChunkFlags,
        vert_count: int,
        tangent_mask_bits: Sequence[int],
    ) -> np.ndarray:
        if flags.is_meshlet_compressed_texcoord1:
            return np.zeros(vert_count, np.int64)
        if flags.tangent_handedness_mode == 2:
            if len(tangent_mask_bits) != vert_count:
                raise ValueError(
                    "Wilds MPLY tangent mask does not match its vertex count"
                )
            return np.where(np.asarray(tangent_mask_bits, dtype=bool), 0x81, 0x7F).astype(np.int64)
        value = 0x7F if flags.tangent_handedness_mode == 0 else 0x81
        return np.full(vert_count, value, np.int64)

    @staticmethod
    def _decode_color_block(data: bytes, is_compressed: bool, vert_count: int) -> np.ndarray:
        count = 1 if is_compressed else vert_count
        expected_size = count * 4
        if len(data) != expected_size:
            raise ValueError(
                "MPLY color block size does not match its flag-owned record "
                f"count ({len(data)} != {expected_size})"
            )
        colors = np.frombuffer(data, np.uint8).astype(np.int64)
        if count == 1 and vert_count > 1:
            colors = np.tile(colors, vert_count)
        return colors

    @staticmethod
    def _decode_uv_block(data: bytes, is_compressed: bool, vert_count: int) -> np.ndarray:
        count = 1 if is_compressed else vert_count
        expected_size = count * 4
        if len(data) != expected_size:
            raise ValueError(
                "MPLY UV block size does not match its flag-owned record "
                f"count ({len(data)} != {expected_size})"
            )
        uvs = np.frombuffer(data, "<f2").astype(np.float64).reshape(-1, 2)
        if count == 1 and vert_count > 1:
            uvs = np.repeat(uvs, vert_count, axis=0)
        return uvs

    def _read_uv_block(self, is_compressed: bool, vert_count: int) -> np.ndarray:
        count = self._shared_count(is_compressed, vert_count)
        return self._decode_uv_block(self.h.read_bytes(count * 4), is_compressed, vert_count)

    @staticmethod
    def _decode_positions(
        data: bytes,
        vert_count: int,
        flags: MPLYChunkFlags,
        center: Tuple[float, float, float],
        relative_aabb: Tuple[float, float, float, float, float, float],
        profile: MeshGameProfile | MeshMainVersion | int,
    ) -> np.ndarray:
        """Same operation order as the shaders' decode."""
        profile = coerce_mesh_profile(profile)
        expected_size = vert_count * MPLYParser._position_size(flags)
        if len(data) != expected_size:
            raise ValueError(
                "MPLY position block size does not match its serialized vertex "
                f"count ({len(data)} != {expected_size})"
            )
        if profile.meshlet_uses_centered_exponent_position_decode:
            # Wilds splits the exponent: bits 16-23 relative AABB, bits 24-31 vertex delta; both bias 143.
            position_unit = math.ldexp(
                1.0,
                flags.position_scale_exponent - 143,
            )
            relative_aabb_unit = math.ldexp(
                1.0,
                flags.relative_aabb_scale_exponent - 143,
            )
            relative_base = (
                center[0] + (relative_aabb[0] - 32768) * relative_aabb_unit,
                center[1] + (relative_aabb[2] - 32768) * relative_aabb_unit,
                center[2] + (relative_aabb[4] - 32768) * relative_aabb_unit,
            )
            if flags.position_compress_level == 0:
                raw_positions = np.frombuffer(data, "<u2").reshape(vert_count, 3).astype(np.int64)
                base = (
                    relative_base
                    if profile.meshlet_uses_dual_exponent_position_decode
                    else center
                )
                return np.asarray(base, np.float64) + (raw_positions - 32768) * position_unit

            if flags.position_compress_level == 1:
                raw_positions = MPLYParser._unpack_10bit(data, vert_count)
                midpoint = 512
            else:
                # Levels 2 and 3 share the same three-byte shader path.
                raw_positions = np.frombuffer(data, np.uint8).reshape(vert_count, 3).astype(np.int64)
                midpoint = 128
            return np.asarray(relative_base, np.float64) + (raw_positions - midpoint) * position_unit

        if flags.use_24bit_pos:
            pos_array = np.frombuffer(data, np.uint8).reshape(vert_count, 3).astype(np.int64) / 255.0
        elif flags.use_32bit_pos:
            inv_10bit = 1.0 / 1023.0
            pos_array = MPLYParser._unpack_10bit(data, vert_count) * inv_10bit
        else:
            pos_array = np.frombuffer(data, "<u2").reshape(vert_count, 3).astype(np.int64) / 65535.0

        if not profile.meshlet_has_wilds_layout:
            scale_byte = (flags.raw >> 16) & 0xFF
            scale = math.ldexp(1.0, scale_byte - 127)
            return (pos_array - 0.5) * scale + np.asarray(center, np.float64)

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
        
        return (((pos_array - 0.5) * quant_scale) + np.asarray(offset_terms, np.float64)) * scale + np.asarray(
            center, np.float64
        )

    @staticmethod
    def _unpack_10bit(data: bytes, vert_count: int) -> np.ndarray:
        packed = np.frombuffer(data, "<u4")[:vert_count].astype(np.int64)
        return np.stack((packed & 0x3FF, (packed >> 10) & 0x3FF, (packed >> 20) & 0x3FF), axis=1)

    @staticmethod
    def _decode_cluster_aabb(
        position_origin: Tuple[float, float, float],
        encoded: Tuple[int, int, int, int, int, int],
        flags: MPLYChunkFlags,
    ) -> MPLYClusterAABB:
        unit = math.ldexp(1.0, flags.relative_aabb_scale_exponent - 143)
        quantized_center = (encoded[0], encoded[2], encoded[4])
        quantized_extent = (encoded[1], encoded[3], encoded[5])
        center = tuple(
            origin + (value - 32768) * unit
            for origin, value in zip(position_origin, quantized_center)
        )
        half_extent = tuple(value * unit for value in quantized_extent)
        return MPLYClusterAABB(center=center, half_extent=half_extent)

    def _read_wilds_group_pivot_info(
        self,
        *,
        blob_base: int,
        blob_size: int,
        layout_raw: bytes,
        lod_offsets: Sequence[int],
    ) -> MPLYGroupPivotInfo:
        blob_raw = _checked_slice(
            self.data,
            blob_base + 128,
            16,
            "Wilds MPLY blob group-pivot descriptor",
        )
        (
            serialized_count,
            blob_relative_offset,
            reserve_u16,
            reserve0,
            reserve1,
            reserve2,
        ) = struct.unpack("<BBHIII", blob_raw)
        skin_weight_count_raw = 0
        if self.profile.meshlet_extension_has_skin_weight_count:
            # DD2: this word is a SkinWeightCount that picks the skinning permutation, as MeshData's does.
            skin_weight_count_raw, reserve0 = reserve0, 0
        pivot_count = _mply_pivot_count(serialized_count, blob_relative_offset)
        pivot_raw = b""
        pivots: Tuple[Tuple[float, float, float], ...] = ()
        if blob_relative_offset or pivot_count:
            if not blob_relative_offset or not pivot_count:
                raise ValueError(
                    "Wilds MPLY group-pivot descriptor has an incomplete "
                    "count/offset pair"
                )
            first_lod_offset = min(
                (int(value) for value in lod_offsets if value),
                default=blob_size,
            )
            pivot_end = blob_relative_offset + pivot_count * 12
            if (
                blob_relative_offset < 144
                or pivot_end > first_lod_offset
                or pivot_end > blob_size
            ):
                raise ValueError(
                    "Wilds MPLY group-pivot Float3 table leaves the blob prefix"
                )
            pivot_raw = _checked_slice(
                self.data,
                blob_base + blob_relative_offset,
                pivot_count * 12,
                "Wilds MPLY group-pivot Float3 table",
            )
            pivots = tuple(
                tuple(values)
                for values in struct.iter_unpack("<3f", pivot_raw)
            )
            self.group_pivots = GroupPivotData(
                buffer_size=len(pivot_raw),
                reserve=reserve_u16 | reserve0 | reserve1 | reserve2,
                buffer_offset=blob_base + blob_relative_offset,
                pivots=list(pivots),
                raw=pivot_raw,
            )
        else:
            self.group_pivots = None

        header_blob_relative_offset = int(self.header.floats_offset)
        return MPLYGroupPivotInfo(
            serialized_count=serialized_count,
            blob_relative_offset=blob_relative_offset,
            reserve_u16=reserve_u16,
            reserve_u32=(reserve0, reserve1, reserve2),
            skin_weight_count_raw=skin_weight_count_raw,
            pivots=pivots,
            layout_mirror_matches_blob=layout_raw == blob_raw,
            header_blob_relative_offset=header_blob_relative_offset,
            header_offset_matches_internal=(
                header_blob_relative_offset == blob_relative_offset
            ),
        )

    def _read_wilds_lod_regions(
        self,
        *,
        blob_base: int,
        blob_size: int,
        header: MPLYMeshletHeader,
    ) -> Tuple[MPLYLodRegion, ...]:
        regions: List[MPLYLodRegion] = []
        for lod_index in range(header.lod_num):
            table_relative = int(header.lod_cluster_offsets[lod_index])
            region_end = int(header.serialized_lod_end_offsets[lod_index])
            table_absolute = blob_base + table_relative
            if (
                table_relative < 128
                or table_absolute + 4 > region_end
                or region_end > blob_base + blob_size
            ):
                raise ValueError(
                    f"Wilds MPLY LOD {lod_index} table/end range is invalid"
                )
            cluster_count = struct.unpack_from(
                "<I",
                self.data,
                table_absolute,
            )[0]
            table_size = 4 + cluster_count * 4
            if table_absolute + table_size > region_end:
                raise ValueError(
                    f"Wilds MPLY LOD {lod_index} offset table exceeds its region"
                )
            chunk_offsets = (
                tuple(
                    struct.unpack_from(
                        f"<{cluster_count}I",
                        self.data,
                        table_absolute + 4,
                    )
                )
                if cluster_count
                else ()
            )
            region_end_relative = region_end - blob_base
            if any(
                value < table_relative + table_size
                or value >= region_end_relative
                for value in chunk_offsets
            ):
                raise ValueError(
                    f"Wilds MPLY LOD {lod_index} chunk offset leaves its region"
                )
            regions.append(
                MPLYLodRegion(
                    lod_index=lod_index,
                    table_relative_offset=table_relative,
                    table_absolute_offset=table_absolute,
                    region_end_absolute=region_end,
                    cluster_count=cluster_count,
                    chunk_relative_offsets=chunk_offsets,
                )
            )
        return tuple(regions)

    def _read_meshlet_layout(self) -> Tuple[int, int, List[int]]:
        self.h.seek(self.header.mesh_offset)
        blob_base = self.h.read_int64()
        min_aabb = tuple(self.h.read_vec3())
        lod_info = self.h.read_uint32()
        lod_num = lod_info & 0xFF
        max_aabb = tuple(self.h.read_vec3())
        valid_lod_bits = self.h.read_uint32()
        lod_offsets = [self.h.read_uint32() for _ in range(8)]
        payload_words0 = tuple(self.h.read_uint32() for _ in range(8))
        payload_words1 = tuple(self.h.read_uint32() for _ in range(8))
        extension_words: Tuple[int, ...] = ()
        extension_raw = b""
        if self.profile.meshlet_has_wilds_layout:
            extension_raw = self.h.read_bytes(16)
            extension_words = struct.unpack("<QQ", extension_raw)
        blob_size = self.h.read_uint32()
        reserved = self.h.read_uint32()
        resident_header = _checked_slice(
            self.data,
            self.header.mesh_offset + 8,
            128,
            "Resident MeshletHeader",
        )
        gpu_header_matches_resident = (
            0 <= blob_base <= len(self.data) - 128
            and resident_header
            == bytes(memoryview(self.data)[blob_base : blob_base + 128])
        )
        meshlet_header = MPLYMeshletHeader(
            min_aabb=min_aabb,
            lod_num=lod_num,
            loaded_lod=(lod_info >> 8) & 0xFF,
            resident_lod=(lod_info >> 16) & 0xFF,
            allocation_lod=(lod_info >> 24) & 0xFF,
            max_aabb=max_aabb,
            valid_lod_bits=valid_lod_bits,
            lod_cluster_offsets=tuple(lod_offsets),
            lod_factor_or_cpu_cluster_offset_size_words=payload_words0,
            bindless_geometry_or_cpu_cluster_size_words=payload_words1,
            lod_factor_encoding=self.profile.meshlet_lod_factor_encoding,
        )
        if lod_num > 8 or blob_base < 0 or blob_base + blob_size > len(self.data):
            raise ValueError("Invalid MPLY meshlet layout")
        group_pivot_info = None
        lod_regions: Tuple[MPLYLodRegion, ...] = ()
        if self.profile.meshlet_has_wilds_layout:
            if blob_size < 144:
                raise ValueError("Wilds MPLY blob is smaller than its header prefix")
            group_pivot_info = self._read_wilds_group_pivot_info(
                blob_base=blob_base,
                blob_size=blob_size,
                layout_raw=extension_raw,
                lod_offsets=lod_offsets[:lod_num],
            )
            lod_regions = self._read_wilds_lod_regions(
                blob_base=blob_base,
                blob_size=blob_size,
                header=meshlet_header,
            )
        self.layout = MPLYMeshletLayout(
            gpu_meshlet_offset=blob_base,
            header=meshlet_header,
            extension_words=extension_words,
            gpu_data_size=blob_size,
            reserved=reserved,
            gpu_header_matches_resident=gpu_header_matches_resident,
            group_pivot_info=group_pivot_info,
            lod_regions=lod_regions,
            gpu_data=bytes(memoryview(self.data)[blob_base:blob_base + blob_size]),
        )
        return blob_base, blob_size, lod_offsets[:lod_num]


    def _read_skin_stream(self, flags, vert_count: int):
        skin_count = self._shared_count(flags.skin_stream_is_shared, vert_count)
        skin_record_bytes = self.h.read_bytes(skin_count * 16)
        skin_is_shared = skin_count == 1 and vert_count > 1
        expanded_skin = skin_record_bytes * vert_count if skin_is_shared else skin_record_bytes
        return (
            skin_record_bytes,
            skin_is_shared,
            _decode_skin_weights(memoryview(expanded_skin), vert_count, self.profile),
        )

    def _parse_chunk(self, start: int, end: Optional[int] = None) -> MPLYChunk:
        if not 0 <= start < len(self.data):
            raise ValueError(f"Invalid MPLY chunk start {start}")
        if end is not None and not start < end <= len(self.data):
            raise ValueError(f"Invalid MPLY chunk range [{start}, {end})")
        self.h.seek(start)
        if len(self.data) - start >= _MPLY_CHUNK_HEADER.size:
            header = _MPLY_CHUNK_HEADER.unpack(self.h.read_bytes(_MPLY_CHUNK_HEADER.size))
            center, (vert_count, face_count, material_id, part_id) = header[:3], header[3:7]
            relative_aabb, raw_flags = header[7:13], header[13]
        else:  # truncated: the field-by-field reads report where the data ends
            center = tuple(self.h.read_vec3())
            vert_count = self.h.read_uint8()
            face_count = self.h.read_uint8()
            material_id = self.h.read_uint8()
            part_id = self.h.read_uint8()
            relative_aabb = tuple(self.h.read_uint16() for _ in range(6))
            raw_flags = self.h.read_uint32()
        flags = MPLYChunkFlags.read(raw_flags, self.profile)
        cluster_aabb = self._decode_cluster_aabb(center, relative_aabb, flags)

        faces = np.frombuffer(self.h.read_bytes(face_count * 3), np.uint8).astype(np.int64)
        face_padding_size = self._aligned(self.h.tell) - self.h.tell
        face_alignment_padding = self.h.read_bytes(face_padding_size)
        positions = self._decode_positions(
            self.h.read_bytes(vert_count * self._position_size(flags)),
            vert_count,
            flags,
            center,
            relative_aabb,
            self.profile,
        )
        position_padding_size = self._aligned(self.h.tell) - self.h.tell
        position_alignment_padding = self.h.read_bytes(position_padding_size)
        if (
            self.profile.meshlet_position_alignment_padding_is_corpus_zero
            and any(position_alignment_padding)
        ):
            raise ValueError(
                f"{self.profile.display_name} MPLY position alignment padding "
                "is nonzero"
            )

        normal_count = self._shared_count(flags.is_meshlet_compressed_normal, vert_count)
        normal_stride = 4 if flags.is_meshlet_no_tangent else 8
        normals, normal_ws, tangents, tangent_ws = self._decode_normal_block(
            self.h.read_bytes(normal_count * normal_stride),
            flags,
            vert_count,
        )

        tangent_mask_bytes = b""
        tangent_mask_bits = np.zeros(0, np.int64)
        if flags.has_tangent_bits_block and (
            not self.profile.meshlet_uses_pragmata_flag_bits
            or flags.is_meshlet_no_tangent
        ):
            tangent_mask_bytes = self.h.read_bytes(((vert_count + 31) // 32) * 4)
            tangent_mask_bits = np.unpackbits(
                np.frombuffer(tangent_mask_bytes, np.uint8), bitorder="little"
            )[:vert_count].astype(np.int64)

        if (
            self.profile.meshlet_uses_mhwilds_tangent_handedness
            and flags.is_meshlet_no_tangent
        ):
            tangents = np.zeros((0, 3))
            tangent_ws = self._decode_mhwilds_tangent_handedness(
                flags,
                vert_count,
                tangent_mask_bits,
            )

        uv0 = self._read_uv_block(flags.is_meshlet_compressed_texcoord1, vert_count)
        uv1 = (
            self._read_uv_block(flags.is_meshlet_compressed_texcoord2, vert_count)
            if flags.is_meshlet_use_texcoord2 else np.zeros((0, 2))
        )

        # DD2's meshlet shader reads the 16-byte skin stream between texcoord2 and texcoord3.
        skin_before_texcoord3 = self.profile.meshlet_skin_before_texcoord3
        skin_record_bytes = b""
        skin_weights: Optional[SkinWeightBuffer] = None
        skin_is_shared = False
        if skin_before_texcoord3 and flags.has_skin_stream:
            skin_record_bytes, skin_is_shared, skin_weights = self._read_skin_stream(flags, vert_count)

        uv2 = (
            self._read_uv_block(flags.is_meshlet_compressed_texcoord3, vert_count)
            if flags.is_meshlet_use_texcoord3 else np.zeros((0, 2))
        )

        colors = np.zeros(0, np.int64)
        if flags.is_meshlet_use_vertex_color:
            color_count = self._shared_count(flags.is_meshlet_compressed_vertex_color, vert_count)
            colors = self._decode_color_block(self.h.read_bytes(color_count * 4), flags.is_meshlet_compressed_vertex_color, vert_count)

        if not skin_before_texcoord3 and flags.has_skin_stream:
            skin_record_bytes, skin_is_shared, skin_weights = self._read_skin_stream(flags, vert_count)

        semantic_end = self.h.tell
        if end is None:
            end = semantic_end
        if semantic_end > end:
            raise ValueError(f"MPLY chunk data exceeds its boundary at {end}")
        trailing_payload = bytes(memoryview(self.data)[semantic_end:end])
        if self.profile.meshlet_chunk_payload_is_fully_decoded and trailing_payload:
            raise ValueError(
                f"{self.profile.display_name} MPLY chunk has "
                f"{len(trailing_payload)} unparsed payload "
                f"bytes before boundary {end}"
            )
        self.h.seek(end)
        return MPLYChunk(
            vert_count=vert_count,
            positions=positions,
            faces=faces,
            normals=normals,
            normal_ws=normal_ws,
            tangents=tangents,
            tangent_ws=tangent_ws,
            uv0=uv0,
            uv1=uv1,
            uv2=uv2,
            colors=colors,
            material_id=material_id,
            part_id=part_id,
            flags=flags,
            skin_record_bytes=skin_record_bytes,
            skin_weights=skin_weights,
            skin_is_shared=skin_is_shared,
            cluster_aabb=cluster_aabb,
            tangent_mask_bytes=tangent_mask_bytes,
            tangent_mask_bits=tangent_mask_bits,
            face_alignment_padding=face_alignment_padding,
            position_alignment_padding=position_alignment_padding,
            trailing_payload=trailing_payload,
        )

    def _read_cluster_headers(self) -> List[List[MPLYClusterHeader]]:
        if not self.header.meshlet_bvh_offset:
            raise ValueError("MPLY has no cluster headers")
        self.h.seek(self.header.meshlet_bvh_offset)
        gpu_cluster_headers = self.h.read_uint64()
        gpu_cluster_quantize_offset = self.h.read_uint64()
        bvh_offset = tuple(self.h.read_vec3())
        bvh_scale = self.h.read_float()
        counts = []
        count_metadata = []
        for _ in range(8):
            if self.profile.meshlet_has_extended_indices:
                counts.append(self.h.read_uint16())
                count_metadata.append(self.h.read_uint16())
            else:
                counts.append(self.h.read_uint32())
                count_metadata.append(0)
        cluster_offsets = [self.h.read_uint32() for _ in range(8)]
        meshlet_bvh_offsets = [self.h.read_uint32() for _ in range(8)]
        total_cluster_count = sum(counts)
        quantized_aabb_centers: Tuple[Tuple[int, int, int], ...] = ()
        if gpu_cluster_quantize_offset and total_cluster_count:
            raw_centers = _checked_slice(
                self.data,
                gpu_cluster_quantize_offset,
                total_cluster_count * 6,
                "MeshletBVH QuantizeAABBCenter table",
            )
            quantized_aabb_centers = tuple(
                tuple(values)
                for values in struct.iter_unpack("<3H", raw_centers)
            )
        self.bvh = MPLYMeshletBVH(
            gpu_cluster_headers=gpu_cluster_headers,
            gpu_cluster_quantize_offset=gpu_cluster_quantize_offset,
            offset=bvh_offset,
            scale=bvh_scale,
            cluster_counts=tuple(counts),
            cluster_count_metadata=tuple(count_metadata),
            cluster_offsets=tuple(cluster_offsets),
            meshlet_bvh_offsets=tuple(meshlet_bvh_offsets),
            quantized_aabb_centers=quantized_aabb_centers,
            scale_exponent_is_signed=(
                self.profile.meshlet_bvh_scale_exponent_is_signed
            ),
        )
        lod_headers: List[List[MPLYClusterHeader]] = []
        for lod_index, count in enumerate(counts):
            headers: List[MPLYClusterHeader] = []
            if count:
                self.h.seek(gpu_cluster_headers + cluster_offsets[lod_index])
                for _ in range(count):
                    word0 = self.h.read_uint32()
                    word1 = self.h.read_uint32()
                    self.h.read_uint32()  # The cluster's vertex byte offset.
                    index_offset = self.h.read_uint32()
                    cluster_header = MPLYClusterHeader(
                        vertex_count=word0 & 0xFF,
                        index_count=(word0 >> 8) & 0x1FF,
                        # Bit 22 is RE9's MeshletMaterialFlagExtension.
                        material_id=(word0 >> 23) & 0xFF,
                        index_offset_bytes=index_offset,
                        part_id=word1 & 0xFF,
                        no_ray_tracing=bool(word0 & (1 << 17)),
                        alpha_test=bool(word0 & (1 << 18)),
                        transparent=bool(word0 & (1 << 19)),
                        position_compress_level=(word0 >> 20) & 0x3,
                        meshlet_material_flag_extension=bool(word0 & (1 << 22)),
                        reserved_bit31=bool(word0 & (1 << 31)),
                        aabb_center_offset=(word1 >> 8) & 0xFFFFFF,
                    )
                    if self.bvh.gpu_cluster_quantize_offset:
                        cluster_header.culling_aabb = (
                            self.bvh.decode_cluster_culling_aabb(
                                lod_index,
                                cluster_header,
                            )
                        )
                    headers.append(cluster_header)
            lod_headers.append(headers)
        return lod_headers

    def _read_parts_layout(self) -> Optional[MPLYMeshletPartsLayout]:
        offset = int(self.header.meshlet_parts_offset)
        if not offset:
            return None
        raw = _checked_slice(self.data, offset, 16, "MeshletPartsLayout")
        parts_indices_offset, parts_num, reserve = struct.unpack("<QII", raw)
        parts_indices_raw = (
            _checked_slice(
                self.data,
                parts_indices_offset,
                parts_num,
                "MeshletPartsLayout.partsIndices",
            )
            if parts_num
            else b""
        )
        if bool(parts_indices_offset) != bool(parts_num):
            raise ValueError(
                "MeshletPartsLayout partsIndices offset/count presence must agree"
            )
        alignment_padding = b""
        if parts_num:
            expected_layout_offset = (
                int(parts_indices_offset) + int(parts_num) + 15
            ) & ~15
            if offset == expected_layout_offset:
                alignment_padding = _checked_slice(
                    self.data,
                    int(parts_indices_offset) + int(parts_num),
                    offset - int(parts_indices_offset) - int(parts_num),
                    "MeshletPartsLayout.partsIndices alignment padding",
                )
        return MPLYMeshletPartsLayout(
            parts_indices_offset=parts_indices_offset,
            parts_num=parts_num,
            reserve=reserve,
            parts_indices_raw=parts_indices_raw,
            alignment_padding=alignment_padding,
        )

    def _read_streaming_faces(
        self,
        cluster_header: MPLYClusterHeader,
    ) -> Optional[np.ndarray]:
        if self.streaming_data is None or cluster_header.index_count == 0:
            return None
        start = cluster_header.index_offset_bytes
        data = _checked_slice(
            self.streaming_data,
            start,
            cluster_header.index_count * 2,
            "MPLY streaming indices",
        )
        return np.frombuffer(data, "<u2").astype(np.int64)

    def parse(self) -> Tuple[MeshBuffer, List[MeshData]]:
        blob_base, blob_size, lod_offsets = self._read_meshlet_layout()
        cluster_headers = self._read_cluster_headers()
        parts_layout = self._read_parts_layout()
        if parts_layout is not None:
            stored_part_ids = parts_layout.parts_indices
            cluster_part_ids = {
                header.part_id
                for lod_headers in cluster_headers
                for header in lod_headers
            }
            if len(set(stored_part_ids)) != len(stored_part_ids):
                raise ValueError("MPLY partsIndices contains duplicate part IDs")
            if set(stored_part_ids) != cluster_part_ids:
                raise ValueError(
                    "MPLY partsIndices does not match compacted-cluster part IDs"
                )
        mesh_buffer = MeshBuffer(self.profile)
        mesh_buffer.meshlet_layout = self.layout
        if self.layout is not None:
            meshlet_header = self.layout.header
            mesh_buffer.mply_runtime_settings = MPLYRuntimeSettings(
                lod_factors=tuple(meshlet_header.active_lod_factors),
                meshlet_raster_switch_threshold=int(
                    meshlet_header.serialized_meshlet_raster_switch_threshold
                ),
                lod_strategy=meshlet_header.serialized_mpmr_lod_strategy,
            )
        mesh_buffer.meshlet_bvh = self.bvh
        mesh_buffer.meshlet_parts_layout = parts_layout
        mesh_buffer.mply_group_pivots = self.group_pivots
        mesh_buffer.mply_cluster_headers = [
            header
            for lod_headers in cluster_headers
            for header in lod_headers
        ]
        mesh_buffer.mply_chunk_flags = []
        lods: List[MeshLOD] = []
        planes: Dict[str, List[np.ndarray]] = {
            name: [] for name in ("positions", "normals", "normal_ws", "tangents", "tangent_ws",
                                  "uv0", "uv1", "uv2", "colors", "faces")
        }
        tangents_seen = False
        face_total = 0
        all_skin_record_bytes = bytearray()
        uv1_complete = True
        uv2_complete = True
        skin_complete = True
        vertex_base = 0

        lod_chunk_offsets: List[Optional[List[int]]] = []
        lod_region_ends: List[Optional[int]] = []
        compatibility_chunk_offsets: List[int] = []
        if self.layout is not None and self.layout.lod_regions:
            for region in self.layout.lod_regions:
                offsets = list(region.chunk_relative_offsets)
                lod_chunk_offsets.append(offsets)
                lod_region_ends.append(int(region.region_end_absolute))
        else:
            for lod_offset in lod_offsets:
                if lod_offset == 0:
                    lod_chunk_offsets.append(None)
                    lod_region_ends.append(None)
                    continue
                self.h.seek(blob_base + lod_offset)
                offsets = [
                    self.h.read_uint32()
                    for _ in range(self.h.read_uint32())
                ]
                lod_chunk_offsets.append(offsets)
                lod_region_ends.append(None)
                if not self.profile.meshlet_chunk_payload_is_fully_decoded:
                    compatibility_chunk_offsets.extend(offsets)

        compatibility_boundaries = sorted(
            {off for off in compatibility_chunk_offsets if off}
            | {off for off in lod_offsets if off}
            | {blob_size}
        )
        for lod_index, chunk_offsets in enumerate(lod_chunk_offsets):
            if chunk_offsets is None:
                continue
            lod = MeshLOD(mesh_buffer)
            lod.lod_level = lod_index
            groups_by_part_id: Dict[int, MeshGroup] = {}
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
                if chunk_index + 1 < len(chunk_offsets):
                    end: Optional[int] = blob_base + chunk_offsets[chunk_index + 1]
                elif lod_region_ends[lod_index] is not None:
                    # Wilds/RE9 serialize the final boundary explicitly.
                    end = lod_region_ends[lod_index]
                elif self.profile.meshlet_chunk_payload_is_fully_decoded:
                    end = None
                else:
                    end = blob_base + compatibility_boundaries[
                        bisect_right(compatibility_boundaries, chunk_offset)
                    ]
                chunk = self._parse_chunk(start, end)
                chunk.lod_index = lod_index
                chunk.no_ray_tracing = cluster_header.no_ray_tracing
                chunk.alpha_test = cluster_header.alpha_test
                chunk.transparent = cluster_header.transparent
                chunk.meshlet_material_flag_extension = (
                    cluster_header.meshlet_material_flag_extension
                )
                mesh_buffer.mply_chunks.append(chunk)
                if chunk.flags is not None:
                    mesh_buffer.mply_chunk_flags.append(chunk.flags)
                if chunk.vert_count != cluster_header.vertex_count:
                    raise ValueError(
                        f"MPLY LOD {lod_index} chunk {chunk_index}: "
                        f"{chunk.vert_count}/{cluster_header.vertex_count} vertices"
                    )
                if chunk.material_id != cluster_header.material_id:
                    raise ValueError(
                        f"MPLY LOD {lod_index} chunk {chunk_index}: "
                        f"material IDs {chunk.material_id}/{cluster_header.material_id}"
                    )
                if chunk.part_id != cluster_header.part_id:
                    raise ValueError(
                        f"MPLY LOD {lod_index} chunk {chunk_index}: "
                        f"part IDs {chunk.part_id}/{cluster_header.part_id}"
                    )
                streaming_faces = self._read_streaming_faces(cluster_header)
                if streaming_faces is not None:
                    exclude = (self.profile.meshlet_streaming_indices_exclude_no_ray_tracing
                               and chunk.no_ray_tracing)
                    expected = np.zeros(len(chunk.faces), np.int64) if exclude else chunk.faces
                    if not np.array_equal(streaming_faces, expected):
                        raise ValueError(
                            f"MPLY LOD {lod_index} chunk {chunk_index}: "
                            "companion topology disagrees with raster/no-RT policy"
                        )
                planes["positions"].append(chunk.positions)
                planes["normals"].append(chunk.normals)
                planes["normal_ws"].append(chunk.normal_ws)
                if len(chunk.tangents):
                    if not tangents_seen and vertex_base:
                        planes["tangents"].append(np.zeros((vertex_base, 3)))
                    tangents_seen = True
                    planes["tangents"].append(chunk.tangents)
                elif tangents_seen:
                    planes["tangents"].append(np.zeros((chunk.vert_count, 3)))
                planes["tangent_ws"].append(chunk.tangent_ws)
                planes["uv0"].append(chunk.uv0)
                if len(chunk.uv1):
                    planes["uv1"].append(chunk.uv1)
                else:
                    uv1_complete = False
                if len(chunk.uv2):
                    planes["uv2"].append(chunk.uv2)
                else:
                    uv2_complete = False
                planes["colors"].append(
                    chunk.colors if len(chunk.colors) else np.full(4 * chunk.vert_count, 255, np.int64)
                )
                if chunk.skin_record_bytes:
                    all_skin_record_bytes.extend(
                        chunk.skin_record_bytes * (chunk.vert_count if chunk.skin_is_shared else 1))
                else:
                    skin_complete = False
                face_offset = face_total
                planes["faces"].append(chunk.faces)
                face_total += len(chunk.faces)
                group = groups_by_part_id.get(cluster_header.part_id)
                if group is None:
                    group = MeshGroup(mesh_buffer)
                    group.group_id = cluster_header.part_id
                    groups_by_part_id[cluster_header.part_id] = group
                sm = Submesh(mesh_buffer)
                sm.material_index = cluster_header.material_id
                sm.part_id = cluster_header.part_id
                sm.indices_count = len(chunk.faces)
                sm.faces_index_offset = face_offset
                sm.verts_index_offset = vertex_base
                sm.vert_count = chunk.vert_count
                sm.file_offset = start
                sm.no_ray_tracing = cluster_header.no_ray_tracing
                sm.alpha_test = cluster_header.alpha_test
                sm.transparent = cluster_header.transparent
                group.submeshes.append(sm)
                group.vertex_count += chunk.vert_count
                group.face_count += len(chunk.faces)
                group.cluster_count += 1
                vertex_base += chunk.vert_count
            lod.mesh_groups.extend(groups_by_part_id.values())
            lod.part_count = len(lod.mesh_groups)
            lods.append(lod)

        mply_skin_weights = None
        if skin_complete and all_skin_record_bytes:
            mply_skin_weights = _decode_skin_weights(
                memoryview(all_skin_record_bytes),
                vertex_base,
                self.profile,
            )

        def joined(name: str, typecode: str) -> array:
            values = array(typecode)
            if planes[name]:
                values.frombytes(np.concatenate([plane.ravel() for plane in planes[name]]).astype(
                    _ARRAY_DTYPES[typecode]).tobytes())
            return values

        mesh_buffer.has_32bit_indices = True
        mesh_buffer.buffer_payloads[0] = MeshBufferPayload(
            vertex_count=vertex_base,
            positions=joined("positions", "f"),
            normals=joined("normals", "f"),
            normal_ws=joined("normal_ws", "B"),
            tangents=joined("tangents", "f"),
            tangent_ws=joined("tangent_ws", "B"),
            uv0=joined("uv0", "d"),
            uv1=joined("uv1", "d") if uv1_complete else array("d"),
            uv2=joined("uv2", "d") if uv2_complete else array("d"),
            colors=joined("colors", "B"),
            faces=array("H"),
            integer_faces=joined("faces", "I"),
            skin_weights=mply_skin_weights,
        )
        mesh_data = MeshData(mesh_buffer)
        mesh_data.lods = lods
        material_ids = {
            int(cluster.material_id)
            for cluster in mesh_buffer.mply_cluster_headers
        }
        mesh_data.material_count = max(material_ids) + 1 if material_ids else 0
        return mesh_buffer, [mesh_data]


def joint_name(mesh, joint):
    """Also used as the Blender bone name, fallback included."""
    name_index = mesh.bone_indices[joint] if joint < len(mesh.bone_indices) else -1
    name = mesh.names[name_index] if 0 <= name_index < len(mesh.names) else ""
    return name or f"joint_{joint}"


@dataclass
class JointNode:
    index: int
    parent_index: int
    sibling_index: int
    child_index: int
    symmetry_index: int
    segment_scale: int
    segment_scale_reserved_bits: int = 0
    reserve: Tuple[int, int] = (0, 0)


    @property
    def reserved(self) -> Tuple[int, int, int]:
        return (self.segment_scale_reserved_bits, *self.reserve)

class MeshFile:
    def __init__(self) -> None:
        self._reset_state()

    def _reset_state(
        self,
        load_mode: MeshLoadMode = MeshLoadMode.FULL,
    ) -> None:
        self.load_mode = load_mode
        self.header = Header()
        self.streaming_info: Optional[MeshStreamingInfo] = None
        self.mesh_buffer: Optional[MeshBuffer] = None
        self.meshes: List[MeshData] = []
        self.shadow_mesh: Optional[ShadowMeshData] = None
        self.occluder_mesh: Optional[OccluderMeshData] = None
        self.group_pivots: Optional[GroupPivotData] = None
        self.bounding_boxes: Optional[BoundingBoxData] = None
        self.sdf_path: str = ""
        self.material_names: List[str] = []
        self.names: List[str] = []
        self.name_offsets: List[int] = []
        self.material_indices: List[int] = []
        self.parts_name_hashes: List[int] = []
        self.parts_name_hash_ids: List[int] = []
        self.parts_name_hashes_implicit = False
        self.bone_indices: List[int] = []
        self.bone_remap_indices: List[int] = []
        self.bones: List["JointNode"] = []
        self.local_matrices: List[List[float]] = []
        self.world_matrices: List[List[float]] = []
        self.inverse_bind_matrices: List[List[float]] = []
        self.joint_count: int = 0
        self.bone_remap_count: int = 0
        self.skeleton_reserved: Tuple[int, int] = (0, 0)
        self.skeleton_hierarchy_offset = 0
        self.skeleton_local_matrix_offset = 0
        self.skeleton_world_matrix_offset = 0
        self.skeleton_inverse_bind_matrix_offset = 0
        self.bone_remap_padding: bytes = b""
        self.blend_shape_indices: List[int] = []
        self.blend_shape_data: Optional["BlendShapeData"] = None
        self.normal_recalc_data: Optional["NormalRecalcData"] = None
        self.streaming_data_loaded = False
        self.source_size = 0
        self.sections: Dict[str, MeshSectionRecord] = {}
        self.semantic_notes: List[MeshSemanticNote] = []

    @property
    def profile(self) -> Optional[MeshGameProfile]:
        return self.header.profile

    @property
    def is_full(self) -> bool:
        return self.load_mode is MeshLoadMode.FULL

    def _canonical_contract_check(self, kind: str):
        contract = getattr(self.profile, "canonical_import_contract", None)
        entry = _CANONICAL_CONTRACTS.get(contract)
        if entry is None:
            return None
        module, check = entry
        return getattr(import_module(f".{module}", __package__), check.format(kind))

    def assert_runtime_contract(self) -> None:
        check = self._canonical_contract_check("runtime_contract")
        if check is not None:
            check(self)

    def assert_import_safe(self) -> None:
        if self.profile is None or not any(
            internal == self.header.version
            for internal, _file in self.profile.canonical_version_pairs
        ):
            raise ValueError("mesh version has no verified canonical authoring contract")
        check = self._canonical_contract_check("import_safe")
        if check is None:
            raise ValueError(
                "unknown canonical mesh authoring contract: "
                f"{self.profile.canonical_import_contract!r}"
            )
        check(self)

    @property
    def gpu_index_buffer_size(self) -> int:
        buffer = self.mesh_buffer
        if buffer is None:
            return 0
        if buffer.index_buffer_size > 0:
            return int(buffer.index_buffer_size)
        if not self.is_full and (
            self.header.shadow_lods_offset or self.header.occluder_mesh_offset
        ):
            return 0

        stride = 4 if buffer.has_32bit_indices else 2
        main_end = max(
            (
                (
                    int(submesh.faces_index_offset)
                    + int(submesh.allocated_index_count)
                )
                * stride
                for mesh_data in self.meshes
                for lod in mesh_data.lods
                for group in lod.mesh_groups
                for submesh in group.submeshes
            ),
            default=0,
        )
        shadow_end = (
            int(buffer.shadow_mesh_index_buffer_offset)
            + int(getattr(self.shadow_mesh, "index_count", 0) or 0) * stride
        )
        occluder_end = (
            int(buffer.occluder_mesh_index_buffer_offset)
            + int(getattr(self.occluder_mesh, "index_count", 0) or 0) * stride
        )
        # D3D11 buffer widths are 4-byte aligned (odd 16-bit index counts).
        maximum = max(main_end, shadow_end, occluder_end)
        return (maximum + 3) & ~3

    @property
    def streaming_buffer_count(self) -> int:
        return len(self.streaming_info.entries) if self.streaming_info else 0


    def note(self, section: str, field: str, message: str) -> None:
        note = MeshSemanticNote(section, field, message)
        if note not in self.semantic_notes:
            self.semantic_notes.append(note)

    def _section_size(self, start: int, sorted_offsets: List[int]) -> int:
        for offset in sorted_offsets:
            if offset > start:
                return offset - start
        return 0

    def _group_pivot_section_offset(self) -> int:
        header = self.header
        if (
            header.magic == MPLY_MAGIC
            and header.profile is not None
            and header.profile.meshlet_has_wilds_layout
        ):
            return 0
        return header.floats_offset

    def _section_offsets(self) -> Dict[str, int]:
        header = self.header
        return {
            "main_mesh": header.lods_offset,
            "shadow_mesh": header.shadow_lods_offset,
            "occluder_mesh": header.occluder_mesh_offset,
            "skeleton": header.bones_offset,
            "normal_recalc": header.normal_recalc_offset,
            "blend_shapes": header.blend_shapes_offset,
            "bounding_boxes": header.bounds_offset,
            "mesh_buffer": header.mesh_offset,
            "meshlet_layout": header.meshlet_layout_offset,
            "meshlet_bvh": header.meshlet_bvh_offset,
            "meshlet_parts": header.meshlet_parts_offset,
            "group_pivots": self._group_pivot_section_offset(),
            "material_name_indices": header.material_indices_offset,
            "bone_name_indices": header.bone_indices_offset,
            "blend_shape_name_indices": header.blend_shape_indices_offset,
            "string_table": header.name_offsets_offset,
            "streaming_info": header.streaming_info_offset,
            "blend_shape_bone_modifiers": header.blend_shape_bone_modifier_offset,
            "parts_name_hashes": header.parts_name_hash_offset,
            "sdf_path": header.sdf_path_offset,
        }

    def _collect_section_offsets(self, file_size: Optional[int] = None) -> List[int]:
        limit = file_size if file_size is not None else self.header.file_size
        return sorted({offset for offset in self._section_offsets().values() if 0 < offset < limit}) + [limit]

    def _capture_sections(self, data: bytes, sorted_offsets: List[int]) -> None:
        self.sections = {}
        for name, offset in self._section_offsets().items():
            if offset <= 0 or offset >= len(data):
                continue
            size = self._section_size(offset, sorted_offsets)
            if size <= 0:
                continue
            self.sections[name] = MeshSectionRecord(
                name,
                offset,
                size,
                _checked_slice(data, offset, size, name),
            )

    def _read_joint_nodes(self, h: BinaryHandler, offset: int, count: int) -> List["JointNode"]:
        with h.seek_temp(offset):
            nodes: List[JointNode] = []
            for _ in range(count):
                indices = tuple(h.read_int16() for _ in range(5))
                segment_scale_word = h.read_uint16()
                node = JointNode(
                    index=indices[0],
                    parent_index=indices[1],
                    sibling_index=indices[2],
                    child_index=indices[3],
                    symmetry_index=indices[4],
                    segment_scale=segment_scale_word & 0x1,
                    segment_scale_reserved_bits=segment_scale_word >> 1,
                    reserve=(h.read_int16(), h.read_int16()),
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
            self.skeleton_reserved = (h.read_int32(), h.read_int32())
            self.skeleton_hierarchy_offset = h.read_int64()
            self.skeleton_local_matrix_offset = h.read_int64()
            self.skeleton_world_matrix_offset = h.read_int64()
            self.skeleton_inverse_bind_matrix_offset = h.read_int64()

            if self.joint_count < 0 or self.bone_remap_count < 0:
                raise ValueError("Skeleton joint and remap counts must be nonnegative")
            _checked_view(h.data, h.tell, self.bone_remap_count * 2, "Skeleton remap")
            for label, offset, stride in (
                ("hierarchy", self.skeleton_hierarchy_offset, 16),
                ("local matrices", self.skeleton_local_matrix_offset, 64),
                ("world matrices", self.skeleton_world_matrix_offset, 64),
                ("inverse bind matrices", self.skeleton_inverse_bind_matrix_offset, 64),
            ):
                if not self.joint_count:
                    continue
                if offset <= 0:
                    raise ValueError(f"Skeleton {label} has no data pointer")
                _checked_view(h.data, offset, self.joint_count * stride, f"Skeleton {label}")

            if self.bone_remap_count > 0:
                self.bone_remap_indices = [h.read_int16() for _ in range(self.bone_remap_count)]
            if self.skeleton_hierarchy_offset:
                padding_start = h.tell
                padding_end = self.skeleton_hierarchy_offset
                if padding_end < padding_start or padding_end > len(h.data):
                    raise ValueError(
                        "Skeleton hierarchy pointer precedes its remap array or "
                        "exceeds the file"
                    )
                self.bone_remap_padding = bytes(
                    h.data[padding_start:padding_end]
                )

            self.bones = self._read_joint_nodes(
                h, self.skeleton_hierarchy_offset, self.joint_count
            )
            self.local_matrices = self._read_matrices(
                h, self.skeleton_local_matrix_offset, self.joint_count
            )
            self.world_matrices = self._read_matrices(
                h, self.skeleton_world_matrix_offset, self.joint_count
            )
            self.inverse_bind_matrices = self._read_matrices(
                h, self.skeleton_inverse_bind_matrix_offset, self.joint_count
            )

    def _parse_group_pivots(self, h: BinaryHandler) -> None:
        if self.header.magic == MPLY_MAGIC and self.group_pivots is not None:
            return
        self.group_pivots = None
        if not self.header.floats_offset:
            return
        with h.seek_temp(self.header.floats_offset):
            result = GroupPivotData(
                buffer_size=h.read_int32(),
                reserve=h.read_int32(),
                buffer_offset=h.read_int64(),
            )
        if result.buffer_size < 0:
            raise ValueError("Group-pivot buffer has a negative size")
        if result.reserve:
            raise ValueError("GroupPivotLayout.reserve must be zero")
        if bool(result.buffer_size) != bool(result.buffer_offset):
            raise ValueError(
                "Group-pivot byte size and data-pointer presence disagree"
            )
        if result.buffer_size % 12:
            raise ValueError(
                f"{self.header.profile.display_name} group-pivot byte size "
                f"{result.buffer_size} is not a whole Float3 array"
            )
        if result.buffer_size:
            raw = _checked_slice(
                h.data,
                result.buffer_offset,
                result.buffer_size,
                "Group pivots",
            )
            result.raw = raw
            result.pivots = [
                tuple(values)
                for values in struct.iter_unpack("<3f", raw)
            ]
        self.group_pivots = result

    def _parse_bounding_boxes(self, h: BinaryHandler) -> None:
        self.bounding_boxes = None
        if not self.header.bounds_offset:
            return
        with h.seek_temp(self.header.bounds_offset):
            result = BoundingBoxData(
                count=h.read_int32(),
                reserve=h.read_int32(),
                boxes_offset=h.read_int64(),
            )
        if result.count < 0:
            raise ValueError("Bounding-box layout has a negative count")
        if result.count and result.boxes_offset <= 0:
            raise ValueError("Bounding-box layout has no box pointer")
        if result.count:
            raw = _checked_slice(
                h.data,
                result.boxes_offset,
                result.count * 32,
                "Bounding boxes",
            )
            for values in struct.iter_unpack("<8f", raw):
                result.boxes.append(
                    MeshAABB(
                        tuple(values[:3]),
                        tuple(values[4:7]),
                        values[3],
                        values[7],
                    )
                )
        self.bounding_boxes = result

    def _parse_sdf_path(self, h: BinaryHandler) -> None:
        self.sdf_path = ""
        if not self.header.sdf_path_offset:
            return
        with h.seek_temp(self.header.sdf_path_offset):
            if self.header.profile.header_layout in {
                MeshHeaderLayout.DD2,
                MeshHeaderLayout.WILDS,
            }:
                self.sdf_path = h.read_wstring()
            else:
                value_offset = h.read_int64()
                if value_offset > 0:
                    with h.seek_temp(value_offset):
                        self.sdf_path = h.read_wstring()

    @staticmethod
    def _index_bytes(
        data: bytes | bytearray,
        start: int,
        count: int,
        use_32bit: bool,
        label: str,
    ) -> bytes:
        if count <= 0:
            return b""
        return _checked_slice(data, start, count * (4 if use_32bit else 2), label)

    def _decode_auxiliary_geometry(self, data: bytes) -> None:
        if self.mesh_buffer is None:
            return
        mesh_buffer = self.mesh_buffer
        use_32bit = mesh_buffer.has_32bit_indices

        def group_signature(group: MeshGroup):
            return (
                group.group_id,
                group.vertex_count,
                group.face_count,
                tuple(
                    (
                        item.material_index,
                        item.is_quad,
                        item.buffer_index,
                        item.indices_count,
                        item.faces_index_offset,
                        item.verts_index_offset,
                    )
                    for item in group.submeshes
                ),
            )

        main_lods = self.meshes[0].lods if self.meshes else []
        main_lods_by_offset = {
            int(offset): lod
            for mesh_data in self.meshes
            for offset, lod in zip(mesh_data.lod_offsets, mesh_data.lods)
            if int(offset) > 0
        }

        def shares_main_topology(
            layer: ShadowMeshData | OccluderMeshData,
        ) -> bool:
            offsets = (layer.lod_offsets if isinstance(layer, ShadowMeshData)
                       else (self.header.occluder_mesh_offset,))
            if offsets and all(int(offset) in main_lods_by_offset for offset in offsets):
                return True
            if mesh_buffer.profile.auxiliary_topology_sharing_uses_lod_pointer_identity:
                return False
            if isinstance(layer, ShadowMeshData):
                if len(layer.lods) > len(main_lods):
                    return False
                return all(
                    tuple(map(group_signature, shadow_lod.mesh_groups))
                    == tuple(map(group_signature, main_lods[index].mesh_groups))
                    for index, shadow_lod in enumerate(layer.lods)
                )
            if not main_lods:
                return False
            return tuple(map(group_signature, layer.mesh_groups)) == tuple(
                map(group_signature, main_lods[0].mesh_groups)
            )

        def domain_end_aliases_main_payload(
            layer: ShadowMeshData | OccluderMeshData,
            index_offset_name: str,
            used_buffers: set[int],
        ) -> bool:
            if not (
                mesh_buffer.profile
                .auxiliary_index_domain_end_marks_main_payload_alias
            ):
                return False
            if not used_buffers or not layer.index_counts_by_buffer:
                return False
            for buffer_index in used_buffers:
                layout = layouts.get(buffer_index)
                main_payload = mesh_buffer.buffer_payloads.get(buffer_index)
                if layout is None or main_payload is None:
                    return False
                header = layout[4]
                complete_index_size = (
                    int(mesh_buffer.index_buffer_size)
                    if buffer_index == 0
                    else int(header.unpadded_buffer_size)
                    - int(header.vertex_buffer_length)
                )
                if int(getattr(header, index_offset_name)) != complete_index_size:
                    return False
                index_values = main_payload.index_values
                if int(layer.index_counts_by_buffer.get(buffer_index, 0)) > len(
                    index_values
                ):
                    return False
                vertex_start = int(
                    layer.vertex_bases_by_buffer.get(buffer_index, 0)
                )
                vertex_end = vertex_start + int(
                    layer.vertex_counts_by_buffer.get(buffer_index, 0)
                )
                if not 0 <= vertex_start <= vertex_end <= main_payload.vertex_count:
                    return False
            return True

        layers = tuple(
            (layer, label, offset_name)
            for layer, label, offset_name in (
                (
                    self.shadow_mesh,
                    "Shadow",
                    "shadow_mesh_index_buffer_offset",
                ),
                (
                    self.occluder_mesh,
                    "Occluder",
                    "occluder_mesh_index_buffer_offset",
                ),
            )
            if layer is not None
        )
        all_buffer_indices = {
            buffer_index
            for layer, _label, _offset_name in layers
            for buffer_index in (
                set(layer.vertex_counts_by_buffer)
                | set(layer.index_counts_by_buffer)
            )
        }
        mesh_buffer.ensure_streaming_payloads(
            buffer_index for buffer_index in all_buffer_indices if buffer_index != 0
        )

        def buffer_layout(buffer_index: int):
            if buffer_index == 0:
                return (
                    data,
                    mesh_buffer.vertex_buffer_offset,
                    mesh_buffer.vertex_buffer_size,
                    mesh_buffer.extra_buffer_headers,
                    mesh_buffer,
                )
            header = mesh_buffer.streaming_buffer_headers.get(buffer_index)
            payload = mesh_buffer.buffer_payloads.get(buffer_index)
            stream_data = mesh_buffer._streaming_data
            if header is None or payload is None or stream_data is None:
                return None
            return (
                stream_data,
                payload.source_offset,
                header.vertex_buffer_length,
                header.extra_vertex_elements,
                header,
            )

        layouts = {
            buffer_index: buffer_layout(buffer_index)
            for buffer_index in all_buffer_indices
        }

        extra_assignments: Dict[
            tuple[int, object],
            Sequence[MeshBufferItemHeader],
        ] = {}
        for buffer_index, layout in layouts.items():
            if layout is None or not layout[3]:
                continue
            candidates = {
                label: layer
                for label, layer in (
                    ("shadow", self.shadow_mesh),
                    ("occluder", self.occluder_mesh),
                )
                if layer is not None
                and layer.vertex_counts_by_buffer.get(buffer_index, 0) > 0
            }
            extra_layouts = split_auxiliary_layouts(layout[3])
            order = tuple(mesh_buffer.profile.auxiliary_vertex_layout_order)
            if len(extra_layouts) == 1 and len(candidates) == 1:
                layer = next(iter(candidates.values()))
                extra_assignments[(buffer_index, layer)] = extra_layouts[0]
                continue
            ordered_candidates = [
                candidates[label]
                for label in order
                if label in candidates
            ]
            if (
                order
                and len(extra_layouts) == len(ordered_candidates)
                and len(ordered_candidates) == len(candidates)
            ):
                for layer, headers in zip(ordered_candidates, extra_layouts):
                    extra_assignments[(buffer_index, layer)] = headers
                continue
            if extra_layouts:
                raise ValueError(
                    f"{mesh_buffer.profile.display_name} buffer {buffer_index} "
                    f"has {len(extra_layouts)} auxiliary vertex layout(s) for "
                    f"{len(candidates)} geometry layer(s), but no canonical "
                    "one-to-one ownership"
                )

        for layer, label, index_offset_name in layers:
            used_buffers = (
                set(layer.vertex_counts_by_buffer)
                | set(layer.index_counts_by_buffer)
            )
            owns_auxiliary_vertices = any(
                (buffer_index, layer) in extra_assignments
                for buffer_index in used_buffers
            )
            aliases_main_payload = shares_main_topology(layer) or (
                domain_end_aliases_main_payload(
                    layer,
                    index_offset_name,
                    used_buffers,
                )
            )
            if not owns_auxiliary_vertices and aliases_main_payload:
                if (
                    isinstance(layer, ShadowMeshData)
                    and layer.lod_offsets
                    and all(int(offset) in main_lods_by_offset for offset in layer.lod_offsets)
                ):
                    layer.lods = [
                        main_lods_by_offset[int(offset)]
                        for offset in layer.lod_offsets
                    ]
                layer.vertex_bases_by_buffer = dict.fromkeys(used_buffers, 0)
                layer.payloads = {
                    buffer_index: mesh_buffer.buffer_payloads[buffer_index]
                    for buffer_index in used_buffers
                    if buffer_index in mesh_buffer.buffer_payloads
                }
                layer.shares_main_payload = (
                    bool(used_buffers) and used_buffers <= layer.payloads.keys()
                )
                continue

            layer.payloads = {}
            for buffer_index in sorted(used_buffers):
                layout = layouts.get(buffer_index)
                if layout is None:
                    raise ValueError(
                        f"{label} geometry references unavailable buffer "
                        f"{buffer_index}"
                    )
                (
                    source_data,
                    vertex_base,
                    vertex_region_size,
                    _extra_headers,
                    header,
                ) = layout
                index_count = layer.index_counts_by_buffer.get(buffer_index, 0)
                index_relative_offset = int(getattr(header, index_offset_name))
                face_bytes = b""
                if index_count:
                    if (
                        mesh_buffer.profile.has_auxiliary_index_offsets
                        and index_relative_offset == 0
                    ):
                        raise ValueError(
                            f"{label} buffer {buffer_index} has indices but no "
                            "canonical auxiliary index offset"
                        )
                    else:
                        index_start = (
                            vertex_base + index_relative_offset
                            if mesh_buffer.profile.has_auxiliary_index_offsets
                            else mesh_buffer.face_buffer_offset
                            + index_relative_offset
                        )
                        face_bytes = self._index_bytes(
                            source_data,
                            index_start,
                            index_count,
                            use_32bit,
                            f"{label} buffer {buffer_index} indices",
                        )

                assigned_headers = extra_assignments.get(
                    (buffer_index, layer),
                    (),
                )
                if assigned_headers:
                    payload = MeshBufferPayload(
                        source_offset=vertex_base,
                        face_bytes=face_bytes,
                        is_auxiliary_geometry=True,
                        auxiliary_vertex_planes_may_omit_zero_suffix=(
                            mesh_buffer.profile
                            .auxiliary_vertex_planes_may_omit_zero_suffix
                        ),
                    )
                    payload.vertex_bytes = _checked_slice(
                        source_data,
                        vertex_base,
                        vertex_region_size,
                        f"{label} buffer {buffer_index} vertex region",
                    )
                    payload.buffer_headers = list(assigned_headers)
                    mesh_buffer._decode_payload(
                        payload,
                        vertex_count=layer.vertex_counts_by_buffer.get(
                            buffer_index,
                            0,
                        ),
                        has_32bit_indices=use_32bit,
                        skin_weight_influence_count=(
                            int(layer.skin_weight_influence_count)
                            if isinstance(layer, ShadowMeshData)
                            and mesh_buffer.profile
                            .skin_weight_count_selects_influence_layout
                            else None
                        ),
                    )
                else:
                    main_payload = mesh_buffer.buffer_payloads.get(buffer_index)
                    if main_payload is None:
                        raise ValueError(
                            f"{label} geometry references missing main buffer "
                            f"{buffer_index}"
                        )
                    vertex_start = layer.vertex_bases_by_buffer.get(
                        buffer_index,
                        0,
                    )
                    vertex_end = vertex_start + layer.vertex_counts_by_buffer.get(
                        buffer_index,
                        0,
                    )
                    if not 0 <= vertex_start <= vertex_end <= main_payload.vertex_count:
                        raise ValueError(
                            f"{label} geometry vertex domain "
                            f"[{vertex_start}, {vertex_end}) leaves main buffer "
                            f"{buffer_index} with {main_payload.vertex_count} vertices"
                        )
                    layer.vertex_bases_by_buffer[buffer_index] = 0
                    payload = copy(main_payload)
                    payload.face_bytes = face_bytes
                    _decode_faces(payload, use_32bit)
                layer.payloads[buffer_index] = payload

    def _note_structural_semantics(self) -> None:
        if self.header.decoded_content_flags is not None:
            if self.header.decoded_content_flags.reserved_bits:
                self.note(
                    "header",
                    "content_flags.reserved",
                    "The reserved content-flag bit is set.",
                )

        if self.meshes:
            if self.header.profile.legacy_reserved_storage_is_corpus_zero:
                if any(
                    mesh.shared_lod_bits or mesh.ukn1
                    for mesh in self.meshes
                ):
                    self.note(
                        "main_mesh",
                        "reserved_storage",
                        f"A {self.header.profile.game} corpus-zero MeshData "
                        "slot is nonzero.",
                    )
                if any(
                    lod.vertex_format
                    for mesh in self.meshes
                    for lod in mesh.lods
                ):
                    self.note(
                        "lod",
                        "reserved_vertex_format",
                        f"A {self.header.profile.game} corpus-zero LOD "
                        "vertex-format byte is nonzero.",
                    )
            else:
                self.note(
                    "main_mesh",
                    "shared_lod_bits",
                    "sharedLodBits is preserved as a bit mask; individual bit "
                    "assignments are not established by MESH.bt.",
                )
                self.note(
                    "lod",
                    "vertex_format",
                    "vertexFormat is preserved for every LOD; its bit-level "
                    "contract is not established by MESH.bt.",
                )

        mesh_lods = [
            lod
            for mesh_data in self.meshes
            for lod in mesh_data.lods
        ]
        if self.shadow_mesh is not None:
            mesh_lods.extend(self.shadow_mesh.lods)
        groups = [
            group
            for lod in mesh_lods
            for group in lod.mesh_groups
        ]
        if self.occluder_mesh is not None:
            groups.extend(self.occluder_mesh.mesh_groups)
        if any(lod.reserve for lod in mesh_lods):
            self.note("lod", "reserve", "At least one LOD reserve field is nonzero.")
        if any(any(group.reserve) for group in groups):
            self.note(
                "mesh_group",
                "reserve",
                "At least one mesh-group reserve block is nonzero.",
            )
        submeshes = [submesh for group in groups for submesh in group.submeshes]
        if self.header.profile.submesh_reserve03_is_runtime_ignored:
            if any(
                submesh.runtime_ignored_reserve03
                for submesh in submeshes
            ):
                self.note(
                    "submesh",
                    "runtime_ignored_reserve03",
                    "MeshCluster byte +3 is native reserve[1]. The source "
                    "contains a preserved nonzero value, but reconstructed draw "
                    "and normal consumers skip it; canonical new construction "
                    "must leave it zero.",
                )
        elif any(submesh.reserve for submesh in submeshes):
            self.note(
                "submesh",
                "reserve",
                "At least one submesh reserve field is nonzero.",
            )
        if any(submesh.streaming_offset2 for submesh in submeshes):
            self.note(
                "submesh",
                "streaming_platform_specific_offset",
                "The platform-specific streaming byte offset is preserved but "
                "not assigned a platform-independent interpretation.",
            )

        weight_streams = tuple(
            stream
            for payload in (
                self.mesh_buffer.buffer_payloads.values()
                if self.mesh_buffer is not None
                else ()
            )
            for stream in (getattr(payload, name) for name in SKIN_WEIGHT_STREAMS)
            if stream is not None
        )
        if (
            not self.header.profile.skin_weight_uses_implicit_last_weight
            and any(stream.trailing_weight_bytes for stream in weight_streams)
        ):
            self.note(
                "mesh_buffer",
                "packed6_trailing_weight_bytes",
                "Packed-six skin records contain producer lanes beyond the five "
                "weights consumed by this profile's runtime; they are preserved "
                "but are not applied as additional influences.",
            )

        if any(
            payload.secondary_skin_weights is not None
            for payload in (
                self.mesh_buffer.buffer_payloads.values()
                if self.mesh_buffer is not None
                else ()
            )
        ):
            self.note(
                "mesh_buffer",
                "shape_key_weight_application",
                "The separately pointed-to secondary records are decoded as the "
                "template's shape-key weight set. Their in-engine switching or "
                "blending rule is not established, so they are visualized "
                "independently and are not summed into primary skeletal skinning.",
            )

        if any(
            payload.additional_vertex_weights is not None
            for payload in (
                self.mesh_buffer.buffer_payloads.values()
                if self.mesh_buffer is not None
                else ()
            )
        ):
            self.note(
                "mesh_buffer",
                "additional_vertex_weights",
                "The native additionalVertexWeights records form a complete "
                "alternative skin stream. Captured DD2 character-edit dispatches "
                "copy this stream into the dynamic skinned-geometry input instead "
                "of concatenating it with the universal-buffer weights.",
            )

        if self.shadow_mesh is not None:
            if self.shadow_mesh.reserve or self.shadow_mesh.dmc5_pad:
                self.note(
                    "shadow",
                    "reserved_header",
                    "The shadow-layout reserve word or legacy reserved qword "
                    "is nonzero.",
                )
            if any(self.shadow_mesh.reserved_slots):
                self.note(
                    "shadow",
                    "reserved_slots",
                    "At least one shadow-layout reserved pointer slot is nonzero.",
                )
        if (
            self.occluder_mesh is not None
            and self.header.profile.legacy_reserved_storage_is_corpus_zero
            and (
                self.occluder_mesh.vertex_format
                or self.occluder_mesh.lod_level
                or self.occluder_mesh.reserve
            )
        ):
            self.note(
                "occluder",
                "reserved_header",
                f"A {self.header.profile.game} corpus-zero occluder "
                "format/LOD/reserve byte is nonzero.",
            )
        if self.group_pivots is not None and self.group_pivots.reserve:
            self.note(
                "group_pivots",
                "reserve",
                "GroupPivotLayout.reserve is nonzero.",
            )
        if self.bounding_boxes is not None and self.bounding_boxes.reserve:
            self.note(
                "bounding_boxes",
                "reserve",
                "BoundingBoxLayout.reserve is nonzero.",
            )
        if any(self.skeleton_reserved):
            self.note(
                "skeleton",
                "reserve",
                "SkeletonLayout contains nonzero reserve values.",
            )
        if any(self.bone_remap_padding):
            self.note(
                "skeleton",
                "bone_remap_padding",
                "Skeleton bone-remap alignment padding is nonzero.",
            )
        if any(any(node.reserved) for node in self.bones):
            self.note(
                "skeleton",
                "joint_reserve",
                "At least one JointNode reserve block is nonzero.",
            )
        if self.normal_recalc_data is not None and any(
            self.normal_recalc_data.reserved
        ):
            self.note(
                "normal_recalc",
                "reserve",
                "NormalRecalcLayout contains nonzero reserve values.",
            )

    def _release_lightweight_raw_buffers(self) -> None:
        if self.is_full or self.mesh_buffer is None:
            return
        for payload in self.mesh_buffer.buffer_payloads.values():
            payload.vertex_bytes = b""
            payload.face_bytes = b""
            payload.skin_weight_continuation_bytes = b""
            payload.secondary_skin_weight_bytes = b""
            payload.additional_vertex_weight_bytes = b""
            payload.attribute_streams.clear()
            payload.unknown_attributes.clear()

    def _decode_separate_skin_weights(self, data: bytes) -> None:
        mesh_buffer = self.mesh_buffer
        if mesh_buffer is None:
            return
        additional = mesh_buffer.profile.supports_additional_vertex_weights
        if not (additional or mesh_buffer.profile.supports_secondary_skin_weights):
            return
        resident_count = mesh_buffer.vertex_counts_by_buffer.get(0, 0)
        # Both kinds sit at the stream header's shape-key weight pointer.
        resident_offset = mesh_buffer.shapekey_weight_buffer_offset
        explicit_size = (
            mesh_buffer.additional_vertex_weights_size
            if additional
            else mesh_buffer.shapekey_weight_buffer_size
        )
        if bool(resident_offset) != bool(explicit_size):
            raise ValueError(
                "Separate skin-weight offset/size presence must agree"
            )
        if resident_offset > 0:
            required = resident_count * 16
            if explicit_size != required:
                raise ValueError(
                    "Separate skin-weight byte size does not match the "
                    f"topology-owned vertex count ({explicit_size} != {required})"
                )
            raw = _checked_slice(
                data,
                resident_offset,
                explicit_size,
                "Resident additional vertex weights" if additional else "Resident secondary skin weights",
            )
            decoded = _decode_skin_weights(memoryview(raw), resident_count, mesh_buffer.profile)
            payload = mesh_buffer.buffer_payloads[0]
            if additional:
                payload.additional_vertex_weight_bytes = raw
                payload.additional_vertex_weights = decoded
            else:
                payload.secondary_skin_weight_bytes = raw
                payload.secondary_skin_weights = decoded

    def _parse_bone_indices(self, data: bytes, sorted_offsets: List[int]):
        self.bone_indices = []
        if not self.header.bone_indices_offset or self.joint_count <= 0:
            return
        size = self.joint_count * 2
        allocated_size = (size + 15) & ~15
        section_size = self._section_size(
            self.header.bone_indices_offset,
            sorted_offsets,
        )
        if section_size != allocated_size:
            raise ValueError(
                "Bone-index allocation must exactly match jointCount aligned "
                f"to 16 bytes ({section_size} != {allocated_size})"
            )
        raw = _checked_slice(data, self.header.bone_indices_offset, size, "Bone indices")
        self.bone_indices = list(struct.unpack(f'<{self.joint_count}h', raw))
        padding = _checked_slice(
            data,
            self.header.bone_indices_offset + size,
            allocated_size - size,
            "Bone-index alignment padding",
        )
        if any(padding):
            raise ValueError("Bone-index alignment padding is nonzero")

    def _finalize_mply_material_count(self) -> None:
        if (
            self.header.magic != MPLY_MAGIC
            or not self.header.profile.mply_material_count_uses_name_partition
        ):
            return
        if not self.meshes:
            raise ValueError("MPLY name partition requires parsed mesh data")
        if (
            self.header.blend_shapes_offset
            or self.header.blend_shape_bone_modifier_offset
            or self.header.blend_shape_indices_offset
        ):
            raise ValueError(
                "Release MPLY does not define a blend-shape name domain"
            )
        material_count = int(self.header.name_count) - int(self.joint_count)
        if material_count < 0:
            raise ValueError("MPLY joint count exceeds the global name count")
        material_ids = {
            int(cluster.material_id)
            for cluster in (
                self.mesh_buffer.mply_cluster_headers
                if self.mesh_buffer is not None
                else ()
            )
        }
        if any(material_id >= material_count for material_id in material_ids):
            raise ValueError(
                "MPLY cluster material ID leaves the material-name domain"
            )
        self.meshes[0].material_count = material_count

    def _parse_parts_name_hashes(
        self,
        data: bytes,
        sorted_offsets: List[int],
    ) -> None:
        self.parts_name_hashes = []
        self.parts_name_hash_ids = []
        self.parts_name_hashes_implicit = False
        offset = self.header.parts_name_hash_offset
        if not self.meshes or not self.meshes[0].lods:
            return
        lod0 = self.meshes[0].lods[0]
        lod0_part_ids = [group.part_id for group in lod0.mesh_groups]
        unique_lod0_part_ids = list(dict.fromkeys(lod0_part_ids))

        if (
            not offset
            and self.header.magic == MPLY_MAGIC
            and self.header.profile.meshlet_has_implicit_part_hashes
            and unique_lod0_part_ids
        ):
            # DD2: 16-byte aligned Murmur3 UTF-16LE hash per first-seen LOD0 part ID, after the last material string.
            table_end = self.header.name_offsets_offset + self.header.name_count * 8
            for index in range(self.header.name_count):
                string_offset = struct.unpack_from(
                    "<Q",
                    data,
                    self.header.name_offsets_offset + index * 8,
                )[0]
                if not 0 < string_offset < len(data):
                    raise ValueError(
                        f"Invalid MPLY material-string offset {string_offset}"
                    )
                terminator = data.find(b"\x00", string_offset)
                if terminator < 0:
                    raise ValueError("Unterminated MPLY material string")
                table_end = max(table_end, terminator + 1)
            offset = (table_end + 15) & ~15
            count = len(unique_lod0_part_ids)
            padded_size = (count * 4 + 15) & ~15
            if self._section_size(offset, sorted_offsets) < padded_size:
                raise ValueError("Implicit MPLY parts-name-hash section is too small")
            padding = _checked_slice(
                data,
                offset + count * 4,
                padded_size - count * 4,
                "Implicit MPLY parts-name-hash padding",
            )
            if any(padding):
                raise ValueError("Implicit MPLY parts-name-hash padding is nonzero")
            self.parts_name_hashes_implicit = True
            self.parts_name_hash_ids = unique_lod0_part_ids
        elif offset:
            if self.header.magic == MPLY_MAGIC:
                layout = (
                    self.mesh_buffer.meshlet_parts_layout
                    if self.mesh_buffer is not None
                    else None
                )
                if layout is None:
                    raise ValueError(
                        "MPLY parts-name hashes require MeshletPartsLayout count ownership"
                    )
                count = layout.parts_num
                self.parts_name_hash_ids = list(range(count))
            else:
                count = lod0.part_count
                self.parts_name_hash_ids = lod0_part_ids
        else:
            return

        size = count * 4
        allocated_size = (size + 15) & ~15
        section_size = self._section_size(offset, sorted_offsets)
        if section_size != allocated_size:
            raise ValueError(
                "Parts-name-hash allocation must exactly match its semantic "
                f"count aligned to 16 bytes ({section_size} != {allocated_size})"
            )
        raw = _checked_slice(data, offset, size, "Parts name hashes")
        self.parts_name_hashes = list(struct.unpack(f"<{count}I", raw))
        padding = _checked_slice(
            data,
            offset + size,
            allocated_size - size,
            "Parts-name-hash alignment padding",
        )
        if any(padding):
            raise ValueError("Parts-name-hash alignment padding is nonzero")
        if self.parts_name_hashes_implicit:
            expected = [
                murmur3_hash_utf16le(f"Part {part_id}")
                for part_id in self.parts_name_hash_ids
            ]
            if self.parts_name_hashes != expected:
                raise ValueError(
                    "Implicit MPLY parts-name hashes do not match LOD0 part IDs"
                )

    def _parse_normal_recalc(
        self,
        data: bytes,
        sorted_offsets: List[int],
    ) -> None:
        self.normal_recalc_data = None
        offset = self.header.normal_recalc_offset
        if not offset:
            return
        if self.mesh_buffer is None or not self.meshes or not self.meshes[0].lods:
            raise ValueError("Normal recalculation requires modeled mesh geometry")

        normal_layout = self.header.profile.normal_recalc_layout
        if normal_layout is NormalRecalcLayout.NONE:
            self.note(
                "normal_recalc",
                "layout",
                f"{self.header.profile.display_name} has a normal-recalculation "
                "section but its profile does not assign a semantic layout; "
                "the raw section remains available for inspection.",
            )
            return
        from .normal_recalc import (
            BUFFER_OWNED_NORMAL_RECALC_PARSERS,
            LOD0_NORMAL_RECALC_PARSERS,
            legacy_normal_recalc_submeshes,
            parse_modern_normal_recalc,
            parse_sf6_normal_recalc,
            sf6_normal_recalc_lod_topologies,
        )

        profile = self.header.profile
        if (
            normal_layout is NormalRecalcLayout.SF6
            or normal_layout in LOD0_NORMAL_RECALC_PARSERS
            or normal_layout in BUFFER_OWNED_NORMAL_RECALC_PARSERS
        ):
            section_size = self._section_size(offset, sorted_offsets)
            if section_size <= 0:
                raise ValueError(
                    f"{profile.display_name} normal-recalculation section has no bounds"
                )
            section_end = offset + section_size
            if normal_layout is NormalRecalcLayout.SF6:
                self.normal_recalc_data = parse_sf6_normal_recalc(
                    data,
                    offset,
                    section_end,
                    sf6_normal_recalc_lod_topologies(self),
                    section_alignment=profile.normal_recalc_section_alignment,
                    split_vertex_page_plane=profile.normal_recalc_has_split_vertex_page_plane,
                )
            elif normal_layout in BUFFER_OWNED_NORMAL_RECALC_PARSERS:
                self.normal_recalc_data = BUFFER_OWNED_NORMAL_RECALC_PARSERS[normal_layout](
                    data, offset, section_end, self, sf6_normal_recalc_lod_topologies(self),
                )
            else:
                self.normal_recalc_data = LOD0_NORMAL_RECALC_PARSERS[normal_layout](
                    data,
                    offset,
                    section_end,
                    sum(group.vertex_count for group in self.meshes[0].lods[0].mesh_groups),
                    legacy_normal_recalc_submeshes(self),
                )
            return

        lod_counts = [
            (
                sum(group.vertex_count for group in lod.mesh_groups),
                sum(group.face_count for group in lod.mesh_groups),
            )
            for lod in self.meshes[0].lods
        ]
        self.normal_recalc_data = parse_modern_normal_recalc(data, offset, profile, lod_counts)
        if (
            self.normal_recalc_data.declared_lod_count
            and not self.normal_recalc_data.lods
        ):
            pointer_state = (
                "its body-list pointer is null or non-positive"
                if self.normal_recalc_data.body_list_offset <= 0
                else "its body list contains no positive body pointers"
            )
            self.note(
                "normal_recalc",
                "body_list",
                "The header declares normal-recalculation LODs but "
                f"{pointer_state}. MESH.bt does not assign an "
                "inline fallback layout, so the header is preserved "
                "without inventing body records.",
            )
        self.note(
            "normal_recalc",
            "vertex_entries/face_entries",
            "This profile's normal-recalculation words are structurally "
            "parsed, but their bit-level runtime meaning is not yet "
            "known; the viewer exposes them as raw uint32 values.",
        )

    def read(
        self,
        data: bytes,
        *,
        file_version: int,
        load_mode: MeshLoadMode | str = MeshLoadMode.FULL,
        streaming_data: Optional[bytes] = None,
        streaming_data_loader: Optional[Callable[[], Optional[bytes]]] = None,
    ) -> bool:
        from .blend_shape import (
            BlendShapeData,
            decode_blend_shape_payloads,
            parse_bone_modifier,
            parse_blend_shapes,
            read_blend_shape_name_indices,
        )

        self._reset_state(MeshLoadMode.coerce(load_mode))
        self.source_size = len(data)

        h = BinaryHandler(data, file_version=file_version)
        if not self.header.read(
            h,
            file_version=file_version,
        ):
            raise ValueError("Not a mesh file")
        sorted_offsets = self._collect_section_offsets(len(data))
        if self.is_full:
            self._capture_sections(data, sorted_offsets)

        if self.header.profile.header_layout is MeshHeaderLayout.LEGACY:
            if self.header.profile.legacy_reserved_storage_is_corpus_zero:
                has_nonzero_reserved_storage = bool(
                    self.header.legacy_solved_offset
                    or (
                        self.header.legacy_light_map_raw
                        and self.header.profile.legacy_light_map_layout
                        is not LegacyLightMapLayout.RE8
                    )
                )
                if has_nonzero_reserved_storage:
                    self.note(
                        "header",
                        "reserved_storage",
                        f"A {self.header.profile.game} corpus-zero legacy "
                        "header slot is nonzero.",
                    )
            else:
                self.note(
                    "header",
                    "legacy_solved_offset",
                    "The legacy solved/buffer-offset byte is preserved; its exact "
                    "runtime role is only partially understood.",
                )
                self.note(
                    "header",
                    "legacy_flags",
                    "The legacy mesh flag byte is preserved, but its individual "
                    "bit meanings are not fully established for this profile.",
                )
        elif self.header.header_pad:
            self.note(
                "header",
                "header_pad",
                f"Header padding is nonzero (0x{self.header.header_pad:04X}).",
            )

        if self.header.streaming_info_offset:
            h.seek(self.header.streaming_info_offset)
            self.streaming_info = MeshStreamingInfo()
            self.streaming_info.read(h)
            self.streaming_data_loaded = bool(streaming_data) and self.streaming_buffer_count > 0

        if (
            self.header.magic == MESH_MAGIC
            and self.header.profile.content_buffer_count_includes_resident
        ):
            flags = self.header.decoded_content_flags
            if flags is None:
                raise ValueError(
                    f"{self.header.profile.display_name} has no decoded "
                    "content flags"
                )
            external_buffer_count = self.header.profile.streaming_layout_count(
                self.streaming_info.entries if self.streaming_info is not None else ()
            )
            # Wilds wraps seven external buffers plus buffer zero to zero.
            count_mask = (1 << (self.header.profile.header_buffer_count_bits or 3)) - 1
            expected_buffer_count = (external_buffer_count + 1) & count_mask
            if flags.buffer_count != expected_buffer_count:
                raise ValueError(
                    f"{self.header.profile.display_name} content buffer count "
                    f"{flags.buffer_count} does not match one resident plus "
                    f"{external_buffer_count} streaming buffer layouts"
                )

        if self.header.magic == MPLY_MAGIC and self.header.mesh_offset:
            if streaming_data is None and streaming_data_loader is not None:
                streaming_data = streaming_data_loader()
            self.mesh_buffer, self.meshes = MPLYParser(
                self.header,
                data,
                streaming_data=streaming_data,
            ).parse()
            self.streaming_data_loaded = bool(streaming_data) and self.streaming_buffer_count > 0
            self.group_pivots = self.mesh_buffer.mply_group_pivots
            parts_layout = self.mesh_buffer.meshlet_parts_layout
            if (
                parts_layout is not None
                and parts_layout.parts_indices_offset > 0
            ):
                sorted_offsets = sorted(
                    {
                        *sorted_offsets,
                        int(parts_layout.parts_indices_offset),
                    }
                )
                if self.is_full:
                    self._capture_sections(data, sorted_offsets)
        elif self.header.mesh_offset:
            h.seek(self.header.mesh_offset)
            self.mesh_buffer = MeshBuffer(self.header.profile)
            self.mesh_buffer.read(
                h,
                streaming_info=self.streaming_info,
                streaming_data=streaming_data,
                streaming_data_loader=streaming_data_loader,
            )

        if self.header.lods_offset and self.mesh_buffer:
            h.seek(self.header.lods_offset)
            md = MeshData(self.mesh_buffer)
            md.read(
                h,
                read_all_lods=self.is_full,
                lightweight=not self.is_full,
            )
            self.meshes.append(md)
            self.streaming_data_loaded = bool(
                self.mesh_buffer._streaming_data
                and self.streaming_buffer_count > 0
            )

        if self.is_full and self.mesh_buffer is not None:
            self._decode_separate_skin_weights(data)

        if self.is_full and self.mesh_buffer is not None:
            if self.header.shadow_lods_offset:
                h.seek(self.header.shadow_lods_offset)
                self.shadow_mesh = ShadowMeshData(self.mesh_buffer)
                self.shadow_mesh.read(h, read_all_lods=True)
            if self.header.occluder_mesh_offset:
                h.seek(self.header.occluder_mesh_offset)
                self.occluder_mesh = OccluderMeshData(self.mesh_buffer)
                self.occluder_mesh.read(h)
            self._decode_auxiliary_geometry(data)

        self._parse_bones(h)
        self._finalize_mply_material_count()
        self._parse_bone_indices(data, sorted_offsets)
        self._parse_parts_name_hashes(data, sorted_offsets)

        if self.is_full:
            self._parse_group_pivots(h)
            self._parse_bounding_boxes(h)
            self._parse_sdf_path(h)
            self._parse_normal_recalc(data, sorted_offsets)

            if self.header.blend_shapes_offset:
                try:
                    blend_section_size = self._section_size(
                        self.header.blend_shapes_offset,
                        sorted_offsets,
                    )
                    self.blend_shape_data = parse_blend_shapes(
                        data,
                        self.header.blend_shapes_offset,
                        self.header.profile,
                        section_end=(
                            self.header.blend_shapes_offset
                            + blend_section_size
                        ),
                        external_bone_modifier_offset=(
                            self.header.blend_shape_bone_modifier_offset
                        ),
                    )
                except (ValueError, struct.error, OverflowError) as exc:
                    raise ValueError(
                        f"Invalid {self.header.format_version.name} "
                        f"blendshape metadata: {exc}"
                    ) from exc

            elif self.header.blend_shape_bone_modifier_offset:
                try:
                    modifier = parse_bone_modifier(
                        data,
                        self.header.blend_shape_bone_modifier_offset,
                    )
                    self.blend_shape_data = BlendShapeData(
                        bodies=[],
                        bone_modifier=modifier,
                    )
                except (ValueError, struct.error, OverflowError) as exc:
                    raise ValueError(
                        f"Invalid {self.header.format_version.name} "
                        f"blendshape bone-modifier metadata: {exc}"
                    ) from exc

            if self.header.blend_shape_indices_offset:
                size = self._section_size(
                    self.header.blend_shape_indices_offset,
                    sorted_offsets,
                )
                if not self.meshes:
                    raise ValueError(
                        "Blendshape name partition requires MeshData"
                    )
                if self.blend_shape_data is None:
                    raise ValueError(
                        "Blendshape name partition requires BlendShapeLayout"
                    )
                # The string table is partitioned into material, joint, then blend-shape channel names.
                semantic_name_count = (
                    self.header.name_count
                    - self.meshes[0].material_count
                    - self.joint_count
                )
                self.blend_shape_indices = read_blend_shape_name_indices(
                    data,
                    self.header.blend_shape_indices_offset,
                    semantic_name_count=semantic_name_count,
                    section_size=size,
                    blend_shapes=self.blend_shape_data,
                )

        if self.header.name_offsets_offset:
            start = self.header.name_offsets_offset
            h.seek(start)
            self.name_offsets = [h.read_int64() for _ in range(self.header.name_count)]
            for off in self.name_offsets:
                if off <= 0 or off >= len(data):
                    raise ValueError(f"Invalid mesh string offset {off}")
                end = data.find(b"\x00", off)
                if end < 0:
                    raise ValueError(f"Unterminated mesh string at offset {off}")
                self.names.append(data[off:end].decode("utf-8"))
            if self.header.material_indices_offset and self.meshes:
                h.seek(self.header.material_indices_offset)
                mcount = self.meshes[0].material_count
                required_size = mcount * 2
                allocated_size = (required_size + 15) & ~15
                section_size = self._section_size(
                    self.header.material_indices_offset,
                    sorted_offsets,
                )
                if section_size != allocated_size:
                    raise ValueError(
                        "Material-index allocation must exactly match "
                        "MeshData.materialCount aligned to 16 bytes "
                        f"({section_size} != {allocated_size})"
                    )
                self.material_indices = [h.read_int16() for _ in range(mcount)]
                padding = _checked_slice(
                    data,
                    self.header.material_indices_offset + required_size,
                    allocated_size - required_size,
                    "Material-index alignment padding",
                )
                if any(padding):
                    raise ValueError(
                        "Material-index alignment padding is nonzero"
                    )
                if self.header.magic == MPLY_MAGIC:
                    self.material_names = self.names[:mcount]
                else:
                    for material_index, name_index in enumerate(self.material_indices):
                        if not 0 <= name_index < len(self.names):
                            raise ValueError(
                                f"Material {material_index} references invalid "
                                f"mesh name index {name_index}"
                            )
                    self.material_names = [
                        self.names[idx] for idx in self.material_indices
                    ]

        if self.blend_shape_data is not None:
            self.blend_shape_data.set_channel_names(
                self.blend_shape_indices,
                self.names,
            )
            if self.mesh_buffer is None or not self.meshes:
                raise ValueError("Blendshapes require parsed mesh buffers")
            body_buffer_indices: Dict[int, int] = {}
            for body in self.blend_shape_data.bodies:
                buffer_index = 0
                if body.lod_index < len(self.meshes[0].lods):
                    indices = {
                        submesh.buffer_index
                        for group in self.meshes[0].lods[
                            body.lod_index
                        ].mesh_groups
                        for submesh in group.submeshes
                    }
                    if len(indices) > 1:
                        raise ValueError(
                            f"Blendshape LOD {body.lod_index} spans multiple "
                            f"streaming buffers {sorted(indices)}"
                        )
                    if indices:
                        buffer_index = next(iter(indices))
                body_buffer_indices[body.lod_index] = buffer_index
                if self.profile.blend_shape_vertex_offsets_are_lod_relative:
                    lod = self.meshes[0].lods[body.lod_index]
                    base = min(s.verts_index_offset for g in lod.mesh_groups for s in g.submeshes)
                    for target in (*body.targets, *body.none_targets):
                        target.ranges = [replace(r, base_vertex_location=r.base_vertex_location + base)
                                         for r in target.ranges]
            payload_offsets = {0: self.mesh_buffer.blend_shape_offset}
            payload_offsets.update(
                {
                    buffer_index: header.blend_shape_offset
                    for buffer_index, header in (
                        self.mesh_buffer.streaming_buffer_headers.items()
                    )
                }
            )
            vertex_counts = dict(self.mesh_buffer.vertex_counts_by_buffer)
            try:
                decode_blend_shape_payloads(
                    self.blend_shape_data,
                    self.mesh_buffer.buffer_payloads,
                    payload_offsets,
                    vertex_counts,
                    body_buffer_indices,
                    self.header.profile,
                    explicit_normals=not bool(
                        self.header.normal_recalc_offset
                    ),
                )
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    f"Unsupported {self.header.format_version.name} "
                    f"blendshape payload: {exc}"
                ) from exc
            for message in self.blend_shape_data.unresolved_semantics:
                self.note("blend_shapes", "payload", message)

        if self.mesh_buffer is not None:
            is_dd2_buffer = (
                self.mesh_buffer.profile.buffer_layout is MeshBufferLayout.DD2
            )
            if self.mesh_buffer.buffer_index_unkn and not is_dd2_buffer:
                self.note(
                    "mesh_buffer",
                    "buffer_index_unkn",
                    "The dword immediately before bufferIndex is preserved; "
                    "its semantic role is unknown.",
                )
            if self.mesh_buffer.unknown_struct and not is_dd2_buffer:
                self.note(
                    "mesh_buffer",
                    "unknown_struct",
                    "The trailing modern-buffer int64 is preserved; its semantic "
                    "role is unknown.",
                )
            unknown_types = {
                stream.type
                for payload in self.mesh_buffer.buffer_payloads.values()
                for stream in payload.attribute_streams
                if stream.type not in {int(item) for item in VertexBufferType}
            }
            for raw_type in sorted(unknown_types):
                self.note(
                    "mesh_buffer",
                    f"vertex_attribute_{raw_type}",
                    "Unknown vertex attribute bytes are preserved verbatim.",
                )
        if self.is_full:
            self.assert_runtime_contract()
        self._note_structural_semantics()
        self._release_lightweight_raw_buffers()
        # The streaming companion is read only during this read; keeping the handler's reader would put the handler (a
        # Qt object) in a reference cycle.
        if self.mesh_buffer is not None:
            self.mesh_buffer._streaming_data_loader = None

        return True
