"""See docs/internals/sampler-catalog.md."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from file_handlers.build_catalog import catalog_file_name, catalog_reader

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/sampler_states"


class SamplerCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SamplerState:
    """``D3D12_SAMPLER_DESC``."""
    filter: int
    address: tuple[int, int, int]
    mip_bias: float
    anisotropy: int
    comparison: int
    border: tuple[float, float, float, float]
    lod: tuple[float, float]

    def descriptor(self) -> tuple:
        """fastmatpreview's sampler tuple; callers prepend the descriptor index."""
        return (self.filter, *self.address, self.mip_bias, self.anisotropy, self.comparison, self.border, *self.lod)


def state_to_dict(state: SamplerState) -> dict:
    return {"filter": state.filter, "address": list(state.address), "mip_bias": state.mip_bias,
            "anisotropy": state.anisotropy, "comparison": state.comparison, "border": list(state.border),
            "lod": list(state.lod)}


def state_from_dict(data: Mapping) -> SamplerState:
    address, border, lod = (tuple(data[key]) for key in ("address", "border", "lod"))
    if len(address) != 3 or len(border) != 4 or len(lod) != 2:
        raise SamplerCatalogError(f"sampler state {dict(data)!r} is not a Direct3D 12 sampler")
    return SamplerState(int(data["filter"]), tuple(int(item) for item in address), float(data["mip_bias"]),
                        int(data["anisotropy"]), int(data["comparison"]), tuple(float(item) for item in border),
                        tuple(float(item) for item in lod))


@dataclass(frozen=True, slots=True)
class SamplerCatalog:
    game: str
    resource_version: int
    states: Mapping[str, SamplerState]
    source: Mapping[str, object]


def catalog_to_dict(catalog: SamplerCatalog) -> dict:
    return {"format": CATALOG_FORMAT, "game": catalog.game, "resource_version": catalog.resource_version,
            "source": dict(catalog.source),
            "states": {name: state_to_dict(state) for name, state in sorted(catalog.states.items())}}


def catalog_from_dict(data: Mapping) -> SamplerCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise SamplerCatalogError(f"sampler catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    return SamplerCatalog(str(data["game"]), int(data["resource_version"]),
                          {str(name): state_from_dict(state) for name, state in data["states"].items()},
                          dict(data["source"]))


sampler_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, SamplerCatalogError, "sampler")


__all__ = [
    "CATALOG_DIRECTORY", "SamplerCatalog", "SamplerCatalogError", "SamplerState", "catalog_file_name",
    "catalog_from_dict", "catalog_to_dict", "sampler_catalog_for", "state_from_dict", "state_to_dict",
]
