from __future__ import annotations

import math
from functools import partial
from typing import Iterable

lazy import numpy as np

lazy from .blend_shape import lod_map_allocation
from .contract_utils import (
    ContractValidator,
    assert_canonical_layout_constants,
    assert_late_normal_recalc_import_storage,
    assert_late_normal_recalc_runtime_storage,
    has_nonzero,
    ordinary_geometry,
)
from .models import MESH_MAGIC, MPLY_MAGIC


MHWILDS_VERTEX_STRIDES = {
    0: 12,  # Position: float3
    1: 8,   # Normal/tangent: two R8G8B8A8_SNORM records
    2: 4,   # UV0: half2
    3: 4,   # UV1: half2
    4: 16,  # Primary packed skin record
    5: 4,   # Color: R8G8B8A8_UNORM
    6: 4,   # UV2/light-map UV: half2
    7: 16,  # Twelve-influence continuation skin record
}

_VALIDATOR = ContractValidator("Monster Hunter Wilds canonical contract")
_fail = _VALIDATOR.fail
_require_zero = _VALIDATOR.require_zero
_require_equal = _VALIDATOR.require_equal


_assert_element_headers = partial(
    _VALIDATOR.assert_element_headers,
    strides=MHWILDS_VERTEX_STRIDES,
    unknown_semantic_expectation=(
        "must be a Wilds corpus-observed vertex semantic"
    ),
    allow_unmodeled_noncanonical=True,
)


def _assert_group_topology(
    mesh: object,
    groups: Iterable[object],
    *,
    is_mply: bool,
    canonical: bool,
) -> None:
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    payloads = (
        getattr(mesh_buffer, "buffer_payloads", {})
        if mesh_buffer is not None
        else {}
    )
    for group_index, group in enumerate(groups):
        _require_zero(f"MeshGroup[{group_index}].reserve", group.reserve)
        _require_equal(
            f"MeshGroup[{group_index}].vertexCount",
            int(group.vertex_count),
            sum(int(item.vert_count) for item in group.submeshes),
        )
        allocated = sum(
            int(item.allocated_index_count) for item in group.submeshes
        )
        expected_index_count = (
            sum(int(item.indices_count) for item in group.submeshes)
            if is_mply
            else allocated
        )
        _require_equal(
            f"MeshGroup[{group_index}].indexCount",
            int(group.index_span_count),
            expected_index_count,
        )

        if not is_mply:
            for left, right in zip(group.submeshes, group.submeshes[1:]):
                _require_equal(
                    f"MeshGroup[{group_index}] consecutive index allocations",
                    int(right.faces_index_offset),
                    int(left.faces_index_offset)
                    + int(left.allocated_index_count),
                )

        for submesh_index, submesh in enumerate(group.submeshes):
            field = f"MeshGroup[{group_index}].MeshCluster[{submesh_index}]"
            reserve03 = int(submesh.runtime_ignored_reserve03)
            if not 0 <= reserve03 <= 1:
                _fail(
                    f"{field}.runtimeIgnoredReserve03",
                    "must be in the corpus-observed range 0 or 1",
                    reserve03,
                )
            if canonical:
                _require_zero(
                    f"{field}.runtimeIgnoredReserve03 for canonical construction",
                    reserve03,
                )
            _require_zero(
                f"{field}.reservedDword04",
                submesh.reserved_dword04,
            )
            _require_zero(
                f"{field}.streamingOffsets",
                (submesh.streaming_offset, submesh.streaming_offset2),
            )
            _require_zero(f"{field}.trailingReserve", submesh.wilds_tail)
            if canonical:
                _require_zero(f"{field}.isQuad", submesh.is_quad)

            payload = payloads.get(int(submesh.buffer_index))
            if payload is None:
                continue
            indices = payload.index_values
            start = int(submesh.faces_index_offset)
            draw_end = start + int(submesh.indices_count)
            allocated_end = start + (
                int(submesh.indices_count)
                if is_mply
                else int(submesh.allocated_index_count)
            )
            if start < 0 or allocated_end > len(indices):
                _fail(
                    f"{field}.indexRange",
                    "must fit its decoded index buffer",
                    (start, allocated_end, len(indices)),
                )
            draw = indices[start:draw_end]
            if draw and (min(draw) < 0 or max(draw) >= int(submesh.vert_count)):
                _fail(
                    f"{field}.indices",
                    "must be local to the submesh vertex range",
                    (min(draw), max(draw), submesh.vert_count),
                )
            if canonical and allocated_end > draw_end:
                _require_zero(
                    f"{field}.indexAlignmentPadding",
                    indices[draw_end:allocated_end],
                )


