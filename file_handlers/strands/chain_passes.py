"""See docs/internals/strands-chain.md, Physics passes."""
from __future__ import annotations

import struct
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.material_preview import (
    RANGE_CBV, RANGE_SAMPLER, RANGE_SRV, RANGE_UAV, SHADER_VISIBILITY, MaterialPreviewError,
)
from file_handlers.mmtr.pretransform import SystemProgram
from file_handlers.mmtr.pretransform_passes import SystemPrograms, dispatch_compute
from file_handlers.mmtr.resident import BUFFER, TEXTURE_2D, TEXTURE_3D, UNORDERED_ACCESS, Resident
from file_handlers.tex.dxgi import DXGI_FORMAT_R32_SINT, DXGI_FORMAT_R32_UINT

from .chain_catalog import BEFORE, NOW, StrandCatalog
from .strands_file import StrandsFile

# The bounds ring's three boxes of six words, minima at +1e8 and maxima at -1e8, order-preserving encoded.
_BOUNDS = (b"\x20\xbc\xbe\xcc" * 3 + b"\xdf\x43\x41\x33" * 3) * 3
# Bytes per element of each kept buffer.
_PER_NODE = {"node_properties": 16, "rest.position": 12, "rest.rotation": 16, "node_wetness": 4}
_PER_NODE_PAIRS = {"rest_data.position": 12, "rest_data.rotation": 16, "nodes.position": 12, "nodes.rotation": 16,
                   "velocities": 24}
_PER_ROOT = {"rest_roots.position": 12, "rest_roots.rotation": 16, "deformed_roots.position": 12,
             "deformed_roots.rotation": 16, "validation": 4}
_PER_SECONDARY_NODE = {"secondary.rest.position": 12, "secondary.rest.rotation": 16,
                       "secondary.posed_rest.position": 12, "secondary.posed_rest.rotation": 16,
                       "secondary.nodes.position": 12, "secondary.nodes.rotation": 16, "secondary.wetness": 4,
                       **{f"secondary.to_primary.{n}": 4 for n in range(3)}}
_VELOCITY_GRIDS = {"grid.velocity_x": DXGI_FORMAT_R32_SINT, "grid.velocity_y": DXGI_FORMAT_R32_SINT,
                   "grid.velocity_z": DXGI_FORMAT_R32_SINT, "grid.mass": DXGI_FORMAT_R32_UINT}
_DENSITY_GRIDS = {"grid.density": DXGI_FORMAT_R32_UINT}
TEXTURE_INPUT_TYPES = ("texture", "rw-typed")
# Bytes per grid cell.
_CELL = 4
MASK = "wetness_mask"
ROOT_CONSTANT = "RootConstant"


@dataclass(frozen=True, slots=True)
class Grid:
    form: int
    cells: tuple[int, int, int]


@dataclass(frozen=True, slots=True)
class SetShape:
    guides: int
    strand_size: int
    roots: int
    secondary: int
    vertices: int
    density_grid: tuple[int, int, int] | None
    velocity_grid: tuple[int, int, int] | None

    @classmethod
    def of(cls, constant: Callable[[str], Sequence[int]], solver: bool) -> SetShape:
        counts = ("strandCount", "strandSize", "renderStrandCount", "secondaryGuideCount", "renderStrandVertexCount")
        (guides,), (size,), (roots,), (secondary,), (vertices,) = map(constant, counts)
        if not solver:
            return cls(0, 0, roots, 0, vertices, None, None)
        density, velocity = (tuple(cells) if any(cells) else None
                             for cells in map(constant, ("gridDimension", "velocityGridDimension")))
        return cls(guides, size, roots, secondary, vertices, density, velocity)

    @property
    def nodes(self) -> int:
        return self.guides * self.strand_size

    @property
    def secondary_nodes(self) -> int:
        return self.secondary * self.strand_size if self.secondary > self.guides else 0

    @property
    def grids(self) -> str:
        return " ".join(name for name, grid in (("vp", self.density_grid), ("is", self.velocity_grid)) if grid)


@dataclass(frozen=True, slots=True)
class FrameState:
    solver: bool
    rods_kept: bool
    masses_kept: bool
    restart: bool
    bound: bool
    quantized: bool
    world: bool
    guided: bool
    guide_only: bool
    guided_rest: bool
    paused: bool
    segments: int


@dataclass(frozen=True, slots=True)
class Step:
    kind: str
    variant: str
    groups: tuple[int, int, int]
    constant: int | None = None


def side(slot: str, odd: bool) -> str:
    if odd and slot.endswith(NOW):
        return slot.removesuffix(NOW) + BEFORE
    if odd and slot.endswith(BEFORE):
        return slot.removesuffix(BEFORE) + NOW
    return slot


