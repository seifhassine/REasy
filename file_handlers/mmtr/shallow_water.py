"""See docs/internals/mesh-preview.md, Shallow water."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

lazy import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.build_catalog import NamedFields, catalog_file_name, catalog_reader
lazy from file_handlers.engine_math import normalized
lazy from file_handlers.sdf.models import program_names
lazy from file_handlers.tex.dxgi import DXGI_FORMAT_R32_UINT

lazy from .engine_buffers import PLACEMENT_BUFFERS, Placement, engine_buffer, laid_out
lazy from .frame_constants import frame_constants_for
lazy from .light_passes import added_targets
lazy from .material_preview import (
    RANGE_CBV, RANGE_SAMPLER, RANGE_SRV, RANGE_UAV, SHADER_VISIBILITY, VIEW_DIMENSIONS, _UAV_DIMENSIONS,
    MaterialPreviewError, _heap_fields_kept, _texture_entry, missing_texture, shader_bindings,
)
lazy from .mesh_draw import program_states
lazy from .mesh_render import _not_drawn, _unbound
lazy from .pretransform import SystemProgram
lazy from .pretransform_passes import SystemPrograms
lazy from .resident import BUFFER, UNORDERED_ACCESS
lazy from .surface_meaning import GBUFFER_PURPOSE

CATALOG_FORMAT = 4
CATALOG_DIRECTORY = "resources/data/shallow_water"
_COMMON = frozenset({"material", "tiles", "grid", "tessellation", "attenuation", "priority", "occlusion",
                     "colour_blend", "normal_blend", "emission_blend"})
_SURFACE = frozenset({"drawn", "tessellation", "tessellation_inverse", "tiles", "tiles_inverse", "colour_blend",
                      "normal_blend", "tile_size", "emission_blend", "axis_x", "attenuation", "axis_z", "camera"})
_FIELD_CONSTANTS = frozenset({"fresnel", "refraction", "colour", "projection_colour"})
_MEANINGS = {"partial": _COMMON | {"point"}, "full": _COMMON | _FIELD_CONSTANTS | {"alpha"}}
DRAWS = {"partial": ("surface", "cheap_tile"), "full": ("surface",)}
_BUFFERS = {"partial": {"surface": _SURFACE,
                        "partial": frozenset({"world", "uv_offset", "uv_scale", "uv_diff", "uv_scale_inverse"})},
            "full": {"surface": _SURFACE | _FIELD_CONSTANTS}}
_LANE_SOURCES = ("density", "point scale")
_SETTLE_FRAMES = 16


class ShallowWaterCatalogError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConstantBuffer:
    buffer: str
    fields: Mapping[str, str]
    fixed: Mapping[str, tuple[float, ...]]
    unwritten: frozenset[str]

    @property
    def filled(self) -> frozenset[str]:
        return frozenset((*self.fields.values(), *self.fixed, *self.unwritten))


@dataclass(frozen=True, slots=True)
class CullingPass:
    file: str
    target: int
    program: str
    threads: int
    indices: str
    arguments: str
    initial_arguments: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CheapTile:
    elements: tuple[tuple[str, int, int, int], ...]
    stride: int
    indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class ShallowWaterKind(NamedFields):
    name: str
    component: str
    fields: Mapping[str, str]
    required: Mapping[str, object]
    programs: Mapping[str, Mapping[bool, str]]
    buffers: Mapping[str, ConstantBuffer]
    point: tuple[tuple[float, ...], tuple[float, ...]] | None = None
    cheap_tile: CheapTile | None = None

    @property
    def read(self) -> frozenset[str]:
        return frozenset((*self.fields.values(), *self.required))

    @property
    def draws(self) -> tuple[str, ...]:
        return DRAWS[self.name]


@dataclass(frozen=True, slots=True)
class ShallowWaterController:
    component: str
    water: str
    when: str
    entries: str
    entry: Mapping[str, str]
    material: str
    fluid: str
    resolution: str
    writes: Mapping[str, tuple[float | tuple[str, int], ...]]


@dataclass(frozen=True, slots=True)
class ShallowWaterCatalog:
    game: str
    resource_version: int
    shading_type: str
    drawn_material: int
    phase: str
    segments: Mapping[int, int]
    segment: int
    targets: Mapping[bool, tuple[tuple[str, int], ...]]
    depth_read: str
    blend_factor: tuple[float, float, float, float]
    simulation: Mapping[str, tuple[str, int, int]]
    alpha_read: str
    alpha_texture: str
    culling: CullingPass
    kinds: Mapping[str, ShallowWaterKind]
    controllers: Mapping[str, ShallowWaterController]


def catalog_from_dict(data: Mapping) -> ShallowWaterCatalog:
    if data.get("format") != CATALOG_FORMAT:
        raise ShallowWaterCatalogError(f"shallow water catalog format {data.get('format')!r} is not {CATALOG_FORMAT}")
    targets = added_targets(data["targets"], ShallowWaterCatalogError)
    factor = tuple(float(value) for value in data["blend_factor"])
    if len(factor) != 4:
        raise ShallowWaterCatalogError("the blend factor is not four values")
    simulation = {str(name): (str(item["read"]), int(item["format"]), int(item["texel_bytes"]))
                  for name, item in data["simulation"].items()}
    if set(simulation) != {"height", "velocity"}:
        raise ShallowWaterCatalogError("the simulation's textures are not named for height and velocity")
    culling = data["culling"]
    initial = tuple(int(word) for word in culling["initial_arguments"])
    if len(initial) != 5 or int(culling["threads"]) < 1:
        raise ShallowWaterCatalogError("the culling's arguments are not Direct3D's five indexed draw words, or it "
                                       "runs no threads")
    kinds = {}
    for name, item in data["components"].items():
        kind = _kind(str(name), item)
        if kind.component in kinds:
            raise ShallowWaterCatalogError(f"two kinds name the component type {kind.component}")
        kinds[kind.component] = kind
    controllers = {str(name): _controller(str(name), item, kinds) for name, item in data["controllers"].items()}
    return ShallowWaterCatalog(
        str(data["game"]), int(data["resource_version"]), str(data["shading_type"]), int(data["drawn_material"]),
        str(data["phase"]), {int(priority): int(segment) for priority, segment in data["segments"].items()},
        int(data["segment"]),
        targets, str(data["depth_read"]), factor,
        simulation, str(data["alpha"]["read"]), str(data["alpha"]["texture"]),
        CullingPass(str(culling["file"]), int(culling["target"]), str(culling["program"]), int(culling["threads"]),
                    str(culling["indices"]), str(culling["arguments"]), initial),
        kinds, controllers)


def _controller(name: str, data: Mapping, kinds: Mapping[str, ShallowWaterKind]) -> ShallowWaterController:
    water = str(data["water"])
    if water not in kinds:
        raise ShallowWaterCatalogError(f"{name} writes a shallow water type the catalog does not draw")
    entry = {str(meaning): str(value) for meaning, value in data["entry"].items()}
    if set(entry) != {"material", "variable", "vector"}:
        raise ShallowWaterCatalogError(f"{name}'s entry fields are not named for its material, variable and vector")
    writes = {}
    for variable, lanes in data["writes"].items():
        read = tuple(float(lane) if isinstance(lane, (int, float)) else (str(lane[0]), int(lane[1]))
                     for lane in lanes)
        if len(read) != 4 or any(isinstance(lane, tuple) and (lane[0] not in _LANE_SOURCES or not 0 <= lane[1] < 3)
                                 for lane in read):
            raise ShallowWaterCatalogError(f"{name} writes {variable} as other than four constants or source axes")
        writes[str(variable)] = read
    return ShallowWaterController(name, water, str(data["when"]), str(data["list"]), entry, str(data["material"]),
                                  str(data["fluid"]["type"]), str(data["fluid"]["resolution"]), writes)


def _kind(name: str, data: Mapping) -> ShallowWaterKind:
    if name not in _MEANINGS:
        raise ShallowWaterCatalogError(f"REasy draws no shallow water of kind {name!r}")
    fields = {str(meaning): str(value) for meaning, value in data["fields"].items()}
    if set(fields) != _MEANINGS[name]:
        raise ShallowWaterCatalogError(f"the {name} kind's fields are not named for every meaning REasy reads")
    programs = {str(draw): {False: str(names["plain"]), True: str(names["occlusion"])}
                for draw, names in data["programs"].items()}
    if tuple(programs) != DRAWS[name]:
        raise ShallowWaterCatalogError(f"the {name} kind's programs are not named for {', '.join(DRAWS[name])}")
    constants = data["constants"]
    if set(constants) != set(_BUFFERS[name]):
        raise ShallowWaterCatalogError(f"the {name} kind's buffers are not {', '.join(_BUFFERS[name])}")
    buffers = {buffer: _constant_buffer(constants[buffer], meanings) for buffer, meanings in _BUFFERS[name].items()}
    point = tile = None
    if name == "partial":
        point = tuple(tuple(float(value) for value in data["point"][part]) for part in ("position", "scale"))
        if any(len(values) != 3 for values in point):
            raise ShallowWaterCatalogError("the point's default position or scale is not three values")
        indices = tuple(int(index) for index in data["cheap_tile"]["indices"])
        if len(indices) % 3 or not all(0 <= index < 16 for index in indices):
            raise ShallowWaterCatalogError("the cheap tile's indices are not triangles over its 16 vertices")
        tile = CheapTile(tuple((str(semantic), int(index), int(form), int(offset))
                               for semantic, index, form, offset in data["cheap_tile"]["elements"]),
                         int(data["cheap_tile"]["stride"]), indices)
    return ShallowWaterKind(name, str(data["type"]), fields, {str(field_name): value for field_name, value
                                                              in data["required"].items()},
                            programs, buffers, point, tile)


def _constant_buffer(data: Mapping, meanings: frozenset[str]) -> ConstantBuffer:
    fields = {str(name): str(value) for name, value in data["fields"].items()}
    if set(fields) != meanings:
        raise ShallowWaterCatalogError(f"{data['buffer']}'s fields are not named for every meaning REasy writes")
    fixed = {str(name): tuple(float(value) for value in values) for name, values in data["fixed"].items()}
    unwritten = frozenset(map(str, data["unwritten"]))
    named = [*fields.values(), *fixed, *unwritten]
    if len(named) != len(set(named)):
        raise ShallowWaterCatalogError(f"{data['buffer']} names a field twice")
    return ConstantBuffer(str(data["buffer"]), fields, fixed, unwritten)


shallow_water_catalog_for = catalog_reader(CATALOG_DIRECTORY, catalog_from_dict, ShallowWaterCatalogError,
                                           "shallow water", (AttributeError,))


@dataclass(frozen=True, slots=True)
class PartialArea:
    world: np.ndarray
    scale: np.ndarray
    drawn: np.ndarray
    uv_offset: tuple[np.float32, np.float32]
    uv_scale: tuple[np.float32, np.float32]
    uv_diff: tuple[np.float32, np.float32]
    uv_scale_inverse: tuple[np.float32, np.float32]
    edges: tuple[tuple[np.float32, np.float32], tuple[np.float32, np.float32]]


def _min(a, b):
    """``minss``: the second where neither is less."""
    return a if a < b else b


def partial_area(world: np.ndarray, scale: Sequence[float], position: Sequence[float],
                 point_scale: Sequence[float], tiles: tuple[int, int]) -> PartialArea:
    f = np.float32
    world = np.asarray(world, f).reshape(4, 4)
    owner, point, sized = (np.asarray(values, f) for values in (scale, position, point_scale))
    drawn = world.copy()
    found = []
    for axis, count in ((0, f(tiles[0])), (2, f(tiles[1]))):
        offset = point[axis] - world[3, axis]
        half = (owner[axis] - sized[axis]) * f(0.5)
        tile = (f(1) / count) * sized[axis]
        clamped = -half if -half > offset else _min(offset, half)
        whole = np.floor(clamped / tile) if clamped > f(0) else np.ceil(clamped / tile)
        uv_scale = (f(1) / owner[axis]) * sized[axis]
        shift = whole * tile
        uv_offset = (f(1) / owner[axis]) * ((owner[axis] * f(0.5) + shift) - sized[axis] * f(0.5))
        halves = (owner[axis] * (f(1) / tile)) * f(0.5)
        inner = whole * (f(1) / halves) - uv_scale
        edges = inner * f(0.5), (uv_scale * f(2) + inner) * f(0.5)
        drawn[axis, axis] = sized[axis]
        drawn[3, axis] = world[3, axis] + shift
        found.append((uv_offset, uv_scale, (f(1) / count) * uv_scale, f(1) / uv_scale, edges))
    drawn[3, 3] = f(1)
    (x, z) = found
    return PartialArea(world, owner, drawn, (x[0], z[0]), (x[1], z[1]), (x[2], z[2]), (x[3], z[3]), (x[4], z[4]))


def density_map_position(world: np.ndarray, scale: Sequence[float], rotation: Sequence[float],
                         resolution: tuple[int, int]) -> tuple[np.float32, np.float32, np.float32] | None:
    f = np.float32
    position = (f(0), f(0), f(0))
    for _frame in range(_SETTLE_FRAMES):
        moved = _density_step(position, np.asarray(world, f).reshape(4, 4), scale, rotation, resolution)
        if moved == position:
            return position
        position = moved
    return None


def _density_step(position, world: np.ndarray, scale, rotation, resolution):
    """Operations in the executable's order: the floats depend on it."""
    f = np.float32
    ux, uy, uz = normalized(world[0, :3])
    vx, vy, vz = normalized(world[1, :3])
    nx, ny, nz = f(vz * uy) - f(vy * uz), f(vx * uz) - f(vz * ux), f(vy * ux) - f(vx * uy)
    dx, dy, dz = (world[3, axis] - position[axis] for axis in range(3))
    cell_u, cell_v = f(scale[0]) / f(resolution[0]), f(scale[1]) / f(resolution[1])
    du = (f(dy * uy) + f(dx * ux)) + f(dz * uz)
    dv = (f(dy * vy) + f(dx * vx)) + f(dz * vz)
    dn = (f(dy * ny) + f(dx * nx)) + f(dz * nz)
    su, sv = f(_truncated(du / cell_u)) * cell_u, f(_truncated(dv / cell_v)) * cell_v
    # The whole cells and the plane's distance, turned by the rotation: v + w t + q x t, t = 2 (q x v).
    qx, qy, qz, qw = (f(value) for value in rotation)
    tx = (f(qy * dn) - f(qz * sv)) * f(2)
    ty = (f(qz * su) - f(qx * dn)) * f(2)
    tz = (f(qx * sv) - f(qy * su)) * f(2)
    moved = ((f(qy * tz) - f(qz * ty)) + (f(qw * tx) + su),
             (f(qz * tx) - f(qx * tz)) + (f(qw * ty) + sv),
             (f(qx * ty) - f(qy * tx)) + (f(qw * tz) + dn))
    return tuple(moved[axis] + position[axis] for axis in range(3))


