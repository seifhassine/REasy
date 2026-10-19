from .base import (
    Half2ValueDomain,
    BlendShapeLayout,
    LegacyLightMapLayout,
    MeshBufferLayout,
    MeshContentFlagLayout,
    MeshGameProfile,
    MeshMainVersion,
    NormalRecalcLayout,
    VertexNormalWSemantic,
    register_mesh_profile,
)


class Re8MeshProfile(MeshGameProfile):
    game = "RE8"
    display_name = "Resident Evil Village"
    aliases = (
        "Resident Evil 8",
        "Resident Evil Village",
        "Resident Evil Village BIOHAZARD VILLAGE",
        "RE Village",
    )
    format_version = MeshMainVersion.RE8
    version_pairs = frozenset({(2020091500, 2101050001)})
    default_for_format = True
    content_flag_layout = MeshContentFlagLayout.RE8
    legacy_light_map_layout = LegacyLightMapLayout.RE8
    buffer_layout = MeshBufferLayout.RE8
    blend_shape_layout = BlendShapeLayout.RE8
    normal_recalc_layout = NormalRecalcLayout.RE8
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )
    has_split_legacy_buffer_sizes = True
    legacy_buffer_sizes_are_vertex_and_index = True
    submesh_has_streaming_offsets = True
    mesh_data_has_legacy_unknown = False
    shadow_has_legacy_pad = False
    legacy_flags_are_content_flags = True
    legacy_reserved_storage_is_corpus_zero = True
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    auxiliary_vertex_layout_order = ("shadow", "occluder")
    blend_shape_standard_w_is_padding = True
    blend_shape_normal_w_is_padding = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    blend_shape_reserved_storage_is_inert = True
    normal_redirect_annotation_is_runtime_semantic = True
    submesh_index_ranges_are_even_aligned = True
    skin_weight_influences_by_raw = ((0x01, 8),)
    canonical_import_contract = "mesh"
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4})
    canonical_ordinary_supports_32bit_indices = False


RE8_MESH_PROFILE = register_mesh_profile(Re8MeshProfile())


__all__ = ["RE8_MESH_PROFILE", "Re8MeshProfile"]
