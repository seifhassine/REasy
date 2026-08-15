"""Monster Hunter Rise integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSoundProfile
from .sound_profile import register_sound_profile


class MHRiseSoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/mhrise.json.gz"
    game_name = "MHRise"
    fallbacks = STANDARD_CUE_FALLBACKS


class MHRiseSoundProfile(ReEngineSoundProfile):
    game = "MHRise"
    display_name = "Monster Hunter Rise"
    aliases = ("MonsterHunterRise", "MHR", "MH Rise")
    bank_versions = frozenset({140})
    wel_versions = frozenset({11})
    required_year = 2021
    required_major = 1
    recommended_version = "2021.1.14.8108"
    platform = "stm"
    file_platforms = frozenset({"x64"})
    sound_root = "natives/stm/sound/"
    metadata_type = MHRiseSoundMetadata
    wem_codecs = ReEngineSoundProfile.platinum_wem_codecs


MHRISE_SOUND_PROFILE = register_sound_profile(MHRiseSoundProfile())


__all__ = ["MHRISE_SOUND_PROFILE", "MHRiseSoundMetadata", "MHRiseSoundProfile"]
