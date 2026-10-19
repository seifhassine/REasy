from .base import (
    Half2ValueDomain,
    BlendShapeLayout,
    MeshBufferLayout,
    MeshContentFlagLayout,
    MeshHeaderLayout,
    MeshMainVersion,
    SkinWeightLayout,
    register_mesh_profile,
)
from .sf6_profile import Sf6MeshProfile


class Dd2OldMeshProfile(Sf6MeshProfile):
    canonical_ordinary_supports_32bit_indices = False
    game = "DD2OLD"
    stmesh_version = None
    display_name = "Dragon's Dogma 2 (early)"
    aliases = ("DD2 Old", "DD2_OLD")
    format_version = MeshMainVersion.DD2_OLD
    version_pairs = frozenset({(230517984, 231011879)})
    header_layout = MeshHeaderLayout.DD2
    content_flag_layout = MeshContentFlagLayout.GENERIC
    buffer_layout = MeshBufferLayout.DD2
    blend_shape_layout = BlendShapeLayout.EXTENDED
    skin_weight_layout = SkinWeightLayout.BYTE8
    # SkinningMesh8 reads WEIGHT0.xyzw and WEIGHT1.xyz; INDEX1.w gets 1 - sum of those seven.
    skin_weight_uses_implicit_last_weight = True
    supports_additional_vertex_weights = True
    ordinary_lod_factor_supports_nan = False
    normal_recalc_has_split_vertex_page_plane = False
    shape_key_weight_pointer_is_corpus_zero = False
    resident_buffer_tail_is_corpus_zero = False
    streaming_buffer_tail_is_corpus_zero = False
    shadow_has_reserved_slots = False
    meshlet_uses_legacy_flag_bits = True
    meshlet_uses_centered_exponent_position_decode = True
    meshlet_skin_before_texcoord3 = True
    meshlet_chunk_payload_is_fully_decoded = True
    meshlet_has_implicit_part_hashes = True
    canonical_import_contract = "dd2"
    canonical_ordinary_requires_colors = True
    canonical_mply_requires_colors = True
    canonical_mply_supports_pivots = False
    canonical_mply_supports_uv2 = False
    canonical_supports_occluders = False
    canonical_single_channel_morph_targets = True
    canonical_lod_levels_follow_order = True
    canonical_single_part_shadow = True
    canonical_mply_supports_material_extension = False
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4, 5})
    authored_blend_shape_codecs = (1,)
    canonical_ordinary_content_policy = 2
    skin_weight_influences_by_raw = ((0x12, 8),)
    name_string_data_alignment = 16
    skeleton_hierarchy_alignment = 16
    canonical_bounding_box_w = 1.0
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )
    @property
    def material_shader_resource_version(self):
        if self.game == "DD2OLD":
            return 240112003
        return super().material_shader_resource_version


class Dd2MeshProfile(Dd2OldMeshProfile):
    game = "DD2"
    registration_key = "DD2RETAIL"
    display_name = "Dragon's Dogma 2 (original retail)"
    aliases = ("DD2 Retail",)
    format_version = MeshMainVersion.DD2
    version_pairs = frozenset({(230517984, 240423143)})
    # Some retail resources align the header to 4 (address % 8 == 4).
    normal_recalc_section_alignment = 4
    submesh_has_runtime_ignored_tail_residue = True
    @property
    def material_shader_resource_version(self):
        if self.game == "DD2":
            return 240423143
        return super().material_shader_resource_version


DD2_OLD_MESH_PROFILE = register_mesh_profile(Dd2OldMeshProfile())
DD2_MESH_PROFILE = register_mesh_profile(Dd2MeshProfile())


__all__ = [
    "DD2_MESH_PROFILE",
    "DD2_OLD_MESH_PROFILE",
    "Dd2MeshProfile",
    "Dd2OldMeshProfile",
]
