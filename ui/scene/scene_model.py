from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

lazy import numpy as np

lazy from file_handlers.mmtr.draw_rules import MESH_DECAL_RECEIVE


@dataclass(slots=True)
class SceneDrawBatch:
    indices: np.ndarray
    material_name: str = ""
    part_index: int | None = None
    alpha_test: bool = False
    transparent: bool = False
    # (LOD, number in ``mesh_lod_parts`` order) of the main geometry submesh; None for other batches.
    submesh: tuple[int, int] | None = None
    # Submeshes from ``submesh`` the batch draws: a meshlet run (``meshlet_runs``) holds more than one.
    submeshes: int = 1


@dataclass(slots=True)
class SceneDrawMesh:
    key: str
    vertices: np.ndarray
    indices: np.ndarray
    color: tuple[float, float, float] | tuple[float, float, float, float]
    force_solid: bool = False
    ignore_highlight_filter: bool = False
    normals: np.ndarray | None = None
    tangents: np.ndarray | None = None
    uvs: np.ndarray | None = None
    uv1: np.ndarray | None = None
    uv2: np.ndarray | None = None
    colors: np.ndarray | None = None
    # Skin influences (n x k): bone remap entries and weights.
    bones: np.ndarray | None = None
    weights: np.ndarray | None = None
    material_name: str = ""
    batches: list[SceneDrawBatch] = field(default_factory=list)
    transform_matrix: np.ndarray | None = None
    bounds_minimum: np.ndarray | None = None
    bounds_maximum: np.ndarray | None = None
    # Whether the recalculated normals leave the edges' out (``via.render.Mesh``'s ``NREdgeExcluded``).
    edge_excluded: bool = False
    decal_receive: tuple[str, str | None] = MESH_DECAL_RECEIVE
    # ``via.render.Mesh``'s ``TransparentBias``; 0 is its default.
    transparent_bias: float = 0.0
    # One of ``stmesh_file.PLACEMENTS``.
    speedtree_placement: str | None = None
    blend_normal: bool = True
    chain_flags: tuple[bool, bool] = (True, False)
    draw_state: Mapping[str, object] | None = None
    lod_parent_mesh: bool = False
