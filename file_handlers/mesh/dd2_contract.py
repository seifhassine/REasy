from __future__ import annotations

import math
from functools import partial

import numpy as np

from utils.hash_util import murmur3_hash_utf16le

from .contract_utils import (
    ContractValidator,
    assert_canonical_layout_constants,
    assert_zero_index_padding,
    has_nonzero,
    ordinary_geometry,
)
from .models import MESH_MAGIC, MPLY_MAGIC


DD2_VERTEX_STRIDES = {
    0: 12,  # Position
    1: 8,   # Normal/tangent
    2: 4,   # UV0
    3: 4,   # UV1
    4: 16,  # Weight8
    5: 4,   # Color
}

_VALIDATOR = ContractValidator("DD2 canonical contract")
_fail = _VALIDATOR.fail
_require_zero = _VALIDATOR.require_zero
_require_equal = _VALIDATOR.require_equal


def _assert_blendshape_runtime_storage(mesh: object) -> None:
    blend_data = getattr(mesh, "blend_shape_data", None)
    if blend_data is None:
        return
    _require_zero("BlendShapeLayout.reserve", blend_data.layout_reserve)
    if blend_data.bone_modifier is not None:
        modifier = blend_data.bone_modifier
        _require_zero(
            "BlendShapeBoneModifierLayout.reserve",
            modifier.reserve,
        )
        payload = (
            getattr(mesh.mesh_buffer, "buffer_payloads", {}).get(0)
            if getattr(mesh, "mesh_buffer", None) is not None
            else None
        )
        positions = (
            np.asarray(payload.positions, dtype=np.float32).reshape(-1, 3)
            if payload is not None and len(payload.positions)
            else None
        )
        joint_count = int(getattr(mesh, "joint_count", len(mesh.bones)))
        for effector_index, effector in enumerate(modifier.effectors):
            if not 0 <= int(effector.joint_index) < joint_count:
                _fail(
                    f"BlendShapeBoneModifier[{effector_index}].jointIndex",
                    "must reference the mesh skeleton",
                    effector.joint_index,
                )
            indices = np.asarray(effector.vertex_indices, dtype=np.int64)
            references = np.asarray(
                effector.vertex_positions,
                dtype=np.float32,
            ).reshape(-1, 3)
            _require_equal(
                f"BlendShapeBoneModifier[{effector_index}] index/reference count",
                len(indices),
                len(references),
            )
            _require_equal(
                f"BlendShapeBoneModifier[{effector_index}] unique indices",
                len(set(map(int, indices))),
                len(indices),
            )
            if not np.isfinite(references).all():
                _fail(
                    f"BlendShapeBoneModifier[{effector_index}].referencePositions",
                    "must be finite",
                    "nonfinite data",
                )
            if positions is not None:
                if np.any(indices < 0) or np.any(indices >= len(positions)):
                    _fail(
                        f"BlendShapeBoneModifier[{effector_index}].vertexIndices",
                        "must reference resident vertices",
                        tuple(map(int, indices)),
                    )
    for body in blend_data.bodies:
        _require_zero(
            f"BlendShapeBody[{body.lod_index}].reserve",
            body.reserve,
        )
        for target_index, target in enumerate((*body.targets, *body.none_targets)):
            if target.aabb is None:
                continue
            _require_zero(
                f"BlendShapeBody[{body.lod_index}].target[{target_index}].AABB.w",
                (target.aabb.minimum_w, target.aabb.maximum_w),
            )


