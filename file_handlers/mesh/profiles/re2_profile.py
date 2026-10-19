from .base import (
    MaterialSkinningLayout,
    MeshGameProfile,
    MeshMainVersion,
    NormalRecalcLayout,
    VertexNormalWSemantic,
    register_mesh_profile,
)
from .re_rt_profile import Re2Re3RtMeshProfile


class Re2MeshProfile(MeshGameProfile):
    game = "RE2"
    display_name = "Resident Evil 2"
    aliases = ("Resident Evil 2", "Resident Evil 2 / Biohazard RE:2")
    format_version = MeshMainVersion.DMC5
    version_pairs = frozenset({(386270720, 1808312334)})
    normal_recalc_layout = NormalRecalcLayout.RE2
    header_trailing_padding = 8
    legacy_buffer_sizes_are_vertex_and_index = True
    material_skinning_layout = MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8
    # Non-RT palette upload uses a uint8 counter, unlike the RT build.
    max_deform_bones = 255
    legacy_flags_are_content_flags = True
    legacy_reserved_storage_is_corpus_zero = True
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    auxiliary_vertex_layout_order = ("shadow", "occluder")
    blend_shape_standard_w_is_padding = True
    blend_shape_normal_w_is_padding = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    submesh_index_ranges_are_even_aligned = True
    skin_weight_influences_by_raw = ((0x01, 8),)
    canonical_import_contract = "mesh"
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4})
    canonical_ordinary_supports_32bit_indices = False


class Re2RtMeshProfile(Re2Re3RtMeshProfile):
    game = "RE2RT"
    display_name = "Resident Evil 2 (RT)"
    aliases = ("RE2 RT", "RE2_RT", "RE2_RT_STM", "Resident Evil 2 RT")
    primary_version_pairs = frozenset()


RE2_MESH_PROFILE = register_mesh_profile(Re2MeshProfile())
RE2RT_MESH_PROFILE = register_mesh_profile(Re2RtMeshProfile())


__all__ = [
    "RE2_MESH_PROFILE",
    "RE2RT_MESH_PROFILE",
    "Re2MeshProfile",
    "Re2RtMeshProfile",
]
