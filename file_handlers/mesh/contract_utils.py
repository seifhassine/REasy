from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

import math

lazy import numpy as np

from .models import MPLY_MAGIC
lazy from .canonical_contract import assert_mesh_semantic_domains
lazy from . import normal_recalc
lazy from .normal_recalc import NormalRecalcData, derive_sf6_normal_indices


def has_nonzero(value: object) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return any(value)
    if isinstance(value, np.ndarray):
        return bool(value.size and np.any(value != 0))
    if isinstance(value, (tuple, list, array)):
        return any(has_nonzero(item) for item in value)
    return bool(value)


@dataclass(frozen=True, slots=True)
class ContractValidator:
    name: str

    def fail(self, field: str, expectation: str, value: object) -> None:
        raise ValueError(
            f"{self.name}: {field} {expectation}; got {value!r}"
        )

    def require(
        self,
        condition: bool,
        field: str,
        expectation: str,
        value: object,
    ) -> None:
        if not condition:
            self.fail(field, expectation, value)

    def require_zero(self, field: str, value: object) -> None:
        if has_nonzero(value):
            self.fail(field, "must be zero", value)

    def require_equal(
        self,
        field: str,
        value: object,
        expected: object,
    ) -> None:
        if value != expected:
            self.fail(field, f"must equal {expected!r}", value)

    def assert_mesh_data_padding(self, mesh_index: int, mesh_data: object) -> None:
        field = f"MeshData[{mesh_index}]"
        self.require_zero(f"{field}.reserve", mesh_data.reserve)
        self.require_zero(f"{field}.AABB chunk", mesh_data.chunk)
        for side, value in (("minimum", mesh_data.aabb_min_padding), ("maximum", mesh_data.aabb_max_padding)):
            self.require(
                int(value) in (0, 1),
                f"{field}.AABB {side} padding",
                "must be either the retail producer sentinel 1 or canonical cleared 0",
                value,
            )

    def assert_content_policy(self, flags: object, is_mply: bool) -> None:
        expected = 0 if is_mply else 2
        for name in ("enable_rebraiding", "prefer_opaque", "prefer_shadow_cast", "prefer_alpha_mask"):
            self.require_equal(f"MeshHeader.contentFlags.{name}", int(getattr(flags, name)), expected)

    def assert_ignored_weight_bytes_zero(self, mesh_buffer: object) -> None:
        for buffer_index, payload in mesh_buffer.buffer_payloads.items():
            for field in ("skin_weights", "extra_skin_weights"):
                weights = getattr(payload, field, None)
                if weights is not None:
                    self.require_zero(
                        f"BufferLayout[{buffer_index}].{field}.runtime-ignored final weight byte",
                        weights.runtime_ignored_weight_bytes,
                    )

    def assert_auxiliary_reserves(self, mesh: object) -> None:
        shadow = getattr(mesh, "shadow_mesh", None)
        if shadow is not None:
            self.require_zero("ShadowMeshLayout.reserve", shadow.reserve)
            self.require_zero("ShadowMeshLayout legacy padding", shadow.dmc5_pad)
            self.require_zero("ShadowMeshLayout reserved slots", shadow.reserved_slots)
        occluder = getattr(mesh, "occluder_mesh", None)
        if occluder is not None:
            self.require_zero(
                "OccluderMeshLayout selector/reserve fields",
                (occluder.vertex_format, occluder.lod_level, occluder.reserve),
            )
        self.require_zero("SkeletonLayout.reserve", mesh.skeleton_reserved)
        self.require_zero("Skeleton bone-remap padding", mesh.bone_remap_padding)
        for joint_index, joint in enumerate(mesh.bones):
            self.require_zero(f"JointNode[{joint_index}].reserved", joint.reserved)
        normal = getattr(mesh, "normal_recalc_data", None)
        if normal is not None:
            self.require_zero("NormalRecalcLayout.reserve", normal.reserved)

    def assert_late_header(self, mesh: object) -> None:
        header = mesh.header
        source_size = int(getattr(mesh, "source_size", 0))
        if source_size:
            self.require_equal("MeshHeader.fileSize", int(header.file_size), source_size)
        self.require_zero("MeshHeader content-flags storage byte 3", int(header.content_flags_storage_raw) >> 24)
        self.require_zero("MeshHeader.alignmentPadding", header.header_pad)
        self.require_zero("MeshHeader trailing reserved dwords", header.header_reserved_dwords)
        self.require_zero("MeshHeader.lightMapMetadata.reserved", header.light_map_reserved)
        dimensions = (header.light_map_width, header.light_map_height)
        if header.light_map_used:
            self.require(min(dimensions) > 0, "MeshHeader.lightMapMetadata dimensions", "must be positive when enabled",
                         dimensions)
        else:
            self.require_zero("MeshHeader unused light-map dimensions", dimensions)
        flags = header.decoded_content_flags
        if not mesh.profile.content_buffer_count_can_wrap:
            self.require(1 <= flags.buffer_count <= 7, "MeshHeader.contentFlags.bufferCount",
                         "must be a nonzero three-bit total", flags.buffer_count)
        self.require_zero("MeshHeader.contentFlags.reservedBits", flags.reserved_bits)
        self.require_equal("MeshHeader.contentFlags.streamingBVH", bool(flags.streaming_bvh),
                           header.magic == MPLY_MAGIC)
        self.require_equal("MeshHeader.contentFlags.useSDF", bool(flags.use_sdf), bool(header.sdf_path_offset))
        for flag_name, pointer_name in (
            ("has_joint", "bones_offset"),
            ("has_blend_shape", "blend_shapes_offset"),
            ("has_vertex_group", "floats_offset"),
        ):
            self.require_equal(
                f"MeshHeader.contentFlags.{flag_name}",
                bool(getattr(flags, flag_name)),
                bool(getattr(header, pointer_name)),
            )

    def assert_late_meshlet_runtime(self, mesh: object, mesh_buffer: object) -> None:
        self.assert_late_meshlet_layout(mesh, mesh_buffer)
        layout = mesh_buffer.meshlet_layout
        mheader = layout.header
        self.assert_meshlet_lod_lanes(mheader)
        self.require(int(mheader.serialized_mpmr_lod_strategy) in (0, 1), "MeshletHeader.MPMRLodStrategy",
                     "must be ScreenArea or Distance", mheader.serialized_mpmr_lod_strategy)
        self.require_zero("MeshletHeader threshold/strategy high reserve", mheader.serialized_lod_control_reserved_bits)
        self.assert_meshlet_region_ends(layout, explicit=True)

    def assert_late_meshlet_layout(self, mesh: object, mesh_buffer: object) -> None:
        """Callers add their LOD-control lanes, region ends and chunk tangent rules."""
        header = mesh.header
        flags = header.decoded_content_flags
        self.require_equal("MPLY ContentFlags.bufferCount", int(flags.buffer_count), 1)
        self.require_equal("MPLY ContentFlags.highMeshCount", int(flags.high_mesh_count), 0)
        layout = mesh_buffer.meshlet_layout
        bvh = mesh_buffer.meshlet_bvh
        parts = mesh_buffer.meshlet_parts_layout
        self.require(
            layout is not None and bvh is not None and parts is not None,
            "MPLY",
            "must contain MeshletLayout, MeshletBVH, and MeshletPartsLayout",
            None,
        )
        self.require_zero(
            "MeshHeader MPLY reserved/ordinary pointer union",
            (
                header.micro_polygon_reserved_offset,
                header.blend_shapes_offset,
                header.buffer_layout_offset,
                header.blend_shape_bone_modifier_offset,
            ),
        )
        self.require_equal("MeshHeader.gpuBufferBase", int(header.gpu_buffer_base_offset),
                           int(layout.gpu_meshlet_offset))
        self.require_zero("MeshletLayout.reserve", layout.reserved)
        self.require(bool(layout.gpu_header_matches_resident), "MeshletLayout GPU header mirror",
                     "must match the resident MeshletHeader", False)
        source_size = int(getattr(mesh, "source_size", 0))
        if source_size:
            blob_start = int(layout.gpu_meshlet_offset)
            blob_end = blob_start + int(layout.gpu_data_size)
            self.require(0 <= blob_start <= blob_end <= source_size, "MeshletLayout GPU blob", "must fit the resource",
                         (blob_start, blob_end, source_size))

        mheader = layout.header
        lod_count = int(mheader.lod_num)
        self.require(1 <= lod_count <= 8, "MeshletHeader.lodNum", "must be in [1, 8]", lod_count)
        self.require_zero(
            "serialized MeshletHeader runtime LOD state",
            (mheader.loaded_lod, mheader.resident_lod, mheader.allocation_lod),
        )
        self.require_equal("MeshletHeader.validLodBits", int(mheader.valid_lod_bits), (1 << lod_count) - 1)
        self.assert_finite_ordered_aabb("MeshletHeader.AABB", mheader.min_aabb, mheader.max_aabb)

        self.require_equal("MeshletLayout LOD-region count", len(layout.lod_regions), lod_count)
        for lod_index in range(8):
            table = int(mheader.lod_cluster_offsets[lod_index])
            end = int(mheader.serialized_lod_end_offsets[lod_index])
            if lod_index < lod_count:
                self.require(table >= 0x80 and end > 0, f"MeshletHeader LOD[{lod_index}] region",
                             "must have an active table and explicit end", (table, end))
            else:
                self.require_zero(f"MeshletHeader inactive LOD[{lod_index}]", (table, end))
        for lod_index, region in enumerate(layout.lod_regions):
            field = f"MeshletLayout LOD[{lod_index}]"
            self.require_equal(f"{field} cluster count", int(region.cluster_count), int(bvh.cluster_counts[lod_index]))
            offsets = region.chunk_relative_offsets
            self.require(all(left < right for left, right in zip(offsets, offsets[1:])), f"{field} chunk offsets",
                         "must be strictly increasing", offsets)

        pivot = layout.group_pivot_info
        self.require(pivot is not None, "MPLY group-pivot descriptor", "must be modeled", None)
        self.require(bool(pivot.layout_mirror_matches_blob), "MPLY group-pivot mirror", "must match the GPU descriptor",
                     False)
        self.require(bool(pivot.header_offset_matches_internal), "MPLY header group-pivot offset",
                     "must match the internal descriptor", False)
        self.require_zero("MPLY group-pivot descriptor reserve", (pivot.reserve_u16, pivot.reserve_u32))
        self.require_equal("MPLY group-pivot descriptor count", len(pivot.pivots),
                           int(pivot.pivot_count) if pivot.present else 0)
        self.require_equal("MPLY contentFlags.hasVertexGroup", bool(flags.has_vertex_group), bool(pivot.present))

        clusters = mesh_buffer.mply_cluster_headers
        clusters_by_lod = []
        cluster_cursor = 0
        expected_cluster_offset = 0
        for lod_index in range(8):
            count = int(bvh.cluster_counts[lod_index])
            offset = int(bvh.cluster_offsets[lod_index])
            self.require_zero(f"MeshletBVH.clusterCount[{lod_index}] reserved byte",
                              bvh.cluster_count_reserved_bytes[lod_index])
            self.require_zero(f"MeshletBVH.meshletBVHOffset[{lod_index}]", bvh.meshlet_bvh_offsets[lod_index])
            if lod_index < lod_count:
                self.require(count > 0, f"MeshletBVH.clusterCount[{lod_index}]", "must be positive", count)
                self.require_equal(f"MeshletBVH.clusterOffset[{lod_index}]", offset, expected_cluster_offset)
                expected_cluster_offset += count * 16
            else:
                self.require_zero(
                    f"MeshletBVH inactive LOD[{lod_index}]",
                    (count, offset, bvh.cluster_bounds_scale_exponent_raw_bytes[lod_index]),
                )
            clusters_by_lod.append(clusters[cluster_cursor:cluster_cursor + count])
            cluster_cursor += count
        total_clusters = sum(int(value) for value in bvh.cluster_counts)
        self.require_equal("MeshletBVH quantized-center count", len(bvh.quantized_aabb_centers), total_clusters)
        self.require_equal("MPLY cluster-header count", len(clusters), total_clusters)
        self.require_equal("MPLY cluster/chunk count", len(mesh_buffer.mply_chunks), total_clusters)
        global_index = 0
        for lod_index, lod_clusters in enumerate(clusters_by_lod):
            for cluster in lod_clusters:
                self.require_equal(f"MPLY cluster[{global_index}].aabbCenterIndex", int(cluster.aabb_center_index),
                                   global_index)
                self.require_equal(f"MPLY cluster[{global_index}].cullingAABB", cluster.culling_aabb,
                                   bvh.decode_cluster_culling_aabb(lod_index, cluster))
                global_index += 1

        self.require_zero("MeshletPartsLayout.reserve", parts.reserve)
        self.require_equal("MeshletPartsLayout.partsNum", int(parts.parts_num), len(parts.parts_indices))
        self.require_equal("MeshletPartsLayout part-ID set", set(parts.parts_indices),
                           {int(cluster.part_id) for cluster in clusters})
        self.require_equal("MeshletPartsLayout unique part IDs", len(set(parts.parts_indices)),
                           len(parts.parts_indices))

        for chunk_index, (cluster, chunk) in enumerate(zip(clusters, mesh_buffer.mply_chunks)):
            field = f"MPLY chunk[{chunk_index}]"
            chunk_flags = chunk.flags
            vert_count = int(chunk.vert_count)
            self.require(chunk_flags is not None, f"{field}.flags", "must be decoded", None)
            self.require_zero(f"{field} trailing payload", chunk.trailing_payload)
            self.require_zero(f"{field} position alignment padding", chunk.position_alignment_padding)
            for name, value, expected in (
                ("position selector", chunk_flags.position_compress_level, cluster.position_compress_level),
                ("vertex count", chunk.vert_count, cluster.vertex_count),
                ("index count", len(chunk.faces), cluster.index_count),
                ("material ID", chunk.material_id, cluster.material_id),
                ("part ID", chunk.part_id, cluster.part_id),
            ):
                self.require_equal(f"{field} {name}", value, expected)
            if len(chunk.faces):
                low, high = int(chunk.faces.min()), int(chunk.faces.max())
                self.require(low >= 0 and high < vert_count, f"{field}.indices",
                             "must index the cluster-local vertex domain", (low, high, chunk.vert_count))
            for name, values in (
                ("positions", chunk.positions),
                ("normals", chunk.normals),
                ("normal W", chunk.normal_ws),
                ("tangent W", chunk.tangent_ws),
                ("UV0", chunk.uv0),
            ):
                self.require_equal(f"{field} {name} count", len(values), vert_count)
            for name, values, declared, size in (
                ("UV1", chunk.uv1, chunk_flags.is_meshlet_use_texcoord2, 1),
                ("UV2", chunk.uv2, chunk_flags.is_meshlet_use_texcoord3, 1),
                ("color-byte", chunk.colors, chunk_flags.is_meshlet_use_vertex_color, 4),
            ):
                self.require_equal(f"{field} {name} count", len(values), vert_count * size if declared else 0)
            skin_records = 0
            if chunk_flags.has_skin_stream:
                skin_records = 1 if chunk_flags.skin_stream_is_shared else vert_count
            self.require_equal(f"{field} skin-record byte size", len(chunk.skin_record_bytes), skin_records * 16)
            self.require(chunk.cluster_aabb is not None, f"{field}.AABB", "must be decoded", None)

    def assert_meshlet_lod_lanes(self, mheader: object) -> None:
        self.require_equal("MeshletHeader binary16 factor lane count", len(mheader.packed_binary16_lod_factor_lanes), 8)
        for lod_index, factor in enumerate(mheader.active_lod_factors):
            self.require(math.isfinite(float(factor)) and float(factor) >= 0.0, f"MeshletHeader.lodFactor[{lod_index}]",
                         "must be finite and nonnegative", factor)
        threshold = int(mheader.serialized_meshlet_raster_switch_threshold)
        self.require(0 <= threshold <= 7, "MeshletHeader.MeshletRasterSwitchThreshold",
                     "must be in the native enum domain [0, 7]", threshold)
        self.require_zero("MeshletHeader threshold payload reserve words",
                          mheader.serialized_lod_payload_reserved_words)

    def assert_meshlet_region_ends(self, layout: object, *, explicit: bool) -> None:
        mheader = layout.header
        for lod_index, region in enumerate(layout.lod_regions):
            field = f"MeshletLayout LOD[{lod_index}]"
            if explicit:
                self.require_equal(f"{field} table offset", int(region.table_relative_offset),
                                   int(mheader.lod_cluster_offsets[lod_index]))
                self.require_equal(f"{field} explicit absolute end", int(region.region_end_absolute),
                                   int(mheader.serialized_lod_end_offsets[lod_index]))
            else:
                start = int(layout.gpu_data_size) if lod_index == 0 else int(mheader.lod_cluster_offsets[lod_index - 1])
                self.require_equal(f"{field} absolute end", int(region.region_end_absolute),
                                   int(layout.gpu_meshlet_offset) + start)

    def assert_meshlet_positions_in_bounds(self, mesh_buffer: object) -> None:
        for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
            tolerance = max(abs(math.ldexp(1.0, int(chunk.flags.relative_aabb_scale_exponent) - 143)), 1e-6)
            minimum = np.asarray(chunk.cluster_aabb.minimum, dtype=np.float64) - tolerance
            maximum = np.asarray(chunk.cluster_aabb.maximum, dtype=np.float64) + tolerance
            self.require(not ((chunk.positions < minimum) | (chunk.positions > maximum)).any(),
                         f"MPLY chunk[{chunk_index}].positions", "must lie inside its native decoded cluster AABB",
                         "out-of-bounds position")

    def assert_late_buffer_layouts(self, mesh: object, mesh_buffer: object, element_headers) -> None:
        header = mesh.header
        flags = header.decoded_content_flags
        streams = mesh.profile.streaming_layout_count(mesh.streaming_info.entries if mesh.streaming_info else ())
        self.require_equal("MeshHeader.contentFlags.bufferCount", int(flags.buffer_count), (streams + 1) & 0x7)
        self.require_equal("MeshHeader.contentFlags.highMeshCount", int(flags.high_mesh_count), streams & 0x7)
        self.require_equal("MeshHeader.gpuBufferBase", int(header.gpu_buffer_base_offset),
                           int(mesh_buffer.vertex_buffer_offset))
        no_auxiliary = mesh.profile.ordinary_vertex_layout_has_no_auxiliary_elements

        def layouts(label: str, elements: object, auxiliary_elements: object) -> None:
            element_headers(f"{label} elements", elements, canonical=False, require_position_at_zero=True)
            if no_auxiliary:
                self.require_zero(f"{label} auxiliary element sets", auxiliary_elements)
                return
            for index, auxiliary in enumerate(split_auxiliary_layouts(auxiliary_elements)):
                element_headers(f"{label} auxiliary[{index}]", auxiliary, canonical=False,
                                require_position_at_zero=False)

        self.require_zero("resident BufferLayout extended-prefix reserve", mesh_buffer.extended_prefix_reserved_dwords)
        self.require_zero("resident BufferLayout.reserve", mesh_buffer.wilds_reserved)
        self.require_zero("resident BufferLayout.bufferIndex", mesh_buffer.buffer_index)
        self.require_zero(
            "resident BufferLayout shape-key-weight storage",
            (mesh_buffer.shapekey_weight_buffer_offset, mesh_buffer.shapekey_weight_buffer_size),
        )
        self.require_equal(
            "resident BufferLayout total size",
            int(mesh_buffer.total_buffer_size),
            int(mesh_buffer.vertex_buffer_size) + int(mesh_buffer.index_buffer_size),
        )
        layouts("resident BufferLayout", mesh_buffer.buffer_headers, mesh_buffer.extra_buffer_headers)
        for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
            field = f"streaming BufferLayout[{buffer_index}]"
            self.require_zero(f"{field} extended-prefix reserve", stream.extended_prefix_reserved_dwords)
            self.require_zero(f"{field}.reserve", stream.wilds_reserved)
            self.require_zero(
                f"{field} shape-key-weight storage",
                (stream.shapekey_weight_buffer_offset, stream.shapekey_weight_buffer_size),
            )
            self.require_equal(f"{field}.bufferIndex", int(buffer_index), int(stream.entry_index) + 1)
            self.require(mesh.streaming_info is not None, field, "requires StreamingInfo", None)
            entry = mesh.streaming_info.entries[int(stream.entry_index)]
            self.require_equal(f"{field}.vertexBufferOffset", int(stream.vertex_buffer_offset), int(entry.start))
            self.require_equal(f"{field}.unpaddedBufferSize", int(stream.unpadded_buffer_size), int(entry.size))
            layouts(field, stream.vertex_elements, stream.extra_vertex_elements)

    def assert_late_ordinary_runtime(self, mesh: object, mesh_buffer: object, element_headers) -> None:
        assert_mesh_semantic_domains(mesh)
        assert_late_normal_recalc_runtime_storage(mesh, self)
        self.assert_late_buffer_layouts(mesh, mesh_buffer, element_headers)
        flags = mesh.header.decoded_content_flags
        for buffer_index, payload in mesh_buffer.buffer_payloads.items():
            types = {int(item.type) for item in payload.buffer_headers}
            self.require_equal(f"MeshHeader.hasVertexColor/BufferLayout[{buffer_index}]", bool(flags.has_vertex_color),
                               5 in types)
            self.require_equal(f"MeshHeader.hasLightmapUV/BufferLayout[{buffer_index}]", bool(flags.has_lightmap_uv),
                               6 in types)

        for mesh_index, mesh_data in enumerate(mesh.meshes):
            self.assert_mesh_data_padding(mesh_index, mesh_data)
            self.assert_finite_ordered_aabb(f"MeshData[{mesh_index}].AABB", mesh_data.bounding_box[0],
                                            mesh_data.bounding_box[1])
            self.require(
                int(mesh_data.skin_weight_count_raw) in (0x19, 0x1B, 0x01, 0x03),
                f"MeshData[{mesh_index}].SkinWeightCount",
                "must select six/twelve influences, with or without the producer-owned upper tag",
                mesh_data.skin_weight_count_raw,
            )

    def assert_late_geometry_reserves(self, mesh: object) -> None:
        for owner, lod_index, lod in all_lods(mesh):
            self.require_zero(f"{owner}.MeshLOD[{lod_index}].vertexFormat", lod.vertex_format)
            self.require_zero(f"{owner}.MeshLOD[{lod_index}].reserve", lod.reserve)
        for group_index, group in enumerate(all_groups(mesh)):
            self.require_zero(f"MeshGroup[{group_index}].reserve", group.reserve)
            for submesh_index, submesh in enumerate(group.submeshes):
                field = f"MeshGroup[{group_index}].MeshCluster[{submesh_index}]"
                self.require_zero(f"{field}.reservedDword04", submesh.reserved_dword04)
                self.require_zero(f"{field}.streaming offsets", (submesh.streaming_offset, submesh.streaming_offset2))
                self.require_zero(f"{field}.trailing reserve", submesh.wilds_tail)
        pivots = getattr(mesh, "group_pivots", None)
        if pivots is not None:
            self.require_zero("GroupPivotLayout.reserve", pivots.reserve)
            self.require_equal("GroupPivotLayout.bufferSize", int(pivots.buffer_size), len(pivots.pivots) * 12)
        boxes = getattr(mesh, "bounding_boxes", None)
        if boxes is not None:
            self.require_zero("BoundingBoxLayout.reserve", boxes.reserve)
            self.require_equal("BoundingBoxLayout.count", int(boxes.count), len(boxes.boxes))
        self.assert_auxiliary_reserves(mesh)

    def assert_late_shared_runtime(self, mesh: object) -> None:
        self.assert_late_geometry_reserves(mesh)
        shadow = getattr(mesh, "shadow_mesh", None)
        if shadow is not None:
            self.require(
                int(shadow.skin_weight_count_raw) in (0x19, 0x1B, 0x01, 0x03),
                "ShadowMeshLayout.SkinWeightCount",
                "must select six/twelve influences, with or without the producer-owned upper tag",
                shadow.skin_weight_count_raw,
            )

    def assert_late_mply_import(self, mesh: object, exponent_max: int) -> None:
        mesh_buffer = mesh.mesh_buffer
        layout = mesh_buffer.meshlet_layout
        bvh = mesh_buffer.meshlet_bvh
        mheader = layout.header
        self.require(
            int(mheader.serialized_meshlet_raster_switch_threshold) in mesh.profile.canonical_mply_raster_thresholds,
            "MeshletHeader.MeshletRasterSwitchThreshold",
            "must use a corpus-authored enum value",
            mheader.serialized_meshlet_raster_switch_threshold,
        )
        for lod_index, factor in enumerate(mheader.packed_binary16_lod_factor_lanes):
            expected = float(lod_index) if lod_index < int(mheader.lod_num) else 0.0
            self.require_equal(f"MeshletHeader.lodFactor[{lod_index}]", float(factor), expected)
        pivot = layout.group_pivot_info
        self.require(int(pivot.blob_relative_offset) in (0, 0x90), "MPLY group-pivot descriptor offset",
                     "must use the shipped absent/0x90 encoding", pivot.blob_relative_offset)
        for lod_index, count in enumerate(bvh.cluster_counts):
            if int(count):
                exponent = int(bvh.cluster_bounds_scale_exponents[lod_index])
                self.require(0 <= exponent <= exponent_max, f"MeshletBVH.clusterBoundsScaleExponent[{lod_index}]",
                             f"must use the build's authored range [0, {exponent_max}]", exponent)
        for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
            field = f"MPLY chunk[{chunk_index}]"
            flags = chunk.flags
            for name, value in (("positionCompressLevel", flags.position_compress_level),
                                ("tangentHandednessMode", flags.tangent_handedness_mode)):
                self.require(int(value) in (0, 1, 2), f"{field}.{name}", "must use shipped mode 0, 1, or 2", value)
            self.require_zero(f"{field} producer-reserved bit 11", flags.producer_reserved_bit11)
            self.require(bool(flags.is_meshlet_use_vertex_color), f"{field} vertex-color declaration",
                         "must remain set in canonical output", False)
            self.require_zero(f"{field} runtime-unread face alignment residue", chunk.face_alignment_padding)

    def assert_late_ordinary_skin_counts(self, mesh: object) -> None:
        for mesh_index, mesh_data in enumerate(mesh.meshes):
            self.require(
                int(mesh_data.skin_weight_count_raw) in (0x19, 0x1B),
                f"MeshData[{mesh_index}].SkinWeightCount",
                "must use native 0x19 (six) or 0x1B (twelve)",
                mesh_data.skin_weight_count_raw,
            )
            self.require_equal(
                f"MeshData[{mesh_index}] AABB padding",
                (int(mesh_data.aabb_min_padding), int(mesh_data.aabb_max_padding)),
                (1, 1),
            )
        shadow = getattr(mesh, "shadow_mesh", None)
        if shadow is not None:
            self.require(
                int(shadow.skin_weight_count_raw) in (0x19, 0x1B),
                "ShadowMeshLayout.SkinWeightCount",
                "must use native 0x19 (six) or 0x1B (twelve)",
                shadow.skin_weight_count_raw,
            )

    def assert_packed_skin_payload(self, field: str, payload: object, influence_count: int) -> None:
        primary = self.attribute_records(payload, 4, 16)
        continuation = self.attribute_records(payload, 7, 16)
        self.require(len(primary) <= 1 and len(continuation) <= 1, field,
                     "must have at most one primary/continuation plane", (len(primary), len(continuation)))
        if not primary:
            self.require_zero(field + " continuation without primary", continuation)
            return
        self.require(influence_count in (6, 12), field + " skin layout owner",
                     "must explicitly select six or twelve influences", influence_count)
        self._require_clear_index_tags(field + " primary packed-index tags", primary[0])
        if influence_count == 6:
            self.require_zero(field + " six-weight continuation", continuation)
            # Runtime reads bytes 8-12 and reconstructs influence 5.
            self.require_zero(field + " primary runtime-unread bytes 13-15", primary[0][:, 13:16])
            return
        self.require_equal(field + " twelve-weight continuation count", len(continuation), 1)
        self._require_clear_index_tags(field + " continuation packed-index tags", continuation[0])
        # Runtime reads bytes 8-10 and reconstructs influence 11.
        self.require_zero(field + " continuation runtime-unread bytes 11-15", continuation[0][:, 11:16])

    def _require_clear_index_tags(self, field: str, records: np.ndarray) -> None:
        tags = records[:, :8].copy().view("<u4").reshape(-1, 2) >> 30
        self.require(bool(np.all(tags == 0)), field, "must be cleared by canonical construction",
                     sorted(set(map(int, tags.flat))))

    def assert_blend_shape_import_reserves(self, mesh: object) -> None:
        blend = getattr(mesh, "blend_shape_data", None)
        if blend is None:
            return
        self.require_zero("BlendShapeLayout.reserve", blend.layout_reserve)
        for body in blend.bodies:
            field = f"BlendShapeBody[{body.lod_index}]"
            self.require(int(body.compression_type) in mesh.profile.authored_blend_shape_codecs,
                         f"{field}.compressionType", "must use a blend-shape codec exercised in this build",
                         body.compression_type)
            self.require_zero(f"{field}.reserve", body.reserve)
            for target in (*body.targets, *body.none_targets):
                if target.aabb is not None:
                    self.require_zero(f"{field} target AABB W", (target.aabb.minimum_w, target.aabb.maximum_w))
            for channel in body.channels:
                for segment in channel.segments:
                    self.require_zero(f"BlendShape channel {channel.index} runtime-unread W lanes",
                                      (segment.position_w, segment.normal_w))

    def attribute_records(self, payload: object, semantic: int, record_size: int) -> list[np.ndarray]:
        records = []
        for stream in getattr(payload, "attribute_streams", ()):
            if int(stream.type) == semantic:
                raw = np.frombuffer(stream.data, dtype=np.uint8)
                self.require_equal(f"vertex semantic {semantic} record alignment", len(raw) % record_size, 0)
                records.append(raw.reshape(-1, record_size))
        return records

    def assert_finite_ordered_aabb(
        self,
        field: str,
        minimum: Sequence[float],
        maximum: Sequence[float],
    ) -> None:
        low = np.asarray(minimum[:3], dtype=np.float64)
        high = np.asarray(maximum[:3], dtype=np.float64)
        self.require(
            bool(
                low.shape == high.shape == (3,)
                and np.isfinite(low).all()
                and np.isfinite(high).all()
                and np.all(high >= low)
            ),
            field,
            "must have three finite, ordered axes",
            (tuple(low), tuple(high)),
        )

    def assert_element_headers(
        self,
        field: str,
        headers: Iterable[object],
        *,
        strides: Mapping[int, int],
        canonical: bool,
        require_position_at_zero: bool,
        unknown_semantic_expectation: str,
        forbidden_canonical_semantics: Mapping[int, str] | None = None,
        allow_unmodeled_noncanonical: bool = False,
        vertex_count: int | None = None,
    ) -> None:
        headers = tuple(headers)
        semantics = tuple(int(item.type) for item in headers)
        if canonical:
            self.require_equal(
                field + " semantic ordering",
                semantics,
                tuple(sorted(set(semantics))),
            )
        if require_position_at_zero:
            self.require(
                bool(
                    headers
                    and int(headers[0].type) == 0
                    and int(headers[0].offset) == 0
                ),
                field,
                "must begin with Position at byte offset zero",
                tuple(
                    (int(item.type), int(item.offset))
                    for item in headers
                ),
            )

        forbidden = forbidden_canonical_semantics or {}
        for item in headers:
            semantic = int(item.type)
            if canonical and semantic in forbidden:
                self.fail(
                    field + ".inputSlot",
                    forbidden[semantic],
                    semantic,
                )
            expected_stride = strides.get(semantic)
            if expected_stride is None:
                if allow_unmodeled_noncanonical and not canonical:
                    continue
                self.fail(
                    field + ".inputSlot",
                    unknown_semantic_expectation,
                    semantic,
                )
            self.require_equal(
                f"{field}[{semantic}].byteStride",
                int(item.size),
                expected_stride,
            )

        if vertex_count is not None and headers:
            expected_offset = (
                0 if require_position_at_zero else int(headers[0].offset)
            )
            for item in headers:
                semantic = int(item.type)
                self.require_equal(
                    f"{field}[{semantic}].byteOffset",
                    int(item.offset),
                    expected_offset,
                )
                expected_offset += int(item.size) * int(vertex_count)


