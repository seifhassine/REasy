"""Monster Hunter Wilds integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSbnkSoundProfile
from .sound_profile import register_sound_profile


class MhWildsSoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/mhwilds.json.gz"
    game_name = "Monster Hunter Wilds"
    fallbacks = STANDARD_CUE_FALLBACKS


class MhWildsSoundProfile(ReEngineSbnkSoundProfile):
    game = "MHWilds"
    display_name = "Monster Hunter Wilds"
    aliases = ("MonsterHunterWilds", "MHWS", "MHWILDS")
    bank_versions = frozenset({145})
    split_sbnk_roles = True
    required_year = 2022
    required_major = 1
    recommended_version = "2022.1.19.8584"
    convolution_reverb_plugin_id = 0x007F0003
    metadata_type = MhWildsSoundMetadata


MHWILDS_SOUND_PROFILE = register_sound_profile(MhWildsSoundProfile())


__all__ = [
    "MHWILDS_SOUND_PROFILE", "MhWildsSoundMetadata", "MhWildsSoundProfile",
]