def _assert_meshlet_runtime_contract(mesh: object, mesh_buffer: object) -> None:
    header = mesh.header
    _require_zero(
        "MeshHeader.MicroPolygon reserved union pointer",
        header.micro_polygon_reserved_offset,
    )
    layout = mesh_buffer.meshlet_layout
    bvh = mesh_buffer.meshlet_bvh
    parts = mesh_buffer.meshlet_parts_layout
    if layout is None or bvh is None:
        _fail("MPLY", "must contain MeshletLayout and MeshletBVH", None)

    _require_zero("MeshletLayout.reserve", layout.reserved)
    if not layout.gpu_header_matches_resident:
        _fail(
            "MeshletLayout GPU header mirror",
            "must equal the resident MeshletHeader byte-for-byte",
            False,
        )
    _require_equal(
        "MeshHeader.onMemorySize",
        header.on_memory_size,
        layout.gpu_meshlet_offset,
    )

    meshlet_header = layout.header
    if not 1 <= meshlet_header.lod_num <= 8:
        _fail("MeshletHeader.lodNum", "must be in [1, 8]", meshlet_header.lod_num)
    for field in ("loaded_lod", "resident_lod", "allocation_lod"):
        _require_zero(
            f"serialized MeshletHeader.{field}",
            getattr(meshlet_header, field),
        )
    _require_equal(
        "MeshletHeader.validLodBits",
        meshlet_header.valid_lod_bits,
        (1 << meshlet_header.lod_num) - 1,
    )

    for lod_index in range(8):
        active = lod_index < meshlet_header.lod_num
        cluster_stream_offset = meshlet_header.lod_cluster_offsets[lod_index]
        bindless_offset = meshlet_header.bindless_geometry_offsets[lod_index]
        if active:
            if cluster_stream_offset == 0:
                _fail(
                    f"MeshletHeader.lodClustersOffset[{lod_index}]",
                    "must be nonzero for an active LOD",
                    cluster_stream_offset,
                )
            if bindless_offset == 0:
                _fail(
                    f"MeshletHeader.bindlessGeometryOffset[{lod_index}]",
                    "must be nonzero for an active LOD",
                    bindless_offset,
                )
            factor = meshlet_header.active_lod_factors[lod_index]
            if not math.isfinite(factor) or factor < 0.0:
                _fail(
                    f"MeshletHeader.lodFactor[{lod_index}]",
                    "must be finite and nonnegative",
                    factor,
                )
        else:
            _require_zero(
                f"MeshletHeader.lodClustersOffset[{lod_index}]",
                cluster_stream_offset,
            )
            _require_zero(
                f"MeshletHeader.bindlessGeometryOffset[{lod_index}]",
                bindless_offset,
            )

    expected_cluster_offset = 0
    for lod_index in range(8):
        count = bvh.cluster_counts[lod_index]
        offset = bvh.cluster_offsets[lod_index]
        if lod_index < meshlet_header.lod_num:
            if count <= 0:
                _fail(
                    f"MeshletBVH.clusterCount[{lod_index}]",
                    "must be positive for an active LOD",
                    count,
                )
            _require_equal(
                f"MeshletBVH.clusterOffset[{lod_index}]",
                offset,
                expected_cluster_offset,
            )
            expected_cluster_offset += count * 16
        else:
            _require_zero(f"MeshletBVH.clusterCount[{lod_index}]", count)
            _require_zero(f"MeshletBVH.clusterOffset[{lod_index}]", offset)
    if bvh.gpu_cluster_quantize_offset:
        _require_equal(
            "MeshletBVH.QuantizeAABBCenter count",
            len(bvh.quantized_aabb_centers),
            sum(bvh.cluster_counts),
        )
    else:
        _require_zero(
            "Meshlet compacted-cluster AABB-center offsets",
            [item.aabb_center_offset for item in mesh_buffer.mply_cluster_headers],
        )

    if parts is not None:
        _require_zero("MeshletPartsLayout.reserve", parts.reserve)
        _require_equal(
            "MeshletPartsLayout.partsNum",
            parts.parts_num,
            len(parts.parts_indices_raw),
        )

        lod0_part_ids = []
        if mesh.meshes and mesh.meshes[0].lods:
            for group in mesh.meshes[0].lods[0].mesh_groups:
                part_id = int(group.part_id)
                if part_id not in lod0_part_ids:
                    lod0_part_ids.append(part_id)
        _require_equal(
            "MPLY implicit part-hash ID domain",
            tuple(map(int, mesh.parts_name_hash_ids)),
            tuple(lod0_part_ids),
        )
        _require_equal(
            "MPLY implicit part hashes",
            tuple(map(int, mesh.parts_name_hashes)),
            tuple(
                murmur3_hash_utf16le(f"Part {part_id}")
                for part_id in lod0_part_ids
            ),
        )

    clusters = mesh_buffer.mply_cluster_headers
    chunks = mesh_buffer.mply_chunks
    _require_equal("MPLY cluster/chunk count", len(clusters), len(chunks))
    for chunk_index, (cluster, chunk) in enumerate(zip(clusters, chunks)):
        _require_zero(
            f"MPLY cluster[{chunk_index}] material-flag extension",
            cluster.meshlet_material_flag_extension,
        )
        _require_zero(
            f"MPLY cluster[{chunk_index}] reserved bit 31",
            cluster.reserved_bit31,
        )
        flags = chunk.flags
        if flags is None:
            _fail(f"MPLY chunk[{chunk_index}].flags", "must be decoded", None)
        _require_zero(
            f"MPLY chunk[{chunk_index}] trailing payload",
            chunk.trailing_payload,
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] position selector",
            flags.position_compress_level,
            cluster.position_compress_level,
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] vertex count",
            chunk.vert_count,
            cluster.vertex_count,
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] index count",
            len(chunk.faces),
            cluster.index_count,
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] material ID",
            chunk.material_id,
            cluster.material_id,
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] part ID",
            chunk.part_id,
            cluster.part_id,
        )
        for attribute_name, attribute in (
            ("normal", chunk.normals),
            ("normal.w", chunk.normal_ws),
            ("tangent", chunk.tangents),
            ("tangent.w", chunk.tangent_ws),
        ):
            _require_equal(
                f"MPLY chunk[{chunk_index}] {attribute_name} count",
                len(attribute),
                chunk.vert_count,
            )
        vectors = np.concatenate((chunk.normals, chunk.tangents)).astype(np.float32)
        if vectors.size and (
            not np.isfinite(vectors).all()
            or np.any(vectors < -1.0)
            or np.any(vectors > 1.0)
        ):
            _fail(
                f"MPLY chunk[{chunk_index}] normal/tangent SNORM",
                "must use finite R8_SNORM values in [-1, 1]",
                "out-of-range data",
            )
        if flags.is_meshlet_no_tangent:
            _require_equal(
                f"MPLY chunk[{chunk_index}] no-tangent default XYZ",
                chunk.tangents.shape == (chunk.vert_count, 3) and bool(np.all(chunk.tangents == 1.0)),
                True,
            )
            _require_equal(
                f"MPLY chunk[{chunk_index}] no-tangent default W",
                chunk.tangent_ws.shape == (chunk.vert_count,) and bool(np.all(chunk.tangent_ws == 127)),
                True,
            )


