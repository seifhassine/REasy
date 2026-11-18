from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
import os

from file_handlers.gcf.model import FontSlotMapping, GcfData
lazy from file_handlers.oft.oft_file import OftFile
lazy from utils.resource_file_utils import (
    ResourceDataLoader,
    ResourceResolutionContext,
)

lazy from file_handlers.fslt.codec import decode_fslt
lazy from file_handlers.fslt.model import FsltData

from .glyphs import DMC5_GUI_GLYPH_POLICY, SF6_GUI_GLYPH_POLICY, GlyphFallbackPolicy, GlyphResolution
from .sfnt import SfntFont, SfntGlyphMetric


class FontCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FontCatalogProfile:
    name: str
    oft_version: int
    platform_suffix: str
    glyph_policy: GlyphFallbackPolicy
    slot_lists: bool = False


DMC5_FONT_CATALOG_PROFILE = FontCatalogProfile(
    "DMC5",
    oft_version=1,
    platform_suffix="x64",
    glyph_policy=DMC5_GUI_GLYPH_POLICY,
)

FONT_CATALOG_PROFILES: dict[int, FontCatalogProfile] = {
    15: DMC5_FONT_CATALOG_PROFILE,
    26: FontCatalogProfile("SF6", oft_version=1, platform_suffix="stm", glyph_policy=SF6_GUI_GLYPH_POLICY,
                           slot_lists=True),
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
    font: SfntFont
    adjust: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)

    def glyph_metric(self, glyph_id: int, *, vertical: bool = False) -> SfntGlyphMetric:
        return self.font.glyph_metric(glyph_id, vertical=vertical)


@dataclass(frozen=True, slots=True)
class FontSlotFaces:
    mapping: FontSlotMapping
    faces: tuple[FontFaceAsset, ...]
    scale: tuple[float, float]


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


@lru_cache(maxsize=32)
def _font(data: bytes, version: int) -> SfntFont:
    return OftFile.from_bytes(data, version=version).font()


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
        self._font_cache: dict[str, tuple[str, SfntFont] | None] = {}
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
            ResourceResolutionContext(project_dir=root, path_prefix=prefix)
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
        if asset_path not in self._font_cache:
            resolved = self.resolve_resource(asset_path)
            self._font_cache[asset_path] = resolved and (resolved[0], _font(resolved[1], self.profile.oft_version))
        return self._font_cache[asset_path]

    def slot_faces(self, language: int, slot: int) -> FontSlotFaces:
        key = int(language), int(slot)
        cached = self._slot_cache.get(key)
        if cached is not None:
            return cached
        mapping = self.config.font_slot(*key)
        faces: list[FontFaceAsset] = []
        listed = [(font.path, font.adjust) for font in self._slot_list(key[0]).slots[key[1]].fonts] if (
            self.profile.slot_lists) else [(path, (0.0, 0.0, 1.0, 1.0)) for path in mapping.asset_paths]
        for candidate_index, (asset_path, adjust) in enumerate(listed):
            if not asset_path:
                continue
            loaded = self._load_font(asset_path)
            if loaded is None:
                continue
            normalized, platform_variant = normalize_font_resource_path(asset_path)
            resource_name, font = loaded
            faces.append(
                FontFaceAsset(
                    candidate_index,
                    asset_path,
                    normalized,
                    platform_variant,
                    resource_name,
                    font,
                    tuple(adjust),
                )
            )
        result = FontSlotFaces(mapping, tuple(faces), (mapping.scale_for(0), mapping.scale_for(1)))
        self._slot_cache[key] = result
        return result

    @property
    def languages(self) -> frozenset[int]:
        config = self.config
        return frozenset(language for language in range(config.language_count) if (
            config.font_slot_list_paths[language] if self.profile.slot_lists else any(
                any(config.font_slot(language, slot).asset_paths) for slot in range(config.font_slot_count))))

    def _slot_list(self, language: int):
        path = self.config.font_slot_list_paths[language]
        if not path:
            return FsltData()
        resolved = self.resource_data_loader(path)
        if resolved is None:
            raise FontCatalogError(f"language {language} names no loadable font slot list ({path!r})")
        return decode_fslt(bytes(resolved[1]))

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
