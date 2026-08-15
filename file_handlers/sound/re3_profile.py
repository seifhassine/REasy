"""Resident Evil 3 legacy/DX11 integration policy for the sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSoundProfile
from .sound_profile import register_sound_profile


class Re3SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/re3.json.gz"
    game_name = "RE3"
    fallbacks = STANDARD_CUE_FALLBACKS


class Re3SoundProfile(ReEngineSoundProfile):
    game = "RE3"
    display_name = "Resident Evil 3 (legacy/DX11)"
    aliases = ("Resident Evil 3", "Resident Evil 3 / Biohazard RE:3")
    bank_versions = frozenset({132})
    wel_versions = frozenset({11})
    required_year = 2018
    required_major = 1
    recommended_version = "2018.1.11"
    hybrid_reverb_plugin_id = 0x00021033
    platform = "stm"
    sound_root = "natives/stm/escape/sound/"
    metadata_type = Re3SoundMetadata


RE3_SOUND_PROFILE = register_sound_profile(Re3SoundProfile())


__all__ = ["RE3_SOUND_PROFILE", "Re3SoundMetadata", "Re3SoundProfile"]