def assert_canonical_layout_constants(
    validator: ContractValidator,
    mesh: object,
) -> None:
    profile = mesh.profile
    name_alignment = int(profile.name_string_data_alignment)
    name_offsets = tuple(int(value) for value in getattr(mesh, "name_offsets", ()))
    if name_offsets:
        validator.require_equal(
            "NameTable first string alignment",
            name_offsets[0] % name_alignment,
            0,
        )

    if getattr(mesh.header, "bones_offset", 0):
        hierarchy_alignment = int(profile.skeleton_hierarchy_alignment)
        remap_end = (
            int(mesh.header.bones_offset)
            + 48
            + int(mesh.bone_remap_count) * 2
        )
        expected_hierarchy = (
            remap_end + hierarchy_alignment - 1
        ) & -hierarchy_alignment
        validator.require_equal(
            "SkeletonLayout hierarchy alignment",
            int(mesh.skeleton_hierarchy_offset),
            expected_hierarchy,
        )

    boxes = getattr(mesh, "bounding_boxes", None)
    if boxes is not None:
        expected_w = float(profile.canonical_bounding_box_w)
        for box_index, box in enumerate(boxes.boxes):
            validator.require_equal(
                f"BoundingBoxLayout[{box_index}] homogeneous W",
                (float(box.minimum_w), float(box.maximum_w)),
                (expected_w, expected_w),
            )

    if not profile.optional_gpu_offsets_use_relative_null:
        return
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return
    owners = [(0, mesh_buffer)]
    owners.extend(
        sorted(
            (
                (int(buffer_index), owner)
                for buffer_index, owner in
                mesh_buffer.streaming_buffer_headers.items()
            ),
            key=lambda item: item[0],
        )
    )
    for buffer_index, owner in owners:
        label = f"BufferLayout[{buffer_index}]"
        null_offset = -int(owner.vertex_buffer_offset)
        if profile.has_wilds_buffer_tail:
            local = int(owner.normal_recalc_local_index_buffer_offset)
            redirect = int(owner.normal_recalc_redirected_list_offset)
            if not int(mesh.header.normal_recalc_offset):
                validator.require_equal(
                    label + " absent normal-work offsets",
                    (local, redirect),
                    (null_offset, null_offset),
                )
            else:
                validator.require(
                    (local == null_offset and redirect == null_offset)
                    or (local > 0 and redirect > local),
                    label + " normal-work offsets",
                    "must be one relative-null pair or one ordered active pair",
                    (local, redirect),
                )
        blend = int(owner.blend_shape_offset)
        if not int(mesh.header.blend_shapes_offset):
            validator.require_equal(
                label + " absent blend payload offset",
                blend,
                null_offset,
            )
        else:
            validator.require(
                blend == null_offset or blend > 0,
                label + " blend payload offset",
                "must be relative-null or positive",
                blend,
            )


