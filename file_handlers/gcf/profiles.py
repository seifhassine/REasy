from __future__ import annotations

from dataclasses import dataclass


GCF_MAGIC = b"GCFG"


class GcfFormatError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GcfProfile:
    version: int
    magic: bytes
    language_names: tuple[str, ...]
    font_slot_names: tuple[str, ...]
    asset_language_names: tuple[str, ...]

    @staticmethod
    def indexed_name(names: tuple[str, ...], index: int, prefix: str) -> str:
        return names[index] if 0 <= index < len(names) else f"{prefix}{index}"

    def language_name(self, index: int) -> str:
        return self.indexed_name(self.language_names, index, "Language")

    def font_slot_name(self, index: int) -> str:
        return self.indexed_name(self.font_slot_names, index, "Slot")

    def asset_language_name(self, index: int) -> str:
        return self.indexed_name(self.asset_language_names, index, "AssetLanguage")


# Numeric order and spellings are from DMC5 TDB 67. Engine misspellings are
# intentionally retained .
DMC5_GCF_V15_PROFILE = GcfProfile(
    version=15,
    magic=GCF_MAGIC,
    language_names=(
        "Japanese",
        "English",
        "French",
        "Italian",
        "German",
        "Spanish",
        "Russian",
        "Polish",
        "Dutch",
        "Portuguese",
        "PortugueseBr",
        "Korean",
        "TransitionalChinese",
        "SimplelifiedChinese",
        "Finnish",
        "Swedish",
        "Danish",
        "Norwegian",
        "Czech",
        "Hungarian",
        "Slovak",
        "Arabic",
        "Turkish",
        "Bulgarian",
        "Greek",
        "Romanian",
        "Thai",
        "Ukrainian",
    ),
    font_slot_names=tuple(f"Slot{index}" for index in range(10)),
    asset_language_names=tuple(f"No{index}" for index in range(4)),
)


GCF_PROFILES: dict[int, GcfProfile] = {15: DMC5_GCF_V15_PROFILE}


def gcf_profile(version: int) -> GcfProfile:
    try:
        return GCF_PROFILES[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(GCF_PROFILES)))
        raise GcfFormatError(
            f"unsupported GCF version {version}; supported versions: {supported}"
        ) from exc
