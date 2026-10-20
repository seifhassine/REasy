"""See docs/internals/strands-rendering.md."""
from __future__ import annotations

import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mmtr.material_preview import (
    RANGE_CBV, RANGE_SAMPLER, RANGE_SRV, RANGE_UAV, _UAV_DIMENSIONS, VIEW_DIMENSIONS, SHADER_VISIBILITY,
    MaterialPreviewError,
)
lazy from file_handlers.mmtr.mesh_draw import program_states
lazy from file_handlers.mmtr.pretransform import SystemProgram
lazy from file_handlers.mmtr.pretransform_passes import SystemPrograms
lazy from file_handlers.mmtr.resident import DEPTH_STENCIL, RENDER_TARGET, UNORDERED_ACCESS, Resident
lazy from file_handlers.sdf.contracts import shader_contract_for_program
from file_handlers.tex.dxgi import (
    DXGI_FORMAT_D32_FLOAT, DXGI_FORMAT_R11G11B10_FLOAT, DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_FORMAT_R16G16B16A16_SNORM,
    DXGI_FORMAT_R16G16_FLOAT, DXGI_FORMAT_R32G32_UINT, DXGI_FORMAT_R32_FLOAT, DXGI_FORMAT_R32_TYPELESS,
    DXGI_FORMAT_R32_UINT,
)

lazy from .chain_catalog import ChainStepKind, RenderSection
lazy from .chain_passes import ROOT_CONSTANT, TEXTURE_INPUT_TYPES, program_variant, block_layouts, side
lazy from .direct_render import LAYOUT as DIRECT_LAYOUT

# Pixels per tile side, and per-tile bytes of the accumulation, offsets and work queue.
TILE, TILE_BYTES = 16, 96
# A view's textures by slot: (shader view format, target view format, layers).
_VIEW_TEXTURES = {"view.oit": (DXGI_FORMAT_R32G32_UINT, None, 5),
                  "view.oit_resolved": (DXGI_FORMAT_R32G32_UINT, None, 1),
                  "view.strand_image": (DXGI_FORMAT_R11G11B10_FLOAT, DXGI_FORMAT_R11G11B10_FLOAT, 1),
                  "view.strand_velocity": (DXGI_FORMAT_R16G16B16A16_SNORM, DXGI_FORMAT_R16G16B16A16_SNORM, 1),
                  "view.strand_depth": (DXGI_FORMAT_R32_FLOAT, DXGI_FORMAT_D32_FLOAT, 1)}
# A texture viewed through several formats needs a typeless resource.
_TYPELESS = {DXGI_FORMAT_R32_FLOAT: DXGI_FORMAT_R32_TYPELESS}
# A set's textures by slot: (format, size, mips).
SET_TEXTURES = {"ds.volume": (DXGI_FORMAT_R32_UINT, (128, 128, 128), 1),
                "ds.volume2": (DXGI_FORMAT_R16G16_FLOAT, (128, 128, 128), 6),
                "ds.lut": (DXGI_FORMAT_R16G16B16A16_FLOAT, (64, 64, 16), 1),
                "indirect_light.image": (DXGI_FORMAT_R11G11B10_FLOAT, (16, 16), 1)}
# A set's buffer bytes by slot.
_SET_BUFFERS = {"shading.bounds": 48, "shading.dispatch": 16, "shading.arguments": 16, "ds.arguments": 144,
                "ds.info": 60, "indirect_light.arguments": 16}
# Bytes of the arguments the renderer writes for a set.
_ARGUMENTS = {"view.arguments": 64, "view.distribute_arguments": 16}
# ``MultipleScattering``'s dual scattering value.
_DUAL_SCATTERING = 1
_AUTOMATIC_LAYERS = ((0.9, 8), (0.5, 6), (0.1, 4))
# Bytes of indirect dispatch and draw arguments.
_DISPATCH_ARGUMENTS, _DRAW_ARGUMENTS = 12, 16
_LEVELS = {"#level": 0, "#level-1": -1}


def work_sizes(segments: int, vertices: int) -> dict[str, int]:
    """Bytes by slot; visibility keeps whole 32-bit words only."""
    return {"work.solid_hair": segments * 15 * 4, "work.queue": segments * 3 * 256, "work.shade": vertices * 4,
            "work.visibility": ((vertices + 31) * 4 >> 5) & ~3, "work.counter": 16}


