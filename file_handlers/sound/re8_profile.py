"""Resident Evil Village integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSoundProfile
from .sound_profile import register_sound_profile


class Re8SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/re8.json.gz"
    game_name = "Resident Evil Village"
    fallbacks = STANDARD_CUE_FALLBACKS


class Re8SoundProfile(ReEngineSoundProfile):
    game = "RE8"
    display_name = "Resident Evil Village"
    aliases = ("Resident Evil 8", "RE Village", "Village", "Biohazard Village")
    bank_versions = frozenset({135})
    wel_versions = frozenset({11})
    required_year = 2019
    required_major = 2
    recommended_version = "2019.2.15.7667"
    convolution_reverb_plugin_id = 0x007F0003
    platform = "stm"
    file_platforms = frozenset({"x64"})
    sound_root = "natives/stm/sound/"
    metadata_type = Re8SoundMetadata


RE8_SOUND_PROFILE = register_sound_profile(Re8SoundProfile())


__all__ = ["RE8_SOUND_PROFILE", "Re8SoundMetadata", "Re8SoundProfile"]
