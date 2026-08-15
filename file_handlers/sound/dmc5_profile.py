"""Devil May Cry 5 integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata
from .re_engine_profile import ReEngineSoundProfile
from .sound_profile import register_sound_profile


class Dmc5SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/dmc5.json.gz"
    game_name = "DMC5"
    fallbacks = {
        "switch_group": {0xDA4ACF20: ("IndoorOutdoor",)},
        "switch_value": {
            0x144A1304: ("Indoor",),
            0x089FE80F: ("Outdoor",),
            0x27785BD2: ("Armor",),
            0x5CF9CF48: ("BACK",),
            0xD7DB12A7: ("RANK_SSS",),
            0xC072AD08: ("location00",),
        },
    }


class Dmc5SoundProfile(ReEngineSoundProfile):
    game = "DMC5"
    display_name = "Devil May Cry 5"
    aliases = ("Devil May Cry 5",)
    bank_versions = frozenset({125})
    wel_versions = frozenset({11})
    required_year = 2017
    required_major = 1
    recommended_version = "2017.1.9"
    sound_root = "natives/x64/sound/"
    metadata_type = Dmc5SoundMetadata


DMC5_SOUND_PROFILE = register_sound_profile(Dmc5SoundProfile())


__all__ = ["DMC5_SOUND_PROFILE", "Dmc5SoundMetadata", "Dmc5SoundProfile"]
