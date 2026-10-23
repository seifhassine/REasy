"""See docs/internals/mesh-preview.md."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from itertools import product

import numpy as np
lazy from PySide6.QtCore import QT_TRANSLATE_NOOP

lazy from file_handlers.mdf.mdf_file import transparent_priority_bias
from file_handlers.sdf.contracts import shader_contract_for_program
lazy from file_handlers.sdf.models import program_names
from file_handlers.sdf.sdf_file import (
    COMPARE_ALWAYS, COMPARE_EQUAL, COMPARE_GREATER, COMPARE_GREATER_EQUAL, COMPARE_LESS, COMPARE_LESS_EQUAL,
    COMPARE_NOT_EQUAL,
)

lazy from .engine_buffers import (
    CAMERA_BUFFERS, PLACEMENT_BUFFERS, Camera, EngineConstant, InstanceInput, Placement, engine_buffer, scene_values,
)
lazy from .draw_order import SORTED_ORDERS, draw_order_for
lazy from .frame_constants import frame_constants_for
lazy from .material_preview import (
    RANGE_CBV, _MATERIAL_HEAP_START, RANGE_SAMPLER, RANGE_SRV, RANGE_UAV, _UAV_DIMENSIONS, VIEW_DIMENSIONS,
    SHADER_VISIBILITY,
    BufferReads, Following, MaterialPreviewError, MaterialRecord, PreviewMaterial, SharedMaterials, TextureReads,
    _heap_fields_kept, _texture_entry, bound_record_size, gbuffer_route, material_record, resident_key, shader_bindings,
    streamed_key,
)
from file_handlers.mesh.mesh_file import MeshFile, VertexBufferType
from utils.native_build import ensure_fastmesh
lazy from file_handlers.rsz.scene_joints import times
lazy from utils.resource_file_utils import resource_path_key

from .mesh_draw import MeshBuffer, SubmeshRange, program_states, submesh_geometry
lazy from .meshlet_deferred import DeferredPasses
from .meshlets import (
    EXTENDED, ROUTE as MESHLET_ROUTE, LodTable, MeshletBuffer, MeshletGroup, MeshletLods, MeshletPrograms,
    RasterBatches, check_clusters, check_culled, check_shader_ids, check_transform_ids, culled_meshlets,
    extended_blocks, instance_records, material_culling_size, material_tables, material_views, meshlet_catalog_for,
    meshlet_lods, meshlet_programs, named_meshlet_programs, receives_decals, record_handles, replace_table,
    root_constant, skinning_records, special_offsets,
)
lazy from .pass_choice import depth_prepass_program, has_scene_program, own_passes, own_program, scene_program
lazy from .pretransform import CHAIN_BUFFERS, ROUTE, ChainStep, Deformation, Destination
lazy from .pretransform import constant_buffer as chain_constants
lazy from .pretransform_passes import ChainPasses, SystemPrograms, check_threads, written_entry
lazy from .wrinkle import WrinkleObject, object_scale, proxy_bones, render_target_form, wrinkle_catalog_for
from .wrinkle_passes import WrinklePasses
lazy from .resident import BUFFER, DEPTH_STENCIL, RENDER_TARGET, TEXTURE_2D, UNORDERED_ACCESS, Resident
lazy from .sampler_states import sampler_catalog_for
lazy from .surface_meaning import GBUFFER_PURPOSE, PREPASS_PURPOSE, TargetFormat, surface_output_catalog_for

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
_fastmesh = ensure_fastmesh()
_SCREEN_TRIANGLE = 3


@dataclass(frozen=True, slots=True)
class MeshPart:
    """``texel_density`` (``uv_density``) lets a scene read its streamed textures only from the mips its camera
    samples; None reads them whole."""
    document: object
    material: PreviewMaterial
    buffer: MeshBuffer | MeshletBuffer
    submesh: SubmeshRange
    route: str | None = None
    meshlet_programs: MeshletPrograms | None = None
    texel_density: float | None = None
    # The mesh LOD of a meshlet part's clusters, for an object with ``lods``.
    level: int = 0


# A streamed texture keeps one mip finer than the one its nearest drawing samples.
_SIDE_MARGIN = 2.0
# Texture files read at a time to stream finer mips in.
_STREAM_READS = 4


def uv_density(positions: np.ndarray, uv_sets: Sequence[np.ndarray], indices: np.ndarray) -> float | None:
    """UV units per local unit of length along the triangles' least stretched direction in the least stretched UV
    set, the least of the triangles'; None without UVs or area. See docs/internals/mesh-preview.md, Texture
    streaming."""
    if not uv_sets or len(indices) < 3:
        return None
    corners = np.asarray(indices[:len(indices) - len(indices) % 3], np.int64).reshape(-1, 3)
    points = np.asarray(positions, np.float64)[corners]
    one, two = points[:, 1] - points[:, 0], points[:, 2] - points[:, 0]
    a, b, c = (one * one).sum(1), (one * two).sum(1), (two * two).sum(1)
    gram = a * c - b * b
    held = gram > 0
    if not held.any():
        return None
    gram, a, b, c, corners = gram[held], a[held], b[held], c[held], corners[held]
    least = np.inf
    for uvs in uv_sets:
        maps = np.asarray(uvs, np.float64)[corners]
        first, second = maps[:, 1] - maps[:, 0], maps[:, 2] - maps[:, 0]
        # The stretch's squares are the eigenvalues of D G^-1 D^T, G the edges' Gram matrix and D the UV edges.
        trace = (c * (first * first).sum(1) - 2 * b * (first * second).sum(1) + a * (second * second).sum(1)) / gram
        determinant = (first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]) ** 2 / gram
        squared = (trace - np.sqrt(np.maximum(0.0, trace * trace - 4 * determinant))) / 2
        least = min(least, float(np.sqrt(max(0.0, squared.min()))))
    return least


@dataclass(frozen=True, slots=True)
class MeshObject:
    """``skins`` are undrawn parts whose masters skin the rest. An object with ``lods`` holds its meshlet parts of
    every LOD and frames draw the LOD instance culling picks; without, every part draws."""
    parts: Sequence[MeshPart]
    placement: Placement
    skinned: bool
    stencil_reference: int = 0
    bounds: MeshBounds | None = None
    deformation: Deformation | None = None
    skins: Sequence[MeshPart] = ()
    engine: Mapping[str, bytes | EngineConstant] = frozendict()
    decal_receive_mode: int = 0
    box: ObjectBox | None = None
    transparent_bias: float = 0.0
    wrinkle: WrinkleObject | None = None
    lods: MeshletLods | None = None

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
        count, least, most = payload.position_bounds()
        if bones is not None:
            sums.append(_weight_sums(payload, count, bones))
            if sums[-1] is None:
                return None
        if count:
            lowest.append(least)
            highest.append(most)
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


def _sort_centre(item: MeshObject) -> np.ndarray:
    found, bones = item.box, item.placement.bones
    if item.skinned and bones is not None and found is not None and found.bones is not None:
        rows = np.asarray(bones, np.float64)[:len(found.bones)]
        corners = np.einsum("rij,rkj->rki", rows[..., :3], found.bones[:len(rows)]) + rows[:, None, :, 3]
    else:
        world = np.asarray(item.placement.world, np.float64).reshape(3, 4)
        corners = _object_box(item)[BOX_CORNERS, range(3)] @ world[:, :3].T + world[:, 3]
    corners = corners.reshape(-1, 3).astype(np.float32)
    return ((corners.min(axis=0) + corners.max(axis=0)) * np.float32(0.5)).astype(np.float32)


def _sort_points(centres: np.ndarray, biases: np.ndarray, eye: np.ndarray) -> np.ndarray:
    """Centres moved away from the eye by their transparent bias, in float32."""
    biased = biases != 0
    if not biased.any():
        return centres
    moved = (centres + (centres - np.asarray(eye, np.float32)) * biases[:, None]).astype(np.float32)
    return np.where(biased[:, None], moved, centres)


_KEY_SCALE = 4294967295.0
_KEY_MAX = 0xFFFFFFFF


def transparent_keys(camera: Camera, points: np.ndarray) -> np.ndarray:
    """Each point's key, ascending as the game sorts. See docs/internals/mesh-preview.md."""
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
    x, y, z = np.asarray(points, f32).reshape(-1, 3).T[:, :, None]
    clip = ((x * matrix[0]).astype(f32) + (y * matrix[1]).astype(f32)).astype(f32)
    clip = ((clip + (z * matrix[2]).astype(f32)).astype(f32) + matrix[3]).astype(f32)
    with np.errstate(divide="ignore", invalid="ignore"):
        share = np.clip((clip[:, 2] / clip[:, 3]).astype(f32), f32(0), f32(1))
        keys = _KEY_MAX - (share.astype(np.float64) * _KEY_SCALE).astype(np.int64)
    return np.where((clip[:, 2] > 0) & (clip[:, 3] > 0), keys, _KEY_MAX)


def _priority_bias(material) -> int:
    return transparent_priority_bias(getattr(material, "material_flags", 0) or 0)


def boxes_seen(boxes: np.ndarray, view_projection: np.ndarray) -> np.ndarray:
    """Whether each world box (n x 2 x 3) may be in the view of ``view_projection`` (for row vectors): frames cull
    objects by this."""
    return np.frombuffer(_fastmesh.boxes_seen(np.ascontiguousarray(boxes, np.float64), np.ascontiguousarray(
        view_projection, np.float64), _BOUNDS_MARGIN), bool)


