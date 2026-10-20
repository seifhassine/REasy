from .base import (
    BlendShapeLayout,
    Half2ValueDomain,
    LegacyLightMapLayout,
    MeshBufferLayout,
    MeshContentFlagLayout,
    MeshGameProfile,
    MeshHeaderLayout,
    MeshMainVersion,
    MeshletLodFactorEncoding,
    NormalRecalcLayout,
    SkinWeightLayout,
    VertexNormalWSemantic,
    register_mesh_profile,
)


class Re9MeshProfile(MeshGameProfile):
    game = "RE9"
    sdf_texture_resource_version = 501229472
    display_name = "Resident Evil Requiem"
    aliases = ("Resident Evil 9", "Resident Evil Requiem")
    format_version = MeshMainVersion.RE9
    version_pairs = frozenset({(250904410, 250925211)})
    meshlet_max_triangles = 126
    meshlet_max_vertices = 128
    default_for_format = True

    header_layout = MeshHeaderLayout.WILDS
    content_flag_layout = MeshContentFlagLayout.WILDS
    legacy_light_map_layout = LegacyLightMapLayout.RE8
    buffer_layout = MeshBufferLayout.PRAGMATA
    blend_shape_layout = BlendShapeLayout.EXTENDED
    normal_recalc_layout = NormalRecalcLayout.RE9
    normal_recalc_section_alignment = 4
    supports_streaming_buffers = True
    streaming_header_size = 80
    streaming_header_has_extended_prefix = True
    streaming_header_has_wilds_tail = True
    skip_empty_stream_entries = True
    has_shape_key_weight_pointer = True
    has_shape_key_weight_size = True
    has_auxiliary_index_offsets = True
    legacy_buffer_sizes_are_vertex_and_index = True
    has_wilds_buffer_tail = True
    submesh_has_modern_material = True
    submesh_has_buffer_index = True
    submesh_has_streaming_offsets = True
    submesh_has_ukn1 = True
    submesh_has_wilds_tail = True
    lod_has_level = True
    mesh_data_has_legacy_unknown = False
    mesh_data_has_modern_bounds = True
    canonical_modern_aabb_padding = 1
    name_string_data_alignment = 16
    skeleton_hierarchy_alignment = 16
    canonical_bounding_box_w = 1.0
    optional_gpu_offsets_use_relative_null = True
    group_pivot_payload_is_trailing = True
    shadow_has_legacy_pad = False
    legacy_flags_are_content_flags = True
    legacy_reserved_storage_is_corpus_zero = True
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    auxiliary_vertex_layout_order = ("shadow", "occluder")
    streaming_buffer_index_is_one_based = True
    content_buffer_count_includes_resident = True
    shape_key_weight_pointer_is_corpus_zero = True

    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_normal_w_is_runtime_ignored = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    blend_shape_range_has_part_surface_identity = True
    blend_shape_reserved_storage_is_inert = True
    blend_shape_layout_tail_is_producer_residue = True
    normal_redirect_annotation_is_runtime_semantic = True

    meshlet_has_wilds_layout = True
    meshlet_has_extended_indices = True
    meshlet_streaming_indices_exclude_no_ray_tracing = True
    meshlet_uses_pragmata_flag_bits = True
    meshlet_uses_centered_exponent_position_decode = True
    meshlet_uses_dual_exponent_position_decode = True
    meshlet_chunk_payload_is_fully_decoded = True
    meshlet_position_alignment_padding_is_corpus_zero = True
    # SkinWeightCount_8 (low bits == 2) with serializer tag 0x10.
    skin_weight_layout = SkinWeightLayout.BYTE8
    # CSSkinning8w reads bytes 8..14; the eighth influence is 1 - sum of the other seven; byte 15 is unread.
    skin_weight_uses_implicit_last_weight = True
    skin_weight_influences_by_raw = ((0x12, 8),)
    ordinary_vertex_layout_has_no_auxiliary_elements = False
    # One zero index follows every odd-sized draw range; the group count includes it.
    submesh_index_ranges_are_even_aligned = True
    # Shared shadow LODs alias the main MeshLOD records and may select a suffix.
    auxiliary_topology_sharing_uses_lod_pointer_identity = True
    auxiliary_index_offsets_are_boundaries = True
    # Tangent sign codec on the no-tangent path: 0 positive, 1/3 negative, 2 per-vertex bits.
    meshlet_uses_mhwilds_tangent_handedness = True
    # Skin records come after UV1 and before UV2 (DD2 order, unlike Wilds).
    meshlet_skin_before_texcoord3 = True
    # The first MeshletHeader lane holds eight binary16 LOD factors (two per uint32); words 4..7 are separate.
    meshlet_lod_factor_encoding = MeshletLodFactorEncoding.PACKED_BINARY16
    # The byte beside each 16-bit cluster count is a signed exponent (0xFF = -1).
    meshlet_bvh_scale_exponent_is_signed = True
    canonical_import_contract = "re9"
    canonical_mply_requires_colors = True
    canonical_mply_supports_uv2 = False
    canonical_mply_requires_shared_skinning = True
    canonical_supports_light_maps = True
    canonical_lod_levels_follow_order = True
    authored_blend_shape_codecs = (0, 1)
    canonical_ordinary_content_policy = 2
    canonical_mply_raster_switch_threshold = 0
    # Shipped meshes store infinities and NaN in UV0 and unused infinities in UV1.
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.IEEE754,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )
    canonical_mply_uv_half2_domains = (
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
    )
    canonical_mply_uv_infinity_replacements = (None, 0.0, None)


RE9_MESH_PROFILE = register_mesh_profile(Re9MeshProfile())


__all__ = ["RE9_MESH_PROFILE", "Re9MeshProfile"]