def split_auxiliary_layouts(
    headers: Iterable[object],
) -> tuple[tuple[object, ...], ...]:
    layouts: list[tuple[object, ...]] = []
    current: list[object] = []
    for item in headers:
        if int(item.type) == 0 and current:
            layouts.append(tuple(current))
            current = []
        current.append(item)
    if current:
        layouts.append(tuple(current))
    return tuple(layouts)


def layer_groups(layer: object, *, unique_lods: bool = False) -> list[object]:
    """The occluder has no LODs; it holds its mesh groups itself."""
    lods = getattr(layer, "lods", (layer,))
    if unique_lods:
        lods = {id(lod): lod for lod in lods}.values()
    return [*lod.mesh_groups for lod in lods]


def layer_payloads(mesh, layer: str) -> dict[int, object]:
    if layer == "main":
        mesh_buffer = getattr(mesh, "mesh_buffer", None)
        return getattr(mesh_buffer, "buffer_payloads", {}) if mesh_buffer else {}
    return getattr(getattr(mesh, f"{layer}_mesh", None), "payloads", None) or {}


def layer_vertex_offset(mesh, layer: str, submesh: object) -> int:
    offset = int(submesh.verts_index_offset)
    if layer == "main":
        return offset
    owner = getattr(mesh, f"{layer}_mesh", None)
    if owner is None or getattr(owner, "shares_main_payload", False):
        return offset
    bases = getattr(owner, "vertex_bases_by_buffer", {})
    return offset - int(bases.get(int(submesh.buffer_index), 0))


