import math

lazy import numpy as np

from .contract_utils import (
    ContractValidator, all_groups, all_lods, assert_canonical_layout_constants,
    assert_late_normal_recalc_runtime_storage,
)
lazy from .dd2_contract import DD2_VERTEX_STRIDES
from .models import MPLY_MAGIC
lazy from .canonical_contract import assert_mesh_semantic_domains, assert_mesh_canonical_storage

V = ContractValidator("Updated DD2 canonical contract")


def _common(mesh):
    h = mesh.header
    f = h.decoded_content_flags
    V.require_equal("fileSize", h.file_size, mesh.source_size)
    V.require_zero("content flag storage high byte", h.content_flags_storage_raw >> 24)
    V.require_zero("content flag reserves", f.reserved_bits)
    V.require_zero("buffer count reserved bits", h.header_pad)
    V.require_zero("header reserved dwords", h.header_reserved_dwords)
    V.require_zero("unused lightmap metadata", h.light_map_metadata_raw)
    V.require_equal("streamingBVH", f.streaming_bvh, h.magic == MPLY_MAGIC)
    for flag, present in (
        ("use_sdf", bool(mesh.sdf_path)),
        ("has_joint", bool(mesh.bones)),
        ("has_blend_shape", bool(h.blend_shapes_offset)),
        ("has_vertex_group", bool(mesh.group_pivots)),
    ):
        V.require_equal(flag, getattr(f, flag), present)
    V.require_zero("skeleton reserves", mesh.skeleton_reserved)
    V.require_zero("bone-remap padding", mesh.bone_remap_padding)
    for bone in mesh.bones:
        V.require_zero("JointNode reserved", bone.reserved)
        V.require_zero("JointNode segment-scale reserves", bone.segment_scale_reserved_bits)
    boxes = mesh.bounding_boxes
    if boxes is not None:
        V.require_zero("BoundingBoxLayout reserve", boxes.reserve)
        V.require_equal("BoundingBoxLayout count", boxes.count, len(boxes.boxes))
    if mesh.streaming_info is not None:
        V.require_zero("StreamingInfo reserve", mesh.streaming_info.pad)
    if mesh.group_pivots is not None:
        V.require_zero("GroupPivotLayout reserve", mesh.group_pivots.reserve)
        V.require_equal("GroupPivotLayout size", mesh.group_pivots.buffer_size,
                        len(mesh.group_pivots.pivots) * 12)
        V.require(bool(np.isfinite(mesh.group_pivots.pivots).all()),
                  "group pivots", "must be finite", "nonfinite")


def _ordinary(mesh):
    assert_mesh_semantic_domains(mesh)
    b = mesh.mesh_buffer
    V.require_equal("GPU buffer base", mesh.header.vertices_offset, b.vertex_buffer_offset)
    owners = [(0, b, b.buffer_headers, b.extra_buffer_headers)]
    owners.extend((i, o, o.vertex_elements, o.extra_vertex_elements)
                  for i, o in b.streaming_buffer_headers.items())
    for i, owner, elements, extra in owners:
        label = f"BufferLayout[{i}]"
        V.require_zero(label + " auxiliary element sets", extra)
        V.assert_element_headers(
            label, elements, strides=DD2_VERTEX_STRIDES, canonical=True,
            require_position_at_zero=True,
            unknown_semantic_expectation="must be a current DD2 vertex semantic",
        )
        V.require_zero(label + " former shape-key size", owner.shapekey_weight_buffer_size)
        V.require_zero(label + " prefix reserves", owner.extended_prefix_reserved_dwords)
        V.require_zero(label + " tail reserve", owner.wilds_reserved)
        V.require_equal(label + " buffer ID", owner.buffer_index, i)
    for md in mesh.meshes:
        V.require_equal("MeshData SkinWeightCount", md.skin_weight_count_raw, 0x12)
        V.require_equal("MeshData AABB padding", (md.aabb_min_padding, md.aabb_max_padding), (1, 1))
        V.require_zero("MeshData chunk/reserve", (md.chunk, md.reserve))
    for _, _, lod in all_lods(mesh):
        V.require_zero("MeshLOD reserves", (lod.vertex_format, lod.reserve))
        V.require(0 <= lod.lod_level <= 7, "MeshLOD level", "must fit [0,7]", lod.lod_level)
    for group in all_groups(mesh):
        V.require_zero("MeshGroup reserve", group.reserve)
        for s in group.submeshes:
            V.require_zero("MeshCluster unused fields", (
                s.is_quad, s.reserve, s.ukn1, s.streaming_offset,
                s.streaming_offset2, s.wilds_tail,
            ))
    for payload in b.buffer_payloads.values():
        for uv in (payload.uv0, payload.uv1, payload.uv2):
            V.require(bool(np.isfinite(uv).all()), "UV values", "must be finite", "nonfinite")
        for weights in (payload.skin_weights, payload.additional_vertex_weights):
            if weights is not None:
                V.require_equal("skin influence count", weights.influence_count, 8)
                V.require_equal("skin residual lane", weights.implicit_last_weight, True)
    if mesh.normal_recalc_data is not None:
        V.require_zero("NormalRecalcLayout reserve", mesh.normal_recalc_data.reserved)
    assert_late_normal_recalc_runtime_storage(mesh, V)