def _assert_blend_shape_runtime_storage(mesh: object) -> None:
    blend_data = getattr(mesh, "blend_shape_data", None)
    if blend_data is None:
        return
    _require_zero(
        "BlendShapeData.unresolvedSemantics",
        blend_data.unresolved_semantics,
    )
    _require_zero("BlendShapeLayout.reserve", blend_data.layout_reserve)
    modifier = getattr(blend_data, "bone_modifier", None)
    if modifier is not None:
        _require_zero("BlendShapeBoneModifierLayout.reserve", modifier.reserve)
        joint_count = int(mesh.joint_count)
        for index, effector in enumerate(modifier.effectors):
            if not 0 <= int(effector.joint_index) < joint_count:
                _fail(
                    f"BlendShapeBoneModifier[{index}].jointIndex",
                    "must reference the mesh skeleton",
                    effector.joint_index,
                )
            _require_equal(
                f"BlendShapeBoneModifier[{index}] index/reference count",
                len(effector.vertex_indices),
                len(effector.vertex_positions),
            )
            if not np.isfinite(
                np.asarray(effector.vertex_positions, dtype=np.float32)
            ).all():
                _fail(
                    f"BlendShapeBoneModifier[{index}].referencePositions",
                    "must be finite",
                    "nonfinite data",
                )
    maximum_target_count = max(
        (len(body.targets) for body in blend_data.bodies),
        default=0,
    )
    maximum_channel_count = max(
        (int(body.channel_count) for body in blend_data.bodies),
        default=0,
    )
    expected_target_map_count = lod_map_allocation(maximum_target_count)
    expected_channel_map_count = lod_map_allocation(maximum_channel_count)
    for body in blend_data.bodies:
        _require_zero(f"BlendShapeBody[{body.lod_index}].reserve", body.reserve)
        _require_equal(
            f"BlendShapeBody[{body.lod_index}].targetLodMap count",
            len(body.target_lod_map),
            expected_target_map_count,
        )
        _require_equal(
            f"BlendShapeBody[{body.lod_index}].channelLodMap count",
            len(body.channel_lod_map),
            expected_channel_map_count,
        )
        for target_index, target in enumerate((*body.targets, *body.none_targets)):
            if target.aabb is None:
                continue
            _require_zero(
                f"BlendShapeBody[{body.lod_index}].target[{target_index}].AABB.w",
                (target.aabb.minimum_w, target.aabb.maximum_w),
            )
    if getattr(mesh.header, "blend_shape_indices_offset", 0):
        material_count = (
            int(mesh.meshes[0].material_count)
            if getattr(mesh, "meshes", ())
            else 0
        )
        semantic_name_count = (
            int(mesh.header.name_count)
            - material_count
            - int(mesh.joint_count)
        )
        if semantic_name_count < 0:
            _fail(
                "BlendShape name partition",
                "must be nonnegative",
                semantic_name_count,
            )
        _require_equal(
            "BlendShape name-index count",
            len(mesh.blend_shape_indices),
            semantic_name_count,
        )


