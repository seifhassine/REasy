from .base import (
    Half2ValueDomain,
    BlendShapeLayout,
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


class PragmataMeshProfile(MeshGameProfile):
    game = "PRAGMATA"
    sdf_texture_resource_version = 501527429
    stmesh_version = 251008524
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_NAN,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )
    canonical_mply_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_NAN,
        Half2ValueDomain.FINITE_OR_NAN,
        Half2ValueDomain.FINITE,
    )
    display_name = "Pragmata"
    format_version = MeshMainVersion.PRAGMATA
    version_pairs = frozenset({(250707828, 251121828)})
    meshlet_max_triangles = 126
    meshlet_max_vertices = 128
    default_for_format = True

    header_layout = MeshHeaderLayout.WILDS
    content_flag_layout = MeshContentFlagLayout.WILDS
    buffer_layout = MeshBufferLayout.PRAGMATA
    blend_shape_layout = BlendShapeLayout.EXTENDED
    normal_recalc_layout = NormalRecalcLayout.MHWILDS
    normal_recalc_section_alignment = 4
    skin_weight_layout = SkinWeightLayout.PACKED_10BIT_6

    supports_streaming_buffers = True
    streaming_header_size = 80
    streaming_header_has_extended_prefix = True
    streaming_header_has_wilds_tail = True
    skip_empty_stream_entries = True
    has_shape_key_weight_pointer = True
    has_wilds_buffer_tail = True
    has_shape_key_weight_size = True
    has_auxiliary_index_offsets = True
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
    shape_key_weight_pointer_is_corpus_zero = True
    content_buffer_count_includes_resident = True
    streaming_buffer_index_is_one_based = True
    skin_weight_count_selects_influence_layout = True
    skin_weight_uses_implicit_last_weight = True
    skin_weight_influences_by_raw = ((0x19, 6), (0x1B, 12))
    material_skinning_layout = MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT

    meshlet_uses_legacy_flag_bits = False
    meshlet_has_wilds_layout = True
    meshlet_has_extended_indices = True
    meshlet_streaming_indices_exclude_no_ray_tracing = True
    meshlet_uses_pragmata_flag_bits = True
    meshlet_uses_mhwilds_tangent_handedness = True
    meshlet_skin_before_texcoord3 = True
    meshlet_uses_centered_exponent_position_decode = True
    meshlet_uses_dual_exponent_position_decode = True
    meshlet_chunk_payload_is_fully_decoded = True
    mply_material_count_uses_name_partition = True

    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    normal_redirect_annotation_is_runtime_semantic = True
    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_normal_w_is_runtime_ignored = True
    blend_shape_range_has_part_surface_identity = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    blend_shape_reserved_storage_is_inert = True
    blend_shape_layout_tail_is_producer_residue = True
    ordinary_vertex_layout_has_no_auxiliary_elements = False
    auxiliary_topology_sharing_uses_lod_pointer_identity = True
    auxiliary_index_offsets_are_boundaries = True
    # ch0910_00 relies on D3D's zero out-of-range loads for two shadow plane suffixes.
    auxiliary_vertex_planes_may_omit_zero_suffix = True
    meshlet_lod_factor_encoding = MeshletLodFactorEncoding.PACKED_BINARY16
    meshlet_position_alignment_padding_is_corpus_zero = True
    legacy_reserved_storage_is_corpus_zero = True
    canonical_import_contract = "pragmata"
    canonical_ordinary_requires_colors = True
    canonical_mply_requires_colors = True
    canonical_mply_supports_uv2 = False
    authored_blend_shape_codecs = (0, 1)
    canonical_ordinary_content_policy = 2
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4, 5, 7})
    canonical_mply_raster_thresholds = frozenset({0, 1, 4, 6, 7})
    canonical_mply_stores_tangents = True


PRAGMATA_MESH_PROFILE = register_mesh_profile(PragmataMeshProfile())


__all__ = ["PRAGMATA_MESH_PROFILE", "PragmataMeshProfile"]
