"""See docs/internals/strands-rendering.md."""
from __future__ import annotations

import dataclasses
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import numpy as np

from file_handlers.clsp.clsp_file import CollisionShape
from file_handlers.mmtr.engine_buffers import Camera
from file_handlers.mmtr.light_passes import LightPasses, Source
from file_handlers.mmtr.material_preview import VIEW_DIMENSIONS, PreviewMaterial
from file_handlers.mmtr.mesh_render import DEPTH_VIEWS
from file_handlers.mmtr.pretransform_passes import SystemPrograms
from file_handlers.mmtr.resident import UNORDERED_ACCESS
from file_handlers.rsz.scene_joints import times

from .binding_skin import POSITION_BYTES, BindingSkin, skin_matrices
from .chain_catalog import BEFORE, NOW, StrandCatalog
from .chain_constants import SetFrame
from .chain_passes import MASK, StrandPasses, frame_steps, kept_buffers, strand_buffers
from .chain_set import StrandSet
from .gpus import GpuStrandsPreset
from .hair_material import material_given, user_material
from .lod import F, LodRecord
from .opacity import StrandOpacity
from .render_constants import DrawFrame, RenderConstants
from .render_passes import DrawShape, DrawState, RenderPasses, composite_steps, draw_steps, tiles_of, view_steps
from .set_lod import BoundMesh, LodRun, LodView, SetLod
from .strands_file import StrandsFile

# Bytes per posed vertex, shading record, projection light records (8 x 120, none lit) and bounds ring slot;
# ``_VIEW_OUTPUTS`` is a view's outputs mask.
_VERTEX_BYTES, _SHADING_RECORD, _PROJECTION_LIGHTS, _VIEW_OUTPUTS, _VIEW, _SLOT = 16, 72, 960, 31, 0, 24
_ENVIRONMENT, _FRAME_COUNT = "EnvironmentInfo", "frameCount"
# Slots all LODs share: root UVs (the rest roots rewrite them) and the first loaded LOD's.
_ROOT_UVS, _FIRST = "strand.root_uvs", ".first"


@dataclass(frozen=True, slots=True, eq=False)
class Binding:
    table: bytes
    quantized: bool
    rest_mesh: bytes
    groups: tuple[tuple[int, int], ...]
    base: int
    weights: int
    uv: int
    remap: tuple[int, ...]
    inverse_binds: np.ndarray
    program: str
    positions: int | None


@dataclass(frozen=True, slots=True)
class SetSource:
    render: Mapping[str, object]
    dynamics: Mapping[str, object]
    strands: StrandsFile
    first_lod: int
    record: LodRecord | None
    binding: Binding | None
    master: str | None
    material: PreviewMaterial | None
    preset: Callable[[str], GpuStrandsPreset | None]
    load: Callable[[str], bytes | None]
    mask: tuple


@dataclass(frozen=True, slots=True)
class SetPlace:
    origin: np.ndarray
    transform: np.ndarray
    own_joint: Callable[[str], np.ndarray | None]
    anchor: tuple[np.ndarray, np.ndarray] | None
    colliders: Sequence[CollisionShape]
    bound: BoundMesh | None
    source: tuple[int, int] | None
    joints: np.ndarray | None
    wind: Sequence[float] | None = None
    turn: np.ndarray | None = None


@dataclass(frozen=True, slots=True)
class Stepped:
    """``simulated`` is 1 / ``DeltaT``."""
    rate: float
    simulated: float
    sub_steps: int | None

    @property
    def sub_step(self) -> float | None:
        """In seconds."""
        return None if not self.sub_steps else 1.0 / (self.simulated * self.sub_steps)


def view_projection(camera: Camera) -> np.ndarray:
    """Single precision, as RE9's view update makes it."""
    return times(np.asarray(camera.view_matrix(), F).T, np.asarray(camera.projection(), F))


@dataclass(slots=True)
class _Set:
    source: SetSource
    physics: StrandSet
    lod: SetLod
    constants: RenderConstants
    layouts: Mapping
    user: Mapping[str, bytes]
    given: dict[str, int]
    sections: dict[int, dict[str, int]]
    readback: tuple[int, int]
    added: bool = False
    level: int | None = None
    skin: BindingSkin | None = None
    runs: int = 0
    before: np.ndarray | None = None
    drawn: DrawState | None = None


