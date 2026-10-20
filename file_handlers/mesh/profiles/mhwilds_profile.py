from .base import (
    Half2ValueDomain,
    MeshContentFlagLayout,
    MeshMainVersion,
    MeshletLodFactorEncoding,
    NormalRecalcLayout,
    SkinWeightLayout,
    VertexNormalWSemantic,
    register_mesh_profile,
)
from .o2_profile import Onimusha2MeshProfile


class MhWildsMeshProfile(Onimusha2MeshProfile):
    canonical_ordinary_supports_32bit_indices = True
    game = "MHWS"
    sdf_texture_resource_version = 481229356
    stmesh_version = 241111606
    canonical_mply_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
    )
    display_name = "Monster Hunter Wilds"
    aliases = ("MHWILDS", "MonsterHunterWilds", "Monster Hunter Wilds")
    format_version = MeshMainVersion.MHWILDS
    version_pairs = frozenset({(240704828, 241111606)})
    meshlet_max_triangles = 128
    meshlet_max_vertices = 128
    content_flag_layout = MeshContentFlagLayout.WILDS
    content_uv_flag_uses_light_map_metadata = True
    skin_weight_layout = SkinWeightLayout.PACKED_10BIT_6
    skin_weight_count_selects_influence_layout = True
    streaming_header_has_wilds_tail = True
    skip_empty_stream_entries = True
    has_wilds_buffer_tail = True
    meshlet_has_wilds_layout = True
    meshlet_has_extended_indices = True
    meshlet_streaming_indices_exclude_no_ray_tracing = True
    # The first 32-byte MeshletHeader union is zero on disk.
    meshlet_lod_factor_encoding = MeshletLodFactorEncoding.CPU_UNUSED
    content_buffer_count_includes_resident = True
    submesh_has_runtime_ignored_tail_residue = False
    submesh_reserve03_is_runtime_ignored = True
    submesh_has_wilds_tail = True
    submesh_index_ranges_are_even_aligned = True
    # Position decode uses the float3 center, relative-AABB minima and exponent bias 143.
    meshlet_uses_centered_exponent_position_decode = True
    meshlet_uses_dual_exponent_position_decode = True
    meshlet_uses_mhwilds_tangent_handedness = True
    meshlet_skin_before_texcoord3 = False
    meshlet_has_implicit_part_hashes = False
    supports_additional_vertex_weights = False
    shape_key_weight_pointer_is_corpus_zero = True
    normal_recalc_layout = NormalRecalcLayout.MHWILDS
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_nan_positions_cull_vertices = False
    blend_shape_type3_is_enum_sentinel = False
    canonical_import_contract = "mhwilds"
    canonical_ordinary_requires_colors = False
    canonical_supports_light_maps = True
    canonical_vertex_semantics = frozenset(range(8))
    canonical_supports_occluders = True
    canonical_single_channel_morph_targets = False
    canonical_lod_levels_follow_order = False
    canonical_single_part_shadow = False
    shadow_has_reserved_slots = True
    canonical_buffer_count_limit = None
    content_buffer_count_can_wrap = True
    canonical_mply_supports_pivots = True
    canonical_mply_supports_uv2 = True
    canonical_mply_requires_colors = False
    canonical_mply_supports_skinning = False
    canonical_mply_supports_skeleton = False
    authored_blend_shape_codecs = (0, 1)
    skin_weight_influences_by_raw = ((0x19, 6), (0x1B, 12))
    ordinary_vertex_layout_has_no_auxiliary_elements = True
    meshlet_position_alignment_padding_is_corpus_zero = True
    legacy_reserved_storage_is_corpus_zero = True
    group_pivot_payload_is_trailing = True


MHWILDS_MESH_PROFILE = register_mesh_profile(MhWildsMeshProfile())


__all__ = ["MHWILDS_MESH_PROFILE", "MhWildsMeshProfile"]
