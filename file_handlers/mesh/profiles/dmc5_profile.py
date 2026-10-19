from .base import (
    Half2ValueDomain,
    MeshGameProfile,
    MeshMainVersion,
    MaterialSkinningLayout,
    NormalRecalcLayout,
    VertexNormalWSemantic,
    register_mesh_profile,
)


class Dmc5MeshProfile(MeshGameProfile):
    game = "DMC5"
    display_name = "Devil May Cry 5"
    aliases = ("Devil May Cry 5",)
    format_version = MeshMainVersion.DMC5
    version_pairs = frozenset({(386270720, 1808282334)})
    default_for_format = True
    normal_recalc_layout = NormalRecalcLayout.DMC5
    header_trailing_padding = 8
    legacy_buffer_sizes_are_vertex_and_index = True
    material_skinning_layout = MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8
    # Native palette upload increments a uint8 counter; 256 entries wrap.
    max_deform_bones = 255
    legacy_flags_are_content_flags = True
    legacy_reserved_storage_is_corpus_zero = True
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    # Auxiliary vertex planes follow the main declaration.
    auxiliary_vertex_layout_order = ("shadow", "occluder")
    blend_shapes_by_descending_weight = True
    blend_shape_standard_w_is_padding = True
    blend_shape_normal_w_is_padding = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    submesh_index_ranges_are_even_aligned = True
    skin_weight_influences_by_raw = ((0x01, 8),)
    canonical_import_contract = "mesh"
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4})
    canonical_ordinary_supports_32bit_indices = False
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_NAN,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )


DMC5_MESH_PROFILE = register_mesh_profile(Dmc5MeshProfile())


__all__ = ["DMC5_MESH_PROFILE", "Dmc5MeshProfile"]
