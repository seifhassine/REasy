from .base import (
    BlendShapeLayout, MeshBufferLayout, MeshContentFlagLayout, MeshGameProfile,
    MeshHeaderLayout, MeshMainVersion, MeshletLodFactorEncoding,
    NormalRecalcLayout, VertexNormalWSemantic,
    register_mesh_profile,
)


class Dd2UpdatedMeshProfile(MeshGameProfile):
    canonical_ordinary_supports_32bit_indices = False
    game = "DD2"
    sdf_texture_resource_version = 502231882
    stmesh_version = 260421070
    display_name = "Dragon's Dogma 2"
    aliases = ("Dragons Dogma 2", "DD2 Updated")
    format_version = MeshMainVersion.DD2_UPDATED
    version_pairs = frozenset({(251205828, 260421070)})
    meshlet_max_triangles = 126
    meshlet_max_vertices = 128
    default_for_format = True

    header_layout = MeshHeaderLayout.WILDS
    content_flag_layout = MeshContentFlagLayout.DD2_UPDATED
    header_buffer_count_bits = 4
    buffer_layout = MeshBufferLayout.PRAGMATA
    blend_shape_layout = BlendShapeLayout.EXTENDED
    normal_recalc_layout = NormalRecalcLayout.DD2_UPDATED
    normal_recalc_section_alignment = 4
    skin_weight_uses_implicit_last_weight = True
    skin_weight_influences_by_raw = ((0x12, 8),)

    supports_streaming_buffers = True
    streaming_header_size = 80
    streaming_header_has_extended_prefix = True
    streaming_header_has_wilds_tail = True
    skip_empty_stream_entries = True
    has_shape_key_weight_pointer = True
    has_shape_key_weight_size = True
    has_wilds_buffer_tail = True
    has_auxiliary_index_offsets = True
    supports_additional_vertex_weights = True
    additional_vertex_weights_size_in_modern_tail = True
    streaming_buffer_index_is_one_based = True
    content_buffer_count_includes_resident = True
    optional_gpu_offsets_use_relative_null = True
    ordinary_vertex_layout_has_no_auxiliary_elements = True
    auxiliary_vertex_layout_order = ("shadow", "occluder")
    auxiliary_topology_sharing_uses_lod_pointer_identity = True
    auxiliary_index_offsets_are_boundaries = True

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
    shadow_has_legacy_pad = False
    name_string_data_alignment = 16
    skeleton_hierarchy_alignment = 16
    canonical_bounding_box_w = 1.0
    group_pivot_payload_is_trailing = True
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    normal_redirect_annotation_is_runtime_semantic = True

    meshlet_has_wilds_layout = True
    meshlet_extension_has_skin_weight_count = True
    meshlet_streaming_indices_exclude_no_ray_tracing = True
    meshlet_has_extended_indices = True
    meshlet_uses_pragmata_flag_bits = True
    meshlet_uses_mhwilds_tangent_handedness = True
    meshlet_skin_before_texcoord3 = True
    meshlet_uses_centered_exponent_position_decode = True
    meshlet_uses_dual_exponent_position_decode = True
    meshlet_chunk_payload_is_fully_decoded = True
    mply_material_count_uses_name_partition = True
    meshlet_lod_factor_encoding = MeshletLodFactorEncoding.PACKED_BINARY16
    meshlet_position_alignment_padding_is_corpus_zero = True

    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_normal_w_is_runtime_ignored = True
    blend_shape_range_has_part_surface_identity = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    blend_shape_reserved_storage_is_inert = True
    blend_shape_layout_tail_is_producer_residue = True
    legacy_reserved_storage_is_corpus_zero = True
    canonical_ordinary_content_policy = 2
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4, 5})
    canonical_import_contract = "dd2_updated"
    authored_blend_shape_codecs = (1,)


DD2_UPDATED_MESH_PROFILE = register_mesh_profile(Dd2UpdatedMeshProfile())
