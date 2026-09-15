"""Resident Evil 7 RT integration policy for the sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSoundProfile
from .sound_profile import register_sound_profile


class Re7RtSoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/re7rt.json.gz"
    game_name = "RE7 RT"
    fallbacks = STANDARD_CUE_FALLBACKS


class Re7RtSoundProfile(ReEngineSoundProfile):
    game = "RE7RT"
    display_name = "Resident Evil 7 biohazard (ray tracing/RT)"
    aliases = (
        "RE7 RT",
        "Resident Evil 7 RT",
        "Resident Evil 7 biohazard RT",
        "Biohazard 7 RT",
    )
    bank_versions = frozenset({135})
    wel_versions = frozenset({11})
    required_year = 2019
    required_major = 2
    recommended_version = "2019.2.15.7667"
    convolution_reverb_plugin_id = 0x007F0003
    platform = "stm"
    sound_root = "natives/stm/sound/"
    metadata_type = Re7RtSoundMetadata


RE7RT_SOUND_PROFILE = register_sound_profile(Re7RtSoundProfile())


__all__ = ["RE7RT_SOUND_PROFILE", "Re7RtSoundMetadata", "Re7RtSoundProfile"]
