"""Resident Evil 4 integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSbnkSoundProfile
from .sound_profile import register_sound_profile


class Re4SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/re4.json.gz"
    game_name = "Resident Evil 4"
    fallbacks = STANDARD_CUE_FALLBACKS


class Re4SoundProfile(ReEngineSbnkSoundProfile):
    game = "RE4"
    display_name = "Resident Evil 4"
    aliases = (
        "Resident Evil 4 Remake", "Biohazard RE:4", "RE4 Remake", "RE4R",
    )
    bank_versions = frozenset({140})
    required_year = 2021
    required_major = 1
    recommended_version = "2021.1.14.8108"
    convolution_reverb_plugin_id = 0x007F0003
    sound_root = "natives/stm/_chainsaw/sound/"
    metadata_type = Re4SoundMetadata
    wem_codecs = ReEngineSbnkSoundProfile.opus_wem_codecs
    default_wem_codec_tag = 0x3041


RE4_SOUND_PROFILE = register_sound_profile(Re4SoundProfile())


__all__ = ["RE4_SOUND_PROFILE", "Re4SoundMetadata", "Re4SoundProfile"]
