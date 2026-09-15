"""PRAGMATA integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSbnkSoundProfile
from .sound_profile import register_sound_profile


class PragmataSoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/pragmata.json.gz"
    game_name = "PRAGMATA"
    fallbacks = STANDARD_CUE_FALLBACKS


class PragmataSoundProfile(ReEngineSbnkSoundProfile):
    game = "Pragmata"
    display_name = "PRAGMATA"
    bank_versions = frozenset({150})
    split_sbnk_roles = True
    required_year = 2023
    required_major = 1
    recommended_version = "2023.1.16.8822"
    convolution_reverb_plugin_id = 0x007F0003
    metadata_type = PragmataSoundMetadata


PRAGMATA_SOUND_PROFILE = register_sound_profile(PragmataSoundProfile())


__all__ = [
    "PRAGMATA_SOUND_PROFILE", "PragmataSoundMetadata", "PragmataSoundProfile",
]
