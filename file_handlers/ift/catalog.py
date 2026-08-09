from __future__ import annotations

from dataclasses import dataclass

from file_handlers.gcf.model import GcfData
from file_handlers.uvs.uvs_file import UvsFile
from utils.resource_file_utils import (
    ResourceDataLoader,
    resource_path_with_version,
    resource_version_from_path,
)

from .ift_file import IftFile
from .model import IconGlyph, IftAtlasValidation


@dataclass(frozen=True, slots=True)
class IconFontCatalogProfile:
    name: str
    ift_version: int
    default_uvs_version: int


DMC5_ICON_FONT_CATALOG_PROFILE = IconFontCatalogProfile("DMC5", 1, 7)
ICON_FONT_CATALOG_PROFILES: dict[int, IconFontCatalogProfile] = {
    15: DMC5_ICON_FONT_CATALOG_PROFILE,
}


def icon_font_catalog_profile(gcf_version: int) -> IconFontCatalogProfile:
    try:
        return ICON_FONT_CATALOG_PROFILES[int(gcf_version)]
    except KeyError as exc:
        supported = ", ".join(map(str, sorted(ICON_FONT_CATALOG_PROFILES)))
        raise ValueError(
            f"no icon-font profile for GCF version {gcf_version}; "
            f"supported GCF versions: {supported}"
        ) from exc


class IconFontCatalog:
    """Resolve a GCF icon font through IFT and its companion UVS atlas."""

    def __init__(
        self,
        icon_font_path: str,
        resource_data_loader: ResourceDataLoader,
        *,
        profile: IconFontCatalogProfile,
    ) -> None:
        if not icon_font_path:
            raise ValueError("icon font resource path cannot be empty")
        self.icon_font_path = icon_font_path
        self.resource_data_loader = resource_data_loader
        self.profile = profile
        self.ift_resource_name = ""
        self.uvs_resource_name = ""
        self.ift: IftFile | None = None
        self.uvs: UvsFile | None = None

    @classmethod
    def from_gcf(
        cls,
        config: GcfData,
        resource_data_loader: ResourceDataLoader,
        *,
        profile: IconFontCatalogProfile | None = None,
    ) -> "IconFontCatalog":
        if not config.icon_font_asset_path:
            raise ValueError("GCF has no icon font resource path")
        return cls(
            config.icon_font_asset_path,
            resource_data_loader,
            profile=profile or icon_font_catalog_profile(config.version),
        )

    def load(self) -> None:
        if self.ift is not None and self.uvs is not None:
            return
        ift_path = resource_path_with_version(
            self.icon_font_path, "ift", self.profile.ift_version
        )
        resolved_ift = self.resource_data_loader(ift_path)
        if resolved_ift is None:
            raise FileNotFoundError(
                f"unable to resolve icon font resource: {self.icon_font_path}"
            )
        ift_name, ift_data = resolved_ift
        self.ift_resource_name = str(ift_name)
        self.ift = IftFile.from_bytes(bytes(ift_data))

        uv_path = resource_path_with_version(
            self.ift.require_model().uv_sequence_path,
            "uvs",
            self.profile.default_uvs_version,
        )
        resolved_uvs = self.resource_data_loader(uv_path)
        if resolved_uvs is None:
            raise FileNotFoundError(f"unable to resolve icon UVS resource: {uv_path}")
        uvs_name, uvs_data = resolved_uvs
        self.uvs_resource_name = str(uvs_name)
        version = (
            resource_version_from_path(self.uvs_resource_name, "uvs")
            or self.profile.default_uvs_version
        )
        self.uvs = UvsFile()
        self.uvs.read(bytes(uvs_data), version=version)

    def resolve(self, name: str) -> IconGlyph | None:
        self.load()
        assert self.ift is not None and self.uvs is not None
        return self.ift.resolve(name, self.uvs)

    def validate(self) -> IftAtlasValidation:
        self.load()
        assert self.ift is not None and self.uvs is not None
        return self.ift.validate_uvs(self.uvs)