def rects_meet(rects: np.ndarray, targets: np.ndarray) -> np.ndarray:
    """Whether each rect (n x least x, y, most x, y) meets any target's, edges touching."""
    return np.frombuffer(_fastmesh.rects_meet(np.ascontiguousarray(rects, np.float64),
                                              np.ascontiguousarray(targets, np.float64)), bool)


def boxes_rects(boxes: np.ndarray, view_projection: np.ndarray) -> np.ndarray:
    """Each world box's (n x 2 x 3) extent on screen, (x, y) of clip space over w, least then most (n x 4); unbounded
    where a corner is not ahead of the eye."""
    return np.frombuffer(_fastmesh.boxes_rects(np.ascontiguousarray(boxes, np.float64), np.ascontiguousarray(
        view_projection, np.float64)), np.float64).reshape(-1, 4).copy()


def world_boxes(transforms: np.ndarray, bounds: Sequence[MeshBounds]) -> np.ndarray:
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


@dataclass(slots=True)
class _Listing:
    """Steps of a scene's draws by slot, each slot a list of steps and one of blended steps, run in slot order: the
    first ``listed`` draws are in it, so an add lists the added ones alone, after the others of each slot."""
    slots: list[tuple[list[int], list[int]]]
    runs: list[list[tuple]]
    listed: int = 0
    steps: list[int] = field(default_factory=list)
    blended: list[int] = field(default_factory=list)

    def extended(self, slots: Sequence[tuple[Iterable[int], Iterable[int]]], runs: Sequence[Iterable[tuple]],
                 listed: int) -> _Listing:
        slots = [([*held, *steps], [*held_blended, *blended])
                 for (held, held_blended), (steps, blended) in zip(self.slots, slots)]
        return _Listing(slots, [[*held, *run] for held, run in zip(self.runs, runs)], listed,
                        [step for steps, _blended in slots for step in steps],
                        [step for _steps, blended in slots for step in blended])


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
                  read_textures: TextureReads | None = None, *, read_buffers: BufferReads | None = None,
                  heap_start: int, engine_heap: Callable | None = None,
                  shared: SharedMaterials | None = None, streamed: Callable[[str], bool] | None = None,
                  resident_file: Callable | None = None,
                  made_texture: Callable[[str], tuple] | None = None) -> tuple[dict, tuple[str, ...], list[tuple]]:
    """Targets, depth and MDF textures are left for the caller to bind."""
    document = part.document
    shaders = {stage: (contract, bytes(program.shader(stage).payload))
               for stage, contract in _program_contracts(part, program).items()}
    tables, heap, missing = shader_bindings(document, shaders, part.material, load_file, buffer, sampler,
                                           engine_texture=texture, read_textures=read_textures,
                                           read_buffers=read_buffers, heap_start=heap_start,
                                           engine_heap=engine_heap, shared=shared, streamed=streamed,
                                           resident_file=resident_file, made_texture=made_texture)
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
    """A frame is ``begin``, the steps from ``gbuffer`` and ``draw_phase`` in render order, then ``finish``, which
    raises the parts refused while the frame's draws were made."""

    def __init__(self, objects: Sequence[MeshObject], load_file: Callable[[str], bytes | None], camera: Camera, *,
                 shader_target: int, hardware: bool = False, phases: Collection[PhaseKey] = (),
                 pass_file: Callable[[str, bytes], object] | None = None,
                 captured: Mapping[str, Mapping[str, Sequence[int]]] = frozendict(),
                 forget_file: Callable[[str], None] | None = None):
        """``forget_file(path)`` lets ``load_file``'s source go of a file once its texture is on the device."""
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
        self.load_file, self.camera, self.objects = load_file, camera, []
        constants = frame_constants_for(self.profile)
        self.frame = {} if constants is None else constants.scene
        self.captured = captured
        self._forward_order = draw_order_for(self.profile)
        self._forget_file = forget_file
        # Shared with the light passes; textures go to the device as they are read.
        self.read_textures = TextureReads(lambda dimension, entry: self.device.read(dimension, entry), self._forget,
                                          self._texture_side, lambda *refilled: self.device.refill(*refilled))
        self._sides: dict[str, float] | None = None
        # The parts' owners, texel densities and materials, numbered; what the sides were last reckoned from.
        self._side_parts: tuple | None = None
        self._seen_from = None
        # Some streamed textures still want finer mips; how many times textures were made anew of finer ones.
        self.streaming, self.refills = False, 0
        # A GPU buffer file is read through a raw view: elements of 4 bytes.
        self.read_buffers = BufferReads(lambda data: self.device.scene.buffer(data, 0, 4), self._forget)
        self.phases, self.shader_target = frozenset(phases), shader_target
        self.size = camera.size
        self._draws: list[_Draw] = []
        self._named: dict[PhaseKey, list[_Draw]] = defaultdict(list)
        # By texture path: the ids of the parts whose draws miss it, 0 for others.
        self._missing: dict[str, set[int]] = {}
        self._geometry: dict[tuple[int, int], dict] = {}
        # By (owner, material): the parts, their programs, their clusters by LOD and the shader ID.
        self._meshlets: dict[tuple[int, int], tuple[list[MeshPart], tuple, dict[int, list], int]] = {}
        # ``CulledMeshlet`` rows and meshlet objects held, and the last of those.
        self._culled_held = (0, 0, -1)
        self._shaders: dict[tuple, int] = {}
        self._transformed: list[tuple[object, PreviewMaterial]] = []
        self._system = SystemPrograms(self.profile, load_file, pass_file)
        self._material_ids: tuple | MaterialPreviewError | None = None
        # Keyed by (id of the part, route).
        self._chosen: dict[tuple[int, str], tuple[tuple | None, dict] | MaterialPreviewError] = {}
        # ``_part_geometry`` by the ids of what it reads; a part's program keeps the last object's.
        self._built: dict[tuple[int, int, int, int], dict] = {}
        self._adopt(objects)
        self._group_meshlets()
        self.device = Resident(hardware)
        self._placed: list[dict[tuple, tuple[int, Callable[[int], bytes]]]] = [{} for _item in objects]
        self._jobs: dict[tuple, tuple[dict, list[tuple]]] = {}
        # By id of the part: the part and its box (``_box_of``).
        self._part_boxes: dict[int, tuple[MeshPart, np.ndarray]] = {}
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
        # Each owner's largest axis scale.
        self._scales = np.zeros(len(self.objects))
        # Owners no frame draws (``hide``), as if culled.
        self._hidden = np.zeros(len(self.objects), bool)
        self._hidden_given: np.ndarray | None = None
        self._moved = set(range(len(self.objects)))
        self._corners: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None
        self._culled: tuple[bytes, np.ndarray] | None = None
        self._step_owners: dict[int, int] = {}
        # (owner, id of the part) of the draws frames leave out (``hide_parts``), and by deformed owner the ids of its
        # parts outside its deformation's groups; ``_left``.
        self._hidden_parts: frozenset[tuple[int, int]] = frozenset()
        self._outside: dict[int, frozenset[int]] = {}
        self._left_out: tuple[set[int], set[int], set[int]] | tuple[()] | None = None
        # The LOD of a meshlet step of an object with ``lods``: frames keep it at its object's.
        self._step_levels: dict[int, int] = {}
        # By step: its owner (-1: none) and LOD (-1: any).
        self._owner_table: tuple[np.ndarray, np.ndarray] | None = None
        self._forward_runs: dict[int, tuple[bool, list[tuple[np.ndarray, int, tuple[int, ...], int]]]] = {}
        # By run: the run and its steps as listed; and what its order is made of.
        self._forward_at: dict[int, tuple] | None = None
        self._forward_held: dict[int, tuple] = {}
        self._chains = ChainPasses(self.device, self.profile, load_file, pass_file)
        self._stale: set[int] = set()
        # Keyed by the id of the part: its first refusal.
        self._refusals: dict[int, tuple[MeshPart, MaterialPreviewError]] = {}
        # By id: parts of objects added that a frame refused, left out of every frame after (``retire``).
        self._retired: dict[int, MeshPart] = {}
        for owner, item in enumerate(objects):
            if item.deformation is not None:
                self._deform(owner, item)
        if self._refusals:
            self.close()
            raise PartError(self._refusals.values())
        self._wrinkles: WrinklePasses | None = None
        for owner, item in enumerate(objects):
            if item.wrinkle is not None:
                self._wrinkle(owner, item.wrinkle)
        self._programs_drawn()

    def _adopt(self, objects: Sequence[MeshObject]) -> None:
        """Appends ``objects`` and their draws; a part the pass choice refuses raises PartError with nothing kept."""
        start = len(self.objects)
        draws, named_draws, geometry, refusals = [], defaultdict(list), {}, []
        meshlets, shaders, transformed = dict(self._meshlets), dict(self._shaders), list(self._transformed)
        held = list(self._culled_held)
        for owner, item in enumerate(objects, start):
            destination = None if item.deformation is None else item.deformation.destination
            for part in item.parts:
                try:
                    if isinstance(part.buffer, MeshletBuffer):
                        self._meshlet_part(owner, item, part, meshlets, shaders, transformed, held)
                        continue
                    route = part.route or item.route
                    if (id(part), route) not in self._chosen:
                        self._chosen[id(part), route] = self._passes(part, route)
                    if isinstance(self._chosen[id(part), route], MaterialPreviewError):
                        refusals.append((part, self._chosen[id(part), route]))
                        continue
                    main, named = self._chosen[id(part), route]
                    read_from = destination if route == ROUTE else None
                    for program in (*named.values(), *(() if main is None else main[1:])):
                        if program is not None:
                            key = id(part), id(program), id(read_from), id(item.engine)
                            if key not in self._built:
                                self._built[key] = _part_geometry(part, program, read_from, item.engine)
                            geometry[id(part), id(program)] = self._built[key]
                except MaterialPreviewError as exc:
                    refusals.append((part, exc))
                    continue
                if main is not None:
                    draws.append(_Draw(owner, part, *main))
                for phase, program in named.items():
                    named_draws[phase].append(_Draw(owner, part, phase[0], program, None))
        if refusals:
            raise PartError(refusals)
        self.objects += objects
        self._draws += draws
        for phase, items in named_draws.items():
            self._named[phase] += items
        self._geometry.update(geometry)
        self._meshlets, self._shaders, self._transformed = meshlets, shaders, transformed
        self._culled_held = tuple(held)

    def _group_meshlets(self) -> None:
        """Records, visibility and shader IDs are numbered in the order of the objects, so added ones keep theirs."""
        records: dict[tuple[int, int], int] = {}
        shown: dict[int, int] = {}
        self._meshlet_groups = []
        for (owner, material), (parts, programs, levels, shader) in self._meshlets.items():
            record = records.setdefault((id(parts[0].document), id(parts[0].material)), len(records))
            visibility = shown.setdefault(id(programs[2]), len(shown) + 1) if programs[0] == GBUFFER_PURPOSE else 0
            lods = sorted(levels.items())
            self._meshlet_groups.append(MeshletGroup(
                owner, material, tuple(parts), *programs[:3],
                tuple(cluster for _level, clusters in lods for cluster in clusters),
                tuple((level, len(clusters)) for level, clusters in lods), shader, visibility, record,
                programs[3] if len(programs) > 3 else None))
        self._shader_ids = len(self._shaders)
        self.meshlets = meshlet_catalog_for(self.profile) if self._meshlets else None

    def _programs_drawn(self) -> None:
        # The program of each object's part's first draw, by (owner, id of the part); meshlets by (owner, material).
        self._drawn_with: dict[tuple[int, int], object] = {}
        for drawn in (*self._draws, *(item for named in self._named.values() for item in named)):
            self._drawn_with.setdefault((drawn.owner, id(drawn.part)), drawn.program)
        self._rastered_with = {(group.owner, group.material): group.raster for group in self._meshlet_groups}

    def add(self, objects: Sequence[MeshObject]) -> None:
        """Appends ``objects``, owners after the scene's; what is drawn of the others stays. A part the pass choice
        refuses raises PartError with the scene unchanged; one the deformation refuses raises it after."""
        start, grouped = len(self.objects), len(self._meshlets)
        self._adopt(objects)
        count = len(self.objects)
        self._placed += [{} for _item in objects]
        self._boxes = np.concatenate([self._boxes, np.zeros((count - start, 2, 3))])
        self._bounded = np.concatenate([self._bounded, np.zeros(count - start, bool)])
        self._scales = np.concatenate([self._scales, np.zeros(count - start)])
        self._hidden = np.concatenate([self._hidden, np.zeros(count - start, bool)])
        self._hidden_given = None
        self._moved.update(range(start, count))
        self._corners = self._culled = self._owner_table = self._sides = self._side_parts = self._lod_table = None
        # The textures the added parts read may want finer mips of textures already read.
        self._seen_from = None
        if len(self._meshlets) != grouped:
            self._regroup()
        refused = len(self._refusals)
        for owner in range(start, count):
            if self.objects[owner].deformation is not None:
                self._deform(owner, self.objects[owner])
        if len(self._refusals) != refused:
            raise PartError(list(self._refusals.values())[refused:])
        for owner in range(start, count):
            if self.objects[owner].wrinkle is not None:
                self._wrinkle(owner, self.objects[owner].wrinkle)
        self._programs_drawn()

    def _regroup(self) -> None:
        """Every object's meshlet buffers, records and steps, made again with the added ones'."""
        old = [*self._meshlet_held.values(), *self._made_bindless, self._sorted, self._raster_draws]
        if self._meshlet_held:
            old += self._culling
        # Before they are made again: buffers of the same bytes are one.
        self.device.release(old)
        self._gbuffer, self._phases = {}, {}
        self._forward_runs.clear()
        self._group_meshlets()
        self._meshlet_buffers()
        if self._deferred is not None:
            self._deferred.forget_chain()
        self._meshlet_screen()

    def _screen(self) -> None:
        self.targets = {target: self.device.texture(form, self.size, RENDER_TARGET)
                        for target, form in self.target_formats.items()}
        self.depth = self.device.texture(self.depth_resource_format, self.size, DEPTH_STENCIL)
        self.depth_before = self.device.texture(self.depth_resource_format, self.size, DEPTH_STENCIL)
        self._visible: tuple[int, int] | None = None
        self._deferred: DeferredPasses | None = None
        self._meshlet_screen()
        self._scratch: dict[tuple[int, int], int] = {}
        self._coverage: dict[tuple[float, int], tuple[int, int, str, int]] = {}
        self._gbuffer: dict[bool, _Listing] = {}
        # By the arguments of ``draw_phase``.
        self._phases: dict[tuple, _Listing] = {}
        # By (id of the draw, id of the program, targets, depth bound): the draw, program and steps (``_steps``).
        self._made_steps: dict[tuple, tuple[_Draw, object, list[int]]] = {}
        # By the light passes' ``texture`` of a phase: what its parts read through (``draw_phase``).
        self._reads: dict[Callable, Callable] = {}

    def _meshlet_screen(self) -> None:
        """Made once the scene has meshlet groups, and transforms."""
        catalog = self.meshlets
        if self._meshlet_groups and self._visible is None:
            self._visible = (self.device.texture(catalog.visibility_format, self.size, RENDER_TARGET),
                             self.device.texture(catalog.material_id_formats[0], self.size, DEPTH_STENCIL))
        if self._meshlet_groups and self._transforms and self._deferred is None:
            self._deferred = DeferredPasses(self.device, catalog.deferred, self._system, catalog.target,
                                            catalog.material_ids.file, self.size)

    def resize(self, size: Sequence[int]) -> None:
        old = [*self.targets.values(), self.depth, self.depth_before, *self._scratch.values(),
               *(texture for held in self._coverage.values() for texture in (held[0], held[3])),
               *(self._visible or ()), *(() if self._deferred is None else self._deferred.resources())]
        self.size = tuple(size)
        self._screen()
        # Jobs bind no target; those the light passes give constants are made anew with their new steps.
        self._jobs = {key: found for key, found in self._jobs.items() if key[-1] is None}
        self._step_owners.clear()
        self._step_levels.clear()
        self._owner_table = None
        self._forward_runs.clear()
        self.device.release(old)

    def hide(self, hidden: np.ndarray) -> None:
        """Which owners frames leave out, as if culled: a scene's other LODs of an object. ``hidden`` is not to be
        changed after."""
        if hidden is self._hidden_given:
            return
        self._hidden_given = hidden
        hidden = np.asarray(hidden, bool)
        if not np.array_equal(hidden, self._hidden):
            self._hidden, self._culled = hidden.copy(), None

    def place(self, owner: int, placement: Placement) -> None:
        item = self.objects[owner] = replace(self.objects[owner], placement=placement)
        self._moved.add(owner)
        self._lod_moved.add(owner)
        row = self._instance_rows.get(owner)
        if row is not None:
            self._instance_worlds[row] = np.asarray(placement.world, np.float64).reshape(3, 4)
            self._instance_data = None
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
            program = self._rastered_with.get((owner, part.buffer.clusters[part.submesh][0].material))
        else:
            program = self._drawn_with.get((owner, id(part)))
        raster = None if program is None else program.rasterizer_state
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
        with self._refusing():
            self._chains.add(owner, item.deformation, self._skinner(owner, item), partial(self._engine_buffer, owner))
        self._stale.add(owner)

    def deform(self, owner: int, deformation: Deformation, skins: Iterable[MeshPart]) -> None:
        """Deforms the object by another deformation of its level (other groups enabled), ``skins`` its parts only
        skinning."""
        item = self.objects[owner] = replace(self.objects[owner], deformation=deformation, skins=tuple(skins))
        with self._refusing():
            self._chains.change(owner, deformation, self._skinner(owner, item))
        self._stale.add(owner)

    def _skinner(self, owner: int, item: MeshObject) -> Callable[[ChainStep, Mapping[str, int]], int]:
        """Skins with the masters of the object's parts of the deformation's groups: frames leave its others out."""
        deformation = item.deformation
        submeshes = {SubmeshRange(submesh.first_index, submesh.index_count, submesh.first): submesh
                     for submesh in deformation.level.submeshes if submesh.group in deformation.enabled}
        parts = [part for part in (*item.parts, *item.skins) if part.submesh in submeshes]
        self._outside[owner] = frozenset(id(part) for part in item.parts if part.submesh not in submeshes)
        self._left_out = self._owner_table = None
        masters = {submeshes[part.submesh].material: part for part in reversed(parts)}

        def skin(step: ChainStep, held: Mapping[str, int]) -> int:
            if step.material not in masters:
                raise ValueError("a deformed object has no part of a material whose master's program skins it")
            return self._skin(owner, masters[step.material], step, held)
        return skin

    def hide_parts(self, parts: Iterable[tuple[int, MeshPart]]) -> None:
        """The (owner, part) draws frames leave out."""
        hidden = frozenset((owner, id(part)) for owner, part in parts)
        if hidden != self._hidden_parts:
            self._hidden_parts, self._left_out, self._owner_table = hidden, None, None

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
                read_textures=self.read_textures, read_buffers=self.read_buffers, heap_start=self._heap_start,
                made_texture=self._made_texture)
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

    def _took(self, heap, missing, part: MeshPart | None = None) -> None:
        self._heap_start = max([self._heap_start, *(entry[0] + 1 for _kind, _dimension, entry in heap)])
        for path in missing:
            self._missing.setdefault(path, set()).add(0 if part is None else id(part))

    def _refuse(self, error: PartError) -> None:
        for part, reason in error.refusals:
            self._refusals.setdefault(id(part), (part, reason))

    @contextmanager
    def _refusing(self) -> Iterator[None]:
        """Keeps the parts a draw refuses and goes on: refused parts are left out of the rest of the frame."""
        try:
            yield
        except PartError as exc:
            self._refuse(exc)

    def _drawable(self, parts: Iterable[MeshPart]) -> bool:
        if not self._refusals and not self._retired:
            return True
        return not any(id(part) in self._refusals or id(part) in self._retired for part in parts)

    def _part_level(self, owner: int, part: MeshPart) -> int:
        return part.level if self.objects[owner].lods is not None else 0

    def _group_drawable(self, group: MeshletGroup) -> bool:
        """While its LOD 0 parts draw (a group is made by one): a refused part draws its object at LOD 0
        (``_forced_owners``)."""
        if not self._refusals and not self._retired:
            return True
        return self._drawable(part for part in group.parts if not self._part_level(group.owner, part))

    def retire(self) -> None:
        """Leaves the parts refused so far out of every frame after, so frames draw what a scene made without them
        draws: added objects cannot be taken out. The frame refused is dropped."""
        self._retired.update((key, part) for key, (part, _reason) in self._refusals.items())
        self._refusals.clear()
        self.device.pending.clear()
        self._gbuffer, self._phases = {}, {}
        self._forward_runs.clear()
        self._step_owners.clear()
        self._step_levels.clear()
        self._owner_table = None

    def _meshlet_part(self, owner: int, item: MeshObject, part: MeshPart, groups: dict, shaders: dict,
                      transformed: list, held: list[int]) -> None:
        """``held`` is ``_culled_held``, kept as parts are taken. A coarser LOD's part joins a group of its object's
        first LOD, so it takes no group, shader ID or transform ID a scene of first LODs would not take."""
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
        level = part.level if item.lods is not None else 0
        if key in groups:
            # The group draws with its first part's programs: a part of another LOD must draw with them too.
            first = groups[key][0][0]
            if part.document is not first.document or part.meshlet_programs != first.meshlet_programs \
                    or part.material.material_flags != first.material.material_flags:
                raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                    "MaterialPreview", "Parts of the meshlet mesh draw one material with different programs, and the "
                                       "renderer draws an object's clusters of one material with one set of "
                                       "programs."))
            programs = shader = None
        elif level:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "A coarser LOD of the meshlet mesh draws a material its first LOD does not, and the "
                                   "preview draws a coarser LOD only with its first LOD's materials."))
        else:
            programs = _meshlet_programs(part.document, part.material, self.shader_target, part.meshlet_programs,
                                         skinning)
            if len(programs) > 3:
                _compute_contract(part, programs[3])
                # A transform ID per transform program and material, as ``_meshlet_buffers`` groups them.
                if not any(program is programs[3] and material == part.material for program, material in transformed):
                    check_transform_ids(len(transformed), catalog)
                    transformed.append((programs[3], part.material))
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
            shader = (id(part.document), id(part.material), programs[0], *(id(program) for program in programs[1:]))
            if shader not in shaders:
                check_shader_ids(len(shaders))
                shaders[shader] = len(shaders) + 1
        check_clusters(catalog, clusters)
        rows, instances, last = held
        instances += owner != last
        check_culled(rows + len(clusters), instances)
        if key not in groups:
            groups[key] = ([], programs, {}, shaders[shader])
        groups[key][0].append(part)
        groups[key][2].setdefault(level, []).extend(clusters)
        held[:] = rows + len(clusters), instances, owner

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
        self._geometry_views: tuple[tuple, ...] | None = None
        self._meshlet_held: dict[str, int] = {}
        self._held_names: frozenset[str] = frozenset()
        self._tables: tuple[bytes, dict[int, bytes | Following]] = (b"", {})
        self._transforms: dict[int, int] = {}
        self._transform_groups: list[MeshletGroup] = []
        # By transform ID, the records of the groups it transforms.
        self._transform_records: list[list[int]] = []
        self._records: list[MaterialRecord | MaterialPreviewError | None] = []
        self._made_records: list[MaterialRecord | None] | None = None
        # The instance records' world of each owner by its row, kept as ``place`` moves them, and their bytes.
        self._instance_rows: dict[int, int] = {}
        self._instance_worlds = np.zeros((0, 3, 4))
        self._instance_data: bytes | None = None
        # By batch: a resize keeps them, as they bind no target.
        self._rasters: dict[int, dict] = {}
        # By batch, its groups' numbers; ``SortData`` and the batch draws' arguments, made when first bound.
        self._batch_members: list[list[int]] = []
        self._batches: RasterBatches | None = None
        self._sorted: int | None = None
        self._raster_draws: int | None = None
        self._shown: tuple | None = None
        self._drawn_groups: tuple | None = None
        self._record_groups: list[list[MeshletGroup]] = []
        self._handles: list[int] = []
        self._room = 0
        self._views: bytes | None = None
        self._made_bindless: set[int] = set()
        # By (group, LOD): the ``RootConstant`` of the group's clusters of the LOD.
        self._span_roots: dict[tuple[int, int], int] = {}
        # The objects with ``lods`` and their rows of the table, made when first asked for; those moved since; the
        # LODs picked from a view and the levels drawn of them (``_lods``).
        self._lod_table: LodTable | None = None
        self._lod_rows: dict[int, int] = {}
        self._lod_moved: set[int] = set()
        self._picked: tuple | None = None
        self._levels: tuple | None = None
        # The refusals the objects drawing LOD 0 for them were found at, and those objects.
        self._forced: tuple[tuple[int, int], list[int]] | None = None
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
                self._transform_records.append([])
            self._transforms[group.shader] = number
            if group.record not in self._transform_records[number]:
                self._transform_records[number].append(group.record)
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
        self._record_groups = [[] for _number in range(max(group.record for group in groups) + 1)]
        for group in groups:
            self._record_groups[group.record].append(group)
        self._records = [None] * len(self._record_groups)
        # Each record at its handle from the start of ``BindlessBuffer``, then the tables.
        self._handles, self._room = record_handles([bound_record_size(first.part.document, first.part.material)
                                                    for first, *_rest in self._record_groups])
        tables, offsets = material_tables(catalog, groups, self._handles, self._room)
        tables += bytes(-len(tables) % 16)
        blocks, extended = extended_blocks(len(tables), [self.objects[owner].engine.get(EXTENDED) for owner in owners])
        tables += blocks
        culled, spans = culled_meshlets(groups, {owner: number for number, owner in enumerate(owners)})
        # A draw's object state is its first object's, and its view table entry 0 its objects' (``_geometry_heap``).
        batches: dict[tuple[int, int, bool], list[int]] = {}
        for number, group in enumerate(groups):
            if group.shade is not None:
                item = self.objects[group.owner]
                block = item.engine.get(EXTENDED)
                key = id(group.raster), item.stencil_reference, block is not None and not any(bytes(block))
                batches.setdefault(key, []).append(number)
        self._batch_members = list(batches.values())
        self._batches = RasterBatches(groups, spans, self._batch_members, catalog.vertices)
        special, self._span_roots = special_offsets(spans, len(groups), self._batches)
        skinned = [owner for owner in owners if self.objects[owner].skinned and catalog.skinning is not None]
        indices_skinning = [skinned.index(owner) if owner in skinned else -1 for owner in owners]

        self._instance_rows = {owner: number for number, owner in enumerate(owners)}
        self._instance_worlds = np.array([self.objects[owner].placement.world for owner in owners],
                                         np.float64).reshape(-1, 3, 4)
        self._instance_data = None
        fixed = ([geometry[owner] for owner in owners], [offsets[owner] for owner in owners],
                 [receives_decals(self.objects[owner].decal_receive_mode, self.objects[owner].skinned)
                  for owner in owners], extended, indices_skinning)

        def instances() -> bytes:
            # Asked every frame (``Resident.refresh``): made again only after an owner moved.
            if self._instance_data is None:
                self._instance_data = instance_records(catalog, self._instance_worlds, *fixed)
            return self._instance_data
        zeros = bytes(material_culling_size())
        self._culling = (self.device.scene.buffer(zeros, UNORDERED_ACCESS), self.device.buffer(zeros))
        self._tables = (tables, {})
        self._meshlet_held = {
            "Instances": self.device.following(("meshlet instances",), instances),
            "CulledMeshlet": self.device.buffer(culled),
            "SpecialShaderOffset": self.device.buffer(special),
            "MaterialCulling": self._culling[0], **self._instance_buffers(owners), **self._skinned_buffers(skinned)}
        # ``BindlessBuffer`` holds the records, so it is made when first bound; ``SortData`` what the scene shows.
        self._held_names = frozenset({*self._meshlet_held, "BindlessBuffer", "SortData"})

    def _records_made(self) -> list[MaterialRecord | None]:
        if self._made_records is None:
            for number, made in enumerate(self._records):
                if made is None:
                    self._records[number] = self._record(number)
            self._made_records = [made if isinstance(made, MaterialRecord) else None for made in self._records]
        return self._made_records

    def _record(self, number: int) -> MaterialRecord | MaterialPreviewError:
        """The record every meshlet program drawing the material reads."""
        groups = self._record_groups[number]
        part = groups[0].part
        programs = {id(program): program for group in groups
                    for program in (group.raster, group.shade, group.transform) if program is not None}
        try:
            with _drawing(*(part for group in groups for part in group.parts)):
                stages = []
                for program in programs.values():
                    compute = [shader.role for shader in program.shaders if shader.present] == ["compute"]
                    contracts = {"compute": _compute_contract(part, program)} if compute else _program_contracts(
                        part, program)
                    stages += [(stage, contract, bytes(program.shader(stage).payload))
                               for stage, contract in contracts.items()]
                made = material_record(part.document, stages, part.material, self.load_file,
                                       read_textures=self.read_textures, read_buffers=self.read_buffers,
                                       heap_start=self._heap_start, streamed=self._streamed,
                                       resident_file=self._resident,
                                       made_texture=self._made_texture)
        except PartError as exc:
            self._refuse(exc)
            return exc.refusals[0][1]
        self._took(made.heap.values(), made.missing)
        return made

    def _bindless(self, element: int) -> bytes | Following:
        """``BindlessBuffer`` in whole elements: the records at their handles, then the tables."""
        tables, held = self._tables
        if element not in held:
            parts = [None if made is None else made.given(lambda data: data, ("held",))
                     for made in self._records_made()]

            def laid(records: Sequence[bytes | None]) -> bytes:
                data = bytearray(tables)
                for handle, record in zip(self._handles, records):
                    if record is not None:
                        data[handle:handle + len(record)] = record
                return bytes(data.ljust(-(-len(data) // element) * element, b"\0"))
            if any(isinstance(item, Following) for item in parts):
                held[element] = Following(("meshlet records", element, tuple(
                    item.key if isinstance(item, Following) else item for item in parts)), lambda: laid(
                    [item.make() if isinstance(item, Following) else item for item in parts]))
            else:
                held[element] = laid(parts)
        return held[element]

    def _shared(self, records: Iterable[int]) -> SharedMaterials:
        made = self._records_made()
        if self._views is None:
            self._views = material_views(self._handles, [0 if item is None or item.view is None else item.view
                                                         for item in made], self._room)
        heap = {}
        for number in records:
            if made[number] is not None:
                heap.update(made[number].heap)
        return SharedMaterials(self._bindless, self._views, heap)

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
            return skinning_records(np.array([self._posed_box(owner) for owner in skinned]), joints)
        return {"SkinningMatrices": self.device.following(("meshlet bones",), matrices),
                "InstancesSkinning": self.device.following(("meshlet skinning",), records)}

    def _posed_box(self, owner: int) -> np.ndarray:
        """A posed skinned meshlet object's world box, as ``InstancesSkinning`` holds it."""
        item = self.objects[owner]
        if item.bounds is None:
            return np.zeros((2, 3))
        return world_boxes(np.asarray(item.placement.bones, np.float64)[None], [item.bounds])[0]

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
        if name == "SortData":
            if self._sorted is None:
                self._sorted = self.device.following(("meshlet sort data",), lambda: self._laid_batches()[0])
            return self._sorted
        if name != "BindlessBuffer":
            return self._meshlet_held[name]
        layout = contract.structured_buffer(name)
        data = self._bindless(layout.byte_size if layout is not None and layout.byte_size else 4)
        made = self.device.following(*data) if isinstance(data, Following) else self.device.buffer(data)
        self._made_bindless.add(made)
        return made

    def _raster_arguments(self) -> int:
        if self._raster_draws is None:
            self._raster_draws = self.device.following(("meshlet raster draws",), lambda: self._laid_batches()[1])
        return self._raster_draws

    def _laid_batches(self) -> tuple[bytes, bytes]:
        """The batches' clusters of objects in view and parts drawn, of each object's LOD, as the game culls clusters
        for its draws."""
        seen, levels, refused = self._seen(), self._lods()[0], (len(self._refusals), len(self._retired))
        if self._drawn_groups is None or self._drawn_groups[0] != refused:
            self._drawn_groups = refused, np.array([self._group_drawable(group) for group in self._meshlet_groups],
                                                   bool)
        if self._shown is None or self._shown[2] != refused or not all(
                was is now or np.array_equal(was, now) for was, now in zip(self._shown[:2], (seen, levels))):
            batches = self._batches
            self._shown = seen, levels, refused, batches.laid(
                seen[batches.owner] & self._drawn_groups[1][batches.group] & (batches.level == levels[batches.owner]))
        return self._shown[3]

    def _geometry_heap(self, _contract, handle, owners: Sequence[int] = ()) -> Sequence[tuple] | None:
        if handle.source is None:
            return None
        if handle.source.name == "BindlessConstantBufferViewTbl" and owners:
            blocks = [self.objects[owner].engine.get(EXTENDED) for owner in owners]
            # A block of zeros names view table entry 0: the view of the record at handle 0.
            if all(block is not None and not any(bytes(block)) for block in blocks):
                first = self._records_made()[0]
                return [] if first is None or first.view is None else [first.heap[first.view]]
        if handle.source.name != "Instances" or handle.dimension is not None:
            return None
        return self._geometry_entries()

    def _geometry_entries(self) -> tuple[tuple, ...]:
        # One tuple every job's heap shares, as the game's draws share its bindless heap.
        if self._geometry_views is None:
            self._geometry_views = tuple((RANGE_SRV, BUFFER, (index, 0, 0, buffer))
                                        for index, buffer in self._geometries.items())
        return self._geometry_views

    def _meshlet_job(self, groups: Sequence[MeshletGroup], program, constant: int, targets: int, texture: Callable,
                     draw: tuple[int, int], constant_buffer: Callable | None = None,
                     stage: str | None = None) -> dict:
        """One draw of ``groups``, each lane reading its own material; object state is the first group's."""
        group, answered = groups[0], set()

        def buffer(contract, resource, owner: int = group.owner) -> bytes | int | None:
            if resource.register_class == "b" and resource.name == "RootConstant":
                return root_constant(contract.buffer(resource.name), constant)
            own = None if stage is None else self._deferred.bound(stage, resource.name)
            if resource.register_class == "t" and own is not None:
                return own
            if resource.register_class == "t" and resource.name in self._held_names:
                return self._held(contract, resource.name)
            if constant_buffer is not None and resource.register_class == "b":
                data = constant_buffer(resource.name, contract.buffer(resource.name))
                if data is not None:
                    answered.add(resource.name)
                    return data
            return self._engine_bytes(contract, resource, owner)
        geometry = {"vertex_buffers": [], "elements": [], "indices": None, "draw": (*draw, 0, 0, 0)}
        with _drawing(*(part for item in groups for part in item.parts)):
            job, missing, engine = _program_draw(
                group.part, program, geometry, targets, self.objects[group.owner].stencil_reference, self.load_file,
                buffer, self._sampler, texture, self.read_textures, read_buffers=self.read_buffers,
                heap_start=self._heap_start,
                engine_heap=partial(self._geometry_heap, owners=list(dict.fromkeys(item.owner for item in groups))),
                shared=self._shared(dict.fromkeys(item.record for item in groups)), streamed=self._streamed,
                resident_file=self._resident, made_texture=self._made_texture)
        self._took(job["heap"], missing)
        job, _placed = self._kept(job, self._not_held(engine), buffer, group.owner, answered, constant_buffer)
        return self.device.bound(job)

    def _not_held(self, engine: Iterable[tuple]) -> list[tuple]:
        return [entry for entry in engine if entry[2].register_class != "t" or entry[2].name not in self._held_names]

    def _system_table(self, stage: str, contract, resource, textures: Mapping[str, tuple] = frozendict(),
                      written: Mapping[str, tuple] = frozendict()) -> tuple:
        kind, name = resource.register_class, resource.name
        place = (resource.bind_point, resource.register_space, resource.bind_count)
        visibility = SHADER_VISIBILITY[stage]
        if kind == "s":
            return (RANGE_SAMPLER, *place, 0, visibility, tuple(
                (index, *self._sampler(name)) for index in range(max(1, resource.bind_count))))
        if kind == "b":
            layout = contract.buffer(name)
            transforms = None if self._deferred is None else self._transforms
            shown = {group.shader: group.visibility for group in self._meshlet_groups if group.visibility}
            data = self.device.buffer(replace_table(layout, self._shader_ids + 1, shown, transforms)) \
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
                    ((0, 0, stride, self._held(contract, name)),) if name in self._held_names else ())
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
        self._records_made()
        # One raster draw per batch, bound to every material of it, of the clusters ``SortData`` gives it.
        rastered, shading = [], {}
        for batch, members in enumerate(self._batch_members):
            drawn = [groups[number] for number in members if self._group_drawable(groups[number])]
            if not drawn:
                continue
            with self._refusing(), _drawing(*(part for group in drawn for part in group.parts)):
                if batch not in self._rasters:
                    batched = [groups[number] for number in members]
                    self._rasters[batch] = self._meshlet_job(
                        batched, batched[0].raster, len(groups) + batch, 1, gbuffer, (catalog.vertices, 0)) | {
                        "indirect": (self._raster_arguments(), 16 * batch)}
                rastered.append((drawn, self._step(self._rasters[batch], [(catalog.visibility_format, visible)],
                                                   self.depth)))
        for group in groups:
            if group.shade is not None and self._group_drawable(group):
                shading.setdefault(group.visibility, []).append(group)
        # One shading draw per visibility ID, of every material its program shades.
        shades = []
        for visibility, members in shading.items():
            if len({group.transform is None for group in members}) > 1:
                raise ValueError("a meshlet shading program shades transformed and untransformed triangles")
            with self._refusing(), _drawing(*(part for group in members for part in group.parts)):
                shade = self._meshlet_job(members, members[0].shade, visibility, len(targets), texture,
                                          (catalog.tile_vertices, 1),
                                          stage=None if members[0].transform is None else "shade")
                shades.append(self._step(shade, targets, ids, id_depth))
        rastered = [(drawn, step) for drawn, step in rastered if any(self._group_drawable(group) for group in drawn)]
        shaded = [group for group in shaded if self._group_drawable(group)]
        if not shaded:
            return []
        try:
            material_ids = self._step(self._material_id_job(), self.scratch([targets[0][0]]), ids, id_depth)
            gathered = [] if deferred is None else deferred.chain(partial(self._system_table, "compute"))
        except MaterialPreviewError as exc:
            self._refuse(PartError([(part, exc) for group in shaded for part in group.parts]))
            return []
        transformed = []
        for group in self._transform_groups:
            if group.shade is not None and self._group_drawable(group):
                with self._refusing():
                    transformed.append((group.owner, self._transform_step(group)))
        # Raster and shading draws cover every object of theirs, so no object's culling leaves them out; the raster
        # draws' clusters are culled.
        return [*steps, *(step for _drawn, step in rastered), material_ids, *gathered,
                *(step for _owner, step in transformed), *shades]

    def _streamed(self, path: str) -> bool:
        return self.load_file(streamed_key(path)) is not None

    def _resident(self, path: str) -> bytes | None:
        return self.load_file(resident_key(path) if self._streamed(path) else path)

    def _forget(self, load: Callable[[str], bytes | None], path: str) -> None:
        if self._forget_file is None:
            return
        if load == self._resident:
            path = resident_key(path) if self._streamed(path) else path
        elif load != self.load_file:
            return
        self._forget_file(path)

    def _transform_step(self, group: MeshletGroup) -> int:
        part, program, deferred = group.part, group.transform, self._deferred
        number, document = self._transforms[group.shader], part.document

        def buffer(contract, resource, owner: int = group.owner) -> bytes | int | None:
            if resource.register_class == "b" and resource.name == "RootConstant":
                return root_constant(contract.buffer(resource.name), number)
            own = deferred.bound("transform", resource.name)
            if resource.register_class == "t" and own is not None:
                return own
            if resource.register_class == "t" and resource.name in self._held_names:
                return self._held(contract, resource.name)
            return self._engine_bytes(contract, resource, owner)
        records = self._transform_records[number]
        with _drawing(*group.parts):
            contract = _compute_contract(part, program)
            shader = bytes(program.shader("compute").payload)
            tables, heap, missing = shader_bindings(
                document, {"compute": (contract, shader)}, part.material, self.load_file, buffer, self._sampler,
                engine_texture=self._engine_texture(GBUFFER_PURPOSE), read_textures=self.read_textures,
                read_buffers=self.read_buffers, heap_start=self._heap_start,
                engine_heap=partial(self._geometry_heap, owners=[group.owner]),
                shared=self._shared(records), streamed=self._streamed, resident_file=self._resident,
                made_texture=self._made_texture)
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

    def _passes(self, part: MeshPart, route: str) -> tuple[tuple | None, dict] | MaterialPreviewError:
        """The part's main draw (``_drawn_as``) and the passes of its own by phase, or why it is refused."""
        try:
            main, named = part_draws(part.document, part.material, self.shader_target, route, self.phases)
            return (None if main is None else self._drawn_as(part, *main, route)), named
        except MaterialPreviewError as exc:
            return exc

    def _drawn_as(self, part: MeshPart, purpose: str, program, route: str) -> tuple[str, object, object | None]:
        """(purpose, program, depth pre-pass program)."""
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
        return purpose, program, None if prepass is None else document.sdf.targets[self.shader_target].programs[prepass]

    @property
    def missing_textures(self) -> tuple[str, ...]:
        """Of the draws frames keep."""
        left = self._left()
        gone = left[1] - left[2] if left else frozenset()
        return tuple(path for path, parts in self._missing.items() if not parts <= gone)

    def _left(self) -> tuple[set[int], set[int], set[int]] | tuple[()]:
        """Of the draws whose steps are made: the steps of those left out, and the ids of their parts and of the
        others'; () when none is left out."""
        if self._left_out is None:
            self._left_out = ()
            if self._hidden_parts or any(self._outside.values()):
                self._left_out = set(), set(), set()
                for item, _program, steps in self._made_steps.values():
                    self._leaves(item, steps)
        return self._left_out

    def _leaves(self, item: _Draw, steps: list[int]) -> None:
        out, gone, drawn = self._left_out
        part = id(item.part)
        if (item.owner, part) in self._hidden_parts or part in self._outside.get(item.owner, ()):
            out.update(steps)
            gone.add(part)
        else:
            drawn.add(part)

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

    def release_texels(self) -> None:
        """Once the scene is made: lets go of the bytes of the textures the device holds. A file read again is that
        texture; other files of its bytes are another."""
        released = self.device.release_texels()
        for (content, *_shape), found in self.read_textures.contents.items():
            if found is None or id(found[-1]) in released:
                content.release()

    def close(self) -> None:
        """The chains hold the scene's callbacks, so the scene waits for the cyclic GC: its device and the bytes of
        the textures it read go now."""
        self.read_textures.stop()
        self.device.close()
        for (content, *_shape), found in self.read_textures.contents.items():
            content.release()
            if found is not None:
                found[-1].release()

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
            entry = _texture_entry(0, path, self.load_file, resource.dimension, self.read_textures)
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

    def _box_of(self, part: MeshPart) -> np.ndarray:
        """``_part_box``, made once a part."""
        if id(part) not in self._part_boxes:
            self._part_boxes[id(part)] = part, _part_box(part)
        return self._part_boxes[id(part)][1]

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
                self.load_file, buffer, self._sampler, texture, self.read_textures, read_buffers=self.read_buffers,
                heap_start=self._heap_start, streamed=self._streamed, resident_file=self._resident,
                made_texture=self._made_texture)
        self._took(job["heap"], missing, item.part)
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

    def _steps(self, item: _Draw, program, job: Callable[[], dict], targets: Sequence[tuple[int, int]],
               bound: bool = True, kept: bool = True) -> list[int]:
        """``job()`` is the draw's job. ``kept``: ``item`` is one of the scene's draws, whose steps are made once, so a
        phase made again draws them with the steps it had (their jobs are the same); else they are made for this
        call."""
        key = id(item), id(program), tuple(targets), bound
        held = self._made_steps.get(key) if kept else None
        if held is None:
            # The item and program are held, so their ids name them while the steps are kept.
            held = item, program, self._made_steps_of(item, program, job(), targets, bound)
            if kept:
                self._made_steps[key] = held
                if self._left_out:
                    self._leaves(item, held[2])
        steps = held[2]
        self._step_owners.update(dict.fromkeys(steps, item.owner))
        self._owner_table = None
        return steps

    def _made_steps_of(self, item: _Draw, program, job: dict, targets: Sequence[tuple[int, int]],
                       bound: bool) -> list[int]:
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
        return steps

    def begin(self) -> None:
        self._stream()
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
        if self._refusals:
            raise PartError(self._refusals.values())
        self.device.pending[:] = self._seen_steps(self._forward_sorted(self.device.pending))
        try:
            return self.device.flush(read)
        except RuntimeError as exc:
            raise _not_drawn(exc) from exc

    def _texture_side(self, path: str) -> int | None:
        """The texels a side of ``path`` must span as the scene samples it from its camera, a power of two; None:
        every mip."""
        if self._sides is None:
            self._sides = self._texture_sides()
        side = self._sides.get(path, np.inf)
        return None if not np.isfinite(side) else 1 << max(0, int(np.ceil(np.log2(max(side, 1.0)))))

    def _stream(self) -> None:
        """Streams finer mips in where the camera now samples them, ``_STREAM_READS`` files at a time."""
        camera = self.camera
        seen_from = (np.asarray(camera.view, np.float64).tobytes(), tuple(camera.scale), tuple(camera.size),
                     camera.orthographic)
        if seen_from == self._seen_from and not self.streaming:
            return
        if seen_from != self._seen_from:
            self._seen_from, self._sides = seen_from, None
        streamed = {key: held[2] for key, held in self.read_textures.streamed.items()}
        self.streaming = self.read_textures.refine(_STREAM_READS)
        self.refills += sum(held[2] != streamed.get(key) for key, held in self.read_textures.streamed.items())

    def _texture_sides(self) -> dict[str, float]:
        """By texture path, the most texels a side of it spans on screen, over the parts whose materials read it."""
        self._box_moved()
        camera = self.camera
        view = np.asarray(camera.view, np.float64).reshape(3, 4)
        eye = -view[:, :3].T @ view[:, 3]
        # World length a pixel spans, at unit distance from the eye in perspective.
        pixel = 2.0 / max(camera.scale[0] * camera.size[0], camera.scale[1] * camera.size[1])
        boxes = self._boxes
        if not camera.orthographic:
            pixel = pixel * np.linalg.norm(eye - np.clip(eye, boxes[:, 0], boxes[:, 1]), axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            reach = np.where(self._bounded & (pixel > 0), _SIDE_MARGIN * self._scales / pixel, np.inf)
        if self._side_parts is None:
            owners, densities, numbers, materials = [], [], [], {}
            for owner, item in enumerate(self.objects):
                for part in item.parts:
                    owners.append(owner)
                    densities.append(part.texel_density or 0.0)
                    numbers.append(materials.setdefault(id(part.material), (len(materials), part.material))[0])
            paths: dict[str, int] = {}
            # (texture path, material) of each texture a material reads.
            pairs = sorted((paths.setdefault(path, len(paths)), number) for number, material in materials.values()
                           for path in material.textures.values())
            # Parts by material and pairs by path, each run's start, for reductions by material and by path.
            order = np.argsort(numbers, kind="stable")
            pair_paths, pair_materials = np.array(pairs, np.int64).reshape(-1, 2).T
            self._side_parts = (np.array(owners, np.int64)[order], np.array(densities, np.float64)[order],
                                np.flatnonzero(np.diff(np.array(numbers)[order], prepend=-1)), pair_materials,
                                np.flatnonzero(np.diff(pair_paths, prepend=-1)), list(paths))
        owners, densities, by_material, pair_materials, by_path, paths = self._side_parts
        with np.errstate(divide="ignore", invalid="ignore"):
            parts = np.where(densities > 0, reach[owners] / densities, np.inf)
        most = np.maximum(np.maximum.reduceat(parts, by_material), 0) if len(parts) else parts
        # A material's NaN leaves its paths to the others.
        sides = np.fmax(np.fmax.reduceat(most[pair_materials], by_path), 0) if len(by_path) else np.zeros(0)
        return {path: side for path, side in zip(paths, sides.tolist()) if side > 0}

    def _seen(self) -> np.ndarray:
        """The last entry, True, answers owner -1 (steps of no object)."""
        self._box_moved()
        hidden = self._hidden
        if self._corners is None or self._corners[0] is not hidden:
            # Owners hidden are not tested: they are left out all the same.
            bounded = np.flatnonzero(self._bounded & ~hidden)
            self._corners = hidden, bounded, self._boxes[bounded]
        _hidden, bounded, boxes = self._corners
        seen = np.ones(len(self._boxes) + 1, bool)
        if not len(bounded):
            seen[:-1] = ~hidden
            return seen
        matrix = scene_values(self.camera)["viewProjMat"]
        key = matrix.tobytes()
        if self._culled is not None and self._culled[0] == key:
            return self._culled[1]
        seen[bounded] = boxes_seen(boxes, matrix)
        seen[:-1] &= ~hidden
        seen.setflags(write=False)
        self._culled = key, seen
        return seen

    def _lods(self) -> tuple[np.ndarray, np.ndarray]:
        """Each owner's meshlet LOD and its ``CulledMeshlet`` word's high byte, as instance culling picks them from the
        view without its jitter (docs/internals/meshlets.md, LODs): 0 for owners without ``lods``, and the last
        entries answer owner -1."""
        if self._lod_table is None:
            owners = [owner for owner in self._instance_rows if self.objects[owner].lods is not None]
            self._lod_rows = {owner: row for row, owner in enumerate(owners)}
            self._lod_table = LodTable([self.objects[owner].lods for owner in owners], np.zeros((len(owners), 3, 4)))
            self._lod_moved, self._picked, self._levels, self._forced = set(owners), None, None, None
        rows = self._lod_rows
        moved = [owner for owner in self._lod_moved if owner in rows]
        self._lod_moved.clear()
        if moved:
            self._lods_placed(moved)
            self._picked = None
        camera = self.camera
        # The render size is MeshletLODScreenSize; jitter only offsets the corners on screen.
        key = np.asarray(camera.view, np.float64).tobytes(), tuple(camera.scale), tuple(self.size), camera.orthographic
        if rows and (self._picked is None or self._picked[0] != key):
            view = np.asarray(camera.view, np.float64).reshape(3, 4)
            matrix = scene_values(replace(camera, jitter=(0.0, 0.0), previous=None))["viewProjMat"]
            self._picked = key, *meshlet_lods(self.meshlets.lods, self._lod_table, matrix,
                                              -view[:, :3].T @ view[:, 3], camera.scale[0], self.size)
        picked, refused = self._picked, (len(self._refusals), len(self._retired))
        if self._levels is None or self._levels[0] is not picked or self._levels[1] != refused:
            levels, high = np.zeros(len(self.objects) + 1, np.int64), np.zeros(len(self.objects) + 1, np.uint32)
            if rows:
                owners = list(rows)
                levels[owners], high[owners] = picked[1], picked[2]
                forced = self._forced_owners(refused)
                levels[forced] = high[forced] = 0
            for made in (levels, high):
                made.setflags(write=False)
            self._levels = picked, refused, levels, high
        return self._levels[2:]

    def _forced_owners(self, refused: tuple[int, int]) -> list[int]:
        """The objects of ``lods`` a part of which is refused: they draw LOD 0, as a scene made without the part draws
        them (``_group_drawable``, scene_lit's ``_without``)."""
        if not self._refusals and not self._retired:
            return []
        if self._forced is None or self._forced[0] != refused:
            rows = self._lod_rows
            self._forced = refused, list({group.owner for group in self._meshlet_groups
                                          if group.owner in rows and not self._drawable(group.parts)})
        return self._forced[1]

    def _lods_placed(self, owners: Sequence[int]) -> None:
        """A posed skinned object's LOD is measured from its world box (``_posed_box``) at identity rows."""
        skinning = self.meshlets.skinning is not None
        posed = [owner for owner in owners if skinning and self.objects[owner].skinned]
        still = [owner for owner in owners if not (skinning and self.objects[owner].skinned)]
        table, rows = self._lod_table, self._lod_rows
        if still:
            table.place([rows[owner] for owner in still], [self.objects[owner].placement.world for owner in still])
        if posed:
            table.place([rows[owner] for owner in posed], np.broadcast_to(np.eye(4)[:3], (len(posed), 3, 4)),
                        np.array([self._posed_box(owner) for owner in posed]))

    def _box_moved(self) -> None:
        if self._moved:
            self._corners = self._culled = None
            owners = list(self._moved)
            worlds = np.array([self.objects[owner].placement.world for owner in owners], np.float64).reshape(-1, 3, 4)
            self._scales[owners] = np.linalg.norm(worlds[:, :, :3], axis=1).max(axis=1)
        moved = defaultdict(list)
        for owner in self._moved:
            item = self.objects[owner]
            transforms = item.placement.bones if item.skinned else item.placement.world[None]
            self._bounded[owner] = item.bounds is not None and transforms is not None
            if self._bounded[owner]:
                moved[np.shape(transforms)].append((owner, transforms))
        for group in moved.values():
            owners = [owner for owner, _transforms in group]
            self._boxes[owners] = world_boxes(np.array([transforms for _owner, transforms in group], np.float64),
                                               [self.objects[owner].bounds for owner in owners])
        self._moved.clear()

    def _seen_steps(self, steps: list[int]) -> list[int]:
        seen, left = self._seen(), self._left()
        out = left[0] if left else ()
        if seen.all() and not self._step_levels and not out:
            return steps
        if self._owner_table is None:
            size = max([*self._step_owners, *self._step_levels, *out], default=-1) + 1
            self._owner_table = np.full(size, -1, np.int64), np.full(size, -1, np.int64)
            for table, given in zip(self._owner_table, (self._step_owners, self._step_levels)):
                table[list(given)] = list(given.values())
            self._owner_table[0][list(out)] = -2
        return _fastmesh.steps_kept(steps, *self._owner_table, seen, self._lods()[0] if self._step_levels else None)

    def gbuffer(self, apart: bool = False) -> list[int]:
        listing = self._gbuffer.get(apart)
        if listing is not None and listing.listed == len(self._draws):
            return listing.steps
        scene = self.device.scene
        unblended = _bound_targets(self.catalog, False)
        textures = {row.target: (row.format, self.targets[row.target]) for row in unblended}
        added = self._draws[0 if listing is None else listing.listed:]
        prepasses = []
        for item in added:
            if item.prepass is not None and item.purpose != "forward" and self._drawable((item.part,)):
                with self._refusing():
                    # The pre-pass only writes depth; the G-buffer pass draws its targets again.
                    prepasses += self._steps(item, item.prepass, partial(
                        self._job, item, item.prepass, len(unblended), self._engine_texture(PREPASS_PURPOSE)),
                        self.scratch(row.format for row in unblended))
        slots = [((), ()), (prepasses, ())]
        if listing is None:
            # Meshlets draw over the pre-pass depth, so they go after the pre-passes.
            slots[0] = [scene.clear(*target) for target in textures.values()] + [
                scene.clear(self.depth_format, self.depth)], ()
            slots.append((self._meshlet_steps(), ()))
            listing = _Listing([([], []) for _slot in range(5)], [])
        else:
            slots.append(((), ()))
        drawn = [item for item in added if item.purpose == GBUFFER_PURPOSE
                 or item.purpose == "blending" and ("blending", ()) not in self.phases]
        for blending in (False, True):
            phase = [item for item in drawn if (item.purpose == "blending") == blending
                     and self._drawable((item.part,))]
            made: list[int] = []
            slots.append((made, ()))
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
                if self._drawable((item.part,)):
                    with self._refusing():
                        made += self._steps(item, item.program, partial(
                            self._job, item, item.program, len(bound), self._engine_texture(GBUFFER_PURPOSE)), onto)
        self._gbuffer[apart] = listing = listing.extended(slots, (), len(self._draws))
        return listing.steps

    def draw_phase(self, phase: PhaseKey, targets: Sequence[tuple[int, int | None]], bound: bool,
                   texture: Callable[[str, object, int], tuple | None],
                   constant_buffer: Callable[[str, object], bytes | None],
                   split: bool = False) -> tuple[list[int], list[int]]:
        """``texture(name, resource, depth)`` returns None for a null view; ``constant_buffer(name, layout)`` is called
        again every frame."""
        purpose = phase[0]
        source = self._draws if purpose in PHASES else self._named[phase]
        key = phase, tuple(targets), bound, texture, constant_buffer, split
        listing = self._phases.get(key)
        if listing is not None and listing.listed == len(source):
            return listing.steps, listing.blended
        if purpose == "blending" and [form for form, _texture in targets] != [
                row.format for row in _bound_targets(self.catalog, True)]:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "REasy's catalogs of this game build give blending passes other targets."))
        added = source[0 if listing is None else listing.listed:]
        drawn = [item for item in added if item.purpose == purpose] if purpose in PHASES else added
        count = next((slot for slot, (form, _texture) in enumerate(targets) if not form), len(targets))
        unread = self.scratch(form for form, _texture in targets[:count])
        onto = [given if given[1] is not None else spare for given, spare in zip(targets, unread)]
        # One a ``texture``, so the jobs made through it are kept with it (``_job``).
        if texture not in self._reads:
            self._reads[texture] = lambda _contract, resource: texture(resource.name, resource, self.depth_before)
        read = self._reads[texture]
        prepasses = []
        for item in drawn:
            if item.prepass is not None and purpose == "forward" and self._drawable((item.part,)):
                with self._refusing():
                    prepasses += self._steps(item, item.prepass, partial(
                        self._job, item, item.prepass, count, self._engine_texture(PREPASS_PURPOSE), constant_buffer),
                        unread)
        slots, runs = [(prepasses, ())], []
        order = self._forward_order if purpose == "forward" else None
        blendings = (None,) if order is None else (False, True)
        for blending in blendings:
            steps, blended = [], []
            slots.append((steps, blended))
            # Meshlet groups are listed once: added ones make the scene's listings anew (``_regroup``).
            for number, group in enumerate(self._meshlet_groups if listing is None else ()):
                program = group.raster
                if group.purpose != purpose or blending not in (None, _blends_first(program)):
                    continue
                # Every record is made, and its refusals known, before the first draw reading them.
                self._records_made()
                if not self._group_drawable(group):
                    continue
                with self._refusing(), _drawing(*group.parts):
                    if not _written(group.part.document, program) <= set(range(len(targets))):
                        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The pass writes a render target the renderer does not bind for it."))
                    # A step per LOD, kept by frames at its object's (``_seen_steps``).
                    leveled = self.objects[group.owner].lods is not None
                    for level, clusters in group.lods:
                        made = self._steps(_Draw(group.owner, group.part, purpose, program, None), program, partial(
                            self._meshlet_job, [group], program, self._span_roots[number, level], count, read,
                            (self.meshlets.vertices, clusters), constant_buffer), onto, bound, kept=False)
                        if leveled:
                            self._step_levels.update(dict.fromkeys(made, level))
                        (blended if split and _blends_first(program) else steps).extend(made)
            steps, blended, run = [], [], []
            slots.append((steps, blended))
            runs.append(run)
            for item in drawn:
                if blending not in (None, _blends_first(item.program)) or not self._drawable((item.part,)):
                    continue
                with self._refusing():
                    if not _written(item.part.document, item.program) <= set(range(len(targets))):
                        raise PartError([(item.part, MaterialPreviewError(QT_TRANSLATE_NOOP(
                            "MaterialPreview", "The pass writes a render target the renderer does not bind for it.")))])
                    made = self._steps(item, item.program, partial(
                        self._job, item, item.program, count, read, constant_buffer), onto, bound)
                    (blended if split and _blends_first(item.program) else steps).extend(made)
                    if made:
                        run.append((self._box_of(item.part), item.owner, tuple(made),
                                    _priority_bias(item.part.material)))
        if listing is None:
            listing = _Listing([([], []) for _slot in slots], [[] for _run in runs])
        self._phases[key] = listing = listing.extended(slots, runs, len(source))
        for blending, run in zip(blendings, listing.runs):
            rule = None if order is None else order.blended if blending else order.unblended
            if rule in SORTED_ORDERS and len(run) > 1:
                self._forward_runs[run[0][2][0]] = (SORTED_ORDERS[rule], run)
        return listing.steps, listing.blended

    def _forward_sorted(self, steps: list[int]) -> list[int]:
        """In place."""
        if not self._forward_runs:
            return steps
        held = self._forward_at
        if held is None or len(held) != len(self._forward_runs) or any(
                self._forward_runs.get(first) is not entry for first, (entry, _flat) in held.items()):
            self._forward_at = held = {first: (entry, [step for _box, _owner, made, _bias in entry[1] for step in made])
                                       for first, entry in self._forward_runs.items()}
            self._forward_held = {first: data for first, data in self._forward_held.items() if first in held}
        rotation, translation = self.camera.view[:, :3], self.camera.view[:, 3]
        camera = -rotation.T @ translation
        at = None
        for first, ((by_object, run), flat) in held.items():
            # A run is sorted where its steps lie together, as listed or not.
            try:
                start = steps.index(first)
            except ValueError:
                continue
            if steps[start:start + len(flat)] != flat:
                if at is None:
                    at = {step: index for index, step in enumerate(steps)}
                places = sorted(at.get(step, -1) for step in flat)
                if places[0] < 0 or places[-1] - places[0] != len(flat) - 1:
                    continue
                start = places[0]
            order = self._forward_ranks(first, by_object, run, camera)
            steps[start:start + len(flat)] = [step for number in order.tolist() for step in run[number][2]]
        return steps

    def _forward_ranks(self, first: int, by_object: bool, run: list, camera: np.ndarray) -> np.ndarray:
        """By the game's keys of its objects, else by the distance of each draw's box from the eye."""
        owners = list(dict.fromkeys(owner for _box, owner, _made, _bias in run)) if by_object \
            else [owner for _box, owner, _made, _bias in run]
        items = [self.objects[owner] for owner in owners]
        held = self._forward_held.get(first)
        if held is None or held[0] is not run or any(item is not was for item, was in zip(items, held[1])):
            if by_object:
                index = {owner: number for number, owner in enumerate(owners)}
                data = (np.array([_sort_centre(item) for item in items], np.float32).reshape(-1, 3),
                        np.float32([item.transparent_bias for item in items]),
                        np.array([index[owner] for _box, owner, _made, _bias in run], np.int64),
                        np.array([bias for _box, _owner, _made, bias in run], np.int64))
            else:
                centres = []
                for (box, _owner, _made, _bias), item in zip(run, items):
                    skinned = item.skinned and item.placement.bones is not None
                    transforms = item.placement.bones if skinned else item.placement.world[None]
                    world = world_boxes(np.asarray(transforms, np.float64)[None], [MeshBounds(box)])[0]
                    centres.append((world[0] + world[1]) / 2)
                data = np.array(centres, np.float64).reshape(-1, 3)
            self._forward_held[first] = held = run, items, data
        if not by_object:
            return np.argsort(np.linalg.norm(held[2] - camera, axis=1), kind="stable")
        centres, biases, objects, extra = held[2]
        keys = np.clip(transparent_keys(self.camera, _sort_points(centres, biases, camera))[objects] + extra, 0,
                       _KEY_MAX)
        return np.lexsort((np.arange(len(run)), objects, keys))


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
        formats = scene.target_formats
        *targets, depth = scene.finish([*(scene.targets[target] for target in sorted(formats)), scene.depth])
    except PartError as exc:
        raise exc.refusals[0][1] from exc
    return GBufferImage(scene.size, tuple(
        GBufferTarget(target, formats[target], texels) for target, texels in zip(sorted(formats), targets)),
        scene.depth_format, depth, scene.missing_textures)


__all__ = ["DEPTH_PLANES", "DEPTH_VIEWS", "GBufferImage", "GBufferTarget", "MeshBounds", "MeshObject", "MeshPart",
           "MeshScene", "ObjectBox", "PHASES", "PartError", "PhaseKey", "draw_purpose", "mesh_bounds", "object_box",
           "part_draws", "render_gbuffer", "transparent_keys"]