def _assert_pivots(mesh: object) -> None:
    header = getattr(mesh, "header", None)
    if (
        not bool(getattr(mesh, "is_full", True))
        and getattr(header, "magic", MESH_MAGIC) != MPLY_MAGIC
    ):
        return
    pivots = getattr(mesh, "group_pivots", None)
    mesh_buffer = getattr(mesh, "mesh_buffer", None)
    pivot_count = 0
    if pivots is not None:
        _require_zero("GroupPivotLayout.reserve", pivots.reserve)
        _require_equal(
            "GroupPivotLayout size/offset presence",
            bool(pivots.buffer_size),
            bool(pivots.buffer_offset),
        )
        pivot_count = len(pivots.pivots)
        _require_equal(
            "GroupPivotLayout.bufferSize",
            int(pivots.buffer_size),
            pivot_count * 12,
        )
        if not np.isfinite(np.asarray(pivots.pivots, dtype=np.float32)).all():
            _fail("GroupPivotLayout.pivots", "must be finite", "nonfinite data")
    if mesh_buffer is None:
        return
    for buffer_index, payload in mesh_buffer.buffer_payloads.items():
        selectors = getattr(payload, "pivot_indices", ())
        if not selectors:
            continue
        maximum = max(selectors)
        if maximum and not pivot_count:
            _fail(
                f"BufferLayout[{buffer_index}] normal.w pivot selector",
                "must be zero when no GroupPivotLayout is present",
                maximum,
            )
        if pivot_count and maximum >= pivot_count:
            _fail(
                f"BufferLayout[{buffer_index}] normal.w pivot selector",
                "must index the mesh GroupPivotLayout",
                (maximum, pivot_count),
            )


