from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import os

from file_handlers.gcf.model import FontSlotMapping, GcfData
from file_handlers.oft.oft_file import OftFile
from utils.resource_file_utils import (
    ResourceDataLoader,
    ResourceResolutionContext,
)

from .glyphs import DMC5_GUI_GLYPH_POLICY, GlyphFallbackPolicy, GlyphResolution
from .sfnt import SfntFont, SfntGlyphMetric


class FontCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FontCatalogProfile:
    name: str
    oft_version: int
    platform_suffix: str
    glyph_policy: GlyphFallbackPolicy


DMC5_FONT_CATALOG_PROFILE = FontCatalogProfile(
    "DMC5",
    oft_version=1,
    platform_suffix="x64",
    glyph_policy=DMC5_GUI_GLYPH_POLICY,
)

FONT_CATALOG_PROFILES: dict[int, FontCatalogProfile] = {
    15: DMC5_FONT_CATALOG_PROFILE,
}


def font_catalog_profile(gcf_version: int) -> FontCatalogProfile:
    try:
        return FONT_CATALOG_PROFILES[int(gcf_version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(FONT_CATALOG_PROFILES)))
        raise FontCatalogError(
            f"no GUI font profile for GCF version {gcf_version}; "
            f"supported GCF versions: {supported}"
        ) from exc


def normalize_font_resource_path(asset_path: str) -> tuple[str, bool]:
    platform_variant = asset_path.startswith("@")
    normalized = asset_path[1:] if platform_variant else asset_path
    normalized = normalized.replace("\\", "/").lstrip("/").casefold()
    if not normalized.endswith(".oft"):
        raise FontCatalogError(f"font path does not end in .oft: {asset_path!r}")
    return normalized, platform_variant


@dataclass(frozen=True, slots=True)
class FontFaceAsset:
    candidate_index: int
    asset_path: str
    normalized_asset_path: str
    platform_variant: bool
    resource_name: str
    adjustment_scale: float
    font: SfntFont

    def glyph_metric(self, glyph_id: int, *, vertical: bool = False) -> SfntGlyphMetric:
        return self.font.glyph_metric(glyph_id, vertical=vertical)

    def scaled_advance(
        self, glyph_id: int, em_size: float, *, vertical: bool = False
    ) -> float:
        metric = self.glyph_metric(glyph_id, vertical=vertical)
        return (
            float(metric.advance)
            * float(em_size)
            * self.adjustment_scale
            / self.font.units_per_em
        )


@dataclass(frozen=True, slots=True)
class FontSlotFaces:
    mapping: FontSlotMapping
    faces: tuple[FontFaceAsset, ...]


@dataclass(frozen=True, slots=True)
class ResolvedGlyph:
    """One codepoint resolved to a face and glyph of a font slot."""

    resolution: GlyphResolution
    face: FontFaceAsset | None
    vertical: bool = False

    @property
    def missing(self) -> bool:
        return self.face is None or self.resolution.missing

    @property
    def glyph_id(self) -> int:
        if self.vertical and self.resolution.vertical_glyph_id is not None:
            return self.resolution.vertical_glyph_id
        return self.resolution.glyph_id

    def advance(self, pixel_size: float) -> float:
        """Pixel advance at ``pixel_size`` (height for vertical text)."""

        if self.missing or self.face is None:
            return 0.0
        return self.face.scaled_advance(
            self.glyph_id, pixel_size, vertical=self.vertical
        )