def all_groups(mesh: object) -> list[object]:
    layers = (*getattr(mesh, "meshes", ()), getattr(mesh, "shadow_mesh", None), getattr(mesh, "occluder_mesh", None))
    return [*layer_groups(layer) for layer in layers if layer is not None]


def all_lods(mesh: object) -> list[tuple[str, int, object]]:
    lods: list[tuple[str, int, object]] = []
    for mesh_index, mesh_data in enumerate(getattr(mesh, "meshes", ())):
        lods.extend(
            (f"MeshData[{mesh_index}]", lod_index, lod)
            for lod_index, lod in enumerate(getattr(mesh_data, "lods", ()))
        )
    shadow = getattr(mesh, "shadow_mesh", None)
    if shadow is not None:
        lods.extend(
            ("ShadowMeshLayout", lod_index, lod)
            for lod_index, lod in enumerate(getattr(shadow, "lods", ()))
        )
    return lods


def ordinary_geometry(
    mesh: object,
) -> tuple[list[object], list[object], list[object]]:
    lods = [lod for _owner, _index, lod in all_lods(mesh)]
    groups = all_groups(mesh)
    submeshes = [
        *getattr(group, "submeshes", ())
        for group in groups
    ]
    return lods, groups, submeshes


def assert_zero_index_padding(mesh: object, validator: ContractValidator) -> None:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return
    owners = [
        (
            "MeshData",
            (
                *lod.mesh_groups
                for mesh_data in getattr(mesh, "meshes", ())
                for lod in mesh_data.lods
            ),
            mesh_buffer.buffer_payloads,
        )
    ]
    shadow = getattr(mesh, "shadow_mesh", None)
    if shadow is not None:
        owners.append(
            (
                "ShadowMeshLayout",
                (*lod.mesh_groups for lod in shadow.lods),
                shadow.payloads,
            )
        )
    occluder = getattr(mesh, "occluder_mesh", None)
    if occluder is not None:
        owners.append(("OccluderMeshLayout", occluder.mesh_groups, occluder.payloads))

    for owner, groups, payloads in owners:
        for group_index, group in enumerate(groups):
            for submesh_index, submesh in enumerate(group.submeshes):
                padding = int(submesh.index_padding_count)
                field = (
                    f"{owner}.MeshGroup[{group_index}]"
                    f".MeshCluster[{submesh_index}]"
                )
                validator.require(
                    padding >= 0, field + " index padding", "must be nonnegative", padding
                )
                if not padding:
                    continue
                payload = payloads.get(int(submesh.buffer_index))
                validator.require(
                    payload is not None,
                    field + " payload",
                    "must be loaded for canonical padding validation",
                    submesh.buffer_index,
                )
                values = payload.index_values
                start = int(submesh.faces_index_offset) + int(submesh.indices_count)
                end = start + padding
                validator.require(
                    0 <= start <= end <= len(values),
                    field + " index padding",
                    "must fit its topology-owned index payload",
                    (start, end, len(values)),
                )
                validator.require_zero(field + " index padding", values[start:end])