def _assert_meshlet_runtime_contract(mesh: object, mesh_buffer: object) -> None:
    _VALIDATOR.assert_late_meshlet_layout(mesh, mesh_buffer)
    layout = mesh_buffer.meshlet_layout
    _require_zero(
        "MeshletHeader serialized CPU-unused words",
        layout.header.serialized_cpu_unused_words,
    )
    _VALIDATOR.assert_meshlet_region_ends(layout, explicit=False)
    for cluster_index, cluster in enumerate(mesh_buffer.mply_cluster_headers):
        _require_zero(
            f"MPLY cluster[{cluster_index}] material-flag extension",
            cluster.meshlet_material_flag_extension,
        )
        _require_zero(
            f"MPLY cluster[{cluster_index}] reserved bit 31",
            cluster.reserved_bit31,
        )
    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        flags = chunk.flags
        vert_count = int(chunk.vert_count)
        _require_equal(
            f"MPLY chunk[{chunk_index}] tangent count",
            len(chunk.tangents),
            0 if flags.is_meshlet_no_tangent else vert_count,
        )
        expected_mask_size = (
            ((vert_count + 31) // 32) * 4
            if int(flags.tangent_handedness_mode) == 2
            else 0
        )
        _require_equal(
            f"MPLY chunk[{chunk_index}] tangent-orientation mask size",
            len(chunk.tangent_mask_bytes),
            expected_mask_size,
        )
    _VALIDATOR.assert_meshlet_positions_in_bounds(mesh_buffer)


def assert_mhwilds_runtime_contract(mesh: object) -> None:
    header = mesh.header
    flags = header.decoded_content_flags
    if flags is None:
        _fail("MeshHeader.contentFlags", "must be decoded", None)
    _VALIDATOR.assert_late_header(mesh)
    _require_equal(
        "MeshHeader.contentFlags.hasLightmapUV",
        flags.has_lightmap_uv,
        bool(header.light_map_used),
    )

    if mesh.streaming_info is not None:
        _require_zero("StreamingInfo.pad", mesh.streaming_info.pad)

    mesh_buffer = mesh.mesh_buffer
    if header.magic == MPLY_MAGIC:
        if mesh_buffer is None:
            _fail("MPLY MeshBuffer", "must be decoded", None)
        _assert_meshlet_runtime_contract(mesh, mesh_buffer)
    elif header.magic == MESH_MAGIC:
        expected_stream_count = header.profile.streaming_layout_count(
            mesh.streaming_info.entries if mesh.streaming_info else ()
        )
        _require_equal(
            "MeshHeader.contentFlags.bufferCount",
            int(flags.buffer_count),
            (expected_stream_count + 1) & 0x7,
        )
        _require_equal(
            "MeshHeader.contentFlags.highMeshCount",
            flags.high_mesh_count,
            expected_stream_count & 0x7,
        )
        if mesh_buffer is None:
            _fail("ordinary MESH BufferLayout", "must be present", None)
        _require_equal(
            "MeshHeader.gpuBufferBase",
            int(header.gpu_buffer_base_offset),
            int(mesh_buffer.vertex_buffer_offset),
        )
        _require_zero("resident BufferLayout.reserve", mesh_buffer.wilds_reserved)
        _require_zero("resident BufferLayout.bufferIndex", mesh_buffer.buffer_index)
        _require_zero(
            "resident BufferLayout shape-key-weight pointer",
            mesh_buffer.shapekey_weight_buffer_offset,
        )
        _require_equal(
            "resident BufferLayout total size",
            int(mesh_buffer.total_buffer_size),
            int(mesh_buffer.vertex_buffer_size) + int(mesh_buffer.index_buffer_size),
        )
        if not 0 <= int(mesh_buffer.vertex_buffer_size) <= int(mesh_buffer.total_buffer_size):
            _fail(
                "resident BufferLayout.vertexBufferSize",
                "must fit totalBufferSize",
                (mesh_buffer.vertex_buffer_size, mesh_buffer.total_buffer_size),
            )
        _assert_element_headers(
            "resident BufferLayout elements",
            mesh_buffer.buffer_headers,
            canonical=False,
            require_position_at_zero=True,
        )
        _require_equal(
            "resident BufferLayout element counts",
            int(mesh_buffer.total_element_count),
            int(mesh_buffer.element_count),
        )
        _require_zero(
            "resident BufferLayout auxiliary elements",
            mesh_buffer.extra_buffer_headers,
        )
        for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
            _require_zero(
                f"streaming BufferLayout[{buffer_index}].reserve",
                stream.wilds_reserved,
            )
            _require_zero(
                f"streaming BufferLayout[{buffer_index}] shape-key-weight pointer",
                stream.shapekey_weight_buffer_offset,
            )
            _require_equal(
                f"streaming BufferLayout[{buffer_index}].bufferIndex",
                int(buffer_index),
                int(stream.entry_index) + 1,
            )
            if mesh.streaming_info is None:
                _fail(
                    f"streaming BufferLayout[{buffer_index}] entry",
                    "must have StreamingInfo metadata",
                    None,
                )
            entry = mesh.streaming_info.entries[int(stream.entry_index)]
            _require_equal(
                f"streaming BufferLayout[{buffer_index}].vertexBufferOffset",
                int(stream.vertex_buffer_offset),
                int(entry.start),
            )
            _require_equal(
                f"streaming BufferLayout[{buffer_index}].unpaddedBufferSize",
                int(stream.unpadded_buffer_size),
                int(entry.size),
            )
            _require_equal(
                f"streaming BufferLayout[{buffer_index}] element counts",
                int(stream.total_vertex_element_count),
                int(stream.main_vertex_element_count),
            )
            _require_zero(
                f"streaming BufferLayout[{buffer_index}] auxiliary elements",
                stream.extra_vertex_elements,
            )
            if not 0 <= int(stream.vertex_buffer_length) <= int(stream.unpadded_buffer_size):
                _fail(
                    f"streaming BufferLayout[{buffer_index}].vertexBufferLength",
                    "must fit unpaddedBufferSize",
                    (stream.vertex_buffer_length, stream.unpadded_buffer_size),
                )
            _assert_element_headers(
                f"streaming BufferLayout[{buffer_index}] elements",
                stream.vertex_elements,
                canonical=False,
                require_position_at_zero=True,
            )
        assert_late_normal_recalc_runtime_storage(mesh, _VALIDATOR)
        for mesh_index, mesh_data in enumerate(mesh.meshes):
            _VALIDATOR.assert_mesh_data_padding(mesh_index, mesh_data)
            _VALIDATOR.assert_finite_ordered_aabb(
                f"MeshData[{mesh_index}].AABB",
                mesh_data.bounding_box[0],
                mesh_data.bounding_box[1],
            )
            if int(mesh_data.skin_weight_runtime_ignored_tag) not in (0, 0x18):
                _fail(
                    f"MeshData[{mesh_index}].SkinWeightCount upper tag",
                    "must be either the Wilds producer tag 0x18 or canonical cleared 0",
                    mesh_data.skin_weight_runtime_ignored_tag,
                )
            if int(mesh_data.skin_weight_influence_count) not in (4, 6, 8, 12):
                _fail(
                    f"MeshData[{mesh_index}].SkinWeightCount",
                    "must select a runtime-supported 4/6/8/12-weight path",
                    mesh_data.skin_weight_count_raw,
                )
            for lod_slot, lod in enumerate(mesh_data.lods):
                if not 0 <= int(lod.lod_level) <= 7:
                    _fail(
                        f"MeshData[{mesh_index}].LOD[{lod_slot}].lodLevel",
                        "must be an authored native LOD identifier in [0, 7]",
                        lod.lod_level,
                    )

    lods, groups, _submeshes = ordinary_geometry(mesh)
    for lod_index, lod in enumerate(lods):
        _require_zero(f"MeshLOD[{lod_index}].vertexFormat", lod.vertex_format)
        _require_zero(f"MeshLOD[{lod_index}].reserve", lod.reserve)
        if not math.isfinite(float(lod.lod_factor)) or float(lod.lod_factor) < 0.0:
            _fail(
                f"MeshLOD[{lod_index}].lodFactor",
                "must be finite and nonnegative",
                lod.lod_factor,
            )
    _assert_group_topology(
        mesh,
        groups,
        is_mply=header.magic == MPLY_MAGIC,
        canonical=False,
    )

    _assert_pivots(mesh)
    boxes = getattr(mesh, "bounding_boxes", None)
    if boxes is not None:
        _require_zero("BoundingBoxLayout.reserve", boxes.reserve)
        _require_equal("BoundingBoxLayout.count", boxes.count, len(boxes.boxes))
        for box_index, box in enumerate(boxes.boxes):
            _VALIDATOR.assert_finite_ordered_aabb(
                f"BoundingBoxLayout.box[{box_index}]",
                box.minimum,
                box.maximum,
            )
    _VALIDATOR.assert_auxiliary_reserves(mesh)
    _assert_blend_shape_runtime_storage(mesh)


def _assert_zero_blend_storage_for_import(mesh: object) -> None:
    blend_data = getattr(mesh, "blend_shape_data", None)
    if blend_data is None:
        return
    for body in blend_data.bodies:
        if int(body.compression_type) not in (0, 1):
            _fail(
                f"BlendShapeBody[{body.lod_index}].compressionType",
                "must use a corpus-observed canonical encoding (0 or 1)",
                body.compression_type,
            )
        for channel in body.channels:
            for segment in channel.segments:
                for name in ("position_w", "normal_w"):
                    value = getattr(segment, name, None)
                    if value is not None and has_nonzero(value):
                        _fail(
                            f"blend-shape channel {channel.index}.{name}",
                            "must be zero because Wilds runtime does not read it",
                            "nonzero producer data",
                        )


def _assert_ordinary_content_stream_import(
    flags: object,
    element_types: set[int],
) -> None:
    if flags.is_skinning and 4 not in element_types:
        _fail(
            "MeshHeader.contentFlags.isSkinning",
            "requires a primary type-4 skin stream",
            "missing stream",
        )
    _require_equal(
        "MeshHeader.contentFlags.hasVertexColor",
        flags.has_vertex_color,
        5 in element_types,
    )


def _assert_ordinary_skin_import(mesh: object) -> None:
    mesh_buffer = mesh.mesh_buffer
    influence_count = int(mesh.meshes[0].skin_weight_influence_count)
    for buffer_index, payload in mesh_buffer.buffer_payloads.items():
        primary_streams = _VALIDATOR.attribute_records(payload, 4, 16)
        continuation_streams = _VALIDATOR.attribute_records(payload, 7, 16)
        if len(primary_streams) > 1 or len(continuation_streams) > 1:
            _fail(
                f"BufferLayout[{buffer_index}] skin streams",
                "must contain at most one primary and continuation stream",
                (len(primary_streams), len(continuation_streams)),
            )
        if not primary_streams:
            if continuation_streams:
                _fail(
                    f"BufferLayout[{buffer_index}] continuation skin stream",
                    "must have a primary stream",
                    True,
                )
            continue
        primary = primary_streams[0]
        packed = primary[:, :8].copy().view("<u4").reshape(-1, 2)
        tags = packed >> 30
        if np.any(tags != 0):
            _fail(
                f"BufferLayout[{buffer_index}] primary packed-index tags",
                "must be cleared by canonical construction",
                sorted(set(map(int, tags.flat))),
            )
        if influence_count == 6:
            if continuation_streams:
                _fail(
                    f"BufferLayout[{buffer_index}] continuation skin stream",
                    "must be absent for six influences",
                    True,
                )
            _require_zero(
                f"BufferLayout[{buffer_index}] primary runtime-unread bytes 13-15",
                primary[:, 13:16],
            )
        elif influence_count == 12:
            if not continuation_streams:
                _fail(
                    f"BufferLayout[{buffer_index}] continuation skin stream",
                    "must be present for twelve influences",
                    False,
                )
            continuation = continuation_streams[0]
            continuation_packed = (
                continuation[:, :8].copy().view("<u4").reshape(-1, 2)
            )
            continuation_tags = continuation_packed >> 30
            if np.any(continuation_tags != 0):
                _fail(
                    f"BufferLayout[{buffer_index}] continuation packed-index tags",
                    "must equal the corpus producer value 0",
                    sorted(set(map(int, continuation_tags.flat))),
                )
            _require_zero(
                f"BufferLayout[{buffer_index}] continuation runtime-unread bytes 11-15",
                continuation[:, 11:16],
            )
        else:
            _fail(
                f"BufferLayout[{buffer_index}] SkinWeightCount",
                "must use the corpus-observed six- or twelve-influence layout",
                influence_count,
            )


def _assert_mply_import(mesh: object, mesh_buffer: object) -> None:
    flags = mesh.header.decoded_content_flags
    for name in ("is_skinning", "has_joint", "has_blend_shape"):
        _require_zero(f"MPLY MeshHeader.contentFlags.{name}", getattr(flags, name))
    for field in (
        "bounds_offset",
        "bones_offset",
        "bone_indices_offset",
        "blend_shape_indices_offset",
    ):
        _require_zero(f"MPLY MeshHeader.{field}", getattr(mesh.header, field))

    layout = mesh_buffer.meshlet_layout
    bvh = mesh_buffer.meshlet_bvh
    info = layout.group_pivot_info
    if info.blob_relative_offset not in (0, 0x90):
        _fail(
            "MPLY pivot descriptor offset",
            "must use the corpus-canonical absent/0x90 encoding",
            info.blob_relative_offset,
        )
    for lod_index in range(8):
        count = int(bvh.cluster_counts[lod_index])
        exponent = int(bvh.cluster_bounds_scale_exponents[lod_index])
        if count and not 0 <= exponent <= 3:
            _fail(
                f"MeshletBVH.clusterBoundsScaleExponent[{lod_index}]",
                "must remain in the corpus-observed range [0, 3]",
                exponent,
            )

    for chunk_index, chunk in enumerate(mesh_buffer.mply_chunks):
        flags = chunk.flags
        if int(flags.position_compress_level) == 3:
            _fail(
                f"MPLY chunk[{chunk_index}].positionCompressLevel",
                "must not synthesize the runtime-supported corpus-absent mode 3",
                3,
            )
        if int(flags.tangent_handedness_mode) == 3:
            _fail(
                f"MPLY chunk[{chunk_index}].tangentHandednessMode",
                "must not synthesize the runtime-supported corpus-absent mode 3",
                3,
            )
        if flags.is_meshlet_use_skinned or flags.is_meshlet_compressed_skinned:
            _fail(
                f"MPLY chunk[{chunk_index}] skin flags",
                "must not synthesize the runtime-supported corpus-absent "
                "skin path",
                (
                    flags.is_meshlet_use_skinned,
                    flags.is_meshlet_compressed_skinned,
                ),
            )
        _require_zero(
            f"MPLY chunk[{chunk_index}] producer bit 7",
            flags.shader_unused_producer_bit7,
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] runtime-unread face alignment residue",
            getattr(chunk, "face_alignment_padding", b""),
        )
        _require_zero(
            f"MPLY chunk[{chunk_index}] position alignment padding",
            getattr(chunk, "position_alignment_padding", b""),
        )
        if chunk.tangent_mask_bytes:
            for bit_index in range(
                int(chunk.vert_count),
                len(chunk.tangent_mask_bytes) * 8,
            ):
                if chunk.tangent_mask_bytes[bit_index >> 3] & (1 << (bit_index & 7)):
                    _fail(
                        f"MPLY chunk[{chunk_index}] tangent-mask padding",
                        "must be zero",
                        bit_index,
                    )