def _truncated(value: np.float32) -> int:
    """``cvttss2si``: toward zero; NaN and values out of range give the integer indefinite."""
    if not np.isfinite(value) or not -2147483648.0 <= float(value) < 2147483648.0:
        return -2147483648
    return int(value)


def controller_writes(controller: ShallowWaterController, density: Sequence[float] | None,
                      point_scale: Sequence[float]) -> dict[str, tuple[float, float, float, float]]:
    sources = {"density": density, "point scale": point_scale}
    written = {}
    for variable, lanes in controller.writes.items():
        if any(isinstance(lane, tuple) and sources[lane[0]] is None for lane in lanes):
            continue
        written[variable] = tuple(float(np.float32(sources[lane[0]][lane[1]] if isinstance(lane, tuple) else lane))
                                  for lane in lanes)
    return written


def _through(vector: Sequence[np.float32], matrix: np.ndarray) -> np.ndarray:
    x, y, z = vector
    return (matrix[0, :3] * x + matrix[1, :3] * y) + matrix[2, :3] * z


@dataclass(frozen=True, slots=True)
class ShallowWater:
    key: str
    kind: ShallowWaterKind
    drawn: np.ndarray
    scale: np.ndarray
    area: PartialArea | None
    tiles: tuple[int, int]
    grid: tuple[int, int]
    fields: Mapping[str, object]
    occlusion: bool
    segment: int
    alpha: str
    mmtr_path: str = ""
    context: object = None
    material: object = None