@dataclass(frozen=True, slots=True)
class RenderLayout:
    tile_bytes: int
    queue_bytes: int
    view_textures: Mapping[str, tuple[int, int | None, int]]
    set_textures: Mapping[str, tuple[int, tuple[int, ...], int]]
    set_buffers: Mapping[str, int]
    arguments: Mapping[str, int]
    work_sizes: Callable[[int, int], dict[str, int]]


# RE9's and PRAGMATA's.
LAYOUT = RenderLayout(TILE_BYTES, TILE_BYTES, _VIEW_TEXTURES, SET_TEXTURES, _SET_BUFFERS, _ARGUMENTS, work_sizes)


def tiles_of(size: tuple[int, int]) -> tuple[int, int]:
    return (size[0] + TILE - 1) // TILE, (size[1] + TILE - 1) // TILE


@dataclass(frozen=True, slots=True)
class DrawShape:
    segments: int
    vertices: int


@dataclass(frozen=True, slots=True)
class DrawState:
    drawn: int
    pretransformed: bool
    mix: bool
    countup: bool
    subdivision: bool
    hardware: bool
    transparency: int
    bits32: bool
    stencil: bool
    stencil_value: int
    motion: bool
    blend: bool
    root_blend: bool
    indirect_light: bool
    scattering: bool
    interpolated: bool
    raster_depth: bool
    volume_copy: bool
    lut: bool
    shrink_shadow: bool
    projection_lights: bool
    shaded: bool
    material_shading: bool

    @classmethod
    def of(cls, render: RenderSection, values: Mapping[str, object], drawn: int, factor: float, first: bool,
           bits32: bool, material: bool) -> DrawState:
        """As the render update picks it; ``values`` by rule name."""
        scattering = values["scattering"] == _DUAL_SCATTERING
        layers = int(render.scripted["transparency"]) or next(
            (count for least, count in _AUTOMATIC_LAYERS if factor > least), 2)
        return cls(drawn, True, bool(values["mix"]), bool(values["countup"]), bool(render.scripted["subdivision"]),
                   bool(render.scripted["hardware"]), layers, bits32, render.writes_stencil,
                   int(values["stencil_value"]), bool(render.camera[render.options["motion"]]),
                   float(values["opacity"]) < 1, float(values["root_blend"]) > 0, True, scattering,
                   bool(values["interpolated_injection"]), bool(values["raster_depth"]), render.version > 1,
                   scattering and first, False, False, True, material)


@dataclass(frozen=True, slots=True)
class Step:
    """``indirect`` is (arguments slot, byte offset); ``constant`` is the root constant."""
    kind: str
    variant: str
    groups: tuple[int, int, int] | None = None
    indirect: tuple[str, int] | None = None
    constant: int | None = None
    level: int | None = None


@dataclass(frozen=True, slots=True)
class Fill:
    """The renderer's ``ClearUnorderedAccessViewUint``."""
    slot: str
    word: int


@dataclass(frozen=True, slots=True)
class Write:
    slot: str
    data: bytes


@dataclass(frozen=True, slots=True)
class Clear:
    slot: str


def _given(slots: Mapping[str, tuple | int | None]) -> dict[str, _Texture | int | None]:
    return {slot: _Texture(value[1], value[0], *value[2:]) if isinstance(value, tuple) else value
            for slot, value in slots.items()}