def dispatch_groups(count: int, threads: int = 64) -> tuple[int, int, int]:
    return -(-count // threads), 1, 1


def program_variant(*flags: tuple[str, bool]) -> str:
    return " ".join(name for name, held in flags if held)


def block_layouts(contracts: Iterable) -> dict:
    found, shapes = {}, {}
    for contract in contracts:
        for resource in contract.resources:
            if resource.register_class != "b" or resource.name == ROOT_CONSTANT:
                continue
            layout = contract.buffer(resource.name)
            shape = layout.byte_size, tuple((item.name, item.byte_offset, item.byte_size) for item in layout.variables)
            if shapes.setdefault(resource.name, shape) != shape:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The strands' programs lay the constant buffer {name} out differently."),
                    {"name": resource.name})
            found.setdefault(resource.name, layout)
    return found


def kept_buffers(shape: SetShape) -> dict[str, bytes | Grid]:
    kept: dict[str, bytes | Grid] = {}

    def keep(sizes: Mapping[str, int], count: int, sides: Sequence[str] = ("",)) -> None:
        for slot, size in sizes.items() if count else ():
            for end in sides:
                kept[slot + end] = bytes(size * count)

    keep(_PER_NODE, shape.nodes)
    keep(_PER_NODE_PAIRS, shape.nodes, (NOW, BEFORE))
    keep({"root_wetness": 4}, shape.guides)
    keep(_PER_ROOT, shape.roots)
    keep({"guided_rest": 16}, shape.vertices)
    keep(_PER_SECONDARY_NODE, shape.secondary_nodes)
    kept["bounds"] = _BOUNDS
    for grid, forms in ((shape.velocity_grid, _VELOCITY_GRIDS), (shape.density_grid, _DENSITY_GRIDS)):
        for slot, form in forms.items() if grid else ():
            for end in (NOW, BEFORE):
                kept[slot + end] = Grid(form, grid)
    return kept


