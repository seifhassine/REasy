from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterator


@dataclass
class FontSlotMapping:
    language_index: int
    language_name: str = field(compare=False)
    slot_index: int
    slot_name: str = field(compare=False)
    # Native order is candidate/path first, then slot, then language.
    asset_paths: tuple[str | None, ...]
    # GCF v15 stores a Float2, indexed by the same candidate argument.
    adjust_scale: tuple[float, float]


@dataclass
class AssetLanguageTriplet:
    asset_language_index: int
    asset_language_name: str = field(compare=False)
    value_0: float
    value_1: float
    value_2: float


@dataclass
class LocalizeAsset:
    slot: int
    slot_name: str = field(compare=False)
    reserved: int
    path: str | None


@dataclass(frozen=True, slots=True)
class GcfLayout:
    root_offset: int
    message_assets_offset: int
    asset_language_triplets_offset: int
    localize_assets_offset: int


@dataclass(frozen=True, slots=True)
class GcfResourceReference:
    kind: str
    path: str
    language_index: int | None = None
    slot_index: int | None = None
    candidate_index: int | None = None
    asset_language_slot: int | None = None


@dataclass
class GcfData:
    version: int
    delay_language_font_load_raw: int
    language_count: int
    font_slot_count: int
    font_asset_path_count: int
    default_ruby_size_ratio: float
    root_reserved: int
    icon_font_asset_path: str | None
    font_slots: list[FontSlotMapping] = field(default_factory=list)
    message_section_reserved: int = 0
    message_asset_paths: list[str | None] = field(default_factory=list)
    # Native DMC5 code relocates this table but exposes no accessor or consumer.
    # The three floats therefore remain deliberately unnamed and lossless.
    asset_language_triplets: list[AssetLanguageTriplet] = field(default_factory=list)
    localize_section_reserved: int = 0
    localize_assets: list[LocalizeAsset] = field(default_factory=list)

    @property
    def delay_language_font_load(self) -> bool:
        return bool(self.delay_language_font_load_raw)

    @delay_language_font_load.setter
    def delay_language_font_load(self, value: bool) -> None:
        self.delay_language_font_load_raw = int(bool(value))

    def font_slot(self, language: int, slot: int) -> FontSlotMapping:
        if not 0 <= language < self.language_count:
            raise IndexError("language index out of range")
        if not 0 <= slot < self.font_slot_count:
            raise IndexError("font slot index out of range")
        index = language * self.font_slot_count + slot
        mapping = self.font_slots[index]
        if (mapping.language_index, mapping.slot_index) != (language, slot):
            # Keep lookup correct if an editor reorders records.
            for mapping in self.font_slots:
                if (mapping.language_index, mapping.slot_index) == (language, slot):
                    return mapping
            raise ValueError(f"missing font slot language={language}, slot={slot}")
        return mapping

    def iter_resource_references(self) -> Iterator[GcfResourceReference]:
        if self.icon_font_asset_path:
            yield GcfResourceReference("icon_font", self.icon_font_asset_path)
        for mapping in self.font_slots:
            for candidate, path in enumerate(mapping.asset_paths):
                if path:
                    yield GcfResourceReference(
                        "font",
                        path,
                        mapping.language_index,
                        mapping.slot_index,
                        candidate,
                    )
        for path in self.message_asset_paths:
            if path:
                yield GcfResourceReference("message", path)
        for asset in self.localize_assets:
            if asset.path:
                yield GcfResourceReference(
                    "localize_asset",
                    asset.path,
                    asset_language_slot=asset.slot,
                )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
