"""See docs/internals/gui-rendering.md, Pass catalog."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from file_handlers.build_catalog import catalog_reader
from file_handlers.mmtr.sampler_states import SamplerState, state_from_dict

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/gui_passes"


class GuiPassCatalogError(ValueError):
    pass


def _holds(when: Mapping[str, list], choices: Mapping[str, str]) -> bool:
    return all(choices.get(key) in allowed for key, allowed in when.items())


@dataclass(frozen=True, slots=True)
class GuiPassCatalog:
    game: str
    resource_version: int
    programs: tuple[str, int]
    filters: tuple[str, int]
    tables: Mapping[str, tuple[str, ...]]
    pickers: Mapping[str, Mapping[str, object]]
    world_view: Mapping[str, object] | None
    blur_chain: Mapping[str, object] | None
    material: Mapping[str, object] | None
    mesh: Mapping[str, object] | None
    camera: Mapping[str, float] | None
    null_texture: str
    samplers: Mapping[str, SamplerState]
    element_sampler: str
    element_texture: str
    depth_texture: str | None
    mask_textures: tuple[str, ...]
    kind_samplers: Mapping[str, str]
    system: Mapping[str, object]
    targets: Mapping[str, Mapping[str, object]]
    glyph_filters: Mapping[str, object] | None
    distance_field: Mapping[str, object] | None
    static_space: int | None
    constants: Mapping[str, object]
    common_flag: Mapping[str, int] | None
    draw_context: Mapping[str, Mapping[str, object]]
    view_choices: Mapping[str, tuple[str, ...]]
    add_color: str | None

    def program_name(self, picker: str, **choices: str) -> str:
        found = self.pickers[picker]
        rule = next((rule for rule in found["rules"] if _holds(rule["when"], choices)), None)
        if rule is None:
            raise GuiPassCatalogError(f"no {picker} program rule takes {choices}")
        table, at = found["table"], rule["at"] + sum(steps.get(choices.get(key), 0)
                                                     for key, steps in rule.get("add", {}).items())
        for step in found.get("after", ()):
            if _holds(step["when"], choices):
                table = step.get("table", table)
                at = step["map"].get(str(at), at) if "map" in step else step.get("to", at + step.get("shift", 0) + sum(
                    offset for low, high, offset in step.get("ranges", ()) if low <= at < high))
        return self.tables[table][at]


def _file(data: Mapping) -> tuple[str, int]:
    return str(data["file"]), int(data["target"])


def catalog_from_dict(data: Mapping) -> GuiPassCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise GuiPassCatalogError(f"GUI pass catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    samplers = {str(name): state_from_dict(state) for name, state in data["samplers"].items()}
    kinds = {str(kind): str(name) for kind, name in data["kind_samplers"].items()}
    if not set(kinds.values()) <= samplers.keys():
        raise GuiPassCatalogError(f"kind samplers {kinds} name samplers the catalog lacks")
    tables = {str(name): tuple(map(str, names)) for name, names in data["tables"].items()}
    pickers = {str(name): dict(picker) for name, picker in data["pickers"].items()}
    named = {picker["table"] for picker in pickers.values()} | {
        step["table"] for picker in pickers.values() for step in picker.get("after", ()) if "table" in step}
    if not named <= tables.keys():
        raise GuiPassCatalogError(f"program pickers name tables the catalog lacks: {sorted(tables)}")
    camera = data.get("camera")
    return GuiPassCatalog(str(data["game"]), int(data["resource_version"]), _file(data["programs"]),
                          _file(data["filters"]), tables, pickers, data.get("world_view"), data.get("blur_chain"),
                          data.get("material"), data.get("mesh"),
                          None if camera is None else {str(key): float(value) for key, value in camera.items()},
                          str(data["null_texture"]), samplers, str(data["element_sampler"]),
                          str(data["element_texture"]), data.get("depth_texture"), tuple(data["mask_textures"]), kinds,
                          {str(key): value for key, value in data["system"].items()},
                          {str(key): dict(value) for key, value in data["targets"].items()}, data.get("glyph_filters"),
                          data["distance_field"], data.get("static_space"), dict(data.get("constants", {})),
                          data.get("common_flag"), dict(data.get("draw_context", {})),
                          {str(key): tuple(names) for key, names in data.get("view_choices", {}).items()},
                          data.get("add_color"))


gui_pass_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, GuiPassCatalogError, "GUI pass",
                                      (AttributeError,))
