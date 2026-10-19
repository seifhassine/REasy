"""See docs/internals/mesh-preview.md."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from functools import partial
from itertools import product
from types import MappingProxyType

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mdf.mdf_file import transparent_priority_bias
from file_handlers.sdf.contracts import shader_contract_for_program
from file_handlers.sdf.models import program_names
from file_handlers.sdf.sdf_file import (
    COMPARE_ALWAYS, COMPARE_EQUAL, COMPARE_GREATER, COMPARE_GREATER_EQUAL, COMPARE_LESS, COMPARE_LESS_EQUAL,
    COMPARE_NOT_EQUAL,
)

from .engine_buffers import (
    CAMERA_BUFFERS, PLACEMENT_BUFFERS, Camera, EngineConstant, InstanceInput, Placement, engine_buffer, scene_values,
)
from .draw_order import SORTED_ORDERS, draw_order_for
from .frame_constants import frame_constants_for
from .material_preview import (
    RANGE_CBV, _MATERIAL_HEAP_START, RANGE_SAMPLER, RANGE_SRV, RANGE_UAV, _UAV_DIMENSIONS, VIEW_DIMENSIONS,
    SHADER_VISIBILITY,
    MaterialPreviewError, PreviewMaterial, _heap_fields_kept, _texture_entry, bound_record_size, gbuffer_route,
    pixel_sampled_textures, resident_key, shader_bindings, streamed_key,
)
from file_handlers.mesh.mesh_file import MeshFile, VertexBufferType
from file_handlers.rsz.scene_joints import times
from utils.resource_file_utils import resource_path_key

from .mesh_draw import MeshBuffer, SubmeshRange, program_states, submesh_geometry
from .meshlet_deferred import DeferredPasses
from .meshlets import (
    EXTENDED, HANDLE_STEP, ROUTE as MESHLET_ROUTE, MeshletBuffer, MeshletGroup, MeshletPrograms, check_clusters,
    check_group, culled_meshlets, extended_blocks, instance_records, material_culling_size, material_tables,
    meshlet_catalog_for, meshlet_programs, named_meshlet_programs, receives_decals, replace_table, root_constant,
    skinning_records,
)
from .pass_choice import depth_prepass_program, has_scene_program, own_passes, own_program, scene_program
from .pretransform import CHAIN_BUFFERS, ROUTE, ChainStep, Deformation, Destination
from .pretransform import constant_buffer as chain_constants
from .pretransform_passes import ChainPasses, SystemPrograms, check_threads, written_entry
from .wrinkle import WrinkleObject, object_scale, proxy_bones, render_target_form, wrinkle_catalog_for
from .wrinkle_passes import WrinklePasses
from .resident import BUFFER, DEPTH_STENCIL, RENDER_TARGET, TEXTURE_2D, UNORDERED_ACCESS, Resident
from .sampler_states import sampler_catalog_for
from .surface_meaning import GBUFFER_PURPOSE, PREPASS_PURPOSE, TargetFormat, surface_output_catalog_for

# Phases drawn between the light passes (``light_passes.MeshPhase.key``).
PHASES = ("blending", "forward")
PhaseKey = tuple[str, tuple[tuple[str, ...], ...]]
# Bytes per texel of the depth plane, then the stencil plane, by depth/stencil format.
DEPTH_PLANES = {20: (4, 1), 40: (4, 0)}
# By depth/stencil format: the typeless format, then the formats shaders read depth and stencil in.
DEPTH_VIEWS = {20: (19, 21, 22), 40: (39, 41, None)}
_STAGES = ("vertex", "hull", "domain", "pixel")
# Coverage tests by depth function: (start depth, depth function, comparison of the depth left with 0 marking covered
# texels). See docs/internals/mesh-preview.md, Coverage.
_COVERAGE = {
    **dict.fromkeys((COMPARE_GREATER, COMPARE_NOT_EQUAL), (-1.0, COMPARE_GREATER, ">")),
    **dict.fromkeys((COMPARE_LESS_EQUAL, COMPARE_EQUAL), (2.0, COMPARE_LESS, "==")),
    **dict.fromkeys((COMPARE_GREATER_EQUAL, COMPARE_ALWAYS), (-1.0, COMPARE_ALWAYS, ">=")),
}
# How far the GPU's float32 transforms may stray from float64, relative to the summed terms.
_BOUNDS_MARGIN = 1e-4
BOX_CORNERS = np.array(list(product((0, 1), repeat=3)))
_SCREEN_TRIANGLE = 3


@dataclass(frozen=True, slots=True)
class MeshPart:
    document: object
    material: PreviewMaterial
    buffer: MeshBuffer | MeshletBuffer
    submesh: SubmeshRange
    route: str | None = None
    meshlet_programs: MeshletPrograms | None = None


@dataclass(frozen=True, slots=True)
class MeshObject:
    """``skins`` are undrawn parts whose masters skin the rest."""
    parts: Sequence[MeshPart]
    placement: Placement
    skinned: bool
    stencil_reference: int = 0
    bounds: MeshBounds | None = None
    deformation: Deformation | None = None
    skins: Sequence[MeshPart] = ()
    engine: Mapping[str, bytes | EngineConstant] = MappingProxyType({})
    decal_receive_mode: int = 0
    box: ObjectBox | None = None
    transparent_bias: float = 0.0
    wrinkle: WrinkleObject | None = None

    @property
    def route(self) -> str:
        if any(isinstance(part.buffer, MeshletBuffer) for part in self.parts):
            return MESHLET_ROUTE
        return gbuffer_route(self.skinned) if self.deformation is None else ROUTE


@dataclass(frozen=True, slots=True)
class MeshBounds:
    box: np.ndarray
    weight_sums: tuple[float, float] = (1.0, 1.0)


def _weight_sums(payload, positions: int, bones: int) -> tuple[float, float] | None:
    streams = [stream for stream in (payload.skin_weights, payload.extra_skin_weights) if stream is not None]
    if not streams or any(stream.vertex_count != positions for stream in streams):
        return None
    weights = np.hstack([np.asarray(stream.weights, np.float64).reshape(positions, -1) for stream in streams])
    indices = np.hstack([np.asarray(stream.deform_indices, np.int64).reshape(positions, -1) for stream in streams])
    if (weights < 0).any() or (indices[weights != 0] >= bones).any():
        return None
    sums = weights.sum(axis=1)
    return float(sums.min(initial=1.0)), float(sums.max(initial=1.0))


def mesh_bounds(payloads: Iterable, bones: int | None) -> MeshBounds | None:
    lowest, highest, sums = [], [], [(1.0, 1.0)]
    for payload in payloads:
        positions = np.asarray(payload.positions, np.float64).reshape(-1, 3)
        if bones is not None:
            sums.append(_weight_sums(payload, len(positions), bones))
            if sums[-1] is None:
                return None
        if len(positions):
            lowest.append(positions.min(axis=0))
            highest.append(positions.max(axis=0))
    if not lowest:
        return None
    return MeshBounds(np.array([np.min(lowest, axis=0), np.max(highest, axis=0)]),
                      (min(least for least, _most in sums), max(most for _least, most in sums)))


def _blends_first(program) -> bool:
    return bool(program.blend_state.render_targets[0].blend_enable)


def _part_box(part: MeshPart) -> np.ndarray:
    data, stride = part.buffer.streams[int(VertexBufferType.Position)]
    positions = np.frombuffer(data, np.uint8).reshape(-1, stride)[:, :12].copy().view(np.float32)
    submesh = part.submesh
    indices = np.frombuffer(part.buffer.indices, np.uint32 if part.buffer.wide else np.uint16)[
        submesh.start:submesh.start + submesh.count].astype(np.int64) + submesh.base_vertex
    used = positions[indices].astype(np.float64)
    return np.array([used.min(axis=0), used.max(axis=0)])


@dataclass(frozen=True, slots=True, eq=False)
class ObjectBox:
    """``bones``: each bone's box corners in bind pose, rows x 8 x 3."""
    box: np.ndarray
    bones: np.ndarray | None = None


def object_box(mesh: MeshFile) -> ObjectBox | None:
    if not mesh.meshes:
        return None
    found = np.array([np.asarray(corner, np.float64)[:3] for corner in mesh.meshes[0].bounding_box])
    if not np.isfinite(found).all() or not (found[0] <= found[1]).all():
        return None
    boxes = mesh.bounding_boxes.boxes if mesh.bounding_boxes is not None else ()
    remap = mesh.bone_remap_indices
    bind = np.array(mesh.world_matrices, np.float64).reshape(-1, 4, 4)
    if not boxes or not mesh.bones or len(remap) < len(boxes) or max(remap[:len(boxes)]) >= len(bind):
        return ObjectBox(found)
    bones = np.array([np.array([item.minimum[:3], item.maximum[:3]], np.float64)[BOX_CORNERS, range(3)]
                      @ bind[joint][:3, :3] + bind[joint][3, :3] for item, joint in zip(boxes, remap)])
    return ObjectBox(found, bones if np.isfinite(bones).all() else None)


def _object_box(item: MeshObject) -> np.ndarray:
    if item.box is not None:
        return np.asarray(item.box.box, np.float64)
    boxes = np.array([_part_box(part) for part in item.parts if not isinstance(part.buffer, MeshletBuffer)])
    return np.array([boxes[:, 0].min(axis=0), boxes[:, 1].max(axis=0)])