def assert_dd2_runtime_contract(mesh: object) -> None:
    header = mesh.header
    source_size = int(getattr(mesh, "source_size", 0))
    if source_size:
        _require_equal("MeshHeader.fileSize", header.file_size, source_size)
    if header.on_memory_size > header.file_size:
        _fail(
            "MeshHeader.onMemorySize",
            "must not exceed fileSize",
            (header.on_memory_size, header.file_size),
        )
    _require_zero(
        "MeshHeader content-flags storage byte 3",
        header.content_flags_storage_raw >> 24,
    )
    _require_zero("MeshHeader.alignmentPadding", header.header_pad)
    _require_zero("MeshHeader.lightMapPadding", header.light_map_padding)
    flags = header.decoded_content_flags
    if flags is None:
        _fail("MeshHeader.contentFlags", "must be decoded", None)
    _require_zero("MeshHeader.contentFlags.reserved", flags.reserved_bits)
    _require_equal(
        "MeshHeader.contentFlags.streamingBVH",
        flags.streaming_bvh,
        header.magic == MPLY_MAGIC,
    )
    if mesh.streaming_info is not None:
        _require_zero("StreamingInfo.pad", mesh.streaming_info.pad)

    mesh_buffer = mesh.mesh_buffer
    if mesh_buffer is not None:
        _require_zero("BufferLayout.reserve", mesh_buffer.reserved)
        if header.magic == MESH_MAGIC:
            _require_zero("resident BufferLayout.chunk", mesh_buffer.chunk)
            if mesh_buffer.index_buffer_offset_size > mesh_buffer.universal_buffer_size:
                _fail(
                    "BufferLayout.indexBufferOffsetSize",
                    "must not exceed universalBufferSize",
                    (
                        mesh_buffer.index_buffer_offset_size,
                        mesh_buffer.universal_buffer_size,
                    ),
                )
            offset = mesh_buffer.additional_vertex_weights_offset
            size = mesh_buffer.additional_vertex_weights_size
            if bool(offset) != bool(size):
                _fail(
                    "BufferLayout.additionalVertexWeights offset/size",
                    "must either both be zero or both be nonzero",
                    (offset, size),
                )
            if size % 16:
                _fail(
                    "BufferLayout.additionalVertexWeightsSize",
                    "must be a multiple of 16",
                    size,
                )
            resident_count = mesh_buffer.vertex_counts_by_buffer.get(0, 0)
            if size and resident_count:
                _require_equal(
                    "BufferLayout.additionalVertexWeightsSize",
                    size,
                    resident_count * 16,
                )
            if source_size and size and offset + size > source_size:
                _fail(
                    "BufferLayout.additionalVertexWeights range",
                    "must fit inside the primary resource",
                    (offset, size, source_size),
                )
        for stream_index, stream_header in enumerate(
            mesh_buffer.streaming_buffer_headers.values()
        ):
            _require_zero(
                f"streaming BufferLayout[{stream_index}].reserve",
                stream_header.reserved,
            )
            _require_equal(
                f"streaming BufferLayout[{stream_index}].chunk",
                stream_header.chunk,
                stream_header.entry_index + 1,
            )
            if (
                stream_header.index_buffer_offset_size
                > stream_header.universal_buffer_size
            ):
                _fail(
                    f"streaming BufferLayout[{stream_index}].indexBufferOffsetSize",
                    "must not exceed universalBufferSize",
                    (
                        stream_header.index_buffer_offset_size,
                        stream_header.universal_buffer_size,
                    ),
                )
        expected_buffer_count = 1 + len(mesh_buffer.streaming_buffer_headers)
        _require_equal(
            "MeshHeader.contentFlags.bufferCount",
            flags.buffer_count,
            expected_buffer_count,
        )
        if flags.high_mesh_count is not None:
            _require_equal(
                "MeshHeader.contentFlags.highMeshCount",
                flags.high_mesh_count,
                len(mesh_buffer.streaming_buffer_headers),
            )

    for mesh_index, mesh_data in enumerate(mesh.meshes):
        _require_zero(f"MeshData[{mesh_index}].reserve", mesh_data.reserve)
        _require_zero(f"MeshData[{mesh_index}].AABB chunk", mesh_data.chunk)
        _VALIDATOR.assert_finite_ordered_aabb(
            f"MeshData[{mesh_index}].AABB", *mesh_data.bounding_box
        )

    lods, groups, submeshes = ordinary_geometry(mesh)
    is_mply = header.magic == MPLY_MAGIC
    for lod_index, lod in enumerate(lods):
        _require_zero(f"MeshLOD[{lod_index}].reserve", lod.reserve)
        if not math.isfinite(float(lod.lod_factor)) or lod.lod_factor < 0.0:
            _fail(
                f"MeshLOD[{lod_index}].lodFactor",
                "must be finite and nonnegative",
                lod.lod_factor,
            )
    for group_index, group in enumerate(groups):
        _require_zero(f"MeshGroup[{group_index}].reserve", group.reserve)
        _require_equal(
            f"MeshGroup[{group_index}].clusterCount",
            len(group.submeshes),
            int(group.cluster_count),
        )
        _require_equal(
            f"MeshGroup[{group_index}].vertexCount",
            sum(int(submesh.vert_count) for submesh in group.submeshes),
            int(group.vertex_count),
        )
        if is_mply:
            _require_equal(
                f"synthetic MPLY MeshGroup[{group_index}].indexCount",
                sum(int(submesh.indices_count) for submesh in group.submeshes),
                int(group.face_count),
            )
        else:
            _require_equal(
                f"MeshGroup[{group_index}].indexCount",
                sum(
                    int(submesh.allocated_index_count)
                    for submesh in group.submeshes
                ),
                int(group.face_count),
            )
            for cluster_index, (submesh, next_submesh) in enumerate(
                zip(group.submeshes, group.submeshes[1:])
            ):
                _require_equal(
                    f"MeshGroup[{group_index}].MeshCluster[{cluster_index + 1}]"
                    ".baseVertex",
                    int(next_submesh.verts_index_offset),
                    int(submesh.verts_index_offset) + int(submesh.vert_count),
                )
                _require_equal(
                    f"MeshGroup[{group_index}].MeshCluster[{cluster_index + 1}]"
                    ".startIndex",
                    int(next_submesh.faces_index_offset),
                    int(submesh.faces_index_offset)
                    + int(submesh.allocated_index_count),
                )
    for cluster_index, submesh in enumerate(submeshes):
        _require_zero(f"MeshCluster[{cluster_index}].reserve", submesh.reserve)
        _require_zero(
            f"MeshCluster[{cluster_index}] streaming offsets",
            (submesh.streaming_offset, submesh.streaming_offset2),
        )

    if mesh.group_pivots is not None:
        _require_zero("GroupPivotLayout.reserve", mesh.group_pivots.reserve)
        _require_equal(
            "GroupPivotLayout size/offset presence",
            bool(mesh.group_pivots.buffer_size),
            bool(mesh.group_pivots.buffer_offset),
        )
        pivot_count = len(mesh.group_pivots.pivots)
        if mesh.group_pivots.buffer_size != pivot_count * 12:
            _fail(
                "GroupPivotLayout.bufferSize",
                "must describe a complete float3 table",
                mesh.group_pivots.buffer_size,
            )
        if not np.isfinite(
            np.asarray(mesh.group_pivots.pivots, dtype=np.float32)
        ).all():
            _fail("GroupPivotLayout.pivots", "must be finite", "nonfinite data")
        if mesh_buffer is not None:
            for buffer_index, payload in mesh_buffer.buffer_payloads.items():
                selectors = np.asarray(payload.pivot_indices, dtype=np.uint16)
                if selectors.size and int(selectors.max()) >= pivot_count:
                    _fail(
                        f"BufferLayout[{buffer_index}] normal.w pivot selector",
                        "must index the mesh GroupPivotLayout",
                        (int(selectors.max()), pivot_count),
                    )
    if mesh.bounding_boxes is not None:
        _require_zero("BoundingBoxLayout.reserve", mesh.bounding_boxes.reserve)
        _require_equal(
            "BoundingBoxLayout.count",
            mesh.bounding_boxes.count,
            len(mesh.bounding_boxes.boxes),
        )
        for box_index, box in enumerate(mesh.bounding_boxes.boxes):
            _VALIDATOR.assert_finite_ordered_aabb(
                f"BoundingBoxLayout.box[{box_index}]", box.minimum, box.maximum
            )
    _VALIDATOR.assert_auxiliary_reserves(mesh)
    _assert_blendshape_runtime_storage(mesh)

    if header.magic == MPLY_MAGIC and mesh_buffer is not None:
        _assert_meshlet_runtime_contract(mesh, mesh_buffer)


