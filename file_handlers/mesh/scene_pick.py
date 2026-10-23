"""Picking a scene's mesh at a texel on the CPU, as the overlay's rules cull (docs/internals/overlay.md, Pick)."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import numpy as np

from file_handlers.sdf.sdf_file import CULL_NONE
from utils.native_build import ensure_fastmesh

lazy from file_handlers.mmtr.engine_buffers import Camera
lazy from file_handlers.mmtr.overlay import view_projection
lazy from ui.scene.scene_view import SceneGeometry, ScenePlacement

_fastmesh = ensure_fastmesh()


@dataclass(frozen=True, slots=True, eq=False)
class ScenePickable:
    """What a frame drew: the numbers of the content's meshes drawn, each batch's culling and facing by its number over
    the content's meshes (None: culled as CULL_NONE), and each mesh's vertices' box centre and half extent."""
    meshes: tuple[int, ...]
    cull: Callable[[int], tuple[int, bool] | None]
    centres: np.ndarray
    extents: np.ndarray


class ScenePicker:
    """Held between picks: each mesh's triangles shown by its culls, and the boxes placed of the meshes drawn."""

    def __init__(self):
        self._key: tuple | None = None
        # By mesh: its culls, and its triangles and runs (None: no part shown).
        self._meshes: dict[int, tuple] = {}
        self._placed: tuple | None = None
        # By mesh: its bones, rest positions and posed positions.
        self._posed: dict[int, tuple] = {}

    def pick(self, camera: Camera, texel: Sequence[int], meshes: Sequence[SceneGeometry],
             placements: Sequence[ScenePlacement], pickable: ScenePickable, hidden: frozenset = frozenset(),
             hidden_parts: frozenset = frozenset()) -> int | None:
        """The number of the mesh nearest at ``texel`` of ``camera``'s image, of those drawn and not hidden."""
        if self._key is None or self._key[0] is not meshes or self._key[1] != hidden_parts:
            if self._key is None or self._key[0] is not meshes:
                self._posed = {}
            self._key, self._meshes, self._placed = (meshes, hidden_parts), {}, None
            self._offsets = np.cumsum([0, *(len(geometry.batches) for geometry in meshes)]).tolist()
        placed = self._placed
        if placed is None or placed[0] is not pickable or placed[1] is not placements or placed[2] != hidden:
            placed = self._placed = pickable, placements, hidden, *self._place(meshes, placements, pickable, hidden)
        numbers, worlds, centres, extents, skinned = placed[3:]
        if not numbers:
            return None
        origin, direction = _ray(camera, texel)
        with np.errstate(divide="ignore", invalid="ignore"):
            inverse = 1 / direction
            near, far = (centres - extents - origin) * inverse, (centres + extents - origin) * inverse
        enter = np.nanmax(np.minimum(near, far), axis=1)
        leave = np.nanmin(np.maximum(near, far), axis=1)
        met = skinned | (leave >= np.maximum(enter, 0))
        enter = np.where(skinned, 0, np.maximum(enter, 0))
        best, found = np.inf, None
        for row in np.flatnonzero(met)[np.argsort(enter[met], kind="stable")].tolist():
            if enter[row] > best:
                break
            number = numbers[row]
            shown = self._mesh(number, meshes, pickable, hidden_parts)
            if shown is None:
                continue
            world = worlds[row]
            rotation = world[:, :3]
            back = np.linalg.inv(rotation)
            hit = _fastmesh.ray_pick(tuple(back @ (origin - world[:, 3])), tuple(back @ direction),
                                     self._positions(number, meshes, placements), *shown,
                                     bool(np.linalg.det(rotation) < 0))
            if hit is not None and hit[0] < best:
                best, found = hit[0], number
        return found

    def _place(self, meshes: Sequence[SceneGeometry], placements: Sequence[ScenePlacement], pickable: ScenePickable,
               hidden: frozenset) -> tuple:
        """The meshes drawn and not hidden, their worlds, placed boxes' centres and half extents, and being skinned."""
        rows = [row for row, number in enumerate(pickable.meshes) if meshes[number].key not in hidden]
        numbers = [pickable.meshes[row] for row in rows]
        worlds = np.array([placements[number].world for number in numbers], np.float64).reshape(-1, 3, 4)
        centres = np.einsum("oij,oj->oi", worlds[:, :, :3], pickable.centres[rows]) + worlds[:, :, 3]
        extents = np.einsum("oij,oj->oi", np.abs(worlds[:, :, :3]), pickable.extents[rows])
        skinned = np.array([placements[number].bones is not None for number in numbers], bool)
        return numbers, worlds, centres, extents, skinned

    def _mesh(self, number: int, meshes: Sequence[SceneGeometry], pickable: ScenePickable,
              hidden_parts: frozenset) -> tuple | None:
        geometry, offset = meshes[number], self._offsets[number]
        culls = tuple(pickable.cull(offset + batch) for batch in range(len(geometry.batches)))
        held = self._meshes.get(number)
        if held is None or held[0] != culls:
            triangles, runs, first = [], [], 0
            for item, cull in zip(geometry.batches, culls):
                if (geometry.key, item.part_index) in hidden_parts:
                    continue
                indices = np.ascontiguousarray(item.indices, "<u4").reshape(-1)
                cull, counter_clockwise = cull or (CULL_NONE, True)
                triangles.append(indices)
                runs.append((first, len(indices) // 3, cull, int(bool(counter_clockwise))))
                first += len(indices) // 3
            held = self._meshes[number] = culls, (np.concatenate(triangles), np.array(runs, "<i4")) if first else None
        return held[1]

    def _positions(self, number: int, meshes: Sequence[SceneGeometry], placements: Sequence[ScenePlacement]):
        """Its vertices as the overlay places them before the world: posed by its bones."""
        geometry = meshes[number]
        positions = np.ascontiguousarray(placements[geometry.vertices].shape.positions, "<f4").reshape(-1, 3)
        bones = placements[number].bones
        if bones is None:
            return positions
        of = meshes[geometry.vertices]
        held = self._posed.get(number)
        if held is None or held[0] is not bones or held[1] is not positions:
            rows = np.asarray(bones, np.float64).reshape(-1, 3, 4)
            matrices = np.zeros((len(rows), 4, 4), "<f4")
            matrices[:, :3, :4], matrices[:, 3, 3] = rows, 1
            posed, _normals = _fastmesh.skin_vertices(
                positions, None, np.ascontiguousarray(of.bones, "<u2"), np.ascontiguousarray(of.weights, "<f4"),
                np.ascontiguousarray(matrices.transpose(0, 2, 1)))
            self._posed[number] = held = bones, positions, np.frombuffer(posed, "<f4").reshape(-1, 3)
        return held[2]


def _ray(camera: Camera, texel: Sequence[int]) -> tuple[np.ndarray, np.ndarray]:
    """Through the texel's centre, from the near plane on: depth over w 1 there, nearer greater."""
    width, height = camera.size
    column, row = texel
    x, y = 2 * (column + 0.5) / width - 1, 1 - 2 * (row + 0.5) / height
    inverse = np.linalg.inv(view_projection(camera).astype(np.float64))
    near, deeper = (np.array([x, y, depth, 1.0]) @ inverse for depth in (1.0, 0.5))
    origin = near[:3] / near[3]
    return origin, deeper[:3] / deeper[3] - origin


__all__ = ["ScenePickable", "ScenePicker"]
