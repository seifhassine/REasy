"""See docs/internals/mesh-preview.md, Primitive shapes."""
from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial

lazy import numpy as np
from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.draw_rules import drawing_rules
lazy from file_handlers.mmtr.engine_buffers import WORLD_BUFFER, Placement, engine_buffer
lazy from file_handlers.mmtr.material_preview import RANGE_CBV, SHADER_VISIBILITY, MaterialPreviewError
lazy from file_handlers.mmtr.mesh_draw import input_elements, program_states
lazy from file_handlers.mmtr.pretransform import SystemProgram
lazy from file_handlers.mmtr.pretransform_passes import SystemPrograms
lazy from file_handlers.mmtr.primitives import (
    VERTEX_STRIDE, colour, material_constants, primitive_catalog_for, transparent_key, world_constants)
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R16_UINT
lazy from utils.resource_file_utils import resource_path_with_version


@dataclass(frozen=True, slots=True)
class ScenePrimitiveSource:
    """``world`` is 3 x 4, as a placement's."""
    key: str
    world: np.ndarray
    fields: Mapping[str, object]
    visible: bool


@dataclass(frozen=True, slots=True)
class SceneLitPrimitive:
    key: str
    world: np.ndarray
    shape: str
    fields: Mapping[str, object]
    # Drawn in the transparent layer: its colour's alpha is below the catalog's opaque alpha.
    transparent: bool = False


translate = QCoreApplication.translate


def gather_primitives(sources: Sequence[ScenePrimitiveSource], profile, files: dict[str, bytes],
                      resolve: Callable[[str], tuple[str, bytes] | None]) -> tuple[tuple[SceneLitPrimitive, ...], str]:
    catalog = primitive_catalog_for(profile)
    if catalog is None or not sources:
        return (), ""
    found = resolve(resource_path_with_version(catalog.file, "sdf", catalog.resource_version))
    if found is None:
        return (), translate("SceneLitPreview", "The game's file {path} was not found.").format(path=catalog.file)
    files[catalog.file] = found[1]
    drawn, refused = [], Counter()
    for source in sources:
        if not source.visible:
            continue
        fields = source.fields
        shape = catalog.shapes.get(int(fields[catalog.fields["shape"]]))
        if fields[catalog.fields["interleave"]]:
            refused[translate("SceneLitPreview", "geometry of separate streams")] += 1
        elif shape is None:
            refused[translate("SceneLitPreview", "a shape no capture shows")] += 1
        else:
            drawn.append(SceneLitPrimitive(source.key, np.asarray(source.world, np.float64), shape, fields,
                                           colour(fields[catalog.fields["color"]])[3] < catalog.opaque_alpha))
    return tuple(drawn), "; ".join(
        translate("SceneLitPreview", "{count} with {reason}").format(count=count, reason=reason)
        for reason, count in refused.items())


class PrimitiveDraws:
    def __init__(self, scene, primitives: Sequence[SceneLitPrimitive], programs: SystemPrograms):
        catalog = primitive_catalog_for(programs.profile)
        self.scene, self.catalog = scene, catalog
        self.layers: dict[bool, tuple[tuple[SceneLitPrimitive, ...], object, dict]] = {}
        for transparent, name in ((False, catalog.program), (True, catalog.transparent_program)):
            drawn = tuple(item for item in primitives if item.transparent == transparent)
            if not drawn:
                continue
            self.layers[transparent] = drawn, *programs.graphics(catalog.target, SystemProgram(catalog.file, name))
        self.decals = drawing_rules(programs.profile).receives_decals((catalog.component, None), False)
        self._held: dict[bool, tuple[tuple, list[list[int]]]] = {}
        if False in self.layers:
            scene.add_gbuffer(catalog.segment, self._gbuffer_steps)
        if True in self.layers:
            scene.add_transparent(partial(self._steps, True), self._ordered)

    def _gbuffer_steps(self) -> list[int]:
        scene = self.scene
        return self._steps(False, [(form, scene.targets[target])
                                   for target, form in sorted(scene.target_formats.items())])

    def _steps(self, transparent: bool, targets: list[tuple[int, int]]) -> list[int]:
        scene = self.scene
        key = (tuple(scene.size), scene.depth, tuple(targets))
        held = self._held.get(transparent)
        if held is None or held[0] != key:
            held = self._held[transparent] = key, self._made(transparent, targets)
        return [step for steps in held[1] for step in steps]

    def _ordered(self) -> tuple[list[int], list[int]]:
        held = self._held.get(True)
        if held is None:
            return [], []
        made, view = held[1], self.scene.camera.view
        eye = -view[:, :3].T @ view[:, 3]
        keys = [transparent_key(primitive.world[:, 3], eye, self.catalog.key_scale)
                for primitive in self.layers[True][0]]
        return ([step for steps in made for step in steps],
                [step for number in sorted(range(len(made)), key=keys.__getitem__) for step in made[number]])

    def _made(self, transparent: bool, targets: list[tuple[int, int]]) -> list[list[int]]:
        scene, catalog = self.scene, self.catalog
        drawn, program, contracts = self.layers[transparent]
        elements = input_elements(program)
        shapes, made = {}, []
        for primitive in drawn:
            if primitive.shape not in shapes:
                vertices, indices = catalog.geometry(primitive.shape)
                shapes[primitive.shape] = (scene.device.buffer(vertices, 1), scene.device.buffer(indices, 1),
                                           len(indices) // 2)
            vertices, indices, count = shapes[primitive.shape]
            made.append(scene.drawn_steps(program, {
                "vertex_shader": bytes(program.shader("vertex").payload),
                "pixel_shader": bytes(program.shader("pixel").payload),
                "vertex_buffers": [(vertices, VERTEX_STRIDE)], "elements": elements, "indices": indices,
                "index_format": DXGI_FORMAT_R16_UINT, "draw": (count, 1, 0, 0, 0),
                "tables": self._tables(primitive, contracts), "heap": [],
                **program_states(program, len(targets), catalog.stencil_reference)}, targets))
        return made

    def _tables(self, primitive: SceneLitPrimitive, contracts: Mapping) -> list:
        scene, catalog, device = self.scene, self.catalog, self.scene.device
        placement = Placement(primitive.world)
        tables = []
        for stage, contract in contracts.items():
            for resource in contract.resources:
                if resource.register_class != "b":
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The primitive program reads {resource}, which REasy does not give."),
                        {"resource": resource.name})
                layout = contract.buffer(resource.name)
                if scene.follows(resource.name):
                    held = device.following(
                        ("primitive", resource.name, layout.byte_size, layout.variables),
                        lambda contract=contract, resource=resource: engine_buffer(
                            contract, resource, scene.camera, placement, scene.frame, scene.captured,
                            clock=scene.clock))
                elif resource.name == catalog.buffer:
                    held = device.buffer(material_constants(layout, catalog, primitive.fields), 1)
                elif resource.name == WORLD_BUFFER:
                    held = device.buffer(world_constants(layout, catalog, primitive.fields, placement), 1)
                else:
                    held = device.buffer(engine_buffer(contract, resource, scene.camera, placement, scene.frame,
                                                       scene.captured, self.decals), 1)
                tables.append((RANGE_CBV, resource.bind_point, resource.register_space, resource.bind_count, 0,
                               SHADER_VISIBILITY[stage], ((0, held),)))
        return tables


__all__ = ["PrimitiveDraws", "SceneLitPrimitive", "ScenePrimitiveSource", "gather_primitives"]
