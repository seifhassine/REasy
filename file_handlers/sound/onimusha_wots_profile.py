"""Onimusha: Way of the Sword integration."""

import re

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSbnkSoundProfile
from .sound_profile import register_sound_profile


class OnimushaWotsSoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/oniwots.json.gz"
    game_name = "Onimusha: Way of the Sword"
    fallbacks = STANDARD_CUE_FALLBACKS


class OnimushaWotsSoundProfile(ReEngineSbnkSoundProfile):
    game = "OnimushaWOTS"
    display_name = "Onimusha: Way of the Sword"
    aliases = ("ONIWOTS", "OnimushaWotS_Demo", "Onimusha Way of the Sword")
    bank_versions = frozenset({150})
    split_sbnk_roles = True
    required_year = 2023
    required_major = 1
    recommended_version = "2023.1.16.8822"
    convolution_reverb_plugin_id = 0x007F0003
    metadata_type = OnimushaWotsSoundMetadata

    def split_bank_family(self, path: str) -> str:
        family = re.sub(r"_1stchunk(?=\.|$)", "", super().split_bank_family(path))
        # Mission event banks share media split across numbered mission banks,
        # including ms10xxxx NPC-space banks. Source IDs still select each link.
        return re.sub(
            r"^(fsm_(?:mainmission|submission))_ms[0-9x]{6}(?:_[^.]+)?(?=\.|$)",
            r"\1", family,
        )


ONIMUSHA_WOTS_SOUND_PROFILE = register_sound_profile(OnimushaWotsSoundProfile())


__all__ = [
    "ONIMUSHA_WOTS_SOUND_PROFILE", "OnimushaWotsSoundMetadata",
    "OnimushaWotsSoundProfile",
]
