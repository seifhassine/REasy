"""See docs/internals/strands-chain.md."""
from __future__ import annotations

import base64
import zlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

from file_handlers.build_catalog import catalog_file_name, catalog_reader

CATALOG_FORMAT = 18
STATES = ("simulation", "shading_quality", "fixed_lod_mode")
PHYSICS_GENERATIONS, RENDER_GENERATIONS = ("cosserat", "spring"), ("oit", "direct")
CATALOG_DIRECTORY = "resources/data/strands"
NOW, BEFORE = "@now", "@before"


class StrandCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ChainStepKind:
    programs: Mapping[str, str | None]
    bindings: Mapping[str, str]
    targets: tuple[str, ...] = ()
    depth: str | None = None
    material: bool = False


@dataclass(frozen=True, slots=True)
class WrapperFields:
    enabled: str
    follow: str
    joint: str
    world: str
    local: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ConstantSources:
    component: str
    copies: Mapping[str, str]
    own: Mapping[str, str]
    arguments: Mapping[str, str]
    wrappers: Mapping[str, WrapperFields]
    literals: Mapping[str, int]
    settle_frames: int


@dataclass(frozen=True, slots=True)
class MadeTexture:
    format: int
    size: tuple[int, int]
    texels: bytes


@dataclass(frozen=True, slots=True)
class Anchor:
    after: int
    phase: int = -1
    inside: bool = False


@dataclass(frozen=True, slots=True)
class RenderSection:
    file: str
    target: int
    steps: Mapping[str, ChainStepKind]
    component: str
    arguments: Mapping[str, str]
    options_component: str
    options: Mapping[str, str]
    settings: Mapping[str, str]
    scripted: Mapping[str, bool | int]
    lut: tuple[int, int, int] | None
    version: int
    textures: Mapping[str, MadeTexture]
    camera: Mapping[str, object]
    stencil_reference: int | None
    anchors: tuple[Anchor, ...]
    drawn_into: Mapping[str, tuple[str, str]]
    generation: str
    unserialized: Mapping[str, object]

    def program(self, step: str, variant: str) -> str:
        return _program(self.steps, step, variant)

    @property
    def writes_stencil(self) -> bool:
        return self.settings["write_stencil"] == "True"

    @property
    def lod_settings(self) -> str | None:
        """RE4's build has none."""
        return self.settings.get("lod_settings")


@dataclass(frozen=True, slots=True)
class LodSection:
    """``level``: ``local`` or ``world``; ``failsafe``: ``grown`` or ``union``."""
    fields: Mapping[str, str]
    dynamics_fields: Mapping[str, str]
    limit: int
    level: str
    failsafe: str
    run_cull: bool
    mesh_component: str
    mesh_fields: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class PlacementSection:
    mesh_component: str
    mesh: Mapping[str, str]
    skeleton_component: str
    skeleton: Mapping[str, str]
    preset_component: str
    preset: Mapping[str, str]
    preset_info: Mapping[str, str]
    stamp_component: str
    stamp: Mapping[str, str]
    dynamics: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class SkinningSection:
    file: str
    target: int
    program: str
    buffers: Mapping[str, str]
    constants: Mapping[str, str]
    plain: Mapping[int, str]


@dataclass(frozen=True, slots=True)
class StrandCatalog:
    game: str
    resource_version: int
    file: str
    target: int
    generation: str
    steps: Mapping[str, ChainStepKind]
    constants: ConstantSources
    render: RenderSection
    lod: LodSection
    placement: PlacementSection
    skinning: SkinningSection | None
    states: Mapping[str, int]
    source: Mapping = field(default_factory=dict)

    def program(self, step: str, variant: str) -> str:
        return _program(self.steps, step, variant)


def _program(steps: Mapping[str, ChainStepKind], step: str, variant: str) -> str:
    programs = steps[step].programs if step in steps else {}
    if programs.get(variant) is None:
        raise StrandCatalogError(f"the build has no program for the step {step!r} with {variant!r}")
    return programs[variant]


def _steps_to_dict(steps: Mapping[str, ChainStepKind]) -> dict:
    return {name: {"programs": dict(step.programs), "bindings": dict(sorted(step.bindings.items())),
                   **({"targets": list(step.targets), "depth": step.depth} if step.targets else {}),
                   **({"material": True} if step.material else {})}
            for name, step in steps.items()}