def _sort_point(item: MeshObject, eye: np.ndarray) -> np.ndarray:
    found, bones = item.box, item.placement.bones
    if item.skinned and bones is not None and found is not None and found.bones is not None:
        rows = np.asarray(bones, np.float64)[:len(found.bones)]
        corners = np.einsum("rij,rkj->rki", rows[..., :3], found.bones[:len(rows)]) + rows[:, None, :, 3]
    else:
        world = np.asarray(item.placement.world, np.float64).reshape(3, 4)
        corners = _object_box(item)[BOX_CORNERS, range(3)] @ world[:, :3].T + world[:, 3]
    corners = corners.reshape(-1, 3).astype(np.float32)
    centre = ((corners.min(axis=0) + corners.max(axis=0)) * np.float32(0.5)).astype(np.float32)
    bias = np.float32(item.transparent_bias)
    if bias:
        centre = (centre + (centre - np.asarray(eye, np.float32)) * bias).astype(np.float32)
    return centre


_KEY_SCALE = 4294967295.0
_KEY_MAX = 0xFFFFFFFF


def transparent_key(camera: Camera, point: np.ndarray) -> int:
    """Ascending, as the game sorts. See docs/internals/mesh-preview.md."""
    f32 = np.float32
    view = np.asarray(camera.view, np.float64).reshape(3, 4)
    rows = np.zeros((4, 4), f32)
    rows[:3, :3], rows[3, :3], rows[3, 3] = view[:, :3].T, view[:, 3], 1
    near, far = f32(camera.near), f32(camera.far)
    projection = np.zeros((4, 4), f32)
    projection[0, 0], projection[1, 1] = f32(camera.scale[0]), f32(camera.scale[1])
    if camera.orthographic:
        depth = f32(f32(1) / f32(near - far))
        projection[2, 2], projection[3, 2], projection[3, 3] = depth, f32(near * depth), 1
    else:
        depth = f32(f32(-far) / f32(far - near))
        projection[2, 2], projection[2, 3], projection[3, 2] = depth, -1, f32(near * depth)
    matrix = times(rows, projection)
    x, y, z = np.asarray(point, f32)
    clip = ((x * matrix[0]).astype(f32) + (y * matrix[1]).astype(f32)).astype(f32)
    clip = ((clip + (z * matrix[2]).astype(f32)).astype(f32) + matrix[3]).astype(f32)
    if not (clip[2] > 0 and clip[3] > 0):
        return _KEY_MAX
    share = min(max(f32(clip[2] / clip[3]), f32(0)), f32(1))
    return ~int(float(share) * _KEY_SCALE) & _KEY_MAX


def _priority_bias(material) -> int:
    return transparent_priority_bias(getattr(material, "material_flags", 0) or 0)


def _world_boxes(transforms: np.ndarray, bounds: Sequence[MeshBounds]) -> np.ndarray:
    """``transforms`` is objects x bones x 3 x 4."""
    boxes = np.array([item.box for item in bounds], np.float64)
    sums = np.array([item.weight_sums for item in bounds], np.float64)
    rotations, offsets = transforms[..., :3], transforms[..., 3]
    centre, extent = (boxes[:, 0] + boxes[:, 1]) / 2, (boxes[:, 1] - boxes[:, 0]) / 2
    centres = np.einsum("obij,oj->obi", rotations, centre) + offsets
    extents = np.einsum("obij,oj->obi", np.abs(rotations), extent)
    moved = np.stack([(centres - extents).min(axis=1), (centres + extents).max(axis=1)], axis=1)
    scaled = moved[:, :, None] * sums[:, None, :, None]
    size = (np.einsum("obij,oj->obi", np.abs(rotations), np.abs(centre) + extent) + np.abs(offsets)).max(axis=(1, 2))
    margin = (sums[:, 1] * _BOUNDS_MARGIN * size)[:, None]
    return np.stack([scaled.min(axis=(1, 2)) - margin, scaled.max(axis=(1, 2)) + margin], axis=1)


@dataclass(frozen=True, slots=True)
class GBufferTarget:
    target: int
    # DXGI_FORMAT.
    format: int
    # Texels after every draw, rows top to bottom, tightly packed.
    texels: bytes


@dataclass(frozen=True, slots=True)
class GBufferImage:
    size: tuple[int, int]
    targets: tuple[GBufferTarget, ...]
    depth_format: int
    # Depth, then stencil plane, tightly packed.
    depth: bytes
    missing_textures: tuple[str, ...]


class PartError(Exception):
    """Tracebacks are dropped so the scene can be freed."""

    def __init__(self, refusals: Iterable[tuple[MeshPart, MaterialPreviewError]]):
        self.refusals = tuple(refusals)
        super().__init__(self.refusals)
        for _part, error in self.refusals:
            seen: set[int] = set()
            cause: BaseException | None = error
            while cause is not None and id(cause) not in seen:
                seen.add(id(cause))
                cause.__traceback__ = None
                cause = cause.__cause__ or cause.__context__


@contextmanager
def _drawing(*parts: MeshPart) -> Iterator[None]:
    try:
        yield
    except MaterialPreviewError as exc:
        raise PartError([(part, exc) for part in parts]) from exc


@dataclass(frozen=True, slots=True)
class _Draw:
    owner: int
    part: MeshPart
    # ``gbuffer``, a phase of ``PHASES`` or a master's own pass.
    purpose: str
    program: object
    prepass: object | None


def _bound_targets(catalog, blending: bool) -> tuple[TargetFormat, ...]:
    bound = sorted((item for item in catalog.formats if item.purpose == GBUFFER_PURPOSE and item.blending == blending),
                   key=lambda item: item.target)
    if not bound or [item.target for item in bound] != list(range(len(bound))):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "REasy's catalog of this game build does not record the targets the renderer binds for "
                               "the pass."))
    return tuple(bound)


def _blends(catalog, program) -> bool:
    if program.blend_state is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The program has no fixed-function states."))
    count = 1 + max((item.target for item in catalog.formats if item.purpose == GBUFFER_PURPOSE), default=-1)
    return any(item.blend_enable for item in program.blend_state.render_targets[:count])


def _written(document, program) -> set[int]:
    return {item.semantic_index for item in document.profile.shader_graph_profile.shader_contract(
        document, program, "pixel").outputs if item.semantic_name.casefold() == "sv_target"}


def _engine_bound(document, tables: Sequence[tuple], contracts: Iterable) -> list[tuple]:
    declared = [(contract, resource) for contract in contracts for resource in contract.resources]
    user_buffers = document.profile.user_material_buffer_names
    return [(index, contract, resource) for index, (contract, resource) in enumerate(declared)
            if tables[index][-1] and (resource.register_class == "b" and resource.name not in user_buffers
                                      or resource.register_class == "t" and resource.name in PLACEMENT_BUFFERS)]


def _program_contracts(part: MeshPart, program) -> dict[str, object]:
    document = part.document
    stages = {shader.role for shader in program.shaders if shader.present}
    if ("vertex" not in stages or not stages <= set(_STAGES) or ("hull" in stages) != ("domain" in stages)
            or program.shader("vertex").spirv is not None):
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The program {name} is not a Direct3D vertex and pixel shader, with or without hull "
                               "and domain shaders."), {"name": program.name})
    if "hull" in stages and frame_constants_for(document.profile) is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The program {name} tessellates, and REasy has no catalog of the engine's state its "
                               "hull shader reads for this game build."), {"name": program.name})
    profile = document.profile.shader_graph_profile
    return {stage: profile.shader_contract(document, program, stage) for stage in _STAGES if stage in stages}


def _part_geometry(part: MeshPart, program, destination: Destination | None,
                   engine: Mapping[str, bytes | EngineConstant]) -> dict:
    instance = {name: data for name, data in engine.items() if isinstance(data, InstanceInput)}
    return submesh_geometry(program, _program_contracts(part, program)["vertex"], part.buffer, part.submesh,
                            destination, instance)


def _program_draw(part: MeshPart, program, geometry: dict, targets: int, stencil_reference: int, load_file,
                  buffer: Callable, sampler: Callable, texture: Callable | None = None,
                  read_textures: dict | None = None, *, heap_start: int, engine_heap: Callable | None = None,
                  shared_holders: Mapping[str, bytes] | None = None, streamed: Callable[[str], bool] | None = None,
                  resident_file: Callable | None = None, pixel_sampled: Collection[str] = (),
                  made_texture: Callable[[str], tuple] | None = None) -> tuple[dict, tuple[str, ...], list[tuple]]:
    """Targets, depth and MDF textures are left for the caller to bind."""
    document = part.document
    shaders = {stage: (contract, bytes(program.shader(stage).payload))
               for stage, contract in _program_contracts(part, program).items()}
    tables, heap, missing = shader_bindings(document, shaders, part.material, load_file, buffer, sampler,
                                           engine_texture=texture, read_textures=read_textures, heap_start=heap_start,
                                           engine_heap=engine_heap, shared_holders=shared_holders, streamed=streamed,
                                           resident_file=resident_file, pixel_sampled=pixel_sampled,
                                           made_texture=made_texture)
    engine = _engine_bound(document, tables, (contract for contract, _shader in shaders.values()))
    return {"vertex_shader": shaders["vertex"][1], "pixel_shader": shaders.get("pixel", (None, b""))[1],
            **{f"{stage}_shader": shaders[stage][1] for stage in ("hull", "domain") if stage in shaders},
            "tables": tables, "heap": heap, **geometry,
            **program_states(program, targets, stencil_reference)}, missing, engine


