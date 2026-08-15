"""Resident Evil 2 integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata
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


RE2_SOUND_PROFILE = register_sound_profile(Re2SoundProfile())


__all__ = ["RE2_SOUND_PROFILE", "Re2SoundMetadata", "Re2SoundProfile"]
