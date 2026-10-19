"""See docs/internals/strands-rendering.md, RE4's chain."""
from __future__ import annotations

import struct
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np

from file_handlers.mmtr.engine_buffers import Camera
from file_handlers.mmtr.light_passes import LightPasses, Source
from file_handlers.mmtr.material_preview import VIEW_DIMENSIONS
from file_handlers.mmtr.mesh_render import DEPTH_VIEWS
from file_handlers.mmtr.pretransform import SystemProgram
from file_handlers.mmtr.pretransform_passes import SystemPrograms
from file_handlers.mmtr.resident import RENDER_TARGET, UNORDERED_ACCESS

from .chain_catalog import BEFORE, NOW, StrandCatalog
from .chain_frame import SetPlace, SetSource, Stepped
from .chain_passes import StrandPasses
from .direct_constants import DirectConstants, DirectFrame, drawn_segments, injected_segments
from .direct_render import IBL_OPTIMIZED, SCATTERING, DirectState, direct_steps, ibl_count, ibl_tables
from .hair_material import material_given, user_material
from .lod import F, world_box
from .render_passes import DrawShape, RenderPasses, tiles_of
from .spring_constants import SpringFrame
from .spring_passes import GRIDS, spring_buffers, spring_kept, spring_steps
from .spring_set import SpringSet

# Byte sizes, except _VIEW (a view number); the projection light record is 8 x 120 bytes, none lit.
_VERTEX_BYTES, _PROJECTION_LIGHTS, _VIEW, _RING = 16, 960, 0, 96
_ENVIRONMENT, _FRAME_COUNT = "EnvironmentInfo", "frameCount"
_HAIR = "material_shading"
# The fine raster writes these as UAVs.
_WRITTEN = ("scene.lit", "scene.velocity")
_DRAWN = tuple("render.vertices" + side for side in (NOW, BEFORE))


def _level(strands, index: int):
    """RE4 files may hold only some LODs."""
    return next(level for level in strands.lods if level.index == index)


@dataclass(slots=True)
class _Set:
    source: SetSource
    physics: SpringSet
    constants: DirectConstants
    layouts: dict
    user: dict
    given: dict
    sections: dict
    readback: tuple[int, int]
    added: bool = False
    level: int | None = None
    lut: bool = True
    steps: list | None = None
    drawn: dict | None = None