def draw_purpose(document, material: PreviewMaterial, shader_target: int, route: str) -> tuple[str, object]:
    catalog = surface_output_catalog_for(document.profile)
    if catalog is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "REasy has no catalog of what the renderer binds for this game build."))
    purpose, index = scene_program(document, shader_target, material.material_flags, route)
    program = document.sdf.targets[shader_target].programs[index]
    if purpose == GBUFFER_PURPOSE and _blends(catalog, program):
        purpose = "blending"
    return purpose, program


def _meshlet_programs(document, material: PreviewMaterial, shader_target: int,
                      names: MeshletPrograms | None = None, skinned: bool = False) -> tuple:
    catalog = meshlet_catalog_for(document.profile)
    if catalog is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "REasy has no catalog of how the renderer draws meshlet meshes for this game build."))
    if names is None:
        return meshlet_programs(document, shader_target, material.material_flags, catalog, skinned)
    if catalog.deferred is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture of this game build shows how the renderer transforms the triangles of "
                               "meshlet materials before shading them."))
    return named_meshlet_programs(document, shader_target, names)


def _compute_contract(part: MeshPart, program):
    document = part.document
    if [shader.role for shader in program.shaders if shader.present] != ["compute"] \
            or program.shader("compute").spirv is not None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The program {name} is not a Direct3D compute program."), {"name": program.name})
    return document.profile.shader_graph_profile.shader_contract(document, program, "compute")


def part_draws(document, material: PreviewMaterial, shader_target: int, route: str,
               phases: Iterable[PhaseKey], meshlet_programs: MeshletPrograms | None = None,
               skinned: bool = False) -> tuple[tuple[str, object] | None, dict[PhaseKey, object]]:
    flags = material.material_flags
    if route == MESHLET_ROUTE:
        purpose, raster, shade, *_transform = _meshlet_programs(document, material, shader_target, meshlet_programs,
                                                                skinned)
        return (purpose, raster if shade is None else shade), {}
    programs = document.sdf.targets[shader_target].programs
    own = own_passes(document, shader_target, flags, route)
    named = {(name, masters): programs[own_program(document, shader_target, flags, name, route)]
             for name, masters in phases if own in masters}
    if own and not has_scene_program(document, shader_target, flags, route):
        if not named:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The master draws the material only in passes of its own, which REasy's light "
                                   "pass catalog of this game build does not record."))
        return None, named
    return draw_purpose(document, material, shader_target, route), named