def assert_late_normal_recalc_runtime_storage(
    mesh: object,
    validator: ContractValidator,
) -> None:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return
    for buffer_index, payload in getattr(
        mesh_buffer,
        "buffer_payloads",
        {},
    ).items():
        local_offset = int(
            getattr(payload, "normal_recalc_local_index_buffer_offset", 0)
        )
        redirect_offset = int(
            getattr(payload, "normal_recalc_redirected_list_offset", 0)
        )
        face_words = tuple(
            getattr(payload, "normal_recalc_face_words", ())
        )
        redirect_words = tuple(
            getattr(payload, "normal_recalc_redirect_words", ())
        )
        slack = bytes(
            getattr(
                payload,
                "normal_recalc_runtime_ignored_capacity_slack",
                b"",
            )
        )
        payload_end = int(
            getattr(payload, "normal_recalc_payload_end", 0)
        )
        if not any(
            (
                local_offset,
                redirect_offset,
                len(face_words),
                len(redirect_words),
                len(slack),
                payload_end,
            )
        ):
            continue
        validator.require(
            local_offset > 0 and redirect_offset > 0,
            f"BufferLayout[{buffer_index}] normal-work offsets",
            "must both be present when normal-work storage is decoded",
            (local_offset, redirect_offset),
        )
        expected_runtime_prefix_end = (
            local_offset + len(face_words) * 4 + 15
        ) & ~15
        validator.require_equal(
            f"BufferLayout[{buffer_index}] normal redirect offset",
            redirect_offset,
            expected_runtime_prefix_end + len(slack),
        )
        validator.require_equal(
            f"BufferLayout[{buffer_index}] runtime-ignored normal capacity alignment",
            len(slack) % 16,
            0,
        )
        expected_payload_end = (
            redirect_offset + len(redirect_words) * 4 + 15
        ) & ~15
        validator.require_equal(
            f"BufferLayout[{buffer_index}] normal-work payload end",
            payload_end,
            expected_payload_end,
        )


