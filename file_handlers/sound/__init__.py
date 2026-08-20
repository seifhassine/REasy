"""Registered game profiles for REasy's Wwise sound support."""

from .sound_profile import (
    SoundGameProfile,
    WemAuthoringCodec,
    register_sound_profile,
    sound_profile_for_bank_version,
    sound_profile_for_game,
    sound_profile_for_handler,
    sound_profile_for_path,
    sound_profiles,
)
from .dd2_profile import DD2_SOUND_PROFILE
from .dmc5_profile import DMC5_SOUND_PROFILE
from .mhrise_profile import MHRISE_SOUND_PROFILE
from .mhwilds_profile import MHWILDS_SOUND_PROFILE
from .pragmata_profile import PRAGMATA_SOUND_PROFILE
from .re2_profile import RE2_SOUND_PROFILE, RE2RT_SOUND_PROFILE
from .re3_profile import RE3_SOUND_PROFILE, RE3RT_SOUND_PROFILE
from .re4_profile import RE4_SOUND_PROFILE
from .re7_profile import RE7RT_SOUND_PROFILE
from .re8_profile import RE8_SOUND_PROFILE
from .re9_profile import RE9_SOUND_PROFILE
from .sf6_profile import SF6_SOUND_PROFILE


__all__ = [
    "DD2_SOUND_PROFILE",
    "DMC5_SOUND_PROFILE",
    "MHRISE_SOUND_PROFILE",
    "MHWILDS_SOUND_PROFILE",
    "PRAGMATA_SOUND_PROFILE",
    "RE2_SOUND_PROFILE",
    "RE2RT_SOUND_PROFILE",
    "RE3_SOUND_PROFILE",
    "RE3RT_SOUND_PROFILE",
    "RE4_SOUND_PROFILE",
    "RE7RT_SOUND_PROFILE",
    "RE8_SOUND_PROFILE",
    "RE9_SOUND_PROFILE",
    "SF6_SOUND_PROFILE",
    "SoundGameProfile",
    "WemAuthoringCodec",
    "register_sound_profile",
    "sound_profile_for_bank_version",
    "sound_profile_for_game",
    "sound_profile_for_handler",
    "sound_profile_for_path",
    "sound_profiles",
]
