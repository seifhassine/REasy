"""Resident Evil 2 sound-editor profiles."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSoundProfile
from .sound_profile import register_sound_profile


class Re2SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/re2.json.gz"
    game_name = "RE2"


class Re2SoundProfile(ReEngineSoundProfile):
    game = "RE2"
    display_name = "Resident Evil 2 (legacy)"
    aliases = ("Resident Evil 2", "Resident Evil 2 / Biohazard RE:2")
    bank_versions = frozenset({125})
    wel_versions = frozenset({11})
    required_year = 2017
    required_major = 1
    recommended_version = "2017.1.9"
    hybrid_reverb_plugin_id = 0x00021033
    sound_root = "natives/x64/sectionroot/sound/"
    metadata_type = Re2SoundMetadata


class Re2RtSoundMetadata(Re2SoundMetadata):
    index_resource = "resources/data/sound/re2rt.json.gz"
    game_name = "RE2 RT"
    fallbacks = STANDARD_CUE_FALLBACKS


class Re2RtSoundProfile(Re2SoundProfile):
    game = "RE2RT"
    display_name = "Resident Evil 2 (ray tracing/RT)"
    aliases = ("RE2 RT", "Resident Evil 2 RT", "Resident Evil 2 (RT)")
    bank_versions = frozenset({135})
    required_year = 2019
    required_major = 2
    recommended_version = "2019.2.15.7667"
    platform = "stm"
    sound_root = "natives/stm/sectionroot/sound/"
    metadata_type = Re2RtSoundMetadata


RE2_SOUND_PROFILE = register_sound_profile(Re2SoundProfile())
RE2RT_SOUND_PROFILE = register_sound_profile(Re2RtSoundProfile())


__all__ = [
    "RE2_SOUND_PROFILE",
    "RE2RT_SOUND_PROFILE",
    "Re2SoundMetadata",
    "Re2SoundProfile",
    "Re2RtSoundMetadata",
    "Re2RtSoundProfile",
]
