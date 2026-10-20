from .base import MaterialSkinningLayout, register_mesh_profile
from .re3_resistance_profile import Re3ResistanceMeshProfile


class ReResistanceMeshProfile(Re3ResistanceMeshProfile):
    game = "RERESISTANCE"
    display_name = "Resident Evil Resistance"
    aliases = (
        "RE Resistance",
        "Resident Evil Resistance",
    )
    version_pairs = frozenset({(386270720, 1902042334)})
    primary_version_pairs = frozenset()
    default_for_format = False
    material_skinning_layout = MaterialSkinningLayout.LEGACY_DXBC_RESIDUAL_BYTE8


RE_RESISTANCE_MESH_PROFILE = register_mesh_profile(ReResistanceMeshProfile())


__all__ = ["RE_RESISTANCE_MESH_PROFILE", "ReResistanceMeshProfile"]