def _assert_additional_weight_reserves(mesh_buffer: object) -> None:
    for buffer_index, payload in mesh_buffer.buffer_payloads.items():
        raw = payload.additional_vertex_weight_bytes
        if not raw:
            continue
        vertex_count = int(payload.vertex_count)
        if len(raw) != vertex_count * 16:
            _fail(
                f"additional vertex weights[{buffer_index}]",
                "must contain exactly one 16-byte record per topology vertex",
                (len(raw), vertex_count),
            )
        for vertex_index in range(vertex_count):
            record = raw[vertex_index * 16 : vertex_index * 16 + 16]
            if record[15] != 0:
                _fail(
                    f"additional vertex weights[{buffer_index}][{vertex_index}].runtime-ignored final weight byte",
                    "must be cleared by canonical construction",
                    record[15],
                )


_assert_canonical_element_headers = partial(
    _VALIDATOR.assert_element_headers,
    strides=DD2_VERTEX_STRIDES,
    canonical=True,
    unknown_semantic_expectation=(
        "must be a DD2 corpus-observed vertex semantic"
    ),
)


def _assert_import_zero_producer_lanes(mesh: object) -> None:
    blend_data = mesh.blend_shape_data
    if blend_data is not None:
        for body in blend_data.bodies:
            for channel in body.channels:
                for segment in channel.segments:
                    for name in ("position_w", "normal_w", "deform_percent"):
                        value = getattr(segment, name, None)
                        if has_nonzero(value):
                            _fail(
                                f"blendshape channel {channel.index}.{name}",
                                "must be zero for canonical import because DD2 runtime ignores it",
                                "nonzero producer data",
                            )

    _lods, _groups, submeshes = ordinary_geometry(mesh)
    for cluster_index, submesh in enumerate(submeshes):
        _require_zero(
            f"MeshCluster[{cluster_index}].runtimeIgnoredProducerResidue",
            submesh.runtime_ignored_tail_residue,
        )


