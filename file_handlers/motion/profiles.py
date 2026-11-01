from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CompactClipLayout:
    version: int
    header_size: int
    node_size: int
    property_size: int
    key_size: int


@dataclass(frozen=True, slots=True)
class MotionFormatProfile:
    name: str
    mot_clip: CompactClipLayout

    def require_versions(self, *, mot_clip: int | None = None) -> None:
        if mot_clip is not None and self.mot_clip.version != mot_clip:
            raise ValueError(f"MotClip v{mot_clip} codec cannot use {self.name} MotClip v{self.mot_clip.version} "
                             "profile")


DMC5_PROFILE = MotionFormatProfile(
    name="Devil May Cry 5",
    mot_clip=CompactClipLayout(version=27, header_size=0x88, node_size=0x60, property_size=0x70, key_size=0x28),
)
