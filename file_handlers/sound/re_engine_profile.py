"""Shared RE Engine path and replacement policy for Wwise sound profiles."""

from __future__ import annotations

import re

from .sound_metadata import SoundMetadata
from .sound_profile import SoundGameProfile, WemAuthoringCodec
from .sound_resources import RelatedSoundPaths, resource_key


_SOUND_NAME = re.compile(
    r"^(?P<stem>.+?)\.(?P<kind>s?bnk\.\d+|s?pck\.\d+)\."
    r"(?P<platform>x64|stm)"
    r"(?P<locale>(?:\.[^/.]+)*)$",
    re.IGNORECASE,
)

_PCM = WemAuthoringCodec(
    0xFFFE, "Wwise PCM", "PCM As Input", frozenset({0x0001})
)
_ADPCM = WemAuthoringCodec(0x0002, "Wwise ADPCM", "ADPCM As Input")
_PLATINUM_ADPCM = WemAuthoringCodec(
    0x8311, "Wwise Platinum ADPCM", "ADPCM As Input", frozenset({0x0002})
)
_VORBIS = WemAuthoringCodec(0xFFFF, "Wwise Vorbis", "Vorbis Quality High")
_OPUS = WemAuthoringCodec(
    0x3041,
    "WEM Opus",
    "REasy WEM Opus",
    conversion_plugin=("WEM Opus", 20, (("Quality", "int32", 128),)),
    required_sample_rate=48_000,
)


class ReEngineSoundProfile(SoundGameProfile):
    """Common Wwise integration used by profiled RE Engine games."""

    # Wwise wraps authored PCM in 0xFFFE while accepting standard PCM input.
    wem_codecs = (_PCM, _ADPCM, _VORBIS)
    platinum_wem_codecs = (_PCM, _PLATINUM_ADPCM, _VORBIS)
    opus_wem_codecs = wem_codecs + (_OPUS,)
    platinum_opus_wem_codecs = platinum_wem_codecs + (_OPUS,)
    default_wem_codec_tag = 0xFFFF
    metadata_type: type[SoundMetadata] | None = None
    platform = "x64"
    file_platforms: frozenset[str] = frozenset()
    sound_root = ""
    bank_resource = "bnk.2"
    package_resource = "pck.3"
    split_sbnk_roles = False

    def metadata(self, source_path: str = "") -> SoundMetadata:
        provider = self.metadata_type
        return provider(source_path) if provider else super().metadata(source_path)

    def metadata_for_handler(self, handler) -> SoundMetadata:
        provider = self.metadata_type
        return (
            provider.for_handler(handler, self)
            if provider
            else super().metadata_for_handler(handler)
        )

    @property
    def accepted_file_platforms(self) -> frozenset[str]:
        return self.file_platforms or frozenset({self.platform})

    def _match_container_name(self, name: str):
        match = _SOUND_NAME.match(name)
        return match if match and match.group("kind").casefold() in {
            self.bank_resource, self.package_resource
        } else None

    def matches_path(self, path: str) -> bool:
        key = resource_key(path)
        native_root = f"natives/{self.platform}/"
        logical = key.replace(native_root + "streaming/", native_root, 1)
        if self.sound_root and not logical.startswith(self.sound_root):
            return False
        name = logical.rsplit("/", 1)[-1]
        match = self._match_container_name(name)
        return bool(
            (match and match.group("platform").casefold() in self.accepted_file_platforms)
            or name.endswith(tuple(f".wel.{version}" for version in self.wel_versions))
        )

    def related_paths(self, path: str) -> RelatedSoundPaths | None:
        key = resource_key(path)
        directory, _, name = key.rpartition("/")
        match = self._match_container_name(name)
        if not match or match.group("platform").casefold() not in self.accepted_file_platforms:
            return None
        stem, locale, file_platform = (
            match.group("stem"), match.group("locale"), match.group("platform").casefold()
        )
        native_root = f"natives/{self.platform}/"
        streaming_root = native_root + "streaming/"
        if directory.startswith(streaming_root):
            index_dir = native_root + directory[len(streaming_root):]
        else:
            index_dir = directory
        streaming_dir = (
            streaming_root + index_dir[len(native_root):]
            if index_dir.startswith(native_root) else index_dir
        )

        def joined(folder: str, filename: str) -> str:
            return f"{folder}/{filename}" if folder else filename

        suffix = f".{file_platform}{locale}"
        return RelatedSoundPaths(
            bank=joined(index_dir, f"{stem}.{self.bank_resource}{suffix}"),
            index_pck=joined(index_dir, f"{stem}.{self.package_resource}{suffix}"),
            streaming_pck=joined(
                streaming_dir, f"{stem}.{self.package_resource}{suffix}"
            ),
        )

    def resolve_replacement(self, handler, result, track):
        from .sound_media import resolve_sound_replacement

        return resolve_sound_replacement(self, handler, result, track)

    def build_replacement_outputs(self, plans, replacements):
        from .sound_media import build_sound_replacement_outputs

        return build_sound_replacement_outputs(plans, replacements)

    def resolve_indexed_package(self, handler):
        from .sound_media import resolve_indexed_streaming_pck

        return resolve_indexed_streaming_pck(self, handler)

    def validate_indexed_package(self, index_data: bytes, streaming_data: bytes) -> None:
        from .sound_media import validate_streaming_pck_match

        validate_streaming_pck_match(index_data, streaming_data)


class ReEngineSbnkSoundProfile(ReEngineSoundProfile):
    """Common STM SBNK/SPCK layout used by newer RE Engine titles."""

    platform = "stm"
    file_platforms = frozenset({"x64"})
    sound_root = "natives/stm/sound/"
    bank_resource = "sbnk.1"
    package_resource = "spck.1"


__all__ = ["ReEngineSbnkSoundProfile", "ReEngineSoundProfile"]