class StrandChain:
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
        self._count, self._previous, self._drawing = 0, None, []
        self._opacity: StrandOpacity | None = None
        lit.add_start(self._run)
        for anchor, step in zip(catalog.render.anchors, composite_steps(catalog.render.writes_stencil), strict=True):
            lit.add((anchor.after, anchor.phase), lambda step=step: self._composite(step), anchor.inside)

    def add(self, source: SetSource) -> int:
        strands, scene = source.strands, self.device.scene
        first = strands.lods[source.first_lod]
        given = {f"render.vertices{side}": scene.buffer(bytes(_VERTEX_BYTES * first.vertex_count), UNORDERED_ACCESS)
                 for side in (NOW, BEFORE)}
        given["render.shading_info"] = scene.buffer(bytes(_SHADING_RECORD), UNORDERED_ACCESS)
        given[MASK] = self.device.read(VIEW_DIMENSIONS["texture2d"], (0, *source.mask))[1:]
        if source.binding is not None:
            rest = source.binding.rest_mesh
            given |= {"binding.table": scene.buffer(source.binding.table, 0),
                      "binding.rest_mesh": scene.buffer(rest + bytes(-len(rest) % 16), 0)}
        contracts = self.render.contracts(source.master)
        layouts = self.render.layouts(source.master)
        user = {} if source.material is None else user_material(
            source.material, [contract for _kind, contract in contracts], self.user_blocks)
        held = _Set(source, StrandSet(self.catalog.constants, self.layouts, strands,
                                      None if source.binding is None else source.binding.quantized),
                    SetLod(self.catalog.lod, source.render, source.dynamics, source.record,
                           [lod.segment_count if lod.index >= source.first_lod else None for lod in strands.lods],
                           (strands.bounds.min, strands.bounds.max)),
                    RenderConstants(self.catalog.render, {name: layout for name, layout in layouts.items()
                                                          if not self.lit.gives(name)}),
                    layouts, user, given | self._material(source, contracts), {},
                    tuple(scene.buffer(bytes(3 * _SLOT), 0) for _ in range(2)))
        self._sets.append(held)
        return len(self._sets) - 1

    def image(self, places: Sequence[SetPlace], rate: float) -> None:
        """``places`` is indexed by the set numbers ``add`` returns."""
        self._places, self._rate = places, rate

    @property
    def drawn_opacity(self) -> tuple[int, int] | None:
        """(view format, resource)."""
        return self._opacity.texture if self._drawing else None

    def _sections(self, held: _Set, level: int) -> dict[str, int]:
        if level not in held.sections:
            shared = next(iter(held.sections.values()), {})
            held.sections[level] = {
                slot: shared[slot] if slot in shared and (slot == _ROOT_UVS or _FIRST in slot)
                else self.device.scene.buffer(bytes(data), UNORDERED_ACCESS)
                for slot, data in strand_buffers(held.source.strands, level, held.source.first_lod).items()}
        return held.sections[level]

    def _engine(self, layouts: Mapping) -> dict[str, bytes]:
        out = {name: self.lit.engine_block(name, layout) for name, layout in layouts.items() if self.lit.gives(name)}
        if _ENVIRONMENT in layouts:
            block = bytearray(out.get(_ENVIRONMENT, bytes(layouts[_ENVIRONMENT].byte_size)))
            count = next(item for item in layouts[_ENVIRONMENT].variables if item.name == _FRAME_COUNT)
            struct.pack_into("<I", block, count.byte_offset, self._count & 0xffffffff)
            out[_ENVIRONMENT] = bytes(block)
        return out

    def _run(self) -> list[int]:
        odd, camera, size = bool(self._count & 1), self.lit.scene.camera, self.lit.scene.size
        now = view_projection(camera)
        view = LodView(now if self._previous is None else self._previous, size, _VIEW_OUTPUTS)
        self._previous = now
        self._scene_view(size)
        # The other parity's readback finished while the GPU was idle.
        read = self.device.flush([held.readback[not odd] for held in self._sets])
        physics, drawing = [], []
        self.stepped = None
        for owner, (held, place, data) in enumerate(zip(self._sets, self._places, read, strict=True)):
            run = self._lod(held, place, view, data)
            if run is not None:
                physics += self._physics(owner, held, place, run, odd)
                self._draw(owner, held, place, run, camera, size)
            if held.drawn is not None:
                drawing.append(owner)
        self._drawing = drawing
        steps = physics
        if drawing:
            before, after = view_steps(size)
            steps += self.render.steps(drawing[0], _VIEW, before, odd)
            for owner in drawing:
                steps += self.render.steps(owner, _VIEW, draw_steps(self._sets[owner].drawn, tiles_of(size),
                                                                     self.render.threads, self.catalog.render.lut), odd)
            steps += self.render.steps(drawing[0], _VIEW, after, odd)
            if self._opacity is None or self._opacity.size != size:
                if self._opacity is not None:
                    self._opacity.release()
                self._opacity = StrandOpacity(self.device, size)
            steps += self._opacity.steps(self.render.texture(_VIEW, "view.oit_resolved")[1])
        self._count += 1
        return steps

    def _lod(self, held: _Set, place: SetPlace, view: LodView, data: bytes) -> LodRun | None:
        if held.lod.culled(place.bound, [view]):
            held.runs = 0
            return None
        slot = held.physics.constants.words("boundingBoxOffset")[0]
        settled = held.runs >= self.catalog.constants.settle_frames
        opacity = held.source.render[self.catalog.render.arguments["opacity"]]
        return held.lod.run(place.origin, [view], struct.unpack_from("<6I", data, slot * _SLOT), place.bound,
                            held.physics.fresh, settled, opacity)

    def _physics(self, owner: int, held: _Set, place: SetPlace, run: LodRun, odd: bool) -> list[int]:
        source, binding = held.source, held.source.binding
        skinning, offsets = [], (0, 0)
        if binding is not None:
            if held.skin is None:
                mesh = held.given["binding.rest_mesh"]
                held.skin = BindingSkin(self.device, self.catalog.skinning, self.programs, binding.program, mesh,
                                        place.source if binding.positions is None else (mesh, binding.positions),
                                        binding.groups, binding.base, binding.weights, len(binding.remap))
            skinning = held.skin.steps(skin_matrices(place.joints, binding.inverse_binds, binding.remap,
                                                     np.asarray(place.origin, F)[3, :3]))
            offsets = (binding.uv, -POSITION_BYTES * binding.base)
        frame = SetFrame(place.origin, run.level, source.first_lod, run.segments, 1.0, self._rate, held.runs,
                         place.anchor, place.colliders, place.own_joint, offsets, (), False, place.wind,
                         place.turn)
        held.runs = min(held.runs, 2) + 1
        blocks, shape, state = held.physics.frame(source.dynamics, source.preset, frame)
        if self.stepped is None:
            constants = held.physics.constants
            delta = np.array(constants.words("DeltaT"), np.uint32).view(F)[0]
            self.stepped = Stepped(self._rate, float(1 / delta) if delta else 0.0, constants.words("subStep")[0])
        given = self._sections(held, run.level) | (
            {} if held.skin is None else {"binding.deformed_mesh": held.skin.positions})
        if not held.added:
            first = source.strands.lods[source.first_lod]
            self.physics.add(owner, kept_buffers(dataclasses.replace(shape, vertices=first.vertex_count)),
                             held.given | given)
            self.render.add(owner, DrawShape(first.segment_count, first.vertex_count), held.given | given | {
                "bounds": self.physics.slot(owner, "bounds", False),
                "render.projection_lights": self.device.buffer(bytes(_PROJECTION_LIGHTS))}, source.master)
            held.added = True
        elif held.level != run.level:
            self.physics.give(owner, given)
            self.render.give(owner, given)
        held.level = run.level
        dispatches = self.physics.frame(owner, frame_steps(shape, state), blocks | self._engine(self.layouts), odd)
        return [*skinning, *dispatches, self.device.copy(held.readback[odd], self.physics.slot(owner, "bounds", odd))]

    def _material(self, source: SetSource, contracts: Sequence) -> dict:
        if source.material is None:
            return {}
        out = {}
        for kind, contract in contracts:
            if kind.material:
                out |= material_given(self.device, kind.bindings, contract.resources, source.material,
                                      source.load)[0]
        return out

    def _draw(self, owner: int, held: _Set, place: SetPlace, run: LodRun, camera: Camera,
              size: tuple[int, int]) -> None:
        origin = np.asarray(place.origin, F)[3, :3]
        before, held.before = (origin if held.before is None else held.before), origin
        if run.views[0] is None:
            held.drawn = None
            return
        factor, drawn = run.views[0]
        inverse = np.linalg.inv(camera.view_matrix())
        source, render = held.source, self.catalog.render
        first = source.strands.lods[source.first_lod]
        frame = DrawFrame(factor, drawn, source.strands.lods[run.level].segment_count, run.box, place.transform,
                          place.own_joint, (origin, before), (inverse[:3, 3], inverse[:3, 2]), inverse[:3, 3],
                          render.camera, 1.0, tiles_of(size)[0], self.render.tiles,
                          _VERTEX_BYTES * first.vertex_count, True, 0, 0, 0.0)
        values = {name: source.render[field] for name, field in render.arguments.items()}
        state = DrawState.of(render, values, drawn, factor, held.drawn is None, self.bits32, source.master is not None)
        blocks = held.constants.frame(source.render, source.strands, frame) | self._engine(held.layouts) | held.user
        self.render.write(owner, blocks)
        held.drawn = state

    def _scene_view(self, size: tuple[int, int]) -> None:
        lit, render = self.lit, self.catalog.render
        slots = {slot for kind in render.steps.values()
                 for slot in (*kind.bindings.values(), *kind.targets, kind.depth) if slot and slot.startswith("scene.")}
        scene = dict.fromkeys(slots)
        for slot, (kind, value) in render.drawn_into.items():
            if kind == "depth":
                form = lit.scene.depth_format
                scene[slot] = (DEPTH_VIEWS[form][1], lit.scene.depth, form)
            else:
                form = lit.shape(Source(kind, value))[0]
                scene[slot] = (form, lit.contents(Source(kind, value), lit.end), form)
        self.render.view(_VIEW, size, scene)

    def _composite(self, step) -> list[int]:
        return self.render.steps(self._drawing[0], _VIEW, [step], bool((self._count - 1) & 1)) if self._drawing else []


__all__ = ["Binding", "SetPlace", "SetSource", "Stepped", "StrandChain", "view_projection"]
