from .base import (
    Half2ValueDomain,
    MeshGameProfile,
    MeshMainVersion,
    MaterialSkinningLayout,
    NormalRecalcLayout,
    VertexNormalWSemantic,
    register_mesh_profile,
)
from .re_rt_profile import Re2Re3RtMeshProfile


class Re3MeshProfile(MeshGameProfile):
    game = "RE3"
    display_name = "Resident Evil 3"
    aliases = ("Resident Evil 3", "Resident Evil 3: Nemesis")
    format_version = MeshMainVersion.DMC5
    version_pairs = frozenset({(386270720, 1902042334)})
    normal_recalc_layout = NormalRecalcLayout.RE3
    header_trailing_padding = 8
    legacy_buffer_sizes_are_vertex_and_index = True
    material_skinning_layout = MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8
    legacy_flags_are_content_flags = True
    legacy_reserved_storage_is_corpus_zero = True
    vertex_normal_w_semantic = VertexNormalWSemantic.LOCAL_PIVOT_INDEX
    auxiliary_vertex_layout_order = ("shadow", "occluder")
    submesh_index_ranges_are_even_aligned = True
    skin_weight_influences_by_raw = ((0x01, 8),)
    canonical_import_contract = "mesh"
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4})
    canonical_ordinary_supports_32bit_indices = False
    # The shipped CSBlendShape* programs never read morph W lanes (see Re3ResistanceMeshProfile).
    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_normal_w_is_runtime_ignored = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )


class Re3RtMeshProfile(Re2Re3RtMeshProfile):
    game = "RE3RT"
    display_name = "Resident Evil 3 (RT)"
    aliases = ("RE3 RT", "RE3_RT", "RE3_RT_STM", "Resident Evil 3 RT")
    primary_version_pairs = frozenset()


RE3_MESH_PROFILE = register_mesh_profile(Re3MeshProfile())
RE3RT_MESH_PROFILE = register_mesh_profile(Re3RtMeshProfile())


__all__ = [
    "RE3_MESH_PROFILE",
    "RE3RT_MESH_PROFILE",
    "Re3MeshProfile",
    "Re3RtMeshProfile",
]
