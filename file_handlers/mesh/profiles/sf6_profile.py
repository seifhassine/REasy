from .base import (
    Half2ValueDomain,
    BlendShapeLayout,
    MeshContentFlagLayout,
    MeshMainVersion,
    NormalRecalcLayout,
    SkinWeightLayout,
    VertexNormalWSemantic,
    register_mesh_profile,
)
from .re4_profile import Re4MeshProfile


class Sf6MeshProfile(Re4MeshProfile):
    game = "SF6"
    sdf_texture_resource_version = None  # SF6 ships no SDF textures; its meshes reference none.
    stmesh_version = 221031760
    display_name = "Street Fighter 6"
    aliases = ("Street Fighter 6",)
    format_version = MeshMainVersion.SF6
    version_pairs = frozenset(
        {
            (220705151, 230110883),
            (230403828, 230110883),
        }
    )
    content_flag_layout = MeshContentFlagLayout.SF6
    blend_shape_layout = BlendShapeLayout.SF6
    normal_recalc_layout = NormalRecalcLayout.SF6
    normal_recalc_has_split_vertex_page_plane = True
    skin_weight_layout = SkinWeightLayout.PACKED_10BIT_6
    skin_weight_uses_implicit_last_weight = True
    # Same record as DD2's VertexElementSkin8; SF6 reads five weights and derives the sixth.
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    # position.w and normal.w are never read by the compute shaders.
    blend_shape_standard_w_is_padding = False
    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_normal_w_is_padding = False
    blend_shape_normal_w_is_runtime_ignored = True
    blend_shape_nan_positions_cull_vertices = True
    blend_shape_type3_is_enum_sentinel = True
    supports_streaming_buffers = True
    streaming_buffer_tail_is_corpus_zero = True
    streaming_buffer_index_is_one_based = True
    content_buffer_count_can_wrap = False
    canonical_buffer_count_limit = 7
    submesh_has_buffer_index = True
    lod_has_level = True
    ordinary_lod_factor_supports_nan = True
    skin_weight_influences_by_raw = ((0x09, 6),)
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4, 5, 6})
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
    )


SF6_MESH_PROFILE = register_mesh_profile(Sf6MeshProfile())


__all__ = ["SF6_MESH_PROFILE", "Sf6MeshProfile"]
