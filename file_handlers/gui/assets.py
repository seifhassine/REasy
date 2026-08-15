"""Resolve GUI UV-sequence and direct-texture references through REasy."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from file_handlers.mdf.mdf_file import MdfFile
from file_handlers.mesh.mesh_file import MeshFile
from file_handlers.tex.tex_file import TexFile
from file_handlers.uvs.uvs_file import UvsFile, UvsPattern
from utils.resource_file_utils import (
    ResourceDataLoader,
    normalize_resource_path,
    resource_path_with_version,
    resource_version_from_path,
)

from .errors import GuiAssetError
from .profiles import GuiFormatProfile


@dataclass(frozen=True, slots=True)
class ResolvedUvPattern:
    uv_sequence_reference: str
    uv_sequence_resource: str
    sequence_index: int
    pattern_index: int
    pattern_flags: int
    uv_bounds: tuple[float, float, float, float]
    cutout_uvs: tuple[tuple[float, float], ...]
    texture_index: int
    texture_reference: str
    texture_resource: str
    texture_size: tuple[int, int]
    texture_format: int

    @property
    def mirrored_x(self) -> bool:
        return self.uv_bounds[2] < self.uv_bounds[0]

    @property
    def mirrored_y(self) -> bool:
        return self.uv_bounds[3] < self.uv_bounds[1]


class GuiAssetCatalog:
    """Resolve GUI-owned UVS/TEX references without a separate file index."""

    def __init__(
        self,
        resource_data_loader: ResourceDataLoader,
        profile: GuiFormatProfile,
    ) -> None:
        self.resource_data_loader = resource_data_loader
        self.profile = profile

    def _load_resource(
        self,
        reference: str,
        extension: str,
        version: int,
    ) -> tuple[str, bytes]:
        candidate = resource_path_with_version(
            normalize_resource_path(reference),
            extension,
            version,
        )
        resolved = self.resource_data_loader(candidate)
        if resolved is None:
            raise GuiAssetError(
                f"unable to resolve {extension.upper()} resource {candidate!r}"
            )
        source, data = resolved
        return str(source), bytes(data)

    @lru_cache(maxsize=None)
    def load_uvs(self, reference: str) -> tuple[str, UvsFile]:
        source, data = self._load_resource(
            reference,
            "uvs",
            self.profile.default_uvs_version,
        )
        version = (
            resource_version_from_path(source, "uvs")
            or resource_version_from_path(reference, "uvs")
            or self.profile.default_uvs_version
        )
        uvs = UvsFile()
        try:
            uvs.read(data, version=version)
        except Exception as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, uvs

    @lru_cache(maxsize=None)
    def load_tex(self, reference: str) -> tuple[str, TexFile]:
        source, data = self._load_resource(
            reference,
            "tex",
            self.profile.default_tex_version,
        )
        version = (
            resource_version_from_path(source, "tex")
            or resource_version_from_path(reference, "tex")
            or self.profile.default_tex_version
        )
        texture = TexFile()
        try:
            if not texture.read(data, file_version=version):
                raise GuiAssetError(f"invalid TEX magic in {source}")
        except Exception as exc:
            if isinstance(exc, GuiAssetError):
                raise
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, texture

    @lru_cache(maxsize=None)
    def load_mdf(self, reference: str) -> tuple[str, MdfFile]:
        source, data = self._load_resource(
            reference,
            "mdf2",
            self.profile.default_mdf_version,
        )
        versioned = resource_path_with_version(
            reference,
            "mdf2",
            resource_version_from_path(source, "mdf2")
            or self.profile.default_mdf_version,
        )
        material = MdfFile()
        try:
            material.read(
                data,
                source if resource_version_from_path(source, "mdf2") else versioned,
            )
        except Exception as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, material

    @lru_cache(maxsize=None)
    def load_mesh(self, reference: str) -> tuple[str, MeshFile]:
        source, data = self._load_resource(
            reference,
            "mesh",
            self.profile.default_mesh_version,
        )
        version = (
            resource_version_from_path(source, "mesh")
            or resource_version_from_path(reference, "mesh")
            or self.profile.default_mesh_version
        )
        mesh = MeshFile()
        try:
            mesh.read(data, file_version=version)
        except Exception as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, mesh

    def resolve_uv(
        self,
        reference: str,
        sequence_index: int,
        pattern_index: int,
    ) -> ResolvedUvPattern:
        uvs_source, uvs = self.load_uvs(reference)
        sequence_index = int(sequence_index)
        pattern_index = int(pattern_index)
        if not 0 <= sequence_index < len(uvs.sequences):
            raise GuiAssetError(
                f"{reference}: sequence {sequence_index} is outside "
                f"0..{len(uvs.sequences) - 1}"
            )
        sequence = uvs.sequences[sequence_index]
        if not 0 <= pattern_index < len(sequence.patterns):
            raise GuiAssetError(
                f"{reference}: pattern {pattern_index} is outside sequence "
                f"{sequence_index}'s 0..{len(sequence.patterns) - 1}"
            )
        pattern: UvsPattern = sequence.patterns[pattern_index]
        texture_index = int(pattern.texture_index)
        if not 0 <= texture_index < len(uvs.textures):
            raise GuiAssetError(
                f"{reference}: texture {texture_index} is outside "
                f"0..{len(uvs.textures) - 1}"
            )
        texture_reference = uvs.textures[texture_index].path
        if not texture_reference:
            raise GuiAssetError(
                f"{reference}: texture {texture_index} has an empty primary path"
            )
        texture_source, texture = self.load_tex(texture_reference)
        return ResolvedUvPattern(
            uv_sequence_reference=reference,
            uv_sequence_resource=uvs_source,
            sequence_index=sequence_index,
            pattern_index=pattern_index,
            pattern_flags=int(pattern.flags),
            uv_bounds=(
                float(pattern.left),
                float(pattern.top),
                float(pattern.right),
                float(pattern.bottom),
            ),
            cutout_uvs=tuple((float(x), float(y)) for x, y in pattern.cutout_uvs),
            texture_index=texture_index,
            texture_reference=texture_reference,
            texture_resource=texture_source,
            texture_size=(texture.header.width, texture.header.height),
            texture_format=texture.header.format,
        )