class GuiFontCatalog:
    """Compose a GCF font-slot table with versioned OFT/SFNT resources."""

    def __init__(
        self,
        config: GcfData,
        resource_data_loader: ResourceDataLoader,
        *,
        profile: FontCatalogProfile | None = None,
        strict: bool = True,
    ) -> None:
        self.config = config
        self.resource_data_loader = resource_data_loader
        self.profile = profile or font_catalog_profile(config.version)
        self.strict = bool(strict)
        self._font_cache: dict[str, SfntFont] = {}
        self._slot_cache: dict[tuple[int, int], FontSlotFaces] = {}

    @classmethod
    def from_asset_root(
        cls,
        config: GcfData,
        asset_root: str | os.PathLike[str],
        *,
        profile: FontCatalogProfile | None = None,
        strict: bool = True,
        path_prefixes: Sequence[str] = ("natives/x64", "natives/stm"),
    ) -> "GuiFontCatalog":
        """Build a catalog that resolves fonts below a filesystem asset root."""

        root = os.fspath(asset_root)
        contexts = [
            ResourceResolutionContext(unpacked_dir=root, path_prefix=prefix)
            for prefix in path_prefixes
        ]

        def loader(path: str) -> tuple[str, bytes] | None:
            for context in contexts:
                resolved = context.resolve(path, allow_selection_dialog=False)
                if resolved is not None:
                    return resolved
            return None

        return cls(config, loader, profile=profile, strict=strict)

    def _candidate_paths(self, asset_path: str) -> tuple[str, ...]:
        normalized, platform_variant = normalize_font_resource_path(asset_path)
        base = f"{normalized}.{self.profile.oft_version}"
        platform = f"{base}.{self.profile.platform_suffix}"
        return (platform, base) if platform_variant else (base, platform)

    def resolve_resource(self, asset_path: str) -> tuple[str, bytes] | None:

        for candidate in self._candidate_paths(asset_path):
            resolved = self.resource_data_loader(candidate)
            if resolved is not None:
                name, data = resolved
                return str(name), bytes(data)
        if self.strict:
            raise FontCatalogError(f"configured font resource is missing: {asset_path!r}")
        return None

    def _load_font(self, asset_path: str) -> tuple[str, SfntFont] | None:
        resolved = self.resolve_resource(asset_path)
        if resolved is None:
            return None
        resource_name, data = resolved
        key = resource_name.casefold()
        font = self._font_cache.get(key)
        if font is None:
            font = OftFile.from_bytes(data, version=self.profile.oft_version).font()
            self._font_cache[key] = font
        return resource_name, font

    def slot_faces(self, language: int, slot: int) -> FontSlotFaces:
        key = int(language), int(slot)
        cached = self._slot_cache.get(key)
        if cached is not None:
            return cached
        mapping = self.config.font_slot(*key)
        faces: list[FontFaceAsset] = []
        for candidate_index, asset_path in enumerate(mapping.asset_paths):
            if not asset_path:
                continue
            loaded = self._load_font(asset_path)
            if loaded is None:
                continue
            adjustment = mapping.scale_for(candidate_index)
            normalized, platform_variant = normalize_font_resource_path(asset_path)
            resource_name, font = loaded
            faces.append(
                FontFaceAsset(
                    candidate_index,
                    asset_path,
                    normalized,
                    platform_variant,
                    resource_name,
                    float(adjustment),
                    font,
                )
            )
        result = FontSlotFaces(mapping, tuple(faces))
        self._slot_cache[key] = result
        return result

    def preload_configured_faces(self) -> tuple[FontFaceAsset, ...]:
        """Load every distinct font resource referenced by the slot table."""

        unique: dict[str, FontFaceAsset] = {}
        for language in range(self.config.language_count):
            for slot in range(self.config.font_slot_count):
                for face in self.slot_faces(language, slot).faces:
                    unique.setdefault(face.normalized_asset_path, face)
        return tuple(unique.values())

    def resolve_glyph(
        self,
        language: int,
        slot: int,
        codepoint: int,
        *,
        vertical: bool = False,
    ) -> ResolvedGlyph:
        """Resolve one codepoint through the slot's face fallback chain."""

        faces = self.slot_faces(language, slot).faces
        if not faces:
            return ResolvedGlyph(
                GlyphResolution(int(codepoint), None, 0, None, None, None),
                None,
                vertical,
            )
        resolution = self.profile.glyph_policy.resolve(
            codepoint,
            [face.font.best_cmap for face in faces],
            vertical_substitutions=[
                face.font.vertical_substitutions for face in faces
            ],
        )
        face = None if resolution.face_index is None else faces[resolution.face_index]
        return ResolvedGlyph(resolution, face, vertical)