def _steps_from_dict(data: Mapping) -> dict[str, ChainStepKind]:
    return {str(name): ChainStepKind(
        {str(variant): None if program is None else str(program) for variant, program in step["programs"].items()},
        {str(resource): str(slot) for resource, slot in step["bindings"].items()},
        tuple(map(str, step.get("targets", ()))), None if step.get("depth") is None else str(step["depth"]),
        bool(step.get("material", False)))
        for name, step in data.items()}


def catalog_to_dict(catalog: StrandCatalog) -> dict:
    render = catalog.render
    return {"format": CATALOG_FORMAT, "game": catalog.game, "resource_version": catalog.resource_version,
            "file": catalog.file, "target": catalog.target, "generation": catalog.generation,
            "steps": _steps_to_dict(catalog.steps),
            "constants": {"component": catalog.constants.component,
                          **{name: dict(sorted(getattr(catalog.constants, name).items()))
                             for name in ("copies", "own", "arguments", "literals")},
                          "wrappers": {group: {**asdict(fields), "local": list(fields.local)}
                                       for group, fields in catalog.constants.wrappers.items()},
                          "settle_frames": catalog.constants.settle_frames},
            "render": {"file": render.file, "target": render.target, "generation": render.generation,
                       "steps": _steps_to_dict(render.steps),
                       "component": render.component, "arguments": dict(sorted(render.arguments.items())),
                       "options_component": render.options_component,
                       "options": dict(sorted(render.options.items())), "settings": dict(render.settings),
                       "scripted": dict(render.scripted), "lut": None if render.lut is None else list(render.lut),
                       "version": render.version,
                       "textures": {slot: {"format": texture.format, "size": list(texture.size),
                                           "texels": base64.b64encode(zlib.compress(texture.texels, 9)).decode("ascii")}
                                    for slot, texture in render.textures.items()},
                       "camera": dict(render.camera), "stencil_reference": render.stencil_reference,
                       "anchors": [asdict(anchor) for anchor in render.anchors],
                       "drawn_into": {slot: list(source) for slot, source in render.drawn_into.items()},
                       "unserialized": dict(sorted(render.unserialized.items()))},
            "lod": {**{name: dict(getattr(catalog.lod, name)) for name in ("fields", "dynamics_fields")},
                    "limit": catalog.lod.limit, "level": catalog.lod.level, "failsafe": catalog.lod.failsafe,
                    "run_cull": catalog.lod.run_cull, "mesh_component": catalog.lod.mesh_component,
                    "mesh_fields": dict(catalog.lod.mesh_fields)},
            "placement": {"mesh_component": catalog.placement.mesh_component, "mesh": dict(catalog.placement.mesh),
                          "skeleton_component": catalog.placement.skeleton_component,
                          "skeleton": dict(catalog.placement.skeleton),
                          "preset_component": catalog.placement.preset_component,
                          "preset": dict(catalog.placement.preset), "preset_info": dict(catalog.placement.preset_info),
                          "stamp_component": catalog.placement.stamp_component,
                          "stamp": dict(catalog.placement.stamp), "dynamics": dict(catalog.placement.dynamics)},
            "skinning": None if catalog.skinning is None else {
                "file": catalog.skinning.file, "target": catalog.skinning.target,
                "program": catalog.skinning.program, "buffers": dict(catalog.skinning.buffers),
                "constants": dict(catalog.skinning.constants),
                "plain": {str(code): name for code, name in sorted(catalog.skinning.plain.items())}},
            "states": dict(catalog.states), "source": dict(catalog.source)}


