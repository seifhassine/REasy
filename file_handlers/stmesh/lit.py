"""See docs/internals/speedtree.md."""
from __future__ import annotations

import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.engine_buffers import EngineConstant, InstanceInput
from file_handlers.mmtr.material_preview import MaterialPreviewError
from file_handlers.mmtr.meshlets import EXTENDED, Cluster, MeshletBuffer, MeshletPrograms
from file_handlers.mmtr.pass_choice import PassChoiceError
from file_handlers.mmtr.mesh_draw import MeshBuffer, SubmeshRange
from file_handlers.mmtr.speedtree_catalog import FoliageBuffers, SpeedTreeCatalog
from file_handlers.mmtr.speedtree_programs import (
    speedtree_cluster_route, speedtree_meshlet_programs, speedtree_placement,
)

from .meshlets import pack_blob
from .stmesh_file import FLAG_BILLBOARD_LOD, BatchType, BillboardFaceCrossFadeThreshold, Geometry, StmeshFile
from .wind import rest_state


@dataclass(frozen=True, slots=True)
class StmeshDraw:
    buffer: MeshBuffer
    submesh: SubmeshRange
    batch: BatchType
    material: int


def _buffer(geometry: Geometry, indices: bytes) -> MeshBuffer:
    return MeshBuffer(geometry.stream_bytes(), geometry.vertex_count, indices, False)


def stmesh_draws(mesh: StmeshFile, lod: int) -> list[StmeshDraw]:
    if mesh.meshlets is not None:
        raise ValueError("a meshlet stmesh is drawn by the meshlet programs")
    indices = mesh.indices.tobytes()
    buffers = {False: None if mesh.geometry is None else _buffer(mesh.geometry, indices),
               True: None if mesh.billboards is None else _buffer(mesh.billboards, indices)}
    draws = []
    for cluster in mesh.lods[lod].clusters:
        buffer = buffers[cluster.batch_type == BatchType.BILLBOARDS]
        if buffer is None:
            raise ValueError("a stmesh's cluster draws geometry the file does not hold")
        draws.append(StmeshDraw(buffer, SubmeshRange(cluster.start_index, cluster.index_count, cluster.base_vertex),
                                BatchType(cluster.batch_type), cluster.material_id))
    return draws


def meshlet_buffer(mesh: StmeshFile, runs: Mapping[SubmeshRange, Sequence[tuple[int, int]]]) -> MeshletBuffer:
    meshlets = mesh.meshlets
    blob, offsets = pack_blob(meshlets, meshlets.blob_offset)
    at = dict(zip(((lod, number) for lod, entry in enumerate(meshlets.lods) for number in range(len(entry.chunks))),
                  offsets))

    def cluster(lod: int, number: int) -> Cluster:
        chunk = meshlets.lods[lod].chunks[number]
        return Cluster(at[lod, number] // 4, len(chunk.triangles), chunk.material_id)
    return MeshletBuffer(blob, MappingProxyType({key: tuple(cluster(*chunk) for chunk in chunks)
                                                 for key, chunks in runs.items()}))


def billboard_lod(mesh: StmeshFile, lod: int) -> bool:
    return bool(mesh.flags & FLAG_BILLBOARD_LOD) and lod == len(mesh.meshlets.lods) - 1


def drawn_chunks(handler, lod: int, submesh, material_flags: int, catalog: SpeedTreeCatalog | None,
                 placement: str | None) -> tuple[MeshletBuffer, SubmeshRange, MeshletPrograms]:
    buffer, found = handler.meshlet_draw(submesh)
    programs = speedtree_meshlet_programs(catalog, placement, material_flags, billboard_lod(handler.stmesh, lod))
    return buffer, found, programs


def billboard_threshold(mesh: StmeshFile, foliage: FoliageBuffers | None) -> float:
    """The billboard cross-fade cosine."""
    views = mesh.billboard_side_view_count
    kind = BillboardFaceCrossFadeThreshold(mesh.billboard_cross_fade).name
    billboards = None if foliage is None else foliage.billboards
    found = None if billboards is None else billboards.cross_fade.get(kind, {}).get(views)
    if found is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "No capture shows how the renderer fades the billboards of a SpeedTree mesh with a "
                               "{kind} face cross-fade threshold and {views} side views."),
            {"kind": kind.lower(), "views": views})
    return found


def _pivots(mesh: StmeshFile) -> bytes:
    """float3 per pivot."""
    return np.asarray(mesh.group_pivots, "<f4").tobytes()


def engine_buffers(mesh: StmeshFile, catalog: SpeedTreeCatalog | None,
                   placement: str | None) -> dict[str, bytes | EngineConstant]:
    try:
        drawn = speedtree_placement(catalog, placement)
    except PassChoiceError:
        return {}
    if drawn.meshlets is not None:
        return {**drawn.meshlets.instance, EXTENDED: drawn.meshlets.extended}
    if drawn.foliage is None:
        buffers: dict[str, bytes | EngineConstant] = {
            item.name: EngineConstant(item.fields, item.world, item.previous_world) for item in drawn.constants}
        if drawn.wind_state is not None:
            name, states = drawn.wind_state
            buffers[name] = rest_state(mesh.wind) * states
        buffers.update({name: InstanceInput(data) for name, data in drawn.instance_inputs.items()})
        if drawn.pivots is not None:
            buffers[drawn.pivots] = _pivots(mesh)
        return buffers
    foliage = drawn.foliage
    names = foliage.buffers
    buffers = {names.wind_state: rest_state(mesh.wind) * names.wind_states,
               names.lod_transition: names.lod_transition_shown}
    if names.density_cull is not None:
        buffers[names.density_cull] = names.density_cull_shown
    if drawn.pivots is not None:
        buffers[drawn.pivots] = _pivots(mesh)
    billboards = foliage.billboards
    if mesh.billboards is None or billboards is None:
        return buffers
    try:
        threshold = billboard_threshold(mesh, foliage)
    except MaterialPreviewError:
        return buffers
    fade = int(np.array(threshold, np.float16).view(np.uint16)) | billboards.cross_fading_flag
    buffers[billboards.mesh_constant] = struct.pack("<fI", billboards.wind_noise_scale, fade).ljust(
        billboards.mesh_constant_size, bytes(1))
    buffers[billboards.normals_buffer] = np.asarray(billboards.normals, np.float32).tobytes()
    return buffers


def cluster_route(handler, lod: int, submesh, material_flags: int, catalog: SpeedTreeCatalog | None,
                  placement: str | None) -> str:
    return speedtree_cluster_route(catalog, placement, handler.cluster_draw(lod, submesh).batch, material_flags,
                                   handler.stmesh.wind.is_grass > 0)


def drawn_cluster(handler, lod: int, submesh, material_flags: int, catalog: SpeedTreeCatalog | None,
                  placement: str | None) -> tuple[StmeshDraw, str]:
    draw = handler.cluster_draw(lod, submesh)
    route = cluster_route(handler, lod, submesh, material_flags, catalog, placement)
    if draw.batch == BatchType.BILLBOARDS:
        billboard_threshold(handler.stmesh, speedtree_placement(catalog, placement).foliage)
    return draw, route


__all__ = ["StmeshDraw", "billboard_lod", "billboard_threshold", "cluster_route", "drawn_chunks", "drawn_cluster",
           "engine_buffers", "meshlet_buffer", "stmesh_draws"]
