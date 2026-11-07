"""See docs/internals/mesh-preview.md, Effect masters."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

lazy import numpy as np

from file_handlers.build_catalog import catalog_file_name, catalog_reader

lazy from .engine_buffers import laid_out

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/effect_meshes"


class EffectMeshCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EffectMeshCatalog:
    game: str
    resource_version: int
    program: str
    routes: frozenset[str]
    buffer: str
    values: Mapping[str, float]
    dynamic: frozenset[str]


def catalog_from_dict(data: Mapping) -> EffectMeshCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise EffectMeshCatalogError(f"effect mesh catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    emitter = data["emitter"]
    return EffectMeshCatalog(str(data["game"]), int(data["resource_version"]), str(data["program"]),
                             frozenset(str(route) for route in data["routes"]), str(emitter["buffer"]),
                             {str(name): float(value) for name, value in emitter["values"].items()},
                             frozenset(str(name) for name in data["dynamic_textures"]))


effect_mesh_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, EffectMeshCatalogError, "effect mesh",
                                         (AttributeError,))


def emitter_constants(layout, catalog: EffectMeshCatalog) -> bytes:
    return bytes(laid_out(layout, {name: np.array([value]) for name, value in catalog.values.items()}, True))


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "EffectMeshCatalog", "EffectMeshCatalogError", "catalog_file_name",
           "catalog_from_dict", "effect_mesh_catalog_for", "emitter_constants"]
