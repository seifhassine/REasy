from .base import (
    Half2ValueDomain,
    MaterialSkinningLayout,
    NormalRecalcLayout,
    register_mesh_profile,
)
from .dmc5_profile import Dmc5MeshProfile


class Re3ResistanceMeshProfile(Dmc5MeshProfile):
    # RE3 and Resistance widened native palette upload to a uint16 counter.
    max_deform_bones = 256
    game = "RE3_RESISTANCE"
    display_name = "Resident Evil 3 / Resistance"
    aliases = ("RE3 / Resistance",)
    version_pairs = frozenset(
        {
            (386270720, 1902042334),
            (21011200, 1902042334),
        }
    )
    primary_version_pairs = version_pairs
    read_only_version_pairs = frozenset({(21011200, 1902042334)})
    normal_recalc_layout = NormalRecalcLayout.RE3
    material_skinning_layout = MaterialSkinningLayout.NONE
    # The shipped CSBlendShape* programs never read the W lanes
    blend_shape_standard_w_is_padding = False
    blend_shape_standard_w_is_runtime_ignored = True
    blend_shape_normal_w_is_padding = False
    blend_shape_normal_w_is_runtime_ignored = True
    blend_shape_aabb_w_is_runtime_ignored = True
    blend_shape_deform_percent_is_runtime_ignored = True
    canonical_ordinary_uv_half2_domains = (
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE_OR_INFINITY,
        Half2ValueDomain.FINITE,
    )


RE3_RESISTANCE_MESH_PROFILE = register_mesh_profile(
    Re3ResistanceMeshProfile()
)


__all__ = ["RE3_RESISTANCE_MESH_PROFILE", "Re3ResistanceMeshProfile"]