def frame_steps(shape: SetShape, state: FrameState) -> list[Step]:
    nodes, vertices, bound = dispatch_groups(shape.nodes), dispatch_groups(shape.vertices), ("bound", state.bound)
    last = Step("bounds_copy", "", (1, 1, 1))
    if not state.solver:
        root = [Step("rest_root", program_variant(bound, ("world", True), ("quantized", state.quantized)),
                     dispatch_groups(shape.roots))] if state.bound else []
        return [*root, Step("transform", program_variant(bound), vertices, state.segments), last]
    steps = []
    start = not state.rods_kept or state.restart
    if start or not state.masses_kept:
        steps.append(Step("init", "" if start else "mass", nodes))
        if shape.secondary_nodes:
            steps.append(Step("init_secondary", "", dispatch_groups(shape.secondary_nodes)))
    grids = [grid for grid in (shape.density_grid, shape.velocity_grid) if grid]
    if grids and all(min(grid) for grid in grids):
        steps.append(Step("clear_grid", shape.grids, (max(-(-grid[0] // 8) for grid in grids),
                                                      max(-(-grid[1] // 8) for grid in grids),
                                                      max(grid[2] for grid in grids))))
    if state.bound:
        steps.append(Step("rest_root", program_variant(bound, ("world", state.world), ("quantized", state.quantized)),
                          dispatch_groups(shape.roots)))
    else:
        steps.append(Step("rest_root", "", dispatch_groups(max(state.segments, shape.guides))))
    steps.append(Step("rest_data", program_variant(bound, ("start", start)), nodes))
    if state.guided and state.segments:
        steps.append(Step("guided_rest", "guided" if state.guided_rest else "bound", vertices))
    steps.append(Step("pause", "", nodes) if state.paused else Step("update", shape.grids, nodes))
    if shape.secondary_nodes:
        steps.append(Step("guide_secondary", program_variant(bound), dispatch_groups(shape.secondary_nodes)))
    if state.segments:
        if not state.guided:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The strands are posed without guides, by programs no capture shows running."))
        steps.append(Step("guide_from_secondary" if shape.secondary_nodes else "guide",
                          program_variant(("guide_only", state.guide_only), ("world", state.world)), vertices, 1))
    return [*steps, last]


@dataclass(slots=True)
class _Set:
    # A pair slot holds its resources in the order an even frame binds them.
    held: dict[str, int]
    grids: dict[str, Grid]
    blocks: dict[str, int]
    made: dict[tuple[Step, bool], int]


class StrandPasses:
    def __init__(self, device: Resident, catalog: StrandCatalog, programs: SystemPrograms, samplers: Mapping):
        self.device, self.catalog, self.programs, self.samplers = device, catalog, programs, samplers
        self._sets: dict[int, _Set] = {}

    def add(self, owner: int, kept: Mapping[str, bytes | Grid], given: Mapping[str, int | tuple[int, int]]) -> None:
        """``kept`` must be sized for the set's first LOD."""
        scene, held, grids = self.device.scene, dict(given), {}
        for slot, start in kept.items():
            if isinstance(start, Grid):
                grids[slot] = start
                held[slot] = scene.texture(start.form, *start.cells, 1, TEXTURE_3D, UNORDERED_ACCESS, None)
            else:
                held[slot] = scene.buffer(start, UNORDERED_ACCESS)
        self._sets[owner] = _Set(held, grids, {}, {})

    def give(self, owner: int, given: Mapping[str, int | tuple[int, int]]) -> None:
        held = self._sets[owner]
        held.held |= given
        held.made.clear()

    def layouts(self) -> dict:
        return block_layouts(self.programs.get(self.catalog.target, SystemProgram(self.catalog.file, name))[1]
                             for kind in self.catalog.steps.values() for name in kind.programs.values() if name)

    def slot(self, owner: int, slot: str, odd: bool) -> int | tuple[int, int]:
        return self._sets[owner].held[side(slot, odd)]

    def _dispatch(self, owner: int, step: Step, odd: bool) -> int:
        held = self._sets[owner]
        kind, visibility, tables = self.catalog.steps[step.kind], SHADER_VISIBILITY["compute"], []
        name = self.catalog.program(step.kind, step.variant)
        shader, contract = self.programs.get(self.catalog.target, SystemProgram(self.catalog.file, name))
        for resource in contract.resources:
            place = (resource.bind_point, resource.register_space, resource.bind_count)
            if resource.register_class == "s":
                tables.append((RANGE_SAMPLER, *place, 0, visibility,
                               ((0, *self.samplers[resource.name].descriptor()),)))
            elif resource.register_class == "b":
                size = contract.buffer(resource.name).byte_size
                if resource.name == ROOT_CONSTANT:
                    bound = self.device.buffer(step.constant.to_bytes(4, "little") + bytes(size - 4))
                elif (bound := held.blocks.get(resource.name)) is None:
                    bound = held.blocks[resource.name] = self.device.scene.buffer(bytes(size), 0)
                tables.append((RANGE_CBV, *place, 0, visibility, ((0, bound),)))
            else:
                slot = kind.bindings[resource.name]
                found = self.slot(owner, slot, odd)
                if resource.input_type not in TEXTURE_INPUT_TYPES:
                    structured = "structured" in resource.input_type
                    stride = contract.structured_buffer(resource.name).byte_size if structured else 0
                    entry, dimension = (0, 0, stride, found), BUFFER
                elif slot == MASK:
                    entry, dimension = (0, *found), TEXTURE_2D
                else:
                    entry, dimension = (0, held.grids[side(slot, odd)].form, found), TEXTURE_3D
                tables.append((RANGE_SRV if resource.register_class == "t" else RANGE_UAV, *place, dimension,
                               visibility, (entry,)))
        return dispatch_compute(self.device.scene, shader, tables, step.groups, name)

    def clear(self, owner: int, slots: Sequence[str]) -> list[int]:
        held, out = self._sets[owner], []
        for slot in slots:
            grid = held.grids[slot]
            width, height, depth = grid.cells
            zeros = self.device.zeros(grid.form, grid.cells, UNORDERED_ACCESS, _CELL * width * height * depth)
            out.append(self.device.copy(held.held[slot], zeros))
        return out

    def frame(self, owner: int, steps: Sequence[Step], constants: Mapping[str, bytes], odd: bool) -> list[int]:
        held, scene = self._sets[owner], self.device.scene
        out = []
        for step in steps:
            if (step, odd) not in held.made:
                held.made[step, odd] = self._dispatch(owner, step, odd)
            out.append(held.made[step, odd])
        for name, bound in held.blocks.items():
            scene.write(bound, constants[name])
        return out


def strand_buffers(strands: StrandsFile, lod: int, first_lod: int) -> dict[str, bytes]:
    posed = strands.lods[lod]
    out = {"strand.root_uvs": b"".join(struct.pack("<2f", *uv) for uv in strands.root_uvs),
           "strand.segments": posed.segment_records, "strand.vertex_strands": posed.vertex_strands}
    for suffix, level in (("", posed), (".first", strands.lods[first_lod])):
        plane = len(level.closest_guide_data) // 6
        out |= {f"strand.vertices{suffix}": level.vertex_records,
                f"strand.offsets{suffix}": struct.pack(f"<{len(level.strand_offsets)}I", *level.strand_offsets),
                **{f"strand.closest_guides{suffix}.{k}": level.closest_guide_data[k * plane:(k + 1) * plane]
                   for k in range(6)}}
    return out


__all__ = ["MASK", "FrameState", "Grid", "SetShape", "Step", "StrandPasses", "block_layouts", "frame_steps",
           "kept_buffers", "side", "strand_buffers"]