def _over(count: int, size: int) -> int:
    return -(-count // size)


def view_steps(size: tuple[int, int]) -> tuple[list, list]:
    """``coarse_z`` finds each tile's farthest depth."""
    groups = (*tiles_of(size), 1)
    before = [Step("reset_oit", "", groups), Step("coarse_z", "", groups), Clear("view.strand_depth"),
              Clear("view.strand_image"), Clear("view.strand_velocity")]
    return before, [Step("resolve", "")]


def composite_steps(stencil: bool) -> list[Step]:
    return [Step("combine", ""), Step("image_copy", ""), Step("resolve_upscale", "", constant=0),
            Step("depth_upscale", "", constant=0), *([Step("stencil_upscale", "")] if stencil else []),
            Step("resolve_upscale", "", constant=1)]


def draw_steps(state: DrawState, tiles: tuple[int, int], threads: Callable[[str, str], tuple[int, int, int]],
               lut: tuple[int, int, int] | None) -> list:
    """``threads`` gives a program's group size; ``lut`` the lookup table dispatch groups, or None for a thread per
    texel."""
    pretransformed = ("pretransformed", state.pretransformed)
    subdivision, hardware = ("subdivision", state.subdivision), ("hardware", state.hardware)
    coarse = program_variant(pretransformed, ("mix", state.mix), ("countup", state.countup), subdivision, hardware)
    groups = _over(state.drawn, threads("coarse", coarse)[0])
    steps = [Fill("view.accumulate", 0 if state.countup else 256), Fill("view.offset", 0), Fill("work.counter", 1),
             Write("view.arguments", struct.pack("<16I", 0, 1, 1, 1, 6, 0, 0, 0, groups, 0, 1, 0, 0, 0, 0, 0))]
    if state.countup:
        x, y, _ = threads("distributor", "countup")
        steps.append(Write("view.distribute_arguments",
                           struct.pack("<4I", _over(tiles[0], x), _over(tiles[1], y), 0, 0)))
    steps.append(Step("coarse", coarse, (groups, 1, 1), constant=state.drawn))
    if state.countup:
        x, y, _ = threads("allocation", "")
        steps += [Step("allocation", "", (_over(tiles[0], x), _over(tiles[1], y), 1)),
                  Step("coarse_alloc", program_variant(pretransformed, ("mix", state.mix), subdivision, hardware),
                       indirect=("view.arguments", 0x20), constant=state.drawn)]
    steps += [Fill("shading.bounds", 0), Step("shading_init", "", (1, 1, 1)),
              Step("shading_bounds", program_variant(pretransformed), indirect=("shading.dispatch", 0)),
              Step("shading_info", "", (1, 1, 1))]
    if state.indirect_light:
        steps += [Step("indirect_light_arguments", "", (1, 1, 1)),
                  Step("indirect_light", "", indirect=("indirect_light.arguments", 0))]
    if state.scattering:
        steps += [Step("ds_arguments", "", (1, 1, 1)), Step("ds_info", "", (1, 1, 1)),
                  Step("ds_clear", "", indirect=("ds.arguments", 0)),
                  Step("ds_raster", program_variant(pretransformed, ("interpolated", state.interpolated)),
                       indirect=("ds.arguments", 0x80))]
        if state.raster_depth:
            steps.append(Step("ds_raster_depth", "", indirect=("ds.arguments", 0x60)))
        if state.volume_copy:
            steps.append(Step("ds_copy", "", indirect=("ds.arguments", 0)))
        steps += [Step("ds_mip", "", indirect=("ds.arguments", 16 * level), constant=level, level=level)
                  for level in range(1, SET_TEXTURES["ds.volume2"][2])]
        if state.lut and lut is not None:
            steps.append(Step("ds_lut", "", lut, constant=0))
        elif state.lut:
            size = SET_TEXTURES["ds.lut"][1]
            steps.append(Step("ds_lut", "", tuple(map(_over, size, threads("ds_lut", "")))))
    steps.append(Step("shading_arguments", "", (1, 1, 1)))
    if not state.shaded:
        return steps
    steps.append(Step("material_shading" if state.material_shading else "shading", program_variant(
        pretransformed, ("shrink_shadow", state.shrink_shadow), ("projection_lights", state.projection_lights),
        ("scattering", state.scattering)), indirect=("shading.arguments", 0)))
    if state.countup:
        steps.append(Step("distributor", "countup", indirect=("view.distribute_arguments", 0)))
    else:
        x, y, _ = threads("distributor", "")
        steps.append(Step("distributor", "", (_over(tiles[0], x), _over(tiles[1], y), 1)))
    if state.mix:
        steps.append(Step("fill", program_variant(pretransformed, ("blend", state.blend),
                                                   ("root_blend", state.root_blend)),
                          indirect=("view.arguments", 0x10)))
    layers = (f"oit{state.transparency}", state.transparency in (2, 4, 6, 8))
    steps.append(Step("fine", program_variant(pretransformed, ("32bit", state.bits32), ("countup", state.countup),
                                       subdivision, layers, ("stencil", state.stencil), ("motion", state.motion)),
                      indirect=("view.arguments", 0), constant=state.stencil_value))
    return steps


@dataclass(frozen=True, slots=True)
class _Texture:
    """``form`` is the shader view format; ``target`` the target format, None if never a target."""
    resource: int
    form: int
    target: int | None = None


@dataclass(slots=True)
class _View:
    size: tuple[int, int]
    textures: dict[str, _Texture]
    # None binds a null view.
    scene: dict[str, _Texture | int | None]
    heap: tuple[tuple, ...]


@dataclass(slots=True)
class _Set:
    master: str | None
    # The posed vertex pair is held as an even frame binds it.
    held: dict[str, int | _Texture]
    blocks: dict[str, int]
    values: dict[str, bytes]
    written: dict[str, tuple[int, bytes]]


class RenderPasses:
    def __init__(self, device: Resident, render: RenderSection, programs: SystemPrograms, samplers: Mapping,
                 material_target: int):
        self.device, self.render, self.programs, self.samplers = device, render, programs, samplers
        self.material_target = material_target
        if render.generation == "direct":
            layout = DIRECT_LAYOUT
        else:
            layout = LAYOUT
        self.layout: RenderLayout = layout
        self._views: dict[int, _View] = {}
        self._sets: dict[int, _Set] = {}
        self._shared: dict[str, int | _Texture] = {}
        self._tiles, self._work = (0, 0), DrawShape(0, 0)
        self._sizes: dict[int, int] = {}
        self._made: dict[tuple, tuple[int, ...]] = {}
        self._clears: dict[int, int] = {}
        self._aside = self._buffer(_DRAW_ARGUMENTS)

    @property
    def tiles(self) -> tuple[int, int]:
        return self._tiles

    def texture(self, number: int, slot: str) -> tuple[int, int]:
        found = self._views[number].textures[slot]
        return found.form, found.resource

    def _release(self, resources) -> None:
        """Callers drop the made steps using them."""
        gone = set(resources)
        self._clears = {resource: step for resource, step in self._clears.items() if resource not in gone}
        self._sizes = {resource: size for resource, size in self._sizes.items() if resource not in gone}
        self.device.release(gone)

    def _buffer(self, size: int) -> int:
        made = self.device.scene.buffer(bytes(size), UNORDERED_ACCESS)
        self._sizes[made] = size
        return made

    def _texture(self, form: int, size: Sequence[int], mips: int, flags: int, layers: int = 1) -> int:
        width, height, depth = (*size, 1)[:3]
        dimension = "texture3d" if depth > 1 else "texture2d-array" if layers > 1 else "texture2d"
        return self.device.scene.texture(_TYPELESS.get(form, form), width, height, max(depth, layers), mips,
                                         VIEW_DIMENSIONS[dimension], flags, None)

    def view(self, number: int, size: tuple[int, int], scene: Mapping[str, tuple[int, int] | int | None],
             heap: Mapping[int, tuple[int, int, str]] | None = None) -> None:
        held = _given(scene)
        entries = tuple((RANGE_SRV, VIEW_DIMENSIONS[dimension], (index, form, resource))
                        for index, (form, resource, dimension) in sorted((heap or {}).items()))
        old = self._views.get(number)
        if old is not None and old.size == size and old.scene == held and old.heap == entries:
            return
        self._made = {key: made for key, made in self._made.items() if key[1] != number}
        if old is not None and old.size == size:
            textures = old.textures
        else:
            if old is not None:
                self._release(texture.resource for texture in old.textures.values())
            textures = {
                slot: _Texture(self._texture(form, size, 1, UNORDERED_ACCESS if target is None else
                                             DEPTH_STENCIL if target == DXGI_FORMAT_D32_FLOAT else RENDER_TARGET,
                                             layers), form, target)
                for slot, (form, target, layers) in self.layout.view_textures.items()}
        self._views[number] = _View(size, textures, held, entries)
        tiles = tuple(map(max, self._tiles, tiles_of(size)))
        if tiles != self._tiles:
            self._tiles = tiles
            count = tiles[0] * tiles[1]
            layout = self.layout
            self._release(
                held if isinstance(held, int) else held.resource for slot, held in self._shared.items()
                if slot in ("view.accumulate", "view.offset", "view.work_queue", "view.coarse_z", *layout.arguments))
            self._shared |= {slot: self._buffer(count * layout.tile_bytes) for slot in ("view.accumulate", "view.offset")}
            self._shared["view.work_queue"] = self._buffer(count * layout.queue_bytes)
            self._shared |= {slot: self._buffer(bytes_) for slot, bytes_ in layout.arguments.items()}
            self._shared["view.coarse_z"] = _Texture(self._texture(DXGI_FORMAT_R32_UINT, tiles, 1, UNORDERED_ACCESS),
                                                     DXGI_FORMAT_R32_UINT)
            self._made.clear()

    def add(self, owner: int, shape: DrawShape, given: Mapping[str, tuple[int, int] | int],
            master: str | None = None) -> None:
        """``given`` holds the slots the set doesn't own; ``master`` is its material's master path."""
        layout = self.layout
        held: dict[str, int | _Texture] = {slot: self._buffer(size) for slot, size in layout.set_buffers.items()}
        held |= {slot: _Texture(self._texture(form, size, mips, UNORDERED_ACCESS), form)
                 for slot, (form, size, mips) in layout.set_textures.items()}
        self._sets[owner] = _Set(master, held | _given(given), {}, {}, {})
        grown = DrawShape(max(self._work.segments, shape.segments), max(self._work.vertices, shape.vertices))
        if grown != self._work:
            self._work = grown
            self._shared |= {slot: self._buffer(size)
                             for slot, size in layout.work_sizes(grown.segments, grown.vertices).items()}
            self._made.clear()

    def give(self, owner: int, given: Mapping[str, tuple[int, int] | int]) -> None:
        """``given`` holds another LOD's sections."""
        self._sets[owner].held |= _given(given)
        self._made = {key: made for key, made in self._made.items() if key[0] != owner}

    def contracts(self, master: str | None) -> list[tuple[ChainStepKind, object]]:
        out = []
        for kind in self.render.steps.values():
            target, file = (self.material_target, master) if kind.material else (self.render.target, self.render.file)
            for name in kind.programs.values() if file is not None else ():
                program = None if name is None else self.programs.program(target, SystemProgram(file, name))
                out += [] if program is None else [
                    (kind, shader_contract_for_program(self.programs.profile.sdf_profile, program, shader.role))
                    for shader in program.shaders if shader.present]
        return out

    def layouts(self, master: str | None) -> dict:
        return block_layouts(contract for _kind, contract in self.contracts(master))

    def threads(self, kind: str, variant: str) -> tuple[int, int, int]:
        _shader, contract = self.programs.get(self.render.target, SystemProgram(self.render.file,
                                                                                self.render.program(kind, variant)))
        return tuple(contract.thread_group_size)

    def _held(self, owner: int, number: int, slot: str, odd: bool) -> int | _Texture | None:
        name = side(slot.split("#")[0], odd)
        view = self._views[number]
        for place in (view.textures, self._shared, self._sets[owner].held, view.scene):
            if name in place:
                return place[name]
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The strands' programs read {slot}, which the scene does not give."), {"slot": name})

    def _tables(self, owner: int, number: int, step: Step, contracts: Mapping, odd: bool) -> list:
        bindings, blocks, tables = self.render.steps[step.kind].bindings, self._sets[owner].blocks, []
        for stage, contract in contracts.items():
            visibility = SHADER_VISIBILITY[stage]
            for resource in contract.resources:
                place = (resource.bind_point, resource.register_space, resource.bind_count)
                if resource.register_class == "s":
                    tables.append((RANGE_SAMPLER, *place, 0, visibility,
                                   ((0, *self.samplers[resource.name].descriptor()),)))
                    continue
                if resource.register_class == "b":
                    size = contract.buffer(resource.name).byte_size
                    if resource.name == ROOT_CONSTANT:
                        bound = self.device.buffer(step.constant.to_bytes(4, "little") + bytes(size - 4))
                    elif (bound := blocks.get(resource.name)) is None:
                        bound = blocks[resource.name] = self.device.scene.buffer(
                            self._sets[owner].values.get(resource.name, bytes(size)), 0)
                    tables.append((RANGE_CBV, *place, 0, visibility, ((0, bound),)))
                    continue
                slot = bindings[resource.name]
                found, written = self._held(owner, number, slot, odd), resource.register_class == "u"
                if resource.input_type in TEXTURE_INPUT_TYPES:
                    dimension = (_UAV_DIMENSIONS if written else VIEW_DIMENSIONS)[resource.dimension]
                    mip = () if "#" not in slot else (step.level + _LEVELS[slot[slot.index("#"):]],)
                    entries = () if found is None else ((0, found.form, found.resource, *mip),)
                else:
                    structured = "structured" in resource.input_type
                    stride = contract.structured_buffer(resource.name).byte_size if structured else 0
                    dimension, entries = VIEW_DIMENSIONS["buffer"], () if found is None else ((0, 0, stride, found),)
                tables.append((RANGE_UAV if written else RANGE_SRV, *place, dimension, visibility, entries))
        return tables

    def _step(self, owner: int, number: int, step: Step, odd: bool) -> tuple[int, ...]:
        key = (owner, number, step, odd)
        if key in self._made:
            return self._made[key]
        name, scene = self.render.program(step.kind, step.variant), self.device.scene
        if self.render.steps[step.kind].material:
            target, file = self.material_target, self._sets[owner].master
            if file is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The strands' hair shading needs their master material, and they have none."))
        else:
            target, file = self.render.target, self.render.file
        program = self.programs.program(target, SystemProgram(file, name))
        if program is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The strands' shader file {path} has no program {name}."),
                {"path": file, "name": name})
        roles = [shader.role for shader in program.shaders if shader.present]
        contracts = {role: shader_contract_for_program(self.programs.profile.sdf_profile, program, role)
                     for role in roles}
        job = {"tables": self._tables(owner, number, step, contracts, odd), "heap": list(self._views[number].heap)}
        before = ()
        if step.indirect is not None:
            bindings = self.render.steps[step.kind].bindings
            arguments = self._held(owner, number, step.indirect[0], odd)
            job["indirect"] = (arguments, step.indirect[1])
            if any(resource.register_class == "u" and bindings[resource.name] == step.indirect[0]
                   for contract in contracts.values() for resource in contract.resources):
                # A step writing its own arguments reads them at its start, so they are copied aside first.
                size = _DISPATCH_ARGUMENTS if roles == ["compute"] else _DRAW_ARGUMENTS
                before = (scene.copy_bytes(self._aside, 0, arguments, step.indirect[1], size),)
                job["indirect"] = (self._aside, 0)
        elif roles == ["compute"]:
            job["groups"] = step.groups
        else:
            # View draws: one full-screen triangle.
            job["draw"] = (3, 1, 0, 0, 0)
        try:
            if roles == ["compute"]:
                made = scene.dispatch(job | {"compute_shader": bytes(program.shader("compute").payload)})
            else:
                kind, (width, height) = self.render.steps[step.kind], self._views[number].size
                targets = [self._held(owner, number, slot, odd) for slot in kind.targets]
                depth = self._held(owner, number, kind.depth, odd)
                made = scene.draw(job | {
                    "vertex_shader": bytes(program.shader("vertex").payload),
                    "pixel_shader": bytes(program.shader("pixel").payload), "vertex_buffers": [], "elements": [],
                    "indices": None, "size": (width, height),
                    "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0), "scissor": (0, 0, width, height),
                    "targets": [(held.target, held.resource) for held in targets],
                    "depth": (depth.target, depth.resource),
                    # Only the stencil-writing draw reads the reference; builds without one never run that draw, so 0 is
                    # safe.
                    **program_states(program, len(targets), self.render.stencil_reference or 0)})
        except RuntimeError as exc:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "Direct3D could not run the strands' program {name}: {reason}"),
                {"name": name, "reason": str(exc)}) from exc
        self._made[key] = (*before, made)
        return self._made[key]

    def steps(self, owner: int, number: int, listed: Sequence, odd: bool) -> list[int]:
        """``odd`` picks the posed vertex side. ``Write`` bytes apply from the next run."""
        out = []
        for item in listed:
            if isinstance(item, Step):
                out += self._step(owner, number, item, odd)
                continue
            found = self._held(owner, number, item.slot, odd)
            if isinstance(item, Clear):
                if found.resource not in self._clears:
                    self._clears[found.resource] = self.device.scene.clear(found.target, found.resource)
                out.append(self._clears[found.resource])
            elif isinstance(item, Fill):
                words = struct.pack("<I", item.word) * (self._sizes[found] // 4)
                out.append(self.device.copy(found, self.device.buffer(words)))
            else:
                written = self._sets[owner].written
                source, data = written.get(item.slot) or (self.device.scene.buffer(item.data, 0), item.data)
                if data != item.data:
                    self.device.scene.write(source, item.data)
                written[item.slot] = (source, item.data)
                out.append(self.device.copy(found, source))
        return out

    def write(self, owner: int, constants: Mapping[str, bytes]) -> None:
        """``constants`` must hold every block the programs declare."""
        held = self._sets[owner]
        held.values.update(constants)
        for name, bound in held.blocks.items():
            self.device.scene.write(bound, constants[name])


__all__ = ["Clear", "DrawShape", "DrawState", "Fill", "LAYOUT", "RenderLayout", "RenderPasses", "SET_TEXTURES", "Step",
           "TILE", "TILE_BYTES", "Write", "composite_steps", "draw_steps", "tiles_of", "view_steps", "work_sizes"]