def _mply(mesh):
    b = mesh.mesh_buffer
    l, v, parts = b.meshlet_layout, b.meshlet_bvh, b.meshlet_parts_layout
    h = l.header
    V.require_equal("MPLY logical buffer count", mesh.header.buffer_count, 1)
    V.require_zero("MPLY unused header pointers", (
        mesh.header.micro_polygon_reserved_offset, mesh.header.blend_shapes_offset,
        mesh.header.buffer_layout_offset, mesh.header.blend_shape_bone_modifier_offset,
    ))
    V.require_equal("MPLY GPU base", mesh.header.vertices_offset, l.gpu_meshlet_offset)
    V.require_zero("MeshletLayout reserve", l.reserved)
    V.require_equal("GPU header mirror", l.gpu_header_matches_resident, True)
    V.require(1 <= h.lod_num <= 8, "LOD count", "must fit [1,8]", h.lod_num)
    V.require_zero("serialized streaming state", (h.loaded_lod, h.resident_lod, h.allocation_lod))
    V.require_equal("valid LOD bits", h.valid_lod_bits, (1 << h.lod_num) - 1)
    V.require_zero("LOD control reserves", h.serialized_lod_control_reserved_bits)
    V.require_zero("LOD payload reserves", h.serialized_lod_payload_reserved_words)
    V.require_zero("corpus-unused distance LOD strategy", int(h.serialized_mpmr_lod_strategy))
    V.require_equal("native active LOD factors", h.active_lod_factors,
                    tuple(float(i) for i in range(h.lod_num)))
    V.assert_finite_ordered_aabb("MeshletHeader bounds", h.min_aabb, h.max_aabb)
    info = l.group_pivot_info
    V.require_equal("pivot descriptor mirror", info.layout_mirror_matches_blob, True)
    V.require_equal("pivot offset mirror", info.header_offset_matches_internal, True)
    V.require_zero("pivot descriptor reserves", (info.reserve_u16, info.reserve_u32))
    V.require_equal("MPLY SkinWeightCount", info.skin_weight_count_raw, 0x12 if mesh.bones else 0)
    V.require_equal("pivot count", len(info.pivots), info.pivot_count if info.present else 0)
    V.require_equal("LOD region count", len(l.lod_regions), h.lod_num)
    cursor = 0
    for i in range(8):
        V.require_zero("BVH reserved count byte", v.cluster_count_reserved_bytes[i])
        V.require_zero("unused meshlet BVH pointer", v.meshlet_bvh_offsets[i])
        if i < h.lod_num:
            V.require_equal("cluster offset", v.cluster_offsets[i], cursor * 16)
            V.require_equal("region cluster count", l.lod_regions[i].cluster_count, v.cluster_counts[i])
            cursor += v.cluster_counts[i]
        else:
            V.require_zero("inactive LOD", (v.cluster_counts[i], v.cluster_offsets[i],
                                           v.cluster_bounds_scale_exponent_raw_bytes[i],
                                           h.lod_cluster_offsets[i], h.serialized_lod_end_offsets[i]))
    V.require_equal("cluster count", len(b.mply_chunks), cursor)
    V.require_equal("cluster header count", len(b.mply_cluster_headers), cursor)
    V.require_equal("quantized AABB count", len(v.quantized_aabb_centers), cursor)
    V.require_zero("MeshletParts reserve", parts.reserve)
    V.require_equal("part count", parts.parts_num, len(parts.parts_indices))
    V.require_equal("part domain", set(parts.parts_indices), {c.part_id for c in b.mply_cluster_headers})
    for i, (chunk, cluster) in enumerate(zip(b.mply_chunks, b.mply_cluster_headers)):
        f = chunk.flags
        V.require_equal("cluster center index", cluster.aabb_center_index, i)
        V.require_zero("cluster reserved bit31", cluster.reserved_bit31)
        V.require_zero("chunk reserved bit11", f.raw & (1 << 11))
        V.require_zero("chunk trailing payload", chunk.trailing_payload)
        V.require_zero("position alignment", chunk.position_alignment_padding)
        V.require_equal("position codec", f.position_compress_level, cluster.position_compress_level)
        V.require_equal("chunk vertex count", chunk.vert_count, cluster.vertex_count)
        V.require_equal("chunk index count", len(chunk.faces), cluster.index_count)
        V.require_equal("chunk identity", (chunk.material_id, chunk.part_id), (cluster.material_id, cluster.part_id))
        V.require(0 <= chunk.material_id < len(mesh.material_names), "material ID", "must index names", chunk.material_id)
        if len(chunk.faces):
            V.require(0 <= int(chunk.faces.min()) <= int(chunk.faces.max()) < chunk.vert_count,
                      "chunk indices", "must index local vertices", i)
        V.require_equal("skin record size", len(chunk.skin_record_bytes),
                        (1 if f.skin_stream_is_shared else chunk.vert_count) * 16 if f.has_skin_stream else 0)
        for uv in (chunk.uv0, chunk.uv1, chunk.uv2):
            V.require(bool(np.isfinite(uv).all()), "MPLY UV values", "must be finite", i)
        tolerance = max(math.ldexp(1.0, f.relative_aabb_scale_exponent - 143), 1e-6)
        positions = chunk.positions
        # Decoding adds quantized deltas to large float32 origins: allow two float32 roundings.
        tolerance = max(tolerance, 2 * float(np.spacing(np.float32(np.abs(positions).max()))))
        V.require(bool(((positions >= np.asarray(chunk.cluster_aabb.minimum) - tolerance)
                        & (positions <= np.asarray(chunk.cluster_aabb.maximum) + tolerance)).all()),
                  "decoded positions", "must fit native cluster bounds", i)