def height_field_constants(layout, water: ShallowWater, camera: Sequence[float]) -> bytes:
    f = np.float32
    kind, fields = water.kind, water.fields
    buffer = kind.buffers["surface"]
    count = (f(water.tiles[0]), f(water.tiles[1]))
    inverse = (f(1) / count[0], f(1) / count[1])
    factor, radius = f(kind.field(fields, "tessellation")), f(kind.field(fields, "attenuation"))
    values = {"drawn": water.drawn, "tessellation": [factor], "tessellation_inverse": [f(1) / factor],
              "tiles": count, "tiles_inverse": inverse,
              "colour_blend": [f(kind.field(fields, "colour_blend"))],
              "normal_blend": [f(kind.field(fields, "normal_blend"))],
              "tile_size": [water.scale[0] / count[0]],
              "emission_blend": [f(kind.field(fields, "emission_blend"))],
              "axis_x": _through((inverse[0], f(0), f(0)), water.drawn),
              "attenuation": [f(1) / (radius * radius)],
              "axis_z": _through((f(0), f(0), inverse[1]), water.drawn),
              "camera": np.asarray(camera, f)}
    for meaning in ("fresnel", "refraction"):
        if meaning in buffer.fields:
            values[meaning] = [f(kind.field(fields, meaning))]
    for meaning, channels in (("colour", 4), ("projection_colour", 3)):
        if meaning in buffer.fields:
            values[meaning] = [f(byte) / f(255) for byte in kind.field(fields, meaning)[:channels]]
    return _constants(layout, buffer, values)


