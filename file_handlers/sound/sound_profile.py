"""Game integration profiles layered over the generic Wwise formats."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .sound_metadata import SoundMetadata
from .sound_resources import RelatedSoundPaths, resource_key


@dataclass(frozen=True, slots=True)
class WemQualitySetting:
    """Codec-specific Wwise quality property and user-facing presets."""

    property_name: str
    property_type: str
    presets: tuple[tuple[str, float | int], ...]
    minimum: float | int
    maximum: float | int
    default: float | int
    step: float | int = 1

    def value(self, preset: str | None) -> float | int | None:
        if preset is None:
            return None
        return dict(self.presets).get(str(preset).casefold())


@dataclass(frozen=True, slots=True)
class WemAuthoringCodec:
    """One codec that a game's required Wwise version can author."""

    tag: int
    name: str
    conversion_setting: str
    match_tags: frozenset[int] = frozenset()
    conversion_plugin: tuple[str, int, tuple[tuple[str, str, object], ...]] | None = None
    required_sample_rate: int | None = None
    quality: WemQualitySetting | None = None
    supports_bitrate_mode: bool = False

    def matches(self, tag: int | None) -> bool:
        return tag == self.tag or tag in self.match_tags


def _game_key(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


class SoundGameProfile:
    """Policy a game supplies around otherwise generic BNK/PCK/WEM handling."""

    game = ""
    display_name = ""
    aliases: tuple[str, ...] = ()
    bank_versions: frozenset[int] = frozenset()
    wel_versions: frozenset[int] = frozenset()
    required_year = 0
    required_major = 0
    recommended_version = ""
    wem_codecs: tuple[WemAuthoringCodec, ...] = ()
    default_wem_codec_tag: int | None = None
    hybrid_reverb_plugin_id: int | None = None
    convolution_reverb_plugin_id: int | None = None
    sound_engine_sample_rate = 48_000

    @property
    def required_family(self):
        return self.required_year, self.required_major

    def wem_codec(self, tag: int | None) -> WemAuthoringCodec | None:
        return next((codec for codec in self.wem_codecs if codec.matches(tag)), None)

    @property
    def default_wem_codec(self) -> WemAuthoringCodec | None:
        return self.wem_codec(self.default_wem_codec_tag)

    @property
    def supports_hybrid_reverb_ir(self) -> bool:
        return self.hybrid_reverb_plugin_id is not None

    @property
    def supports_convolution_reverb_ir(self) -> bool:
        return self.convolution_reverb_plugin_id is not None

    @property
    def required_version_text(self):
        return f"{self.required_year}.{self.required_major}.x"

    def accepts(self, version):
        return bool(self.required_year and version.family == self.required_family)

    def requirement_message(self):
        return (
            f"{self.display_name} requires Wwise {self.required_version_text} "
            f"(recommended: Wwise {self.recommended_version})."
        )

    def matches_path(self, path: str) -> bool:
        return False

    def related_paths(self, path: str) -> RelatedSoundPaths | None:
        return None

    def metadata(self, source_path: str = "") -> SoundMetadata:
        return SoundMetadata()

    def metadata_for_handler(self, handler) -> SoundMetadata:
        return self.metadata(getattr(handler, "filepath", ""))

    def resolve_replacement(self, handler, result, track):
        raise ValueError(f"Sound replacement is not configured for {self.display_name}.")

    def build_replacement_outputs(self, plans, replacements):
        raise ValueError(f"Bulk sound replacement is not configured for {self.display_name}.")

    def resolve_indexed_package(self, handler):
        return None

    def validate_indexed_package(self, index_data: bytes, streaming_data: bytes) -> None:
        raise ValueError(f"Indexed PCK validation is not configured for {self.display_name}.")

    def matching_companion_path(self, path: str) -> str | None:
        paths = self.related_paths(path)
        if paths is None:
            return None
        normalized = str(path or "").replace("\\", "/")
        current = resource_key(normalized)
        companion = paths.streaming_pck if current == paths.bank else paths.bank
        marker = normalized.casefold().find("natives/")
        return normalized[:marker] + companion if marker >= 0 else companion

    def streaming_package_hint(self, path: str) -> str | None:
        paths = self.related_paths(path)
        if paths is None or paths.streaming_pck == resource_key(path):
            return None
        return paths.streaming_pck


_PROFILES: dict[str, SoundGameProfile] = {}
_ALIASES: dict[str, str] = {}


def register_sound_profile(profile: SoundGameProfile) -> SoundGameProfile:
    key = _game_key(profile.game)
    if not key:
        raise ValueError("A sound profile must have a game key")
    _PROFILES[key] = profile
    for alias in (profile.game, profile.display_name, *profile.aliases):
        _ALIASES[_game_key(alias)] = key
    return profile


def sound_profile_for_game(game) -> SoundGameProfile | None:
    return _PROFILES.get(_ALIASES.get(_game_key(game), _game_key(game)))


def sound_profiles() -> tuple[SoundGameProfile, ...]:
    return tuple(_PROFILES.values())


def sound_profile_for_bank_version(version) -> SoundGameProfile | None:
    matches = [profile for profile in _PROFILES.values() if version in profile.bank_versions]
    return matches[0] if len(matches) == 1 else None


def sound_profile_for_path(path: str) -> SoundGameProfile | None:
    matches = [profile for profile in _PROFILES.values() if profile.matches_path(path)]
    return matches[0] if len(matches) == 1 else None


def sound_profile_for_handler(handler, bank_version=None) -> SoundGameProfile | None:
    context = getattr(handler, "resource_context", None)
    app = getattr(handler, "app", None)
    settings = getattr(app, "settings", {}) if app is not None else {}
    candidates = (
        getattr(context, "game", ""),
        getattr(handler, "game_version", ""),
        getattr(app, "current_game", "") if app is not None else "",
        settings.get("game_version", "") if isinstance(settings, dict) else "",
    )
    for game in candidates:
        if game:
            return sound_profile_for_game(game)
    path = getattr(handler, "filepath", "") or getattr(handler, "filename", "")
    by_version = {
        profile for profile in _PROFILES.values()
        if bank_version in profile.bank_versions
    }
    by_path = {profile for profile in _PROFILES.values() if profile.matches_path(path)}
    matches = by_version & by_path if by_version and by_path else by_version or by_path
    return next(iter(matches)) if len(matches) == 1 else None

__all__ = [
    "SoundGameProfile",
    "WemAuthoringCodec",
    "WemQualitySetting",
    "register_sound_profile",
    "sound_profile_for_bank_version",
    "sound_profile_for_game",
    "sound_profile_for_handler",
    "sound_profile_for_path",
    "sound_profiles",
]