def catalog_from_dict(data: Mapping) -> StrandCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise StrandCatalogError(f"strand catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    held = data["constants"]
    constants = ConstantSources(
        str(held["component"]), *({str(key): str(value) for key, value in held[name].items()}
                                  for name in ("copies", "own", "arguments")),
        {str(group): WrapperFields(str(fields["enabled"]), str(fields["follow"]), str(fields["joint"]),
                                   str(fields["world"]), tuple(map(str, fields["local"])))
         for group, fields in held["wrappers"].items()},
        {str(key): int(value) for key, value in held["literals"].items()}, int(held["settle_frames"]))
    drawn = data["render"]
    plain = {name: {str(key): str(value) for key, value in drawn[name].items()}
             for name in ("arguments", "options", "settings")}
    render = RenderSection(str(drawn["file"]), int(drawn["target"]), _steps_from_dict(drawn["steps"]),
                           str(drawn["component"]), plain["arguments"], str(drawn["options_component"]),
                           plain["options"], plain["settings"],
                           {str(key): value for key, value in drawn["scripted"].items()},
                           None if drawn["lut"] is None else tuple(int(count) for count in drawn["lut"]),
                           int(drawn["version"]),
                           {str(slot): MadeTexture(int(texture["format"]), (int(texture["size"][0]),
                                                                            int(texture["size"][1])),
                                                   zlib.decompress(base64.b64decode(texture["texels"])))
                            for slot, texture in drawn["textures"].items()},
                           dict(drawn["camera"]),
                           None if drawn["stencil_reference"] is None else int(drawn["stencil_reference"]),
                           tuple(Anchor(int(anchor["after"]), int(anchor["phase"]), bool(anchor["inside"]))
                                 for anchor in drawn["anchors"]),
                           {str(slot): (str(kind), str(value)) for slot, (kind, value) in drawn["drawn_into"].items()},
                           _generation(drawn["generation"], RENDER_GENERATIONS),
                           {str(field): value for field, value in drawn["unserialized"].items()})
    held = data["lod"]
    if held["level"] not in ("local", "world"):
        raise StrandCatalogError(f"no vertex LOD rule {held['level']!r}")
    if held["failsafe"] not in ("grown", "union"):
        raise StrandCatalogError(f"no failsafe box {held['failsafe']!r}")
    plain = {name: {str(key): str(value) for key, value in held[name].items()}
             for name in ("fields", "dynamics_fields", "mesh_fields")}
    lod = LodSection(plain["fields"], plain["dynamics_fields"], int(held["limit"]), str(held["level"]),
                     str(held["failsafe"]), bool(held["run_cull"]), str(held["mesh_component"]), plain["mesh_fields"])
    held = data["placement"]
    named = {name: {str(key): str(value) for key, value in held[name].items()}
             for name in ("mesh", "skeleton", "preset", "preset_info", "stamp", "dynamics")}
    placement = PlacementSection(str(held["mesh_component"]), named["mesh"], str(held["skeleton_component"]),
                                 named["skeleton"], str(held["preset_component"]), named["preset"],
                                 named["preset_info"], str(held["stamp_component"]), named["stamp"],
                                 named["dynamics"])
    held = data["skinning"]
    skinning = None if held is None else SkinningSection(
        str(held["file"]), int(held["target"]), str(held["program"]),
        *({str(key): str(value) for key, value in held[name].items()} for name in ("buffers", "constants")),
        {int(code): str(name) for code, name in held["plain"].items()})
    if set(data["states"]) != set(STATES):
        raise StrandCatalogError(f"the catalog's states are {sorted(data['states'])}, not {list(STATES)}")
    states = {name: int(data["states"][name]) for name in STATES}
    return StrandCatalog(str(data["game"]), int(data["resource_version"]), str(data["file"]), int(data["target"]),
                         _generation(data["generation"], PHYSICS_GENERATIONS), _steps_from_dict(data["steps"]),
                         constants, render, lod, placement, skinning, states, dict(data.get("source", {})))


def _generation(value, known: tuple[str, ...]) -> str:
    if value not in known:
        raise StrandCatalogError(f"no strand chain generation {value!r}")
    return str(value)


strand_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, StrandCatalogError,
                                    "strand", (AttributeError, zlib.error))


__all__ = ["Anchor", "BEFORE", "CATALOG_DIRECTORY", "CATALOG_FORMAT", "NOW", "PHYSICS_GENERATIONS",
           "RENDER_GENERATIONS", "STATES", "ChainStepKind", "ConstantSources",
           "LodSection", "MadeTexture", "PlacementSection", "RenderSection", "SkinningSection", "StrandCatalog",
           "StrandCatalogError", "WrapperFields", "catalog_file_name", "catalog_from_dict", "catalog_to_dict",
           "strand_catalog_for"]