def assert_dd2_import_safe(mesh: object) -> None:
    assert_dd2_runtime_contract(mesh)
    assert_canonical_layout_constants(_VALIDATOR, mesh)
    header = mesh.header
    flags = header.decoded_content_flags
    is_mply = header.magic == MPLY_MAGIC

    _require_zero("MeshHeader.lightMapMetadata", header.light_map_metadata_raw)
    _require_zero(
        "MeshHeader light-map fields",
        (
            header.light_map_reserved,
            header.light_map_used,
            header.light_map_width,
            header.light_map_height,
        ),
    )
    _require_zero("MeshHeader.contentFlags.solvedOffset", flags.solved_offset)
    _require_equal("MeshHeader.contentFlags.hasVertexColor", flags.has_vertex_color, True)
    for name in (
        "quad_enable",
        "has_tertiary_uv",
        "has_lightmap_uv",
        "use_ray_tracing_vertex_animation",
        "merge_static_mesh_bvh",
    ):
        _require_zero(
            f"MeshHeader.contentFlags.{name}",
            getattr(flags, name),
        )
    _require_equal(
        "MeshHeader.contentFlags.useSDF",
        flags.use_sdf,
        bool(header.sdf_path_offset),
    )
    _VALIDATOR.assert_content_policy(flags, is_mply)

    mesh_buffer = mesh.mesh_buffer
    if mesh_buffer is not None:
        _VALIDATOR.assert_ignored_weight_bytes_zero(mesh_buffer)
        _assert_additional_weight_reserves(mesh_buffer)
        for stream_index, stream_header in enumerate(
            mesh_buffer.streaming_buffer_headers.values()
        ):
            _require_zero(
                f"streaming BufferLayout[{stream_index}] additional weights",
                (
                    stream_header.additional_vertex_weights_offset,
                    stream_header.additional_vertex_weights_size,
                ),
            )
            _require_equal(
                f"streaming BufferLayout[{stream_index}] element counts",
                stream_header.total_vertex_element_count,
                stream_header.main_vertex_element_count,
            )
            _require_equal(
                f"streaming BufferLayout[{stream_index}] auxiliary index offsets",
                stream_header.shadow_mesh_index_buffer_offset,
                stream_header.occluder_mesh_index_buffer_offset,
            )

    expected_skin_count = 0 if is_mply else 0x12
    expected_aabb_padding = 0 if is_mply else 1
    for mesh_index, mesh_data in enumerate(mesh.meshes):
        _require_equal(
            f"MeshData[{mesh_index}].skinWeightCount",
            mesh_data.skin_weight_count_raw,
            expected_skin_count,
        )
        _require_equal(
            f"MeshData[{mesh_index}].AABB minimum padding",
            mesh_data.aabb_min_padding,
            expected_aabb_padding,
        )
        _require_equal(
            f"MeshData[{mesh_index}].AABB maximum padding",
            mesh_data.aabb_max_padding,
            expected_aabb_padding,
        )
        _require_equal(
            f"MeshData[{mesh_index}].has32BitIndexBuffer",
            mesh_data.has_32bit_index_buffer,
            False,
        )
        for lod_index, lod in enumerate(mesh_data.lods):
            _require_zero(
                f"MeshData[{mesh_index}].LOD[{lod_index}].vertexFormat",
                lod.vertex_format,
            )
            if lod.lod_level != lod_index:
                _fail(
                    f"MeshData[{mesh_index}].LOD[{lod_index}].lodLevel",
                    "must equal its canonical LOD index",
                    lod.lod_level,
                )
            for group_index, group in enumerate(lod.mesh_groups):
                for cluster_index, submesh in enumerate(group.submeshes):
                    _require_zero(
                        "MeshCluster.isQuad "
                        f"[{mesh_index}][{lod_index}][{group_index}]"
                        f"[{cluster_index}]",
                        submesh.is_quad,
                    )
    if mesh.occluder_mesh is not None or header.occluder_mesh_offset:
        _fail(
            "OccluderMeshLayout",
            "must remain absent because neither complete DD2 corpus contains one",
            header.occluder_mesh_offset,
        )
    if mesh.shadow_mesh is not None:
        _require_equal(
            "ShadowMeshLayout.skinWeightCount",
            mesh.shadow_mesh.skin_weight_count,
            0x12,
        )
        _require_equal("ShadowMeshLayout LOD count", len(mesh.shadow_mesh.lods), 1)
        _require_equal(
            "ShadowMeshLayout.has32BitIndexBuffer",
            mesh.shadow_mesh.has_32bit_index_buffer,
            False,
        )
        shadow_lod = mesh.shadow_mesh.lods[0]
        _require_equal("Shadow MeshLOD group count", len(shadow_lod.mesh_groups), 1)
        _require_zero("Shadow MeshLOD vertexFormat", shadow_lod.vertex_format)
        _require_zero("Shadow MeshLOD lodLevel", shadow_lod.lod_level)
        _require_zero("Shadow MeshGroup partId", shadow_lod.mesh_groups[0].part_id)

    blend_data = mesh.blend_shape_data
    if blend_data is not None:
        for body in blend_data.bodies:
            _require_equal(
                f"BlendShapeBody[{body.lod_index}].compressionType",
                body.compression_type,
                1,
            )
            _require_zero(
                f"BlendShapeBody[{body.lod_index}].unvisualizedPayload",
                body.raw_unvisualized_payload,
            )
            for target_index, target in enumerate(body.targets):
                _require_equal(
                    f"BlendShapeBody[{body.lod_index}].target[{target_index}].channelCount",
                    target.channel_count,
                    1,
                )
                _require_equal(
                    f"BlendShapeBody[{body.lod_index}].target[{target_index}].isBlendShape",
                    target.is_blend_shape,
                    True,
                )
            for target_index, target in enumerate(body.none_targets):
                label = (
                    f"BlendShapeBody[{body.lod_index}]"
                    f".noneTarget[{target_index}]"
                )
                _require_equal(
                    f"{label} inactive fields",
                    (
                        target.channel_location,
                        target.channel_count,
                        target.same_target_id,
                        target.is_blend_shape,
                    ),
                    (0, 0, 0, False),
                )
                _require_equal(f"{label}.AABB", target.aabb, None)
                for range_index, target_range in enumerate(target.ranges):
                    _require_zero(
                        f"{label}.range[{range_index}] payload/tag",
                        (
                            target_range.blend_shape_location,
                            target_range.tag,
                        ),
                    )

    if not is_mply:
        if mesh_buffer is None:
            _fail("BufferLayout", "must be present for ordinary MESH", None)
        _assert_canonical_element_headers(
            "resident BufferLayout elements",
            mesh_buffer.buffer_headers,
            require_position_at_zero=True,
            vertex_count=int(getattr(mesh_buffer.buffer_payloads.get(0), "vertex_count", 0)),
        )
        _assert_canonical_element_headers(
            "resident BufferLayout auxiliary elements",
            mesh_buffer.extra_buffer_headers,
            require_position_at_zero=False,
        )
        for stream_index, stream_header in enumerate(
            mesh_buffer.streaming_buffer_headers.values()
        ):
            _assert_canonical_element_headers(
                f"streaming BufferLayout[{stream_index}] elements",
                stream_header.vertex_elements,
                require_position_at_zero=True,
                vertex_count=int(getattr(mesh_buffer.buffer_payloads.get(stream_header.buffer_index),
                                         "vertex_count", 0)),
            )
            _assert_canonical_element_headers(
                f"streaming BufferLayout[{stream_index}] auxiliary elements",
                stream_header.extra_vertex_elements,
                require_position_at_zero=False,
            )
        element_types = {
            int(item.type)
            for item in mesh_buffer.buffer_headers
        }
        for field, expected in (
            ("is_skinning", 4 in element_types),
            ("has_joint", bool(header.bones_offset)),
            ("has_blend_shape", bool(header.blend_shapes_offset)),
            ("has_vertex_group", bool(header.floats_offset)),
            ("has_tertiary_uv", 6 in element_types),
            ("has_vertex_color", 5 in element_types),
        ):
            _require_equal(
                f"MeshHeader.contentFlags.{field}",
                getattr(flags, field),
                expected,
            )
        _require_equal(
            "MeshHeader.contentFlags.highMeshCount",
            flags.high_mesh_count,
            len(mesh_buffer.streaming_buffer_headers),
        )
        assert_zero_index_padding(mesh, _VALIDATOR)
        _assert_import_zero_producer_lanes(mesh)
        return

    _require_equal(
        "MPLY MeshHeader.contentFlags.hasJoint",
        flags.has_joint,
        bool(header.bones_offset),
    )
    for name in (
        "has_blend_shape",
        "has_vertex_group",
    ):
        _require_zero(
            f"MPLY MeshHeader.contentFlags.{name}",
            getattr(flags, name),
        )

    has_skin_storage = any(
        bool(chunk.skin_weights)
        for chunk in (mesh_buffer.mply_chunks if mesh_buffer is not None else ())
    )
    if flags.is_skinning and not has_skin_storage:
        _fail(
            "MPLY MeshHeader.contentFlags.isSkinning",
            "may only enable skinning when a meshlet skin stream exists",
            flags.is_skinning,
        )

    for field in (
        "blend_shapes_offset",
        "buffer_layout_offset",
        "blend_shape_bone_modifier_offset",
        "floats_offset",
        "blend_shape_indices_offset",
        "parts_name_hash_offset",
    ):
        _require_zero(f"MPLY MeshHeader.{field}", getattr(header, field))

    if mesh_buffer is None:
        _fail("MPLY", "must contain decoded meshlet data", None)
    bvh = mesh_buffer.meshlet_bvh
    _require_zero(
        "MeshletBVH.gpuClusterQuantizeOffset",
        bvh.gpu_cluster_quantize_offset,
    )
    _require_zero("MeshletBVH quantized centers", bvh.quantized_aabb_centers)
    _require_zero("MeshletBVH.meshletBVHOffset", bvh.meshlet_bvh_offsets)

    parts = mesh_buffer.meshlet_parts_layout
    if parts is None:
        _fail("MeshletPartsLayout", "must be present", None)
    part_ids = parts.parts_indices
    _require_equal(
        "MeshletPartsLayout.partsIndices canonical ordering",
        part_ids,
        tuple(sorted(part_ids)),
    )
    _require_equal(
        "MeshletPartsLayout.partsIndices unique cluster-part set",
        set(part_ids),
        {item.part_id for item in mesh_buffer.mply_cluster_headers},
    )
    _require_equal(
        "MeshletPartsLayout descriptor placement",
        header.meshlet_parts_offset,
        (parts.parts_indices_offset + parts.parts_num + 15) & ~15,
    )
    _require_zero(
        "MeshletPartsLayout.partsIndices alignment padding",
        parts.alignment_padding,
    )

    layout = mesh_buffer.meshlet_layout
    for lod_index, word in enumerate(
        layout.header.lod_factor_or_cpu_cluster_offset_size_words[
            layout.header.lod_num :
        ],
        start=layout.header.lod_num,
    ):
        _require_zero(f"MeshletHeader inactive lodFactor[{lod_index}]", word)

    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        flags = chunk.flags
        _require_equal(
            f"MPLY chunk[{chunk_index}] positionCompressLevel",
            flags.position_compress_level,
            0,
        )
        if not 125 <= flags.position_scale_exponent <= 139:
            _fail(
                f"MPLY chunk[{chunk_index}] positionScaleExponent",
                "must stay within the observed canonical range [125, 139]",
                flags.position_scale_exponent,
            )
        _require_zero(
            f"MPLY chunk[{chunk_index}] UV3 flags",
            (
                flags.is_meshlet_compressed_texcoord3,
                flags.is_meshlet_use_texcoord3,
            ),
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] vertex-color flag",
            flags.is_meshlet_use_vertex_color,
            True,
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] producer bit 7",
            flags.shader_unused_producer_bit7,
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] shader-unread bits 14-15",
            flags.shader_unused_bits14_15,
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] shader-unread upper byte",
            flags.shader_unused_upper_byte,
        )
        raw_skin = chunk.skin_record_bytes
        for record_index in range(0, len(raw_skin), 16):
            record = raw_skin[record_index : record_index + 16]
            if len(record) != 16:
                _fail(
                    f"MPLY chunk[{chunk_index}] skin record",
                    "must be exactly 16 bytes",
                    len(record),
                )
            _require_zero(
                f"MPLY chunk[{chunk_index}] skin record[{record_index // 16}] "
                "runtime-ignored eighth weight",
                record[15],
            )

    _assert_import_zero_producer_lanes(mesh)


__all__ = [
    "assert_dd2_import_safe",
    "assert_dd2_runtime_contract",
]
