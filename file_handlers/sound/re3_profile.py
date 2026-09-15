"""Resident Evil 3 sound-editor profiles."""

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


class Re3RtSoundMetadata(Re3SoundMetadata):
    index_resource = "resources/data/sound/re3rt.json.gz"
    game_name = "RE3 RT"


class Re3RtSoundProfile(Re3SoundProfile):
    game = "RE3RT"
    display_name = "Resident Evil 3 (ray tracing/RT)"
    aliases = ("RE3 RT", "Resident Evil 3 RT", "Resident Evil 3 (RT)")
    bank_versions = frozenset({135})
    required_year = 2019
    required_major = 2
    recommended_version = "2019.2.15.7667"
    metadata_type = Re3RtSoundMetadata


RE3_SOUND_PROFILE = register_sound_profile(Re3SoundProfile())
RE3RT_SOUND_PROFILE = register_sound_profile(Re3RtSoundProfile())


__all__ = [
    "RE3_SOUND_PROFILE",
    "RE3RT_SOUND_PROFILE",
    "Re3SoundMetadata",
    "Re3SoundProfile",
    "Re3RtSoundMetadata",
    "Re3RtSoundProfile",
]