class MeshScene:
    """A frame is ``begin``, the steps from ``gbuffer`` and ``draw_phase`` in render order, then ``finish``."""

    def __init__(self, objects: Sequence[MeshObject], load_file: Callable[[str], bytes | None], camera: Camera, *,
                 shader_target: int, hardware: bool = False, phases: Collection[PhaseKey] = (),
                 pass_file: Callable[[str, bytes], object] | None = None,
                 captured: Mapping[str, Mapping[str, Sequence[int]]] = MappingProxyType({})):
        parts = [part for item in objects for part in (*item.parts, *item.skins)]
        if not parts:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The mesh has no submesh to draw."))
        if any(bool(masters) == (name in PHASES) for name, masters in phases):
            raise ValueError(f"the phases {sorted(phases)} tell one of {PHASES} by its masters, or another by none")
        self.profile = parts[0].document.profile
        self.catalog, self.samplers = surface_output_catalog_for(self.profile), sampler_catalog_for(self.profile)
        if self.catalog is None or self.samplers is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy has no catalog of what the renderer binds for this game build."))
        self.depth_format = self.catalog.depth_formats.get(GBUFFER_PURPOSE)
        if self.depth_format is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy's catalog of this game build does not record the G-buffer's depth format."))
        self.load_file, self.camera, self.objects = load_file, camera, list(objects)
        constants = frame_constants_for(self.profile)
        self.frame = {} if constants is None else constants.scene
        self.captured = captured
        self._forward_order = draw_order_for(self.profile)
        self._read_textures: dict = {}
        self.phases, self.shader_target = frozenset(phases), shader_target
        self.size = camera.size
        self._draws: list[_Draw] = []
        self._named: dict[PhaseKey, list[_Draw]] = defaultdict(list)
        self._missing: list[str] = []
        self._geometry: dict[tuple[int, int], dict] = {}
        meshlets: dict[tuple[int, int], tuple[list[MeshPart], tuple, list]] = {}
        self._system = SystemPrograms(self.profile, load_file, pass_file)
        self._material_ids: tuple | MaterialPreviewError | None = None
        refusals = []
        for owner, item in enumerate(objects):
            destination = None if item.deformation is None else item.deformation.destination
            for part in item.parts:
                try:
                    if isinstance(part.buffer, MeshletBuffer):
                        self._meshlet_part(owner, item, part, meshlets)
                        continue
                    route = part.route or item.route
                    main, named = part_draws(part.document, part.material, shader_target, route, self.phases)
                    drawn = None if main is None else self._draw(owner, part, *main, route)
                    read_from = destination if route == ROUTE else None
                    for program in (*named.values(), *(() if drawn is None else (drawn.program, drawn.prepass))):
                        if program is not None:
                            self._geometry[id(part), id(program)] = _part_geometry(part, program, read_from,
                                                                                   item.engine)
                except MaterialPreviewError as exc:
                    refusals.append((part, exc))
                    continue
                if drawn is not None:
                    self._draws.append(drawn)
                for phase, program in named.items():
                    self._named[phase].append(_Draw(owner, part, phase[0], program, None))
        if refusals:
            raise PartError(refusals)
        self._meshlet_groups = [MeshletGroup(owner, material, tuple(parts), *programs[:3], tuple(clusters), number,
                                             programs[3] if len(programs) > 3 else None)
                                for number, ((owner, material), (parts, programs, clusters))
                                in enumerate(meshlets.items(), 1)]
        self.meshlets = meshlet_catalog_for(self.profile) if meshlets else None
        self.device = Resident(hardware)
        self._placed: list[dict[tuple, tuple[int, Callable[[int], bytes]]]] = [{} for _item in objects]
        self._jobs: dict[tuple, tuple[dict, list[tuple]]] = {}
        self._engine_textures: dict[str, Callable] = {}
        # Bindless draws share one descriptor heap.
        self._heap_start = _MATERIAL_HEAP_START
        self._meshlet_buffers()
        self.render_textures: dict[str, tuple[str, int, int, tuple[int, ...]]] = {}
        self.depth_resource_format = DEPTH_VIEWS[self.depth_format][0]
        self.target_formats = {row.target: row.format for row in _bound_targets(self.catalog, False)}
        self._screen()
        self._boxes = np.zeros((len(self.objects), 2, 3))
        self._bounded = np.zeros(len(self.objects), bool)
        self._moved = set(range(len(self.objects)))
        self._corners: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._culled: tuple[bytes, np.ndarray] | None = None
        self._step_owners: dict[int, int] = {}
        self._owner_table: np.ndarray | None = None
        self._forward_runs: dict[int, tuple[bool, list[tuple[np.ndarray, int, tuple[int, ...], int]]]] = {}
        self._chains = ChainPasses(self.device, self.profile, load_file, pass_file)
        self._stale: set[int] = set()
        for owner, item in enumerate(objects):
            if item.deformation is not None:
                self._deform(owner, item)
        self._wrinkles: WrinklePasses | None = None
        for owner, item in enumerate(objects):
            if item.wrinkle is not None:
                self._wrinkle(owner, item.wrinkle)

    def _screen(self) -> None:
        self.targets = {target: self.device.texture(form, self.size, RENDER_TARGET)
                        for target, form in self.target_formats.items()}
        self.depth = self.device.texture(self.depth_resource_format, self.size, DEPTH_STENCIL)
        self.depth_before = self.device.texture(self.depth_resource_format, self.size, DEPTH_STENCIL)
        self._visible: tuple[int, int] | None = None
        self._deferred: DeferredPasses | None = None
        if self._meshlet_groups:
            catalog = self.meshlets
            self._visible = (self.device.texture(catalog.visibility_format, self.size, RENDER_TARGET),
                             self.device.texture(catalog.material_id_formats[0], self.size, DEPTH_STENCIL))
            if self._transforms:
                self._deferred = DeferredPasses(self.device, catalog.deferred, self._system, catalog.target,
                                                catalog.material_ids.file, self.size)
        self._scratch: dict[tuple[int, int], int] = {}
        self._coverage: dict[tuple[float, int], tuple[int, int, str, int]] = {}
        self._gbuffer: dict[bool, list[int]] = {}

    def resize(self, size: Sequence[int]) -> None:
        old = [*self.targets.values(), self.depth, self.depth_before, *self._scratch.values(),
               *(texture for held in self._coverage.values() for texture in (held[0], held[3])),
               *(self._visible or ()), *(() if self._deferred is None else self._deferred.resources())]
        self.size = tuple(size)
        self._screen()
        self._jobs.clear()
        self._step_owners.clear()
        self._owner_table = None
        self._forward_runs.clear()
        self.device.release(old)

    def place(self, owner: int, placement: Placement) -> None:
        item = self.objects[owner] = replace(self.objects[owner], placement=placement)
        self._moved.add(owner)
        for buffer, make in self._placed[owner].values():
            self.device.scene.write(buffer, make(owner))
        if item.deformation is not None:
            _check_still(item)
            self._stale.add(owner)
        if item.wrinkle is not None:
            self._wrinkles.place(owner)
            self._stale.add(owner)

    def faces(self, owner: int, part: MeshPart) -> tuple[int, bool] | None:
        """Cull mode in D3D12 numbering."""
        if isinstance(part.buffer, MeshletBuffer):
            material = part.buffer.clusters[part.submesh][0].material
            group = next((item for item in self._meshlet_groups if item.owner == owner and item.material == material),
                         None)
            raster = None if group is None else group.raster.rasterizer_state
            return None if raster is None else (raster.cull_mode_id, bool(raster.front_counter_clockwise))
        draws = (*self._draws, *(item for named in self._named.values() for item in named))
        found = next((item for item in draws if item.owner == owner and item.part is part), None)
        raster = None if found is None else found.program.rasterizer_state
        return None if raster is None else (raster.cull_mode_id, bool(raster.front_counter_clockwise))

    def morphed(self, owner: int) -> tuple[int, int]:
        if self._chains.morphed(owner) is None:
            self._chains.keep_morphed(owner)
            self._stale.add(owner)
        return self._chains.morphed(owner), self.objects[owner].deformation.destination.positions

    def deformed(self, owner: int) -> tuple[int, int]:
        return self._chains.destination(owner), self.objects[owner].deformation.destination.positions

    def morph(self, owner: int, weights: Sequence[float]) -> None:
        item, weights = self.objects[owner], np.asarray(weights, np.float32)
        self.objects[owner] = replace(item, deformation=replace(item.deformation, weights=weights))
        self._chains.morph(owner, weights)
        self._stale.add(owner)

    def _deform(self, owner: int, item: MeshObject) -> None:
        _check_still(item)
        deformation = item.deformation
        submeshes = {SubmeshRange(submesh.first_index, submesh.index_count, submesh.first): submesh
                     for submesh in deformation.level.submeshes if submesh.group in deformation.enabled}
        parts = (*item.parts, *item.skins)
        if not all(part.submesh in submeshes for part in parts):
            raise ValueError("a part of a deformed object is not a submesh of the mesh groups its deformation draws")
        masters = {submeshes[part.submesh].material: part for part in reversed(parts)}

        def skin(step: ChainStep, held: Mapping[str, int]) -> int:
            if step.material not in masters:
                raise ValueError("a deformed object has no part of a material whose master's program skins it")
            return self._skin(owner, masters[step.material], step, held)
        self._chains.add(owner, deformation, skin, partial(self._engine_buffer, owner))
        self._stale.add(owner)

    def _wrinkle(self, owner: int, wrinkle: WrinkleObject) -> None:
        if self._wrinkles is None:
            catalog = wrinkle_catalog_for(self.profile)
            if catalog is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "REasy has no catalog of the wrinkle passes of this game build."))
            self._wrinkles = WrinklePasses(self.device, self._system, catalog)
        data = self.load_file(wrinkle.render_target)
        if data is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The render texture file {path} was not found."), {"path": wrinkle.render_target})
        form, size = render_target_form(data)
        texture = self.render_texture(wrinkle.render_target, form, size)
        self._wrinkles.add(owner, wrinkle, (form, texture, size),
                           lambda: object_scale(self.objects[owner].placement.world),
                           {name: partial(self._proxy_bytes, owner) for name in PLACEMENT_BUFFERS})
        self._stale.add(owner)

    def _proxy_bytes(self, owner: int, contract, resource) -> bytes | None:
        item = self.objects[owner]
        placement = item.placement
        posed = Placement(placement.world, proxy_bones(item.wrinkle, placement.world, placement.bones))
        return engine_buffer(contract, resource, self.camera, posed, self.frame, self.captured)

    def _engine_buffer(self, owner: int, contract, resource) -> int | None:
        name, constant = resource.name, resource.register_class == "b"
        make = partial(self._engine_bytes, contract, resource)
        if not constant and resource.input_type == "structured-buffer" and name in PLACEMENT_BUFFERS:
            layout = contract.structured_buffer(name)
            return self._placed_buffer(owner, (name, layout.byte_size, layout.variables, None, None), make)
        if constant and name not in PLACEMENT_BUFFERS and name not in CAMERA_BUFFERS:
            return self.device.buffer(make(owner))
        return None

    def _skin(self, owner: int, part: MeshPart, step: ChainStep, held: Mapping[str, int]) -> int:
        document, name = part.document, step.program

        def buffer(contract, resource, owner: int = owner) -> bytes | int | None:
            if resource.register_class == "b" and resource.name in CHAIN_BUFFERS:
                return chain_constants(contract.buffer(resource.name), step.constants)
            if resource.register_class == "t" and resource.name in step.buffers:
                return held[step.buffers[resource.name]]
            return self._engine_bytes(contract, resource, owner)
        with _drawing(part):
            target = document.sdf.targets[self.shader_target]
            names = program_names(target)
            program = target.programs[names.index(name)] if name in names else None
            if program is None or [shader.role for shader in program.shaders if shader.present] != ["compute"] \
                    or program.shader("compute").spirv is not None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The master has no Direct3D compute program {name}, which the renderer "
                                       "deforms the mesh with."), {"name": name})
            contract = document.profile.shader_graph_profile.shader_contract(document, program, "compute")
            check_threads(contract, name)
            shader = bytes(program.shader("compute").payload)
            tables, heap, missing = shader_bindings(
                document, {"compute": (contract, shader)}, part.material, self.load_file, buffer, self._sampler,
                read_textures=self._read_textures, heap_start=self._heap_start, made_texture=self._made_texture)
            for index, resource in enumerate(contract.resources):
                if resource.register_class == "u":
                    tables[index] = (*tables[index][:-1], (written_entry(resource, step, held),))
        self._took(heap, missing)
        job, placed = self._kept({"compute_shader": shader, "tables": tables, "heap": heap},
                                 _engine_bound(document, tables, [contract]), buffer, owner)
        job = self.device.bound(job)
        try:
            return self.device.scene.dispatch(job | {
                "tables": self._placed_tables(job["tables"], placed, owner), "groups": (step.groups, 1, 1)})
        except RuntimeError as exc:
            raise PartError([(part, _not_drawn(exc))]) from exc

    def _engine_bytes(self, contract, resource, owner: int) -> bytes | None:
        item = self.objects[owner]
        own = item.engine.get(resource.name)
        if isinstance(own, EngineConstant) and resource.register_class == "b":
            return own.laid_out(contract.buffer(resource.name), item.placement)
        return own if own is not None else engine_buffer(contract, resource, self.camera, item.placement, self.frame,
                                                         self.captured)

    def _placed_by(self, owner: int, name: str) -> bool:
        own = self.objects[owner].engine.get(name)
        return name in PLACEMENT_BUFFERS or isinstance(own, EngineConstant) and own.placed

    def _took(self, heap, missing) -> None:
        self._heap_start = max([self._heap_start, *(entry[0] + 1 for _kind, _dimension, entry in heap)])
        self._missing += missing

    def _meshlet_part(self, owner: int, item: MeshObject, part: MeshPart, groups: dict) -> None:
        bones = item.placement.bones
        catalog = meshlet_catalog_for(self.profile)
        skinning = item.skinned and catalog.skinning is not None
        if item.skinned and bones is None:
            raise ValueError("a skinned meshlet object has no bones")
        posed = item.skinned and not np.array_equal(bones, np.broadcast_to(item.placement.world, bones.shape))
        if posed and not skinning and not catalog.bind_pose_skinned or item.deformation is not None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The preview draws meshlet meshes only undeformed, and skinned ones only in their "
                                   "bind pose."))
        clusters = part.buffer.clusters.get(part.submesh)
        if not clusters or len({cluster.material for cluster in clusters}) != 1:
            raise ValueError("a part of a meshlet mesh is none of its clusters, or of several materials")
        key = (owner, clusters[0].material)
        if key not in groups:
            check_group(len(groups))
            programs = _meshlet_programs(part.document, part.material, self.shader_target, part.meshlet_programs,
                                         skinning)
            if len(programs) > 3:
                _compute_contract(part, programs[3])
            if programs[0] == GBUFFER_PURPOSE:
                self._material_id_program()
            elif programs[0] == "blending":
                if ("blending", ()) not in self.phases:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The renderer draws the material among the blending G-buffer passes, "
                                           "which REasy's light pass catalog of this game build does not record."))
            elif ("forward", ()) not in self.phases:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The master has no G-buffer program for the material: the renderer draws it "
                                       "forward, after the light passes."))
            for program in filter(None, programs[1:3]):
                _program_contracts(part, program)
            groups[key] = ([], programs, [])
        check_clusters(meshlet_catalog_for(self.profile), clusters)
        groups[key][0].append(part)
        groups[key][2].extend(clusters)

    def _material_id_program(self) -> tuple[object, dict]:
        if self._material_ids is None:
            catalog = meshlet_catalog_for(self.profile)
            try:
                program = self._system.program(catalog.target, catalog.material_ids)
                if program is None or {shader.role for shader in program.shaders if shader.present} != {
                        "vertex", "pixel"}:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The game's shader file {path} has no program {name} of a vertex and a "
                                           "pixel shader."),
                        {"path": catalog.material_ids.file, "name": catalog.material_ids.name})
                self._material_ids = program, {
                    stage: shader_contract_for_program(self.profile.sdf_profile, program, stage)
                    for stage in ("vertex", "pixel")}
            except MaterialPreviewError as exc:
                self._material_ids = exc
        if isinstance(self._material_ids, MaterialPreviewError):
            raise self._material_ids
        return self._material_ids

    def _meshlet_buffers(self) -> None:
        groups, catalog = self._meshlet_groups, self.meshlets
        self._geometries: dict[int, int] = {}
        self._meshlet_held: dict[str, int] = {}
        self._tables: tuple[bytes, dict[int, int]] = (b"", {})
        self._transforms: dict[int, int] = {}
        self._transform_groups: list[MeshletGroup] = []
        if not groups:
            return
        for group in groups:
            if group.transform is None:
                continue
            number = next((number for number, first in enumerate(self._transform_groups)
                           if first.transform is group.transform and first.part.material == group.part.material),
                          None)
            if number is None:
                number = len(self._transform_groups)
                self._transform_groups.append(group)
            self._transforms[group.id] = number
        if len(self._transform_groups) > (0 if catalog.deferred is None else catalog.deferred.transform_ids):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The scene draws more meshlet materials transforming their triangles than the "
                                   "renderer dispatches transforms for."))
        indices: dict[int, int] = {}
        geometry: dict[int, int] = {}
        for group in groups:
            blob = group.part.buffer
            if id(blob) not in indices:
                indices[id(blob)] = self._heap_start
                self._geometries[self._heap_start] = self.device.buffer(blob.blob + bytes(-len(blob.blob) % 4))
                self._heap_start += 1
            geometry[group.owner] = indices[id(blob)]
        owners = sorted(geometry)
        # Bindless draws find every material at handle 0, so the tables start past the largest record.
        room = max(bound_record_size(group.part.document, group.part.material) for group in groups)
        tables, offsets = material_tables(catalog, groups, -(-room // HANDLE_STEP) * HANDLE_STEP)
        tables += bytes(-len(tables) % 16)
        blocks, extended = extended_blocks(len(tables), [self.objects[owner].engine.get(EXTENDED) for owner in owners])
        tables += blocks
        culled, order, starts = culled_meshlets(groups, {owner: number for number, owner in enumerate(owners)})
        skinned = [owner for owner in owners if self.objects[owner].skinned and catalog.skinning is not None]
        indices_skinning = [skinned.index(owner) if owner in skinned else -1 for owner in owners]

        def instances() -> bytes:
            worlds = np.array([self.objects[owner].placement.world for owner in owners], np.float64)
            return instance_records(catalog, worlds.reshape(-1, 3, 4), [geometry[owner] for owner in owners],
                                    [offsets[owner] for owner in owners],
                                    [receives_decals(self.objects[owner].decal_receive_mode, self.objects[owner].skinned)
                                     for owner in owners], extended, indices_skinning)
        zeros = bytes(material_culling_size())
        self._culling = (self.device.scene.buffer(zeros, UNORDERED_ACCESS), self.device.buffer(zeros))
        self._tables = (tables, {})
        self._meshlet_held = {
            "Instances": self.device.following(("meshlet instances",), instances),
            "BindlessBuffer": self.device.buffer(tables), "CulledMeshlet": self.device.buffer(culled),
            "SortData": self.device.buffer(order), "SpecialShaderOffset": self.device.buffer(starts),
            "MaterialCulling": self._culling[0], **self._instance_buffers(owners), **self._skinned_buffers(skinned)}

    def _skinned_buffers(self, skinned: Sequence[int]) -> dict[str, int]:
        if not skinned:
            return {}
        counts = [len(self.objects[owner].placement.bones) for owner in skinned]
        starts = np.cumsum([0, *(2 * count for count in counts[:-1])])
        joints = [(int(start), int(start) + count) for start, count in zip(starts, counts)]

        def matrices() -> bytes:
            rows = []
            for owner in skinned:
                placement = self.objects[owner].placement
                previous = placement.bones if placement.previous_bones is None else placement.previous_bones
                rows += [np.asarray(placement.bones, "<f4").reshape(-1, 12),
                         np.asarray(previous, "<f4").reshape(-1, 12)]
            return np.concatenate(rows).tobytes()

        def records() -> bytes:
            boxes = [np.zeros((2, 3)) if self.objects[owner].bounds is None else _world_boxes(
                np.asarray(self.objects[owner].placement.bones, np.float64)[None], [self.objects[owner].bounds])[0]
                for owner in skinned]
            return skinning_records(np.array(boxes), joints)
        return {"SkinningMatrices": self.device.following(("meshlet bones",), matrices),
                "InstancesSkinning": self.device.following(("meshlet skinning",), records)}

    def _instance_buffers(self, owners: Sequence[int]) -> dict[str, int]:
        sizes: dict[str, int] = {}
        for owner in owners:
            for name, data in self.objects[owner].engine.items():
                if name != EXTENDED and isinstance(data, (bytes, bytearray)) \
                        and sizes.setdefault(name, len(data)) != len(data):
                    raise ValueError(f"the meshlet objects give {name} in records of different sizes")
        made = {}
        for name, size in sizes.items():
            data = b"".join(bytes(self.objects[owner].engine.get(name, bytes(size))) for owner in owners)
            made[name] = self.device.buffer(data + bytes(-len(data) % 4))
        return made

    def _held(self, contract, name: str) -> int:
        if name != "BindlessBuffer":
            return self._meshlet_held[name]
        layout = contract.structured_buffer(name)
        element = layout.byte_size if layout is not None and layout.byte_size else 4
        tables, held = self._tables
        if element not in held:
            held[element] = self.device.buffer(tables.ljust(-(-len(tables) // element) * element, b"\0"))
        return held[element]

    def _geometry_heap(self, _contract, handle, owner: int | None = None) -> list[tuple] | None:
        if handle.source is None:
            return None
        if handle.source.name == "BindlessConstantBufferViewTbl" and owner is not None:
            block = self.objects[owner].engine.get(EXTENDED)
            if block is not None and not any(bytes(block)):
                return []
        if handle.source.name != "Instances" or handle.dimension is not None:
            return None
        return self._geometry_entries()

    def _geometry_entries(self) -> list[tuple]:
        return [(RANGE_SRV, BUFFER, (index, 0, 0, buffer)) for index, buffer in self._geometries.items()]

    def _meshlet_job(self, group: MeshletGroup, program, constant: int, targets: int, texture: Callable,
                     draw: tuple[int, int], constant_buffer: Callable | None = None,
                     stage: str | None = None) -> dict:
        held, answered = self._meshlet_held, set()

        def buffer(contract, resource, owner: int = group.owner) -> bytes | int | None:
            if resource.register_class == "b" and resource.name == "RootConstant":
                return root_constant(contract.buffer(resource.name), constant)
            own = None if stage is None else self._deferred.bound(stage, resource.name)
            if resource.register_class == "t" and own is not None:
                return own
            if resource.register_class == "t" and resource.name in held:
                return self._held(contract, resource.name)
            if constant_buffer is not None and resource.register_class == "b":
                data = constant_buffer(resource.name, contract.buffer(resource.name))
                if data is not None:
                    answered.add(resource.name)
                    return data
            return self._engine_bytes(contract, resource, owner)
        geometry = {"vertex_buffers": [], "elements": [], "indices": None, "draw": (*draw, 0, 0, 0)}
        with _drawing(*group.parts):
            job, missing, engine = _program_draw(
                group.part, program, geometry, targets, self.objects[group.owner].stencil_reference, self.load_file,
                buffer, self._sampler, texture, self._read_textures, heap_start=self._heap_start,
                engine_heap=partial(self._geometry_heap, owner=group.owner),
                shared_holders={"BindlessBuffer": self._tables[0]}, streamed=self._streamed,
                resident_file=self._resident, pixel_sampled=self._pixel_sampled(group),
                made_texture=self._made_texture)
        self._took(job["heap"], missing)
        job, _placed = self._kept(job, self._not_held(engine), buffer, group.owner, answered, constant_buffer)
        return self.device.bound(job)

    def _not_held(self, engine: Iterable[tuple]) -> list[tuple]:
        return [entry for entry in engine if entry[2].register_class != "t" or entry[2].name not in self._meshlet_held]

    def _system_table(self, stage: str, contract, resource, textures: Mapping[str, tuple] = MappingProxyType({}),
                      written: Mapping[str, tuple] = MappingProxyType({})) -> tuple:
        kind, name = resource.register_class, resource.name
        place = (resource.bind_point, resource.register_space, resource.bind_count)
        visibility, held = SHADER_VISIBILITY[stage], self._meshlet_held
        if kind == "s":
            return (RANGE_SAMPLER, *place, 0, visibility, tuple(
                (index, *self._sampler(name)) for index in range(max(1, resource.bind_count))))
        if kind == "b":
            layout = contract.buffer(name)
            transforms = None if self._deferred is None else self._transforms
            data = self.device.buffer(replace_table(layout, len(self._meshlet_groups) + 1, transforms)) \
                if name == "ShaderReplaceTable" else self.device.following(
                    ("meshlet", name, layout.byte_size, layout.variables),
                    partial(self._engine_bytes, contract, resource, self._meshlet_groups[0].owner))
            return RANGE_CBV, *place, 0, visibility, ((0, data),)
        if kind == "t" and resource.input_type == "texture":
            entry = textures.get(name)
            return (RANGE_SRV, *place, VIEW_DIMENSIONS[resource.dimension], visibility,
                    () if entry is None else (entry,))
        if kind == "t":
            stride = (contract.structured_buffer(name).byte_size
                      if resource.input_type == "structured-buffer" else 0)
            return (RANGE_SRV, *place, BUFFER, visibility,
                    ((0, 0, stride, self._held(contract, name)),) if name in held else ())
        dimension = resource.dimension if resource.input_type.startswith("rw-typed") else "buffer"
        entry = written.get(name)
        return RANGE_UAV, *place, _UAV_DIMENSIONS.get(dimension, 1), visibility, () if entry is None else (entry,)

    def _material_id_job(self) -> dict:
        program, contracts = self._material_id_program()
        catalog, visible = self.meshlets, self._visible[0]
        textures = {"ReadonlyDepth": (0, DEPTH_VIEWS[self.depth_format][1], self.depth),
                    "VisibilityBuffer": (0, catalog.visibility_format, visible)}
        written = {"RWMaterialCulling": (0, 0, 0, self._culling[0])}
        tables = []
        for stage, contract in contracts.items():
            for resource in contract.resources:
                given = written
                if self._deferred is not None and resource.register_class == "u":
                    own = self._deferred.entry("material_ids", resource)
                    given = written if own is None else {resource.name: own}
                tables.append(self._system_table(stage, contract, resource, textures, given))
        return {"vertex_shader": bytes(program.shader("vertex").payload),
                "pixel_shader": bytes(program.shader("pixel").payload), "vertex_buffers": [], "elements": [],
                "indices": None, "draw": (_SCREEN_TRIANGLE, 1, 0, 0, 0), "tables": tables,
                # Each cluster's chunk is read from the blob its instance record names.
                "heap": self._geometry_entries(),
                **program_states(program, 1, 0)}

    def _meshlet_steps(self) -> list[int]:
        groups = self._meshlet_groups
        shaded = [group for group in groups if group.purpose == GBUFFER_PURPOSE]
        if not shaded:
            return []
        catalog, scene, (visible, ids) = self.meshlets, self.device.scene, self._visible
        deferred = self._deferred
        _resource, id_depth, id_view = catalog.material_id_formats
        steps = [scene.clear(catalog.visibility_format, visible), scene.clear(id_depth, ids),
                 self.device.copy(*self._culling), *(() if deferred is None else deferred.starts())]
        gbuffer = self._engine_texture(GBUFFER_PURPOSE)

        def texture(contract, resource) -> tuple | None:
            if resource.name == "VisibilityBuffer":
                return 0, catalog.visibility_format, visible
            if resource.name == "MaterialID":
                return 0, id_view, ids
            own = None if deferred is None else deferred.entry("shade", resource)
            if own is not None:
                return own
            return gbuffer(contract, resource)
        targets = [(form, self.targets[target]) for target, form in sorted(self.target_formats.items())]
        drawn = []
        for number, group in enumerate(groups):
            if group.shade is None:
                continue
            with _drawing(*group.parts):
                raster = self._meshlet_job(group, group.raster, number, 1, gbuffer,
                                           (catalog.vertices, len(group.clusters)))
                shade = self._meshlet_job(group, group.shade, group.id, len(targets), texture,
                                          (catalog.tile_vertices, 1),
                                          stage=None if group.transform is None else "shade")
                drawn.append((group.owner, self._step(raster, [(catalog.visibility_format, visible)], self.depth),
                              self._step(shade, targets, ids, id_depth)))
        try:
            material_ids = self._step(self._material_id_job(), self.scratch([targets[0][0]]), ids, id_depth)
            gathered = [] if deferred is None else deferred.chain(partial(self._system_table, "compute"))
        except MaterialPreviewError as exc:
            raise PartError([(part, exc) for group in shaded for part in group.parts]) from exc
        transformed = [(group.owner, self._transform_step(group)) for group in self._transform_groups
                       if group.shade is not None]
        self._step_owners.update((step, owner) for owner, *made in drawn for step in made)
        self._owner_table = None
        return [*steps, *(raster for _owner, raster, _shade in drawn), material_ids, *gathered,
                *(step for _owner, step in transformed), *(shade for _owner, _raster, shade in drawn)]

    def _streamed(self, path: str) -> bool:
        return self.load_file(streamed_key(path)) is not None

    def _resident(self, path: str) -> bytes | None:
        return self.load_file(resident_key(path) if self._streamed(path) else path)

    def _pixel_sampled(self, group: MeshletGroup) -> frozenset[str]:
        part = group.part
        pixel = []
        for program in (group.raster, group.shade, group.transform):
            if program is not None and any(shader.role == "pixel" and shader.present for shader in program.shaders):
                contract = part.document.profile.shader_graph_profile.shader_contract(part.document, program, "pixel")
                pixel.append((contract, bytes(program.shader("pixel").payload)))
        return pixel_sampled_textures(part.document, part.material, pixel)

    def _transform_step(self, group: MeshletGroup) -> int:
        part, program, deferred = group.part, group.transform, self._deferred
        number, document, held = self._transforms[group.id], part.document, self._meshlet_held

        def buffer(contract, resource, owner: int = group.owner) -> bytes | int | None:
            if resource.register_class == "b" and resource.name == "RootConstant":
                return root_constant(contract.buffer(resource.name), number)
            own = deferred.bound("transform", resource.name)
            if resource.register_class == "t" and own is not None:
                return own
            if resource.register_class == "t" and resource.name in held:
                return self._held(contract, resource.name)
            return self._engine_bytes(contract, resource, owner)
        with _drawing(*group.parts):
            contract = _compute_contract(part, program)
            shader = bytes(program.shader("compute").payload)
            tables, heap, missing = shader_bindings(
                document, {"compute": (contract, shader)}, part.material, self.load_file, buffer, self._sampler,
                engine_texture=self._engine_texture(GBUFFER_PURPOSE), read_textures=self._read_textures,
                heap_start=self._heap_start, engine_heap=partial(self._geometry_heap, owner=group.owner),
                streamed=self._streamed, resident_file=self._resident, pixel_sampled=self._pixel_sampled(group),
                shared_holders={"BindlessBuffer": self._tables[0]}, made_texture=self._made_texture)
            for index, resource in enumerate(contract.resources):
                if resource.register_class == "u":
                    entry = deferred.entry("transform", resource)
                    if entry is None:
                        raise PartError([(part, MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The compute program {program} writes {name}, which no capture shows "
                                               "the renderer binding."), {"program": program.name,
                                                                          "name": resource.name}))])
                    tables[index] = (*tables[index][:-1], (entry,))
        self._took(heap, missing)
        job, _placed = self._kept({"compute_shader": shader, "tables": tables, "heap": heap},
                                  self._not_held(_engine_bound(document, tables, [contract])), buffer, group.owner)
        try:
            return self.device.scene.dispatch(self.device.bound(job) | {"indirect": deferred.arguments(number)})
        except RuntimeError as exc:
            raise PartError([(part, _not_drawn(exc))]) from exc

    def _draw(self, owner: int, part: MeshPart, purpose: str, program, route: str) -> _Draw:
        document = part.document
        if purpose == "forward" and (purpose, ()) not in self.phases:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The master has no G-buffer program for the material: the renderer draws it "
                                   "forward, after the light passes."))
        if purpose != "forward" and not _written(document, program) <= set(range(len(_bound_targets(
                self.catalog, purpose == "blending")))):
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The pass writes a render target the renderer does not bind for it."))
        prepass = depth_prepass_program(document, self.shader_target, part.material.material_flags, route)
        return _Draw(owner, part, purpose, program, None if prepass is None else
                     document.sdf.targets[self.shader_target].programs[prepass])

    @property
    def missing_textures(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self._missing))

    def render_texture(self, path: str, form: int, size: Sequence[int]) -> int:
        key = resource_path_key(path)
        if key not in self.render_textures:
            self.render_textures[key] = ("texture2d", form, self.device.texture(form, size, RENDER_TARGET), tuple(size))
        found = self.render_textures[key]
        if found[1] != form or found[3] != tuple(size):
            raise ValueError(f"the render texture {path} is made {found[1]} of {found[3]}, not {form} of {size}")
        return found[2]

    def _made_texture(self, path: str) -> tuple[str, int, int] | None:
        found = self.render_textures.get(resource_path_key(path))
        return None if found is None else found[:3]

    def _sampler(self, name: str) -> tuple:
        state = self.samplers.states.get(name)
        if state is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy's catalog of this game build does not record the sampler {name}."),
                {"name": name})
        return state.descriptor()

    def _engine_texture(self, purpose: str) -> Callable:
        if purpose in self._engine_textures:
            return self._engine_textures[purpose]

        def texture(_contract, resource) -> tuple | None:
            path = self.catalog.engine_texture(purpose, resource.name)
            if path is None:
                return None
            data = self.load_file(path)
            entry = _texture_entry(0, data, resource.dimension, self._read_textures) if data else None
            if entry is None:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "The game's texture {path} was not found, or is not the shape the pass "
                                       "reads."), {"path": path})
            return entry
        self._engine_textures[purpose] = texture
        return texture

    def _job(self, item: _Draw, program, targets: int, texture: Callable | None = None,
             constant_buffer: Callable | None = None) -> dict:
        owner = item.owner
        key = (id(item.part), id(program), targets, self.objects[owner].stencil_reference, texture, constant_buffer)
        if key not in self._jobs:
            self._jobs[key] = self._made_job(item, program, targets, texture, constant_buffer)
        job, placed = self._jobs[key]
        job = job | {"tables": self._placed_tables(job["tables"], placed, owner)}
        if self.objects[owner].deformation is None:
            return job
        destination = self._chains.destination(owner)
        return job | {"vertex_buffers": [(destination, *entry[1:]) if entry[0] is None else entry
                                         for entry in job["vertex_buffers"]]}

    def _placed_tables(self, tables: Sequence[tuple], placed: Iterable[tuple], owner: int) -> list[tuple]:
        tables = list(tables)
        for index, entry, kind, make in placed:
            tables[index] = (*tables[index][:-1], ((*entry, self._placed_buffer(owner, kind, make)),))
        return tables

    def _placed_buffer(self, owner: int, kind: tuple, make: Callable[[int], bytes]) -> int:
        own = self._placed[owner]
        if kind not in own:
            own[kind] = (self.device.scene.buffer(make(owner), 0), make)
        return own[kind][0]

    def _made_job(self, item: _Draw, program, targets: int, texture: Callable | None,
                  constant_buffer: Callable | None) -> tuple[dict, list[tuple]]:
        answered = set()

        def buffer(contract, resource, owner: int = item.owner) -> bytes | None:
            if constant_buffer is not None and resource.register_class == "b":
                data = constant_buffer(resource.name, contract.buffer(resource.name))
                if data is not None:
                    answered.add(resource.name)
                    return data
            return self._engine_bytes(contract, resource, owner)
        drawn = self.objects[item.owner]
        with _drawing(item.part):
            job, missing, engine = _program_draw(
                item.part, program, self._geometry[id(item.part), id(program)], targets, drawn.stencil_reference,
                self.load_file, buffer, self._sampler, texture, self._read_textures, heap_start=self._heap_start,
                streamed=self._streamed, resident_file=self._resident, made_texture=self._made_texture)
        self._took(job["heap"], missing)
        job, placed = self._kept(job, engine, buffer, item.owner, answered, constant_buffer)
        return self.device.held(job), placed

    def _kept(self, job: dict, engine: Iterable[tuple], buffer: Callable, owner: int,
              answered: Collection[str] = (), constant_buffer: Callable | None = None) -> tuple[dict, list[tuple]]:
        tables, kept = job["tables"], []
        for index, contract, resource in engine:
            name = resource.name
            placed = self._placed_by(owner, name)
            if name not in answered and not placed and name not in CAMERA_BUFFERS:
                continue
            *entry, bound = tables[index][-1][0]
            constant = resource.register_class == "b"
            layout = contract.buffer(name) if constant else contract.structured_buffer(name)

            def make(owner: int, contract=contract, resource=resource, layout=layout, bound=bound,
                     constant=constant) -> bytes:
                data = buffer(contract, resource, owner)
                return _heap_fields_kept(layout, data, bound) if constant else data
            # Buffers of one layout, origin and heap fields hold the same bytes.
            kind = (name, layout.byte_size, layout.variables,
                    _heap_fields_kept(layout, bytes(len(bound)), bound) if constant else None,
                    constant_buffer if name in answered else None)
            if name not in answered and placed:
                kept.append((index, tuple(entry), kind, make))
                found = None
            else:
                found = self.device.following(kind, partial(make, owner))
            tables[index] = (*tables[index][:-1], ((*entry, found),))
        return job, kept

    def _step(self, job: dict, targets: Sequence[tuple[int, int]], depth: int | None,
              depth_format: int | None = None) -> int:
        width, height = self.size
        try:
            return self.device.scene.draw(job | {
                "size": (width, height), "viewport": (0.0, 0.0, float(width), float(height), 0.0, 1.0),
                "scissor": (0, 0, width, height), "targets": list(targets),
                "depth": None if depth is None else (depth_format or self.depth_format, depth)})
        except RuntimeError as exc:
            raise _not_drawn(exc) from exc

    def scratch(self, forms: Iterable[int]) -> list[tuple[int, int]]:
        found = []
        for slot, form in enumerate(forms):
            if (form, slot) not in self._scratch:
                self._scratch[form, slot] = self.device.texture(form, self.size, RENDER_TARGET)
            found.append((form, self._scratch[form, slot]))
        return found

    @property
    def coverage(self) -> list[tuple[int, str]]:
        """A texel compared with 0 by ``comparison`` marks where draws leaving no depth drew over the far plane."""
        return [(texture, comparison) for texture, _start, comparison, _source in self._coverage.values()]

    def _covering(self, start: float, function: int, comparison: str) -> int:
        if (start, function) not in self._coverage:
            scene, (width, height) = self.device.scene, self.size
            planes = (np.full(width * height, start, "<f4").tobytes()
                      + bytes(width * height * DEPTH_PLANES[self.depth_format][1]))
            shape = (self.depth_resource_format, width, height, 1, 1, TEXTURE_2D, DEPTH_STENCIL)
            texture, source = scene.texture(*shape, planes), scene.texture(*shape, planes)
            self._coverage[start, function] = (texture, scene.copy(texture, source), comparison, source)
        return self._coverage[start, function][0]

    def _steps(self, item: _Draw, program, job: dict, targets: Sequence[tuple[int, int]],
               bound: bool = True) -> list[int]:
        scene = self.device.scene
        reads_depth = any(entry[2:] == (self.depth_before,)
                          for table in job["tables"] if table[0] == RANGE_SRV for entry in table[-1])
        steps = [scene.copy(self.depth_before, self.depth)] if reads_depth else []
        steps.append(self._step(job, targets, self.depth if bound else None))
        state = program.depth_stencil_state
        coverage = _COVERAGE.get(state.depth_function_id if bound and state.depth_enable else COMPARE_ALWAYS)
        if (coverage is not None and item.prepass is None
                and not (bound and state.depth_enable and state.depth_write_mask_id)):
            start, function, comparison = coverage
            texture = self._covering(start, function, comparison)
            if DEPTH_PLANES[self.depth_format][1]:
                steps.append(scene.copy(texture, self.depth, 1, 1))
            tested = job["depth_stencil"][3:] if bound else (0, *job["depth_stencil"][4:])
            steps.append(self._step(job | {"depth_stencil": (1, 1, function, *tested)},
                                    self.scratch(form for form, _texture in targets), texture))
        self._step_owners.update(dict.fromkeys(steps, item.owner))
        self._owner_table = None
        return steps

    def begin(self) -> None:
        self.device.refresh()
        self.device.pending += [start for _texture, start, _comparison, _source in self._coverage.values()]
        if self._stale:
            seen = self._seen()
            run = sorted(owner for owner in self._stale if seen[owner])
            for owner in run:
                if self.objects[owner].deformation is not None:
                    self.device.pending += self._chains.steps(owner)
                self._stale.discard(owner)
            drawn_into = {resource_path_key(self.objects[owner].wrinkle.render_target) for owner in run
                          if self.objects[owner].wrinkle is not None}
            for owner, item in enumerate(self.objects):
                if item.wrinkle is not None and resource_path_key(item.wrinkle.render_target) in drawn_into:
                    self.device.pending += self._wrinkles.steps(owner)

    def finish(self, read: Sequence[int] = ()) -> list[bytes]:
        self.device.pending[:] = self._seen_steps(self._forward_sorted(self.device.pending))
        try:
            return self.device.flush(read)
        except RuntimeError as exc:
            raise _not_drawn(exc) from exc

    def _seen(self) -> np.ndarray:
        """The last entry, True, answers owner -1 (steps of no object)."""
        self._box_moved()
        if self._corners is None:
            bounded = np.flatnonzero(self._bounded)
            corners = self._boxes[bounded][:, BOX_CORNERS, range(3)]
            corners = np.concatenate([corners, np.ones((*corners.shape[:2], 1))], axis=2)
            self._corners = bounded, corners, np.abs(corners)
        bounded, corners, magnitudes = self._corners
        seen = np.ones(len(self._boxes) + 1, bool)
        if not len(bounded):
            return seen
        view_projection = scene_values(self.camera)["viewProjMat"]
        key = view_projection.tobytes()
        if self._culled is not None and self._culled[0] == key:
            return self._culled[1]
        x, y, _z, w = np.moveaxis(corners @ view_projection, 2, 0)
        margin = _BOUNDS_MARGIN * (magnitudes @ np.abs(view_projection)).sum(axis=2)
        beyond = [(side > margin).all(axis=1) for side in (x - w, -x - w, y - w, -y - w, -w)]
        seen[bounded] = ~np.any(beyond, axis=0)
        seen.setflags(write=False)
        self._culled = key, seen
        return seen

    def _box_moved(self) -> None:
        if self._moved:
            self._corners = self._culled = None
        moved = defaultdict(list)
        for owner in self._moved:
            item = self.objects[owner]
            transforms = item.placement.bones if item.skinned else item.placement.world[None]
            self._bounded[owner] = item.bounds is not None and transforms is not None
            if self._bounded[owner]:
                moved[np.shape(transforms)].append((owner, transforms))
        for group in moved.values():
            owners = [owner for owner, _transforms in group]
            self._boxes[owners] = _world_boxes(np.array([transforms for _owner, transforms in group], np.float64),
                                               [self.objects[owner].bounds for owner in owners])
        self._moved.clear()

    def _seen_steps(self, steps: list[int]) -> list[int]:
        seen = self._seen()
        if seen.all():
            return steps
        if self._owner_table is None:
            self._owner_table = np.full(max(self._step_owners, default=-1) + 1, -1)
            self._owner_table[list(self._step_owners)] = list(self._step_owners.values())
        ids = np.asarray(steps, np.int64)
        known = ids < len(self._owner_table)
        owners = np.full(len(ids), -1)
        owners[known] = self._owner_table[ids[known]]
        return ids[seen[owners]].tolist()

    def gbuffer(self, apart: bool = False) -> list[int]:
        if apart in self._gbuffer:
            return self._gbuffer[apart]
        scene = self.device.scene
        unblended = _bound_targets(self.catalog, False)
        drawn = [item for item in self._draws if item.purpose == GBUFFER_PURPOSE
                 or item.purpose == "blending" and ("blending", ()) not in self.phases]
        textures = {row.target: (row.format, self.targets[row.target]) for row in unblended}
        steps = [scene.clear(*target) for target in textures.values()] + [scene.clear(self.depth_format, self.depth)]
        for item in self._draws:
            if item.prepass is not None and item.purpose != "forward":
                # The pre-pass only writes depth; the G-buffer pass draws its targets again.
                job = self._job(item, item.prepass, len(unblended), self._engine_texture(PREPASS_PURPOSE))
                steps += self._steps(item, item.prepass, job, self.scratch(row.format for row in unblended))
        # Meshlets draw over the pre-pass depth, so they go after the pre-passes.
        steps += self._meshlet_steps()
        for blending in (False, True):
            phase = [item for item in drawn if (item.purpose == "blending") == blending]
            if not phase:
                continue
            bound = _bound_targets(self.catalog, blending)
            for row in bound if blending else ():
                if not row.over_unblended:
                    if apart:
                        continue
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The renderer fills target {target} for blending passes from the others' "
                                           "in between, which the preview does not do."), {"target": row.target})
                if textures.get(row.target, (None,))[0] != row.format:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "REasy's catalog of this game build gives blending passes target {target} "
                                           "in another format."), {"target": row.target})
            onto = [textures.get(row.target) for row in bound]
            if not all(row.over_unblended for row in bound if blending):
                onto = [given if row.over_unblended else spare
                        for row, given, spare in zip(bound, onto, self.scratch(row.format for row in bound))]
            for item in phase:
                job = self._job(item, item.program, len(bound), self._engine_texture(GBUFFER_PURPOSE))
                steps += self._steps(item, item.program, job, onto)
        self._gbuffer[apart] = steps
        return steps

    def draw_phase(self, phase: PhaseKey, targets: Sequence[tuple[int, int | None]], bound: bool,
                   texture: Callable[[str, object, int], tuple | None],
                   constant_buffer: Callable[[str, object], bytes | None],
                   split: bool = False) -> tuple[list[int], list[int]]:
        """``texture(name, resource, depth)`` returns None for a null view; ``constant_buffer(name, layout)`` is called
        again every frame."""
        purpose = phase[0]
        drawn = [item for item in self._draws if item.purpose == purpose] if purpose in PHASES else self._named[phase]
        if purpose == "blending" and [form for form, _texture in targets] != [
                row.format for row in _bound_targets(self.catalog, True)]:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy's catalogs of this game build give blending passes other targets."))
        count = next((slot for slot, (form, _texture) in enumerate(targets) if not form), len(targets))
        unread = self.scratch(form for form, _texture in targets[:count])
        onto = [given if given[1] is not None else spare for given, spare in zip(targets, unread)]
        steps, blended = [], []

        def read(_contract, resource) -> tuple | None:
            return texture(resource.name, resource, self.depth_before)
        for item in drawn:
            if item.prepass is not None and purpose == "forward":
                job = self._job(item, item.prepass, count, self._engine_texture(PREPASS_PURPOSE), constant_buffer)
                steps += self._steps(item, item.prepass, job, unread)
        order = self._forward_order if purpose == "forward" else None
        for blending in (None,) if order is None else (False, True):
            for number, group in enumerate(self._meshlet_groups):
                program = group.raster
                if group.purpose != purpose or blending not in (None, _blends_first(program)):
                    continue
                with _drawing(*group.parts):
                    if not _written(group.part.document, program) <= set(range(len(targets))):
                        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The pass writes a render target the renderer does not bind for it."))
                    job = self._meshlet_job(group, program, number, count, read,
                                            (self.meshlets.vertices, len(group.clusters)), constant_buffer)
                    made = self._steps(_Draw(group.owner, group.part, purpose, program, None), program, job, onto,
                                       bound)
                (blended if split and _blends_first(program) else steps).extend(made)
            run = []
            for item in drawn:
                if blending not in (None, _blends_first(item.program)):
                    continue
                if not _written(item.part.document, item.program) <= set(range(len(targets))):
                    raise PartError([(item.part, MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The pass writes a render target the renderer does not bind for it.")))])
                job = self._job(item, item.program, count, read, constant_buffer)
                made = self._steps(item, item.program, job, onto, bound)
                (blended if split and _blends_first(item.program) else steps).extend(made)
                if made:
                    run.append((_part_box(item.part), item.owner, tuple(made), _priority_bias(item.part.material)))
            rule = None if order is None else order.blended if blending else order.unblended
            if rule in SORTED_ORDERS and len(run) > 1:
                self._forward_runs[run[0][2][0]] = (SORTED_ORDERS[rule], run)
        return steps, blended

    def _forward_sorted(self, steps: list[int]) -> list[int]:
        if not self._forward_runs:
            return steps
        at = {step: index for index, step in enumerate(steps)}
        rotation, translation = self.camera.view[:, :3], self.camera.view[:, 3]
        camera = -rotation.T @ translation
        steps = list(steps)
        for first, (by_object, run) in self._forward_runs.items():
            if first not in at:
                continue
            flat = [step for _box, _owner, made, _bias in run for step in made]
            places = sorted(at.get(step, -1) for step in flat)
            start = places[0]
            if start < 0 or places[-1] - start != len(flat) - 1:
                continue
            if by_object:
                keys, objects, firsts = [], {}, {}
                for number, (_box, owner, _made, bias) in enumerate(run):
                    if owner not in objects:
                        objects[owner] = transparent_key(self.camera, _sort_point(self.objects[owner], camera))
                    keys.append(min(max(objects[owner] + bias, 0), _KEY_MAX))
                    firsts.setdefault(owner, number)
                order = sorted(range(len(run)), key=lambda number: (keys[number], firsts[run[number][1]], number))
                steps[start:start + len(flat)] = [step for number in order for step in run[number][2]]
                continue
            distances = []
            for box, owner, _made, _bias in run:
                item = self.objects[owner]
                skinned = item.skinned and item.placement.bones is not None
                transforms = item.placement.bones if skinned else item.placement.world[None]
                world = _world_boxes(np.asarray(transforms, np.float64)[None], [MeshBounds(box)])[0]
                distances.append(float(np.linalg.norm((world[0] + world[1]) / 2 - camera)))
            order = sorted(range(len(run)), key=lambda number: distances[number])
            steps[start:start + len(flat)] = [step for number in order for step in run[number][2]]
        return steps


def _check_still(item: MeshObject) -> None:
    placement = item.placement
    if placement.previous_world is not None or placement.previous_bones is not None:
        raise ValueError("a deformed object's placement tells a pose of a frame before")


def _not_drawn(exc: RuntimeError) -> MaterialPreviewError:
    return MaterialPreviewError(QT_TRANSLATE_NOOP(
        "MaterialPreview", "Direct3D could not draw the pass: {reason}"), {"reason": str(exc)})


def render_gbuffer(parts: Sequence[MeshPart], load_file: Callable[[str], bytes | None], camera: Camera,
                   placement: Placement, *, skinned: bool, shader_target: int, stencil_reference: int = 0,
                   hardware: bool = False) -> GBufferImage:
    try:
        scene = MeshScene([MeshObject(parts, placement, skinned, stencil_reference)], load_file, camera,
                          shader_target=shader_target, hardware=hardware)
        scene.begin()
        scene.device.pending += scene.gbuffer(apart=True)
    except PartError as exc:
        raise exc.refusals[0][1] from exc
    formats = scene.target_formats
    *targets, depth = scene.finish([*(scene.targets[target] for target in sorted(formats)), scene.depth])
    return GBufferImage(scene.size, tuple(
        GBufferTarget(target, formats[target], texels) for target, texels in zip(sorted(formats), targets)),
        scene.depth_format, depth, scene.missing_textures)


__all__ = ["DEPTH_PLANES", "DEPTH_VIEWS", "GBufferImage", "GBufferTarget", "MeshBounds", "MeshObject", "MeshPart",
           "MeshScene", "ObjectBox", "PHASES", "PartError", "PhaseKey", "draw_purpose", "mesh_bounds", "object_box",
           "part_draws", "render_gbuffer", "transparent_key"]
