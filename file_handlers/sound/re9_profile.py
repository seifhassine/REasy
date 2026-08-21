"""Resident Evil Requiem integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSbnkSoundProfile
from .sound_profile import register_sound_profile


class Re9SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/re9.json.gz"
    game_name = "Resident Evil Requiem"
    fallbacks = STANDARD_CUE_FALLBACKS


class Re9SoundProfile(ReEngineSbnkSoundProfile):
    game = "RE9"
    display_name = "Resident Evil Requiem"
    aliases = (
        "Resident Evil 9", "Resident Evil requiem", "Biohazard requiem",
        "Resident Evil Requiem Biohazard Requiem",
    )
    bank_versions = frozenset({145})
    split_sbnk_roles = True
    required_year = 2022
    required_major = 1
    recommended_version = "2022.1.19.8584"
    convolution_reverb_plugin_id = 0x007F0003
    metadata_type = Re9SoundMetadata
    wem_codecs = ReEngineSbnkSoundProfile.opus_wem_codecs
    default_wem_codec_tag = 0x3041


RE9_SOUND_PROFILE = register_sound_profile(Re9SoundProfile())


__all__ = ["RE9_SOUND_PROFILE", "Re9SoundMetadata", "Re9SoundProfile"]