def assert_dd2_updated_runtime_contract(mesh):
    _common(mesh)
    (_mply if mesh.header.magic == MPLY_MAGIC else _ordinary)(mesh)


def assert_dd2_updated_import_safe(mesh):
    assert_dd2_updated_runtime_contract(mesh)
    f = mesh.header.decoded_content_flags
    for name in ("quad_enable", "solved_offset", "merge_static_mesh_bvh",
                 "use_ray_tracing_vertex_animation", "has_lightmap_uv"):
        V.require_zero("corpus-unused " + name, getattr(f, name))
    V.assert_content_policy(f, mesh.header.magic == MPLY_MAGIC)
    if mesh.header.magic != MPLY_MAGIC:
        assert_mesh_canonical_storage(mesh)
        V.require_zero("corpus-unused occluder layout", mesh.header.occluder_mesh_offset)
        for md in mesh.meshes:
            V.require_zero("corpus-unused 32-bit ordinary indices", md.has_32bit_index_buffer)
        if mesh.blend_shape_data is not None:
            for body in mesh.blend_shape_data.bodies:
                V.require_equal("corpus-supported BlendShapeType", body.compression_type, 1)
    else:
        assert_canonical_layout_constants(V, mesh)
        h = mesh.mesh_buffer.meshlet_layout.header
        V.require_zero("inactive LOD factors", h.lod_factors[h.lod_num:])
        for c in mesh.mesh_buffer.mply_chunks:
            V.require_zero("corpus-unused tertiary UV flags", c.flags.raw & ((1 << 3) | (1 << 10)))
            V.require(c.flags.position_compress_level < 3,
                      "position codec", "must be an exercised codec [0,2]", c.flags.position_compress_level)
            V.require(c.flags.tangent_handedness_mode < 3,
                      "tangent orientation", "must be an exercised mode [0,2]", c.flags.tangent_handedness_mode)
            V.require((c.flags.raw & 192) != 128,
                      "skin flags", "shared-only state is corpus-absent", c.flags.raw & 192)
            if c.skin_weights is not None:
                V.require_zero("unread eighth weight", c.skin_weights.runtime_ignored_weight_bytes)