def assert_late_normal_recalc_import_storage(
    mesh: object,
    validator: ContractValidator,
) -> None:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    if mesh_buffer is None:
        return
    for buffer_index, payload in getattr(
        mesh_buffer,
        "buffer_payloads",
        {},
    ).items():
        slack = bytes(
            getattr(
                payload,
                "normal_recalc_runtime_ignored_capacity_slack",
                b"",
            )
        )
        validator.require_equal(
            f"BufferLayout[{buffer_index}] runtime-ignored normal capacity size",
            len(slack),
            0,
        )

    normal = getattr(mesh, "normal_recalc_data", None)
    if normal is None:
        return

    topologies = normal_recalc.sf6_normal_recalc_lod_topologies(mesh)
    validator.require_equal(
        "NormalRecalcLayout canonical LOD count",
        len(normal.lods),
        int(normal.declared_lod_count),
    )
    validator.require(
        len(topologies) >= int(normal.declared_lod_count),
        "NormalRecalcLayout topology count",
        "must cover every declared normal LOD",
        (len(topologies), int(normal.declared_lod_count)),
    )
    for lod, topology in zip(
        normal.lods,
        topologies[: int(normal.declared_lod_count)],
    ):
        words = tuple(int(word) for word in lod.face_entries)
        cursor = 0
        for submesh_index, submesh in enumerate(topology.submeshes):
            count = len(submesh.indices)
            aligned_count = (count + 1) & ~1
            validator.require_zero(
                f"NormalRecalcLayout LOD[{lod.lod_index}] submesh "
                f"[{submesh_index}] runtime-unread alignment word",
                words[cursor + count : cursor + aligned_count],
            )
            cursor += aligned_count
        validator.require_equal(
            f"NormalRecalcLayout LOD[{lod.lod_index}] work-table size",
            len(words),
            cursor,
        )
        canonical_words = derive_sf6_normal_indices(
            NormalRecalcData(redirects=tuple(lod.redirects)),
            topology.submeshes,
        )
        validator.require_equal(
            f"NormalRecalcLayout LOD[{lod.lod_index}] canonical work-slot assignment",
            words,
            canonical_words,
        )


__all__ = [
    "ContractValidator",
    "all_groups",
    "all_lods",
    "assert_canonical_layout_constants",
    "assert_late_normal_recalc_import_storage",
    "assert_late_normal_recalc_runtime_storage",
    "assert_zero_index_padding",
    "has_nonzero",
    "layer_groups",
    "layer_payloads",
    "layer_vertex_offset",
    "ordinary_geometry",
    "split_auxiliary_layouts",
]
