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


VIA_LANGUAGE_NAMES: tuple[str, ...] = (
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
    "Vietnamese",
    "Indonesian",
    "Fiction",
    "Hindi",
    "LatinAmericanSpanish",
)

EARLY_FONT_SLOT_NAMES: tuple[str, ...] = tuple(f"Slot{index}" for index in range(10))
LATE_FONT_SLOT_NAMES: tuple[str, ...] = tuple(f"Slot{index}" for index in range(16))
ASSET_LANGUAGE_NAMES: tuple[str, ...] = tuple(f"No{index}" for index in range(4))

DMC5_GCF_V15_PROFILE = GcfProfile(
    version=15,
    magic=GCF_MAGIC,
    language_names=VIA_LANGUAGE_NAMES[:28],
    font_slot_names=EARLY_FONT_SLOT_NAMES,
    asset_language_names=ASSET_LANGUAGE_NAMES,
)


def _profile(version: int, language_count: int, *, late_slots: bool) -> GcfProfile:
    return GcfProfile(
        version=version,
        magic=GCF_MAGIC,
        language_names=VIA_LANGUAGE_NAMES[:language_count],
        font_slot_names=LATE_FONT_SLOT_NAMES if late_slots else EARLY_FONT_SLOT_NAMES,
        asset_language_names=ASSET_LANGUAGE_NAMES,
    )


GCF_PROFILES: dict[int, GcfProfile] = {
    12: _profile(12, 23, late_slots=False),
    15: DMC5_GCF_V15_PROFILE,
    19: _profile(19, 30, late_slots=False),
    24: _profile(24, 32, late_slots=True),
    26: _profile(26, 33, late_slots=True),
    27: _profile(27, 33, late_slots=True),
    28: _profile(28, 33, late_slots=True),
    29: _profile(29, 33, late_slots=True),
}


def gcf_profile(version: int) -> GcfProfile:
    try:
        return GCF_PROFILES[int(version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(GCF_PROFILES)))
        raise GcfFormatError(
            f"unsupported GCF version {version}; supported versions: {supported}"
        ) from exc


def gcf_profile_or_generic(version: int) -> GcfProfile:
    try:
        return gcf_profile(version)
    except GcfFormatError:
        return GcfProfile(
            version=int(version),
            magic=GCF_MAGIC,
            language_names=VIA_LANGUAGE_NAMES,
            font_slot_names=LATE_FONT_SLOT_NAMES,
            asset_language_names=ASSET_LANGUAGE_NAMES,
        )