def partial_constants(layout, water: ShallowWater) -> bytes:
    area = water.area
    return _constants(layout, water.kind.buffers["partial"], {
        "world": area.world, "uv_offset": area.uv_offset, "uv_scale": area.uv_scale, "uv_diff": area.uv_diff,
        "uv_scale_inverse": area.uv_scale_inverse})


def _constants(layout, buffer: ConstantBuffer, values: Mapping[str, object]) -> bytes:
    named = {buffer.fields[meaning]: np.asarray(value, np.float64) for meaning, value in values.items()}
    named.update({name: np.asarray(value, np.float64) for name, value in buffer.fixed.items()})
    return bytes(laid_out(layout, named, True))


def unfilled(layout, buffer: ConstantBuffer) -> list[str]:
    return [variable.name for variable in layout.variables if variable.name not in buffer.filled]


def cheap_tile_vertices(area: PartialArea) -> bytes:
    f = np.float32
    steps = [[f(-0.5), *edges, f(0.5)] for edges in area.edges]
    return np.array([(x, f(0), z, x + f(0.5), z + f(0.5)) for z in steps[1] for x in steps[0]], "<f4").tobytes()


class MeshSceneShallowWater:
    def shallow_water(self, document, water: ShallowWater, targets: Sequence[tuple[int, int]],
                      programs: SystemPrograms) -> list[int]:
        key = water.key, tuple(targets)
        if key not in self._shallow:
            self._shallow[key] = self._shallow_steps(document, water, list(targets), programs)
        return self._shallow[key]

    def _shallow_steps(self, document, water: ShallowWater, targets: list[tuple[int, int]],
                       programs: SystemPrograms) -> list[int]:
        catalog = shallow_water_catalog_for(self.profile)
        if catalog is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy has no catalog of the shallow water draws of this game build."))
        if water.key not in self._shallow_held:
            words = water.tiles[0] * water.tiles[1]
            initial = np.asarray(catalog.culling.initial_arguments, "<u4").tobytes()
            arguments = self.device.scene.buffer(initial, UNORDERED_ACCESS)
            textures = [self.device.zeros(form, water.grid, 0, water.grid[0] * water.grid[1] * texel)
                        for _name, form, texel in (catalog.simulation["height"], catalog.simulation["velocity"])]
            self._shallow_held[water.key] = (self.device.scene.buffer(bytes(4 * words), UNORDERED_ACCESS), arguments,
                                             self.device.copy(arguments, self.device.buffer(initial)), *textures)
        indices, arguments, reset, height, velocity = self._shallow_held[water.key]
        simulated = {read: (form, held) for (read, form, _texel), held in
                     ((catalog.simulation["height"], height), (catalog.simulation["velocity"], velocity))}
        own = {held.buffer: (meaning, held) for meaning, held in water.kind.buffers.items()}

        def buffer(contract, resource) -> bytes | None:
            name = resource.name
            if resource.register_class == "b" and name in own:
                layout = contract.buffer(name)
                meaning, held = own[name]
                missing = unfilled(layout, held)
                if missing:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The shallow water reads {field} of {buffer}, which REasy does not fill."),
                        {"field": missing[0], "buffer": name})
                if meaning == "partial":
                    return partial_constants(layout, water)
                view = np.linalg.inv(self.camera.view_matrix())
                return height_field_constants(layout, water, view[:3, 3])
            if name in PLACEMENT_BUFFERS:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The shallow water reads {buffer}, which no capture shows the renderer giving "
                                       "it."), {"buffer": name})
            return engine_buffer(contract, resource, self.camera, Placement(np.eye(4)[:3]), self.frame, self.captured,
                                 clock=self.clock)

        def texture(contract, resource) -> tuple | None:
            if resource.name in simulated:
                return 0, *simulated[resource.name]
            if resource.name == catalog.depth_read:
                return self._depth_copy_entry(resource)
            return self._engine_texture(GBUFFER_PURPOSE)(contract, resource)
        steps = [reset, self._shallow_culling(catalog, water, programs, indices, arguments, buffer)]
        for draw in water.kind.draws:
            steps.append(self._shallow_draw(document, catalog, water, draw, targets, buffer, texture,
                                            (indices, arguments) if draw == "surface" else None))
        return steps

    def _following_buffer(self, water: ShallowWater, own: bool, contract, resource, make: Callable,
                          bound: bytes | None = None) -> int:
        layout = contract.buffer(resource.name)
        kept = None if bound is None else _heap_fields_kept(layout, bytes(len(bound)), bound)
        kind = ("shallow water", water.key if own else None, resource.name, layout.byte_size, layout.variables, kept)
        if bound is None:
            return self.device.following(kind, lambda: make(contract, resource))
        return self.device.following(kind, lambda: _heap_fields_kept(layout, make(contract, resource), bound))

    def _shallow_culling(self, catalog, water: ShallowWater, programs: SystemPrograms, indices: int, arguments: int,
                         buffer: Callable) -> int:
        culling = catalog.culling
        shader, contract = programs.get(culling.target, SystemProgram(culling.file, culling.program))
        if tuple(contract.thread_group_size or ()) != (culling.threads, 1, 1):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The compute program {name} runs other groups than the renderer dispatches."),
                {"name": culling.program})
        visibility, tables = SHADER_VISIBILITY["compute"], []
        written = {culling.indices: indices, culling.arguments: arguments}
        own = {held.buffer for held in water.kind.buffers.values()}
        for resource in contract.resources:
            place = (resource.bind_point, resource.register_space, resource.bind_count)
            kind = resource.register_class
            if kind == "u" and resource.name in written:
                tables.append((RANGE_UAV, *place, _UAV_DIMENSIONS.get(resource.dimension, BUFFER), visibility,
                               ((0, 0, 0, written[resource.name]),)))
            elif kind == "b":
                tables.append((RANGE_CBV, *place, 0, visibility, ((0, self._following_buffer(
                    water, resource.name in own, contract, resource, buffer)),)))
            elif kind == "s":
                state = self._sampler(resource.name)
                tables.append((RANGE_SAMPLER, *place, 0, visibility,
                               tuple((slot, *state) for slot in range(max(1, resource.bind_count)))))
            elif kind == "t" and resource.name == catalog.alpha_read and resource.dimension in VIEW_DIMENSIONS:
                entry = _texture_entry(0, water.alpha, self.load_file, resource.dimension, self.read_textures)
                if entry is None:
                    raise missing_texture(water.alpha)
                tables.append((RANGE_SRV, *place, VIEW_DIMENSIONS[resource.dimension], visibility, (entry,)))
            else:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The compute program {program} declares {name}, which no capture shows the "
                                       "renderer binding."), {"program": culling.program, "name": resource.name})
        try:
            return self.device.scene.dispatch(self.device.bound({"compute_shader": shader, "tables": tables,
                                                                 "heap": []})
                                              | {"groups": (water.tiles[0] * water.tiles[1] // culling.threads, 1, 1)})
        except RuntimeError as exc:
            raise _not_drawn(exc) from exc

    def _shallow_draw(self, document, catalog, water: ShallowWater, draw: str, targets: list[tuple[int, int]],
                      buffer: Callable, texture: Callable, indirect: tuple[int, int] | None) -> int:
        name = water.kind.programs[draw][water.occlusion]
        sdf = document.sdf.targets[self.shader_target]
        names = program_names(sdf)
        program = sdf.programs[names.index(name)] if name in names else None
        stages = None if program is None else tuple(shader.role for shader in program.shaders if shader.present)
        if stages not in (("vertex", "hull", "domain", "pixel"), ("vertex", "pixel")) \
                or any(program.shader(stage).spirv is not None for stage in stages):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The master has no Direct3D program {name}, which the renderer draws the shallow "
                                   "water with."), {"name": name})
        if (error := _unbound(document, program, len(targets))) is not None:
            raise error
        contracts = {stage: document.profile.shader_graph_profile.shader_contract(document, program, stage)
                     for stage in stages}
        if "hull" in stages and frame_constants_for(document.profile) is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The program {name} tessellates, and REasy has no catalog of the engine's state its "
                                   "hull shader reads for this game build."), {"name": name})
        shaders = {stage: (contracts[stage], bytes(program.shader(stage).payload)) for stage in stages}
        tables, heap, missing = shader_bindings(
            document, shaders, water.material, self.load_file, buffer, self._sampler, engine_texture=texture,
            read_textures=self.read_textures, read_buffers=self.read_buffers, heap_start=self._heap_start,
            made_texture=self.made_texture)
        self._took(heap, missing)
        self._read_clock(program)
        own = {held.buffer for held in water.kind.buffers.values()}
        declared = [(contract, resource) for contract in contracts.values() for resource in contract.resources]
        user_buffers = set(document.profile.user_material_buffer_names)
        for index, (contract, resource) in enumerate(declared):
            if resource.register_class == "b" and tables[index][-1] and resource.name not in user_buffers \
                    and (resource.name in own or self.follows(resource.name)):
                *entry, bound = tables[index][-1][0]
                tables[index] = (*tables[index][:-1], ((*entry, self._following_buffer(
                    water, resource.name in own, contract, resource, buffer, bound)),))
        if indirect is None:
            tile = water.kind.cheap_tile
            elements = program.input_layout.elements if program.input_layout is not None else ()
            if [(element.semantic, element.semantic_index, element.dxgi_format_id, element.aligned_byte_offset)
                    for element in elements] != list(tile.elements) \
                    or any(element.input_slot or element.per_instance for element in elements):
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The program {name} reads other vertices than the renderer's cheap tile."),
                    {"name": name})
            geometry = {"vertex_buffers": [(cheap_tile_vertices(water.area), tile.stride)],
                        "elements": [(element.semantic, element.semantic_index, element.dxgi_format_id, 0,
                                      element.aligned_byte_offset, 0, 0) for element in elements],
                        "indices": self.device.buffer(np.asarray(tile.indices, "<u4").tobytes()),
                        "index_format": DXGI_FORMAT_R32_UINT, "draw": (len(tile.indices), 1, 0, 0, 0)}
        else:
            geometry = {"vertex_buffers": [], "elements": [], "indices": indirect[0],
                        "index_format": DXGI_FORMAT_R32_UINT, "indirect": (indirect[1], 0)}
        job = {**{f"{stage}_shader": shaders[stage][1] for stage in stages}, "tables": tables, "heap": heap,
               **geometry, **program_states(program, len(targets), 0), "blend_factor": catalog.blend_factor}
        job = self.device.bound(job)
        job["vertex_buffers"] = [(self.device.buffer(data, 1), stride) for data, stride in job["vertex_buffers"]]
        return self._step(job, targets, self.depth)


__all__ = ["CATALOG_DIRECTORY", "CATALOG_FORMAT", "DRAWS", "CheapTile", "ConstantBuffer", "CullingPass",
           "MeshSceneShallowWater", "PartialArea", "ShallowWater", "ShallowWaterCatalog", "ShallowWaterCatalogError",
           "ShallowWaterController", "ShallowWaterKind", "catalog_file_name", "catalog_from_dict",
           "cheap_tile_vertices", "controller_writes", "density_map_position", "height_field_constants", "partial_area",
           "partial_constants", "shallow_water_catalog_for", "unfilled"]
