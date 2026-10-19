from .base import (
    Half2ValueDomain,
    BlendShapeLayout,
    LegacyLightMapLayout,
    MeshBufferLayout,
    MeshContentFlagLayout,
    MeshMainVersion,
    NormalRecalcLayout,
    register_mesh_profile,
)
from .re8_profile import Re8MeshProfile


class ReRtMeshProfile(Re8MeshProfile):
    game = "RE_RT"
    display_name = "RE Engine RT version family"
    aliases = ("RE RT",)
    format_version = MeshMainVersion.RE_RT
    version_pairs = frozenset(
        {
            (21041600, 2109108288),
            (21041600, 220128762),
            (21041600, 2109148288),
            (21061800, 2109148288),
            (21091000, 2109148288),
        }
    )
    buffer_layout = MeshBufferLayout.RE_RT
    blend_shape_layout = BlendShapeLayout.RE_RT
    normal_recalc_layout = NormalRecalcLayout.RE_RT
    has_rt_buffer_padding = True
    has_split_legacy_buffer_sizes = False
    canonical_import_contract = None
    submesh_index_ranges_are_even_aligned = False
    skin_weight_influences_by_raw = ()
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )


class Re2Re3RtMeshProfile(ReRtMeshProfile):
    game = "RE2_RE3_RT"
    display_name = "Resident Evil 2 / 3 (RT)"
    aliases = ("RE2 / RE3 RT",)
    version_pairs = frozenset({(21041600, 2109108288)})
    primary_version_pairs = version_pairs
    default_for_format = False
    blend_shape_layout = BlendShapeLayout.RE2_RT
    normal_recalc_layout = NormalRecalcLayout.RE2_RT
    content_flag_layout = MeshContentFlagLayout.GENERIC
    legacy_light_map_layout = LegacyLightMapLayout.BYTE_FIELDS
    skin_weight_uses_implicit_last_weight = True
    blend_shape_layout_tail_is_producer_residue = True
    submesh_index_ranges_are_even_aligned = True
    skin_weight_influences_by_raw = ((0x12, 8),)
    canonical_import_contract = "mesh"
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE,
    )


RE_RT_MESH_PROFILE = register_mesh_profile(ReRtMeshProfile())
RE2_RE3_RT_MESH_PROFILE = register_mesh_profile(Re2Re3RtMeshProfile())


__all__ = [
    "RE2_RE3_RT_MESH_PROFILE",
    "RE_RT_MESH_PROFILE",
    "Re2Re3RtMeshProfile",
    "ReRtMeshProfile",
]