class SpringChain:
    def __init__(self, lit: LightPasses, catalog: StrandCatalog, programs: SystemPrograms, material_target: int):
        self.lit, self.catalog, self.device, self.programs = lit, catalog, lit.device, programs
        states = lit.samplers.states
        self.physics = StrandPasses(self.device, catalog, programs, states)
        self.render = RenderPasses(self.device, catalog.render, programs, states, material_target)
        self.layouts, self.bits32 = self.physics.layouts(), not self.device.scene.atomics64()
        self.user_blocks = programs.profile.user_material_buffer_names
        self._sets: list[_Set] = []
        self._places: Sequence[SetPlace] = ()
        self._rate: float | None = None
        self.stepped: Stepped | None = None
        self._count, self._drawing = 0, []
        self._written: dict[str, tuple[int, int]] = {}
        self._size: tuple[int, int] | None = None
        lit.add_start(self._run)
        anchor, = catalog.render.anchors
        lit.add((anchor.after, anchor.phase), self._draw, anchor.inside)

    def add(self, source: SetSource) -> int:
        strands, scene = source.strands, self.device.scene
        first = _level(strands, source.first_lod)
        given = {f"render.vertices{side}": scene.buffer(bytes(_VERTEX_BYTES * first.vertex_count), UNORDERED_ACCESS)
                 for side in (NOW, BEFORE)}
        given["render.projection_lights"] = self.device.buffer(bytes(_PROJECTION_LIGHTS))
        dominant = source.render[self.catalog.render.arguments["ibl_count"]]
        given |= {slot: scene.buffer(data, 0) for slot, data in ibl_tables(ibl_count(int(dominant))).items()}
        if source.binding is not None:
            rest = source.binding.rest_mesh
            given |= {"binding.table": scene.buffer(source.binding.table, 0),
                      "binding.rest_mesh": scene.buffer(rest + bytes(-len(rest) % 16), 0)}
        contracts = self.render.contracts(source.master)
        layouts = self.render.layouts(source.master)
        user = {} if source.material is None else user_material(
            source.material, [contract for _kind, contract in contracts], self.user_blocks)
        material = {}
        for kind, contract in contracts:
            if kind.material and source.material is not None:
                material |= material_given(self.device, kind.bindings, contract.resources, source.material,
                                           source.load)[0]
        held = _Set(source, SpringSet(self.catalog.constants, self.layouts, strands, source.binding is not None),
                    DirectConstants(self.catalog.render, {name: layout for name, layout in layouts.items()
                                                          if not self.lit.gives(name)}),
                    layouts, user, given | material, {}, tuple(scene.buffer(bytes(_RING), 0) for _ in range(2)))
        self._sets.append(held)
        return len(self._sets) - 1

    def image(self, places: Sequence[SetPlace], rate: float) -> None:
        """The time step is 1 / ``rate``."""
        self._places, self._rate = places, rate

    @property
    def drawn_opacity(self) -> tuple[int, int] | None:
        """The direct generation draws into the lit image itself."""
        return None

    def _sections(self, held: _Set, level: int) -> dict[str, int]:
        if level not in held.sections:
            held.sections[level] = {slot: self.device.scene.buffer(bytes(data), UNORDERED_ACCESS)
                                    for slot, data in spring_buffers(held.source.strands, level).items()}
        return held.sections[level]

    def _engine(self, layouts) -> dict[str, bytes]:
        out = {name: self.lit.engine_block(name, layout) for name, layout in layouts.items() if self.lit.gives(name)}
        if _ENVIRONMENT in layouts:
            block = bytearray(out.get(_ENVIRONMENT, bytes(layouts[_ENVIRONMENT].byte_size)))
            count = next(item for item in layouts[_ENVIRONMENT].variables if item.name == _FRAME_COUNT)
            struct.pack_into("<I", block, count.byte_offset, self._count & 0xffffffff)
            out[_ENVIRONMENT] = bytes(block)
        return out

    def _run(self) -> list[int]:
        odd, size = bool(self._count & 1), self.lit.scene.size
        self._scene_view(size)
        read = self.device.flush([held.readback[not odd] for held in self._sets])
        steps, drawing = [], []
        self.stepped = None
        for owner, (held, place, data) in enumerate(zip(self._sets, self._places, read, strict=True)):
            ring = None if self._count == 0 else struct.unpack("<24I", data)
            steps += self._physics(owner, held, place, ring, odd)
            if held.steps:
                drawing.append(owner)
        self._drawing = drawing
        self._count += 1
        return steps

    def _physics(self, owner: int, held: _Set, place: SetPlace, ring, odd: bool) -> list[int]:
        source = held.source
        binding = source.binding
        offsets = (0, 0) if binding is None or place.source is None else (binding.uv, place.source[1])
        frame = SpringFrame(place.origin, 0, 1.0 / self._rate, place.own_joint, offsets)
        if self.stepped is None:
            self.stepped = Stepped(self._rate, self._rate, None)
        arguments, scripted = self.catalog.constants.arguments, self.catalog.render.scripted
        fields = {**source.dynamics, **{arguments[name]: value for name, value in scripted.items() if name in arguments}}
        run = held.physics.frame(fields, frame, ring)
        level = next(index for index, lod in enumerate(source.strands.lods) if lod.index == run.level)
        given = self._sections(held, level) | (
            {} if binding is None or place.source is None else {"binding.deformed_mesh": place.source[0]})
        out = []
        if not held.added:
            first = _level(source.strands, source.first_lod)
            self.physics.add(owner, spring_kept(run.shape, first.vertex_count), held.given | given)
            self.render.add(owner, DrawShape(first.segment_count, first.vertex_count), held.given | given,
                            source.master)
            held.added = True
        elif held.level != level:
            self.physics.give(owner, given)
            self.render.give(owner, given)
        held.level = level
        drawn = {slot: held.given[slot] for slot in _DRAWN} if run.state.simulated else dict.fromkeys(
            _DRAWN, self._sections(held, level)["strand.vertices"])
        if drawn != held.drawn:
            self.render.give(owner, drawn)
            held.drawn = drawn
        if run.state.start:
            out += self.physics.clear(owner, GRIDS)
        out += self.physics.frame(owner, spring_steps(run.shape, run.state), run.blocks | self._engine(self.layouts),
                                  odd)
        out.append(self.device.copy(held.readback[odd], self.physics.slot(owner, "bounds", odd)))
        self._update(owner, held, place, run, level)
        return out

    def _update(self, owner: int, held: _Set, place: SetPlace, run, level: int) -> None:
        source, render = held.source, self.catalog.render
        values = {name: ({**render.unserialized, **source.render})[field] for name, field in render.arguments.items()}
        opacity = F(values["opacity"])
        if not opacity > 0:
            held.steps = None
            return
        factor = F(source.render[self.catalog.lod.fields["lod_factor"]])
        segments = source.strands.lods[level].segment_count
        drawn = drawn_segments(factor, segments)
        camera: Camera = self.lit.scene.camera
        inverse = np.linalg.inv(camera.view_matrix())
        size = self.lit.scene.size
        simulated = run.state.simulated
        box = run.box if simulated else world_box((source.strands.bounds.min, source.strands.bounds.max),
                                                  np.asarray(place.transform, F).reshape(4, 4))
        frame = DirectFrame(factor, segments, drawn, box, place.transform, place.own_joint, True,
                            (inverse[:3, 3], inverse[:3, 2]), tiles_of(size)[0], 0)
        blocks = held.constants.frame(source.render, source.strands, frame) | self._engine(held.layouts) | held.user
        self.render.write(owner, blocks)
        scripted, scattering = render.scripted, int(values["scattering"])
        state = DirectState(
            drawn=drawn, injected=injected_segments(segments, drawn, values["injection_rate"]),
            pretransformed=simulated, mix=bool(scripted.get("mix", values["mix"])),
            countup=bool(values["countup"]), bits32=self.bits32, blend=bool(opacity < 1),
            scattering=SCATTERING.get(scattering), interpolated=bool(values["interpolated_injection"]),
            lut=held.lut, shrink_shadow=False, projection_lights=False, material_shading=source.master is not None,
            ibl=None if scattering == IBL_OPTIMIZED else ibl_count(int(values["ibl_count"])))
        held.lut = False
        held.steps = direct_steps(state, self.render.tiles, lambda kind, variant: self._threads(held, kind, variant))

    def _threads(self, held: _Set, kind: str, variant: str) -> tuple[int, int, int]:
        if kind != _HAIR:
            return self.render.threads(kind, variant)
        _shader, contract = self.programs.get(self.render.material_target, SystemProgram(
            held.source.master, self.catalog.render.program(kind, variant)))
        return tuple(contract.thread_group_size)

    def _scene_view(self, size: tuple[int, int]) -> None:
        lit, render = self.lit, self.catalog.render
        slots = {slot for kind in render.steps.values()
                 for slot in (*kind.bindings.values(), *kind.targets, kind.depth) if slot and slot.startswith("scene.")}
        scene = dict.fromkeys(slots)
        if size != self._size:
            self.device.release(own for _scene, own in self._written.values())
            self._written, self._size = {}, size
        for slot, (kind, value) in render.drawn_into.items():
            if kind == "depth":
                form = lit.scene.depth_format
                scene[slot] = (DEPTH_VIEWS[form][1], lit.scene.depth, form)
                continue
            source = Source(kind, value)
            form = lit.shape(source)[0]
            resource = lit.contents(source, lit.end)
            if slot in _WRITTEN:
                if slot not in self._written:
                    own = self.device.scene.texture(form, *size, 1, 1, VIEW_DIMENSIONS["texture2d"],
                                                    UNORDERED_ACCESS | RENDER_TARGET, None)
                    self._written[slot] = (resource, own)
                resource = self._written[slot][1]
            scene[slot] = (form, resource, form)
        self.render.view(_VIEW, size, scene)

    def _draw(self) -> list[int]:
        if not self._drawing:
            return []
        odd = bool((self._count - 1) & 1)
        steps = [self.device.copy(own, scene) for scene, own in self._written.values()]
        for owner in self._drawing:
            steps += self.render.steps(owner, _VIEW, self._sets[owner].steps, odd)
        steps += [self.device.copy(scene, own) for scene, own in self._written.values()]
        return steps


__all__ = ["SpringChain"]
