"""Resolve GUI UV-sequence and direct-texture references through REasy."""

from __future__ import annotations

import struct
from collections.abc import Callable
from dataclasses import dataclass
from functools import cache, cached_property
from typing import Any
from uuid import UUID

lazy from file_handlers.gcp.gcp_file import ColorPreset, preset_lookup
lazy from file_handlers.mdf.mdf_file import MdfFile
lazy from file_handlers.mesh.mesh_file import MeshFile, MeshLoadMode
lazy from file_handlers.rsz.game_registry import game_type_registry
lazy from file_handlers.rsz.plain_values import plain_value
lazy from file_handlers.rsz.rsz_file import RszFile
lazy from file_handlers.tex.tex_file import TexFile
lazy from file_handlers.uvs.uvs_file import UvsFile, UvsPattern
lazy from utils.resource_file_utils import ResourceDataLoader, resource_version_from_path

lazy from .errors import GuiAssetError


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

    def __init__(self, resource_data_loader: ResourceDataLoader, preset_path: Callable[[], str | None]) -> None:
        self.resource_data_loader, self.preset_path = resource_data_loader, preset_path
        for name in ("load_uvs", "load_tex", "load_mdf", "load_mesh", "load_text_animation"):
            setattr(self, name, cache(getattr(self, name)))

    @cached_property
    def color_presets(self) -> dict[UUID, ColorPreset]:
        if (path := self.preset_path()) is None:
            return {}
        source, data, _version = self._load_resource(path, "gcp")
        try:
            return preset_lookup(data)
        except ValueError as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc

    def _load_resource(self, reference: str, extension: str) -> tuple[str, bytes, int]:
        resolved = self.resource_data_loader(reference)
        if resolved is None:
            raise GuiAssetError(f"unable to resolve {extension.upper()} resource {reference!r}")
        source = str(resolved[0])
        version = resource_version_from_path(source, extension)
        if version is None:
            raise GuiAssetError(f"{source} names no {extension.upper()} version")
        return source, bytes(resolved[1]), version

    def load_uvs(self, reference: str) -> tuple[str, UvsFile]:
        source, data, version = self._load_resource(reference, "uvs")
        uvs = UvsFile()
        try:
            uvs.read(data, version=version)
        except Exception as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, uvs

    def load_tex(self, reference: str) -> tuple[str, TexFile]:
        source, data, version = self._load_resource(reference, "tex")
        texture = TexFile()
        try:
            if not texture.read(data, file_version=version):
                raise GuiAssetError(f"invalid TEX magic in {source}")
        except Exception as exc:
            if isinstance(exc, GuiAssetError):
                raise
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, texture

    def load_mdf(self, reference: str) -> tuple[str, MdfFile]:
        source, data, _version = self._load_resource(reference, "mdf2")
        material = MdfFile()
        try:
            material.read(data, source)
        except Exception as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, material

    def load_mesh(self, reference: str) -> tuple[str, MeshFile]:
        source, data, version = self._load_resource(reference, "mesh")
        mesh = MeshFile()
        try:
            mesh.read(
                data,
                file_version=version,
                load_mode=MeshLoadMode.SCENE,
            )
        except Exception as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc
        return source, mesh

    def load_text_animation(self, reference: str, game: str) -> tuple[str, dict[str, Any]]:
        source, data, _version = self._load_resource(reference, "tean")
        rsz = RszFile()
        rsz.filepath, rsz.type_registry, rsz.game_version = source, game_type_registry(game), game
        try:
            rsz.read_headless(data[struct.unpack_from("<Q", data, 8)[0]:])
            root = rsz.object_table[0]
            return rsz.type_registry.get_type_info(rsz.instance_infos[root].type_id)["name"], {
                name: plain_value(value, rsz.parsed_elements.__getitem__)
                for name, value in rsz.parsed_elements[root].items()}
        except Exception as exc:
            raise GuiAssetError(f"failed to parse {source}: {exc}") from exc

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
