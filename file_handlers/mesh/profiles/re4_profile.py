from .base import (
    Half2ValueDomain,
    BlendShapeLayout,
    MeshBufferLayout,
    MeshContentFlagLayout,
    MeshHeaderLayout,
    MeshMainVersion,
    NormalRecalcLayout,
    register_mesh_profile,
)
from .re_rt_profile import ReRtMeshProfile


class Re4MeshProfile(ReRtMeshProfile):
    game = "RE4"
    sdf_texture_resource_version = 1628766211
    stmesh_version = 221108255
    display_name = "Resident Evil 4"
    aliases = ("Resident Evil 4", "Resident Evil 4 Remake")
    format_version = MeshMainVersion.RE4
    version_pairs = frozenset({(220822879, 221108797)})
    header_layout = MeshHeaderLayout.RE4
    content_flag_layout = MeshContentFlagLayout.GENERIC
    buffer_layout = MeshBufferLayout.RE4
    blend_shape_layout = BlendShapeLayout.RE4
    normal_recalc_layout = NormalRecalcLayout.RE4
    has_shape_key_weight_pointer = True
    has_auxiliary_index_offsets = True
    has_rt_buffer_padding = False
    submesh_has_modern_material = True
    mesh_data_has_modern_bounds = True
    canonical_modern_aabb_padding = 1
    optional_gpu_offsets_use_relative_null = True
    content_buffer_count_includes_resident = True
    blend_shape_layout_tail_is_producer_residue = True
    resident_buffer_tail_is_corpus_zero = True
    shape_key_weight_pointer_is_corpus_zero = True
    submesh_index_ranges_are_even_aligned = True
    skin_weight_influences_by_raw = ((0x12, 8),)
    canonical_import_contract = "mesh"
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4, 5})
    canonical_ordinary_supports_32bit_indices = True
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.IEEE754,
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
    )


RE4_MESH_PROFILE = register_mesh_profile(Re4MeshProfile())


__all__ = ["RE4_MESH_PROFILE", "Re4MeshProfile"]
