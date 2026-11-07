"""See docs/internals/mesh-preview.md, Shallow water."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace

lazy import numpy as np
from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

lazy from file_handlers.mdf.mdf_file import material_shading_types
lazy from file_handlers.mmtr.draw_rules import DrawRules, drawing_rules
lazy from file_handlers.mmtr.light_passes import LightPasses
lazy from file_handlers.mmtr.material_preview import MaterialPreviewError
lazy from file_handlers.mmtr.pretransform_passes import SystemPrograms
lazy from file_handlers.mmtr.shallow_water import (ShallowWater, ShallowWaterCatalog, controller_writes,
                                                   density_map_position, partial_area, shallow_water_catalog_for)
lazy from utils.resource_file_utils import resource_path_with_version

lazy from .material_resolver import MdfSurfaceProfile, read_mdf, surface_profile
lazy from .material_shader_preview import preview_material

translate = QCoreApplication.translate


@dataclass(frozen=True, slots=True)
class SceneShallowWaterFluid:
    """``world`` is 4 x 4 of row vectors."""
    world: np.ndarray
    rotation: tuple[float, float, float, float] | None
    scale: tuple[float, float, float]
    resolution: tuple[int, int]
    drawn: bool


@dataclass(frozen=True, slots=True)
class SceneShallowWaterSource:
    """``world`` and ``point_world`` are 4 x 4 of row vectors."""
    key: str
    component: str
    world: np.ndarray
    scale: tuple[float, float, float]
    point_world: np.ndarray | None
    point_scale: tuple[float, float, float] | None
    point_missing: bool
    fields: Mapping[str, object]
    visible: bool
    resolve: Callable[[str], tuple[str, bytes] | None]
    controller: str | None = None
    fluid: SceneShallowWaterFluid | None = None
    fluid_children: int = 0


def drawn_shallow_water(source: SceneShallowWaterSource, catalog: ShallowWaterCatalog, rules: DrawRules
                        ) -> tuple[ShallowWater, MdfSurfaceProfile] | None:
    kind = catalog.kinds[source.component]
    fields = _controlled(source, catalog)
    for name, value in kind.required.items():
        if fields.get(name) != value:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "No capture shows the renderer drawing a shallow water component whose {field} is "
                                   "not {value}."), {"field": name, "value": str(value)})
    material = str(kind.field(fields, "material") or "").strip()
    if not material:
        return None
    if source.point_missing:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The shallow water's simulation point is an object of another scene."))
    width, height = kind.field(fields, "tiles")
    # set_TileCount truncates the file's floats.
    tiles = int(np.trunc(np.float32(width))), int(np.trunc(np.float32(height)))
    # The partial kind's SimulationGrid is one count (a square), the full kind's two.
    grid = kind.field(fields, "grid")
    grid = (int(grid), int(grid)) if isinstance(grid, (int, float)) else (int(grid[0]), int(grid[1]))
    if min(*tiles, *grid) < 1:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The shallow water has no tiles or simulation cells."))
    if tiles[0] * tiles[1] % catalog.culling.threads:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture shows the renderer culling {count} tiles, which its groups of {threads} do "
                               "not divide."), {"count": tiles[0] * tiles[1], "threads": catalog.culling.threads})
    mdf = read_mdf(source.resolve, material)
    if len(mdf.materials) <= catalog.drawn_material:
        return None
    drawn = mdf.materials[catalog.drawn_material]
    if material_shading_types(catalog.game).get(int(drawn.header.shader_type)) != catalog.shading_type:
        return None
    surface = rules.overridden(surface_profile(drawn, mdf.file_version), kind.component, fields)
    area = None
    drawn_by, scale = np.asarray(source.world, np.float32).reshape(4, 4), np.asarray(source.scale, np.float32)
    if kind.point is not None:
        if source.point_world is None:
            position, point_scale = kind.point
        else:
            position, point_scale = np.asarray(source.point_world, np.float32)[3, :3], source.point_scale
        area = partial_area(source.world, source.scale, position, point_scale, tiles)
        drawn_by, scale = area.drawn, area.scale
    alpha = str(kind.field(fields, "alpha") or "").strip() if "alpha" in kind.fields else ""
    segment = catalog.segments.get(int(kind.field(fields, "priority")), catalog.segment)
    return ShallowWater(source.key, kind, drawn_by, scale, area, tiles, grid, fields,
                        bool(kind.field(fields, "occlusion")), segment, alpha or catalog.alpha_texture), surface


def _controlled(source: SceneShallowWaterSource, catalog: ShallowWaterCatalog) -> Mapping[str, object]:
    controller = catalog.controllers.get(source.controller) if source.controller else None
    if controller is None or source.point_scale is None:
        return source.fields
    names, listed = controller.entry, source.fields.get(controller.entries, ())
    wanted = {entry.get(names["variable"]) for entry in listed
              if entry.get(names["material"]) == controller.material} & set(controller.writes)
    reads_density = any(isinstance(lane, tuple) and lane[0] == "density"
                        for variable in wanted for lane in controller.writes[variable])
    written = controller_writes(controller, _density(source, controller) if reads_density else None,
                                source.point_scale)
    entries = tuple(
        frozendict({**entry, names["vector"]: written[entry.get(names["variable"])]})
        if entry.get(names["material"]) == controller.material and entry.get(names["variable"]) in written else entry
        for entry in listed)
    return frozendict({**source.fields, controller.entries: entries})


def _density(source: SceneShallowWaterSource, controller) -> tuple[float, float, float] | None:
    if source.fluid_children > 1:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "Several children of the shallow water's simulation point hold a {type}; REasy does not "
                               "know which one its controller reads."), {"type": controller.fluid})
    fluid = source.fluid
    if fluid is None:
        return None
    if fluid.rotation is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The shallow water's {type} is placed turned, which REasy does not follow."),
            {"type": controller.fluid})
    # Never drawn, its density map stays where its constructor put it.
    if not fluid.drawn:
        return 0.0, 0.0, 0.0
    density = density_map_position(fluid.world, fluid.scale, fluid.rotation, fluid.resolution)
    if density is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The density map of the shallow water's {type} does not settle."),
            {"type": controller.fluid})
    return density


def gather_shallow_waters(sources: Sequence[SceneShallowWaterSource], profile, files: dict,
                          located: Callable[[str], object], gather_surface: Callable[[MdfSurfaceProfile, Callable], str]
                          ) -> tuple[tuple, str]:
    catalog = shallow_water_catalog_for(profile) if sources else None
    if catalog is None:
        return (), ""
    rules, found, refused = drawing_rules(profile), [], []
    for source in sources:
        if not source.visible:
            continue
        try:
            drawn = drawn_shallow_water(source, catalog, rules)
        except MaterialPreviewError as exc:
            refused.append(str(exc))
            continue
        if drawn is None:
            continue
        water, surface = drawn
        if surface.instance_context is None:
            refused.append(translate("SceneLitPreview", "No MDF material."))
            continue
        read = (catalog.culling.file, water.alpha)
        for path in read:
            if path not in files and (held := _shallow_file(source, catalog, path, located)):
                files[path] = held
        missing = next((path for path in read if path not in files), None)
        if missing is not None:
            refused.append(translate("SceneLitPreview", "The game's file {path} was not found.").format(path=missing))
            continue
        reason = gather_surface(surface, source.resolve)
        if reason:
            refused.append(reason)
            continue
        found.append(replace(water, mmtr_path=surface.mmtr_path, context=surface.instance_context,
                             material=preview_material(surface)))
    return tuple(found), "; ".join(dict.fromkeys(refused))


def _shallow_file(source: SceneShallowWaterSource, catalog: ShallowWaterCatalog, path: str,
                  located: Callable[[str], object]):
    if path.endswith(".sdf"):
        held = source.resolve(resource_path_with_version(path, "sdf", catalog.resource_version))
        return None if held is None else held[1]
    return located(path)


class ShallowWaterDraws:
    def __init__(self, lit: LightPasses, catalog: ShallowWaterCatalog, waters: Sequence[tuple[ShallowWater, object]],
                 programs: SystemPrograms, refused: Callable[[str], None]):
        number = lit.purpose_phase(catalog.phase, QT_TRANSLATE_NOOP(
            "MaterialPreview", "The light pass catalog has no {phase} phase to draw the shallow water after."))
        lit.check_bound(target for listed in catalog.targets.values() for target in listed)
        self.lit, self.catalog, self.number, self.programs = lit, catalog, number, programs
        self.waters = []
        for water, document in waters:
            try:
                lit.scene.shallow_water(document, water, self._targets(water), programs)
            except MaterialPreviewError as exc:
                refused(str(exc))
                continue
            self.waters.append((water, document))
        if self.waters:
            lit.add((lit.catalog.phases[number].after, number), self._frame)

    def _targets(self, water: ShallowWater) -> list[tuple[int, int]]:
        return self.lit.bound_targets(self.number, self.catalog.targets[water.occlusion])

    def _frame(self) -> list[int]:
        scene = self.lit.scene
        steps = [step for water, document in self.waters
                 for step in scene.shallow_water(document, water, self._targets(water), self.programs)]
        return [*scene.water_depth(), *steps]


__all__ = ["SceneShallowWaterFluid", "SceneShallowWaterSource", "ShallowWaterDraws", "drawn_shallow_water",
           "gather_shallow_waters"]
