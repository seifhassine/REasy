"""See docs/internals/surface-output-catalog.md."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from file_handlers.build_catalog import catalog_file_name, catalog_reader

CATALOG_FORMAT = 5
CATALOG_DIRECTORY = "resources/data/surface_outputs"
GBUFFER_PURPOSE = "gbuffer"
PREPASS_PURPOSE = "depth-prepass"


class SurfaceMeaningError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class TargetMeaning:
    purpose: str
    target: int
    slot: str
    read_as: tuple[str, ...] = ()
    # A ``pixel_shader_digest``; empty for every pass of the purpose.
    shader: str = ""


@dataclass(frozen=True, slots=True)
class TargetFormat:
    """A target without one is unbound."""
    purpose: str
    target: int
    blending: bool
    # The engine's ``via.render.TextureFormat``, numbered as Direct3D's DXGI_FORMAT.
    format: int
    # For blending passes: the unblended passes' texture, or one the renderer fills in between.
    over_unblended: bool = False


@dataclass(frozen=True, slots=True)
class EngineTexture:
    purpose: str
    name: str
    file: str


@dataclass(frozen=True, slots=True)
class SurfaceOutputCatalog:
    game: str
    resource_version: int
    layout: str
    targets: tuple[TargetMeaning, ...]
    formats: tuple[TargetFormat, ...] = ()
    # Pass purpose -> depth target format (``via.render.TextureFormat``).
    depth_formats: Mapping[str, int] = field(default_factory=dict)
    source: Mapping = field(default_factory=dict)
    engine_textures: tuple[EngineTexture, ...] = ()

    def engine_texture(self, purpose: str, name: str) -> str | None:
        return next((item.file for item in self.engine_textures if item.purpose == purpose and item.name == name),
                    None)

    def target(self, purpose: str, semantic: str, semantic_index: int, shader: str = "") -> TargetMeaning | None:
        if semantic.casefold() != "sv_target":
            return None
        found = [item for item in self.targets if item.purpose == purpose and item.target == int(semantic_index)
                 and item.shader in ("", shader)]
        return next((item for item in found if shader and item.shader == shader), next(iter(found), None))


def catalog_to_dict(catalog: SurfaceOutputCatalog) -> dict:
    return {
        "format": CATALOG_FORMAT,
        "game": catalog.game,
        "resource_version": catalog.resource_version,
        "layout": catalog.layout,
        "targets": [{"purpose": item.purpose, "target": item.target, "slot": item.slot, "read_as": list(item.read_as)}
                    | ({"shader": item.shader} if item.shader else {}) for item in catalog.targets],
        "formats": [{"purpose": item.purpose, "target": item.target, "blending": item.blending, "format": item.format,
                     "over_unblended": item.over_unblended} for item in catalog.formats],
        "depth_formats": dict(catalog.depth_formats),
        "engine_textures": [{"purpose": item.purpose, "name": item.name, "file": item.file}
                            for item in catalog.engine_textures],
        "source": dict(catalog.source),
    }


def catalog_fingerprint(catalog: SurfaceOutputCatalog | None) -> str:
    if catalog is None:
        return ""
    return hashlib.sha256(json.dumps(catalog_to_dict(catalog), sort_keys=True).encode("utf-8")).hexdigest()


def catalog_from_dict(data: Mapping) -> SurfaceOutputCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise SurfaceMeaningError(f"surface output catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    return SurfaceOutputCatalog(
        str(data["game"]), int(data["resource_version"]), str(data["layout"]),
        tuple(TargetMeaning(str(item["purpose"]), int(item["target"]), str(item["slot"]),
                            tuple(str(name) for name in item["read_as"]), str(item.get("shader", "")))
              for item in data["targets"]),
        tuple(TargetFormat(str(item["purpose"]), int(item["target"]), bool(item["blending"]), int(item["format"]),
                           bool(item["over_unblended"])) for item in data["formats"]),
        {str(purpose): int(value) for purpose, value in data["depth_formats"].items()},
        dict(data.get("source", {})),
        tuple(EngineTexture(str(item["purpose"]), str(item["name"]), str(item["file"]))
              for item in data["engine_textures"]),
    )


def shader_bytes(shader) -> bytes:
    return bytes(shader.spirv.module) if shader.spirv is not None else bytes(shader.payload)


def pixel_shader_digest(shader) -> str:
    if shader.binary_format == "dxbc-container":
        return shader.reflection.sha256
    return hashlib.sha256(shader_bytes(shader)).hexdigest()


surface_output_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, SurfaceMeaningError, "surface output")


__all__ = [
    "CATALOG_DIRECTORY", "EngineTexture", "GBUFFER_PURPOSE", "PREPASS_PURPOSE", "SurfaceMeaningError",
    "SurfaceOutputCatalog", "TargetFormat", "TargetMeaning",
    "catalog_file_name", "catalog_fingerprint", "catalog_from_dict", "catalog_to_dict", "pixel_shader_digest",
    "shader_bytes", "surface_output_catalog_for",
]
