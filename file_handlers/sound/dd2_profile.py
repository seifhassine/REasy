"""Dragon's Dogma 2 integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSbnkSoundProfile
from .sound_profile import register_sound_profile


class Dd2SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/dd2.json.gz"
    game_name = "Dragon's Dogma 2"
    fallbacks = STANDARD_CUE_FALLBACKS


class Dd2SoundProfile(ReEngineSbnkSoundProfile):
    game = "DD2"
    display_name = "Dragon's Dogma 2"
    aliases = ("Dragons Dogma 2", "Dragon's Dogma II", "DragonsDogma2")
    bank_versions = frozenset({140})
    required_year = 2021
    required_major = 1
    recommended_version = "2021.1.14.8108"
    convolution_reverb_plugin_id = 0x007F0003
    metadata_type = Dd2SoundMetadata
    wem_codecs = ReEngineSbnkSoundProfile.platinum_opus_wem_codecs


DD2_SOUND_PROFILE = register_sound_profile(Dd2SoundProfile())


__all__ = ["DD2_SOUND_PROFILE", "Dd2SoundMetadata", "Dd2SoundProfile"]
