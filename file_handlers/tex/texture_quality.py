from __future__ import annotations

from dataclasses import dataclass


DEFAULT_TEXTURE_QUALITY = "balanced"


@dataclass(frozen=True, slots=True)
class TextureQualityProfile:
    label: str
    max_dimension: int | None
    prefer_streaming: bool
    anisotropy: float
    description: str


TEXTURE_QUALITY_PROFILES = {
    "low": TextureQualityProfile(
        "Low", 256, False, 1.0, "Resident TEX up to 256 px; 1x sampling"
    ),
    "balanced": TextureQualityProfile(
        "Balanced", 512, False, 4.0, "Resident TEX up to 512 px; up to 4x sampling"
    ),
    "high": TextureQualityProfile(
        "High", None, True, 16.0, "Full resolution, prefers streaming TEX; up to 16x sampling"
    ),
}


def normalize_texture_quality(value) -> str:
    name = str(value or "").strip().lower()
    return name if name in TEXTURE_QUALITY_PROFILES else DEFAULT_TEXTURE_QUALITY


def texture_quality_profile(value) -> TextureQualityProfile:
    if isinstance(value, TextureQualityProfile):
        return value
    return TEXTURE_QUALITY_PROFILES[normalize_texture_quality(value)]


def choose_texture_mip(tex, quality) -> int:
    profile = texture_quality_profile(quality)
    if profile.max_dimension is None:
        return 0

    mip_count = max(1, int(getattr(tex.header, "mip_count", 1)))
    width = max(1, int(getattr(tex.header, "width", 1)))
    height = max(1, int(getattr(tex.header, "height", 1)))
    mip = 0
    while mip + 1 < mip_count and max(width, height) > profile.max_dimension:
        width, height = max(1, width // 2), max(1, height // 2)
        mip += 1
    return mip
