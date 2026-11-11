"""See docs/internals/gui-rendering.md, Pass catalog."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from file_handlers.build_catalog import catalog_reader
from file_handlers.mmtr.sampler_states import SamplerState, state_from_dict

CATALOG_FORMAT = 1
CATALOG_DIRECTORY = "resources/data/gui_passes"


class GuiPassCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GuiPassCatalog:
    game: str
    resource_version: int
    programs: tuple[str, int]
    filters: tuple[str, int]
    names: Mapping[str, Mapping[str, Mapping[str, str]]]
    mask_write_space: Mapping[str, str]
    world_view: Mapping[str, object]
    blur_chain: Mapping[str, object]
    material: Mapping[str, object]
    camera: Mapping[str, float]
    null_texture: str
    samplers: Mapping[str, SamplerState]
    element_sampler: str
    element_texture: str
    mask_textures: tuple[str, ...]
    kind_samplers: Mapping[str, str]
    system: Mapping[str, object]
    targets: Mapping[str, Mapping[str, object]]
    glyph_filters: Mapping[str, object]

    def program_name(self, grammar: str, **choices: str) -> str:
        if choices.get("mask") == "write":
            choices["space"] = self.mask_write_space.get(choices["space"], choices["space"])
        return "".join(segment[choices[key]] if key in choices else "" for key, segment in self.names[grammar].items())


def _file(data: Mapping) -> tuple[str, int]:
    return str(data["file"]), int(data["target"])


def catalog_from_dict(data: Mapping) -> GuiPassCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise GuiPassCatalogError(f"GUI pass catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    samplers = {str(name): state_from_dict(state) for name, state in data["samplers"].items()}
    kinds = {str(kind): str(name) for kind, name in data["kind_samplers"].items()}
    if not set(kinds.values()) <= samplers.keys():
        raise GuiPassCatalogError(f"kind samplers {kinds} name samplers the catalog lacks")
    names = {grammar: {key: {str(choice): str(text) for choice, text in segment.items()}
                       for key, segment in segments.items()} for grammar, segments in data["names"].items()}
    return GuiPassCatalog(str(data["game"]), int(data["resource_version"]), _file(data["programs"]),
                          _file(data["filters"]), names, dict(data["mask_write_space"]), dict(data["world_view"]),
                          dict(data["blur_chain"]), dict(data["material"]),
                          {str(key): float(value) for key, value in data["camera"].items()},
                          str(data["null_texture"]), samplers, str(data["element_sampler"]),
                          str(data["element_texture"]), tuple(data["mask_textures"]), kinds,
                          {str(key): value for key, value in data["system"].items()},
                          {str(key): dict(value) for key, value in data["targets"].items()},
                          dict(data["glyph_filters"]))


gui_pass_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, GuiPassCatalogError, "GUI pass",
                                      (AttributeError,))
