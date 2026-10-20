from .base import (
    BlendShapeLayout,
    LegacyLightMapLayout,
    MaterialSkinningLayout,
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


class OnimushaWotsMeshProfile(MeshGameProfile):
    canonical_ordinary_supports_32bit_indices = False
    game = "ONIMUSHAWOTS"
    registration_key = "ONIMUSHAWOTSDEMO"
    display_name = "Onimusha: Way of the Sword (demo)"
    aliases = (
        "OnimushaWotS_Demo",
        "ONIWOTS_DEMO",
    )
    format_version = MeshMainVersion.ONIMUSHA_WOTS
    version_pairs = frozenset({(250203152, 251215606)})
    default_for_format = True

    header_layout = MeshHeaderLayout.WILDS
    content_flag_layout = MeshContentFlagLayout.WILDS
    legacy_light_map_layout = LegacyLightMapLayout.RE8
    buffer_layout = MeshBufferLayout.PRAGMATA
    blend_shape_layout = BlendShapeLayout.EXTENDED
    normal_recalc_layout = NormalRecalcLayout.ONIMUSHA_WOTS
    normal_recalc_section_alignment = 4
    skin_weight_layout = SkinWeightLayout.PACKED_10BIT_6

    supports_streaming_buffers = True
    streaming_header_size = 80
    streaming_header_has_extended_prefix = True
    streaming_header_has_wilds_tail = True
    skip_empty_stream_entries = True
    has_shape_key_weight_pointer = True
    has_shape_key_weight_size = True
    has_auxiliary_index_offsets = True
    has_wilds_buffer_tail = True
    submesh_has_modern_material = True
    submesh_has_buffer_index = True
    submesh_has_streaming_offsets = True
    submesh_has_ukn1 = True
    submesh_has_wilds_tail = True
    submesh_index_ranges_are_even_aligned = True
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
    auxiliary_vertex_layout_order = ("shadow", "occluder")
    auxiliary_topology_sharing_uses_lod_pointer_identity = True
    auxiliary_index_offsets_are_boundaries = True
    ordinary_vertex_layout_has_no_auxiliary_elements = True
    shape_key_weight_pointer_is_corpus_zero = True
    content_buffer_count_includes_resident = True
    streaming_buffer_index_is_one_based = True

    skin_weight_count_selects_influence_layout = True
    skin_weight_uses_implicit_last_weight = True
    skin_weight_influences_by_raw = ((0x19, 6), (0x1B, 12))
    material_skinning_layout = MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT
    material_shader_resource_version = 251112814

    meshlet_has_wilds_layout = True
    meshlet_has_extended_indices = True
    meshlet_uses_pragmata_flag_bits = True
    meshlet_uses_mhwilds_tangent_handedness = True
    meshlet_skin_before_texcoord3 = True
    meshlet_uses_centered_exponent_position_decode = True
    meshlet_uses_dual_exponent_position_decode = True
    meshlet_chunk_payload_is_fully_decoded = True
    meshlet_has_implicit_part_hashes = False
    mply_material_count_uses_name_partition = True
    meshlet_lod_factor_encoding = MeshletLodFactorEncoding.PACKED_BINARY16

    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    normal_redirect_annotation_is_runtime_semantic = True
    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_normal_w_is_runtime_ignored = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    blend_shape_range_has_part_surface_identity = True
    blend_shape_range_identity_is_corpus_zero = True
    blend_shape_u8x3_uses_aabb_quantization = True
    blend_shape_reserved_storage_is_inert = True
    blend_shape_layout_tail_is_producer_residue = True
    meshlet_position_alignment_padding_is_corpus_zero = True
    legacy_reserved_storage_is_corpus_zero = True
    canonical_import_contract = "onimusha_wots"
    canonical_ordinary_requires_colors = True
    canonical_mply_requires_colors = True
    canonical_mply_supports_skinning = False
    canonical_mply_supports_uv2 = False
    canonical_mply_supports_render_flags = False
    canonical_mply_raster_thresholds = frozenset({0, 1})
    canonical_ordinary_content_policy = 2
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4, 5, 7})
    canonical_mply_stores_tangents = False
    authored_blend_shape_codecs = (0, 3)
    authored_meshlet_bounds_exponent_max = 3
    allows_dormant_skin_storage = False


ONIMUSHA_WOTS_MESH_PROFILE = register_mesh_profile(OnimushaWotsMeshProfile())


__all__ = ["ONIMUSHA_WOTS_MESH_PROFILE", "OnimushaWotsMeshProfile"]
