"""See docs/internals/mesh-preview.md, Water meshes."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import partial

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import catalog_file_name, catalog_reader

lazy from .engine_buffers import laid_out
lazy from .light_passes import added_targets
lazy from .material_preview import MaterialPreviewError
lazy from .mesh_render import DEPTH_VIEWS, WATER_PURPOSE, MeshObject, PartError, _Draw, _part_geometry, _unbound
lazy from .pass_choice import PassChoiceError, route_core, water_program
lazy from .pretransform import ROUTE
lazy from .resident import DEPTH_STENCIL
lazy from .surface_meaning import GBUFFER_PURPOSE

CATALOG_FORMAT = 2
CATALOG_DIRECTORY = "resources/data/water_meshes"


class WaterMeshCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WaterMeshCatalog:
    game: str
    resource_version: int
    component: str
    fields: Mapping[str, str]
    lod: int
    group: int
    material: int
    programs: Mapping[bool, str]
    buffer: str
    constants: Mapping[str, str]
    phase: str
    segment: int
    targets: Mapping[bool, tuple[tuple[str, int], ...]]
    depth_read: str
    blend_factor: tuple[float, float, float, float]
    controller: str | None = None

    def program_name(self, route: str, occlusion: bool) -> str:
        return self.programs[occlusion].format(core=route_core(route))

    @property
    def read(self) -> frozenset[str]:
        return frozenset((self.fields["priority"], self.fields["occlusion"], *self.constants.values()))

    def priority(self, fields: Mapping[str, object]) -> int:
        return int(fields[self.fields["priority"]])

    def occlusion(self, fields: Mapping[str, object]) -> bool:
        return bool(fields[self.fields["occlusion"]])


def catalog_from_dict(data: Mapping) -> WaterMeshCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise WaterMeshCatalogError(f"water mesh catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    component, drawn, programs, constants = data["component"], data["drawn"], data["programs"], data["constants"]
    fields = {str(name): str(value) for name, value in component.items() if name != "type"}
    if set(fields) != {"mesh", "material", "priority", "occlusion"}:
        raise WaterMeshCatalogError("the component's fields are not named for every meaning REasy reads")
    targets = added_targets(data["targets"], WaterMeshCatalogError)
    factor = tuple(float(value) for value in data["blend_factor"])
    if len(factor) != 4:
        raise WaterMeshCatalogError("the blend factor is not four values")
    # The scene lists a renderable's LOD 0 (``ScnRenderableMesh.drawn_group``).
    if int(drawn["lod"]) != 0:
        raise WaterMeshCatalogError(f"the renderer draws LOD {drawn['lod']}, which the scene does not list")
    return WaterMeshCatalog(
        str(data["game"]), int(data["resource_version"]), str(component["type"]), fields, int(drawn["lod"]),
        int(drawn["group"]), int(drawn["material"]),
        {False: str(programs["plain"]), True: str(programs["occlusion"])}, str(constants["buffer"]),
        {str(name): str(value) for name, value in constants["fields"].items()}, str(data["phase"]),
        int(data["segment"]),
        targets,
        str(data["depth_read"]), factor, None if data.get("controller") is None else str(data["controller"]))


water_mesh_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, WaterMeshCatalogError, "water mesh",
                                        (AttributeError,))


def water_constants(layout, catalog: WaterMeshCatalog, fields: Mapping[str, object]) -> bytes:
    return bytes(laid_out(layout, {name: np.array([float(fields[source])])
                                    for name, source in catalog.constants.items()}, True))


class WaterMeshDraws:
    def __init__(self, lit, catalog: WaterMeshCatalog):
        number = lit.purpose_phase(catalog.phase, QT_TRANSLATE_NOOP(
            "MaterialPreview", "The light pass catalog has no {phase} phase to draw the water meshes after."))
        lit.check_bound(target for listed in catalog.targets.values() for target in listed)
        self.lit, self.catalog, self.number = lit, catalog, number
        lit.add((lit.catalog.phases[number].after, number), self._frame)

    def _frame(self) -> list[int]:
        return self.lit.scene.water({occlusion: self.lit.bound_targets(self.number, listed)
                                     for occlusion, listed in self.catalog.targets.items()})


class MeshSceneWater:
    def _is_water(self, item: MeshObject) -> bool:
        return self._water is not None and item.decal_receive[0] == self._water.component

    def _water_draws(self, owner: int, item: MeshObject, geometry: dict, refusals: list) -> list[_Draw]:
        draws = []
        occlusion = self._water.occlusion(item.draw_state)
        destination = None if item.deformation is None else item.deformation.destination
        for part in item.parts:
            route = part.route or item.route
            read_from = destination if route == ROUTE else None
            try:
                program = part.document.sdf.targets[self.shader_target].programs[
                    water_program(part.document, self.shader_target, self._water, route, occlusion)]
                key = id(part), id(program), id(read_from), id(item.engine)
                if key not in self._built:
                    self._built[key] = _part_geometry(part, program, read_from, item.engine)
                geometry[id(part), id(program)] = self._built[key]
            except (MaterialPreviewError, PassChoiceError) as exc:
                refusals.append((part, exc))
                continue
            draws.append(_Draw(owner, part, WATER_PURPOSE, program, None))
        return draws

    def water_depth(self) -> list[int]:
        if self._water_copied:
            return []
        self._water_copied = True
        return [self.device.copy(self._water_depth_texture(), self.depth)]

    def _water_depth_texture(self) -> int:
        if self._water_depth is None:
            self._water_depth = self.device.texture(self.depth_resource_format, self.size, DEPTH_STENCIL)
        return self._water_depth

    def water(self, targets: Mapping[bool, Sequence[tuple[int, int]]]) -> list[int]:
        key = len(self._draws), tuple(tuple(targets[occlusion]) for occlusion in (False, True))
        if self._water_listing is not None and self._water_listing[0] == key:
            return [*self.water_depth(), *self._water_listing[1]] if self._water_listing[1] else []
        drawn = sorted((item for item in self._draws if item.purpose == WATER_PURPOSE and self._drawable((item.part,))),
                       key=lambda item: self._water.priority(self.objects[item.owner].draw_state))
        steps = []
        if drawn:
            self._water_depth_texture()
        for item in drawn:
            onto = list(targets[self._water.occlusion(self.objects[item.owner].draw_state)])
            with self._refusing():
                if (error := _unbound(item.part.document, item.program, len(onto))) is not None:
                    raise PartError([(item.part, error)])
                steps += self._steps(item, item.program, partial(self._water_job, item, len(onto)), onto)
        self._water_listing = key, steps
        return [*self.water_depth(), *steps] if steps else []

    def _water_job(self, item: _Draw, targets: int) -> dict:
        return self._job(item, item.program, targets, self._water_read) | {"blend_factor": self._water.blend_factor}

    def _water_texture(self, contract, resource) -> tuple | None:
        if resource.name != self._water.depth_read:
            return self._engine_texture(GBUFFER_PURPOSE)(contract, resource)
        return self._depth_copy_entry(resource)

    def _depth_copy_entry(self, resource) -> tuple:
        _resource, as_float, as_uint = DEPTH_VIEWS[self.depth_format]
        form = {"float": as_float, "uint": as_uint}.get(resource.return_type)
        if form is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The water draw reads the depth as {kind}."), {"kind": resource.return_type})
        return 0, form, self._water_depth_texture()


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "MeshSceneWater", "WaterMeshCatalog", "WaterMeshCatalogError",
           "WaterMeshDraws", "catalog_file_name", "catalog_from_dict", "water_constants", "water_mesh_catalog_for"]