def assert_mhwilds_import_safe(mesh: object) -> None:
    assert_mhwilds_runtime_contract(mesh)
    assert_canonical_layout_constants(_VALIDATOR, mesh)
    header = mesh.header
    flags = header.decoded_content_flags
    is_mply = header.magic == MPLY_MAGIC

    for name in (
        "solved_offset",
        "quad_enable",
        "use_ray_tracing_vertex_animation",
        "merge_static_mesh_bvh",
    ):
        _require_zero(f"MeshHeader.contentFlags.{name}", getattr(flags, name))
    _VALIDATOR.assert_content_policy(flags, is_mply)

    mesh_buffer = mesh.mesh_buffer
    if is_mply:
        _assert_mply_import(mesh, mesh_buffer)
        _assert_zero_blend_storage_for_import(mesh)
        return

    _assert_element_headers(
        "resident BufferLayout elements",
        mesh_buffer.buffer_headers,
        canonical=True,
        require_position_at_zero=True,
    )
    all_element_types = {int(item.type) for item in mesh_buffer.buffer_headers}
    for buffer_index, stream in mesh_buffer.streaming_buffer_headers.items():
        _assert_element_headers(
            f"streaming BufferLayout[{buffer_index}] elements",
            stream.vertex_elements,
            canonical=True,
            require_position_at_zero=True,
        )
        all_element_types.update(int(item.type) for item in stream.vertex_elements)

    _assert_ordinary_content_stream_import(flags, all_element_types)
    for mesh_index, mesh_data in enumerate(mesh.meshes):
        if int(mesh_data.skin_weight_count_raw) not in (0x19, 0x1B):
            _fail(
                f"MeshData[{mesh_index}].SkinWeightCount",
                "must use native 0x19 (six) or 0x1B (twelve)",
                mesh_data.skin_weight_count_raw,
            )
        _require_equal(
            f"MeshData[{mesh_index}] AABB padding",
            (
                int(mesh_data.aabb_min_padding),
                int(mesh_data.aabb_max_padding),
            ),
            (1, 1),
        )
    _assert_ordinary_skin_import(mesh)
    assert_late_normal_recalc_import_storage(mesh, _VALIDATOR)

    _lods, groups, _submeshes = ordinary_geometry(mesh)
    _assert_group_topology(
        mesh,
        groups,
        is_mply=False,
        canonical=True,
    )
    _assert_zero_blend_storage_for_import(mesh)


__all__ = [
    "MHWILDS_VERTEX_STRIDES",
    "assert_mhwilds_import_safe",
    "assert_mhwilds_runtime_contract",
]
