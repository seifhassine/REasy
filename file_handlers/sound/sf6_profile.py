"""Street Fighter 6 integration policy for the generic sound editor."""

from .indexed_sound_metadata import IndexedSoundMetadata, STANDARD_CUE_FALLBACKS
from .re_engine_profile import ReEngineSbnkSoundProfile
from .sound_profile import register_sound_profile


class Sf6SoundMetadata(IndexedSoundMetadata):
    index_resource = "resources/data/sound/sf6.json.gz"
    game_name = "Street Fighter 6"
    fallbacks = STANDARD_CUE_FALLBACKS


class Sf6SoundProfile(ReEngineSbnkSoundProfile):
    game = "SF6"
    display_name = "Street Fighter 6"
    aliases = ("StreetFighter6", "Street Fighter VI")
    bank_versions = frozenset({135})
    split_sbnk_roles = True
    required_year = 2019
    required_major = 2
    recommended_version = "2019.2.15.7667"
    sound_root = "natives/stm/product/sound/"
    metadata_type = Sf6SoundMetadata


SF6_SOUND_PROFILE = register_sound_profile(Sf6SoundProfile())


__all__ = ["SF6_SOUND_PROFILE", "Sf6SoundMetadata", "Sf6SoundProfile"]
