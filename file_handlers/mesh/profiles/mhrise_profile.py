from .base import (
    Half2ValueDomain,
    BlendShapeLayout,
    LegacyLightMapLayout,
    MeshContentFlagLayout,
    NormalRecalcLayout,
    register_mesh_profile,
)
from .re_rt_profile import ReRtMeshProfile


class MhRiseMeshProfile(ReRtMeshProfile):
    game = "MHRISE"
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE,
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
    )
    display_name = "Monster Hunter Rise / Sunbreak"
    aliases = (
        "MHR",
        "MHRise",
        "MHRSunbreak",
        "MonsterHunterRise",
        "Monster Hunter Rise",
        "Monster Hunter Rise Sunbreak",
    )
    version_pairs = frozenset(
        {
            (21061800, 2109148288),
            (21091000, 2109148288),
        }
    )
    primary_version_pairs = version_pairs
    default_for_format = False
    content_flag_layout = MeshContentFlagLayout.GENERIC
    legacy_light_map_layout = LegacyLightMapLayout.BYTE_FIELDS
    blend_shape_vertex_offsets_are_lod_relative = True
    blend_shape_layout = BlendShapeLayout.MHRISE
    normal_recalc_layout = NormalRecalcLayout.MHRISE
    auxiliary_index_domain_end_marks_main_payload_alias = True
    blend_shape_layout_tail_is_producer_residue = True
    submesh_index_ranges_are_even_aligned = True
    skin_weight_influences_by_raw = ((0x12, 8),)
    canonical_import_contract = "mesh"
    canonical_vertex_semantics = frozenset({0, 1, 2, 3, 4, 5})


MHRISE_MESH_PROFILE = register_mesh_profile(MhRiseMeshProfile())


__all__ = ["MHRISE_MESH_PROFILE", "MhRiseMeshProfile"]
