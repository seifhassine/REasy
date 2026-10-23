from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

lazy import numpy as np

lazy from file_handlers.mmtr.engine_buffers import Camera
lazy from file_handlers.mmtr.overlay import Overlay
lazy from file_handlers.mmtr.overlay_scene import OverlayGeometry, OverlayPart, OverlayScene
lazy from file_handlers.sdf.sdf_file import CULL_NONE
lazy from ui.scene.scene_view import SceneGeometry, ScenePlacement, moved_placements

lazy from .lit_preview import LitOverlays
lazy from .scene_pick import ScenePickable


class SceneOverlay:
    def __init__(self, culls: Callable[[int], tuple[int, bool] | None]):
        """``culls(batch)``: the batch's (cull mode, winding); None while not known, culled as CULL_NONE until it is."""
        self._culls = culls
        self._key: tuple | None = None
        self._drawn: Sequence[int] | None = None
        self._scene: OverlayScene | None = None
        self._meshes: list[int] = []
        # By object: its geometry.
        self._numbers: list[int] = []
        self._objects: dict[int, int] = {}
        self._groups: dict[tuple, int] = {}
        self._firsts: list[int] = []
        # Each mesh's first batch number.
        self._offsets: list[int] = []
        # By object a batch of which is of faces not known yet: that batch's number.
        self._unfaced: dict[int, int] = {}
        self._given: tuple[list, np.ndarray, list] | None = None
        self._placed: Sequence[ScenePlacement] | None = None
        # The mesh of each object, and of each geometry's vertices.
        self._rows = self._first_rows = np.zeros(0, np.int64)
        # The meshes hidden, the objects drawn then, and each object's being of one.
        self._hiding: tuple | None = None
        # The parts hidden the objects are cast for.
        self._hidden_parts: frozenset = frozenset()
        self._pickable: ScenePickable | None = None

    def forget(self) -> None:
        """Call on leaving its device: the held scene's buffers live on it."""
        self._key = self._drawn = self._scene = self._given = self._placed = None

    def _group(self, meshes: Sequence[SceneGeometry], mesh: int, hidden_parts: frozenset) -> tuple:
        geometry, offset = meshes[mesh], self._offsets[mesh]
        return geometry.vertices, tuple(
            (batch, self._culls(offset + batch)) for batch, item in enumerate(geometry.batches)
            if (geometry.key, item.part_index) not in hidden_parts), geometry.solid

    def _cast(self, group: tuple, mesh: int, number: int) -> tuple:
        """``group`` with its batches of faces not known culled as CULL_NONE."""
        vertices, batches, solid = group
        unknown = next((batch for batch, cull in batches if cull is None), None)
        if unknown is None:
            self._unfaced.pop(number, None)
            return group
        self._unfaced[number] = self._offsets[mesh] + unknown
        return vertices, tuple((batch, cull or (CULL_NONE, True)) for batch, cull in batches), solid

    def _number(self, group: tuple, first: int, meshes: Sequence[SceneGeometry],
                placements: Sequence[ScenePlacement], made: list[OverlayGeometry]) -> int:
        if group not in self._groups:
            self._groups[group] = len(self._groups)
            self._firsts.append(first)
            vertices, batches, solid = group
            of, drawn = meshes[vertices], meshes[self._meshes[first]]
            made.append(OverlayGeometry(
                placements[vertices].shape.positions,
                tuple(OverlayPart(drawn.batches[batch].indices, *cull) for batch, cull in batches),
                of.bones, of.weights, not solid))
        return self._groups[group]

    def _made(self, overlay: Overlay, meshes: Sequence[SceneGeometry], drawn: Sequence[int],
              placements: Sequence[ScenePlacement], hidden_parts: frozenset) -> None:
        """Meshes drawn after the others, the others' faces told since and parts hidden change the held scene alone."""
        key = (overlay, id(meshes))
        if key == self._key and drawn is self._drawn and hidden_parts == self._hidden_parts:
            if self._scene.size != overlay.size:
                self._scene.resize(overlay.size)
            return
        if key != self._key or list(drawn[:len(self._meshes)]) != self._meshes:
            if self._scene is not None and self._scene.device is overlay.device:
                self._scene.release()
            self._key, self._scene, self._meshes, self._numbers, self._objects = key, None, [], [], {}
            self._groups, self._firsts, self._unfaced = {}, [], {}
            self._offsets = np.cumsum([0, *(len(geometry.batches) for geometry in meshes)]).tolist()
            self._given = ([], np.zeros((0, 3, 4)), [])
            self._hidden_parts = hidden_parts
        self._drawn, start, self._pickable = drawn, len(self._meshes), None
        made, numbers, recast = [], [], {}
        # An object's batches are told their faces together, by its first LOD; objects of meshes whose parts hidden
        # changed are cast again.
        again = {number for number, batch in self._unfaced.items() if self._culls(batch) is not None}
        changed = {name for name, _piece in hidden_parts ^ self._hidden_parts}
        if changed:
            again.update(number for number, mesh in enumerate(self._meshes) if meshes[mesh].key in changed)
        self._hidden_parts = hidden_parts
        for number in sorted(again):
            mesh = self._meshes[number]
            group = self._cast(self._group(meshes, mesh, hidden_parts), mesh, number)
            if self._groups.get(group) != self._numbers[number]:
                recast[number] = self._numbers[number] = self._number(group, number, meshes, placements, made)
        self._meshes += drawn[start:]
        for number, mesh in enumerate(drawn[start:], start):
            group = self._cast(self._group(meshes, mesh, hidden_parts), mesh, number)
            numbers.append(self._number(group, number, meshes, placements, made))
        self._numbers += numbers
        if self._scene is None:
            self._scene = OverlayScene(overlay.device, overlay.size, made, numbers)
        else:
            self._scene.add(made, numbers)
            self._scene.recast(recast)
            if self._scene.size != overlay.size:
                self._scene.resize(overlay.size)
        self._objects.update((mesh, number) for number, mesh in enumerate(drawn[start:], start))
        # The objects added placed and posed here, and those cast again posed again: the others are as they were.
        shapes, worlds, bones = self._given
        added = [placements[mesh] for mesh in drawn[start:]]
        placed = np.array([placement.world for placement in added], np.float64).reshape(-1, 3, 4)
        for number, placement in enumerate(added, start):
            self._scene.place(number, placement.world)
        for number, posed in [*((number, bones[number]) for number in recast),
                              *enumerate((placement.bones for placement in added), start)]:
            if posed is not None:
                self._scene.pose(number, posed)
        self._given = ([*shapes, *(placements[meshes[self._meshes[first]].vertices].shape
                                   for first in self._firsts[len(shapes):])],
                       np.concatenate([worlds, placed]), bones + [placement.bones for placement in added])
        self._rows = np.array(self._meshes, np.int64)
        self._first_rows = np.array([meshes[self._meshes[first]].vertices for first in self._firsts], np.int64)

    def _place(self, meshes: Sequence[SceneGeometry], placements: Sequence[ScenePlacement]) -> None:
        if placements is self._placed:
            return
        scene, (shapes, worlds, bones) = self._scene, self._given
        moved = moved_placements(placements, self._placed)
        for number in np.flatnonzero(moved[self._first_rows]).tolist():
            shape = placements[self._first_rows[number]].shape
            if shape is not shapes[number]:
                scene.write(number, shape.positions)
                shapes[number] = shape
                self._pickable = None
        for number in np.flatnonzero(moved[self._rows]).tolist():
            placement = placements[self._meshes[number]]
            world = np.asarray(placement.world, np.float64).reshape(3, 4)
            if (world != worlds[number]).any():
                scene.place(number, world)
                worlds[number] = world
            if placement.bones is not None and placement.bones is not bones[number]:
                scene.pose(number, placement.bones)
            bones[number] = placement.bones
        self._placed = placements

    def steps(self, overlay: Overlay, shown: int | None, overlays: LitOverlays, camera: Camera,
              meshes: Sequence[SceneGeometry], drawn: Sequence[int], placements: Sequence[ScenePlacement],
              hidden_parts: frozenset, hidden: frozenset = frozenset()) -> list[int]:
        """``hidden``: keys of meshes drawn hidden, as if out of view."""
        scene, scene_shown = None, overlays.scene
        if scene_shown is not None:
            self._made(overlay, meshes, drawn, placements, hidden_parts)
            self._place(meshes, placements)
            held = self._hiding
            if held is None or held[0] != hidden or held[1] is not self._meshes or held[2] != len(self._meshes):
                self._hiding = hidden, self._meshes, len(self._meshes), np.array(
                    [meshes[mesh].key in hidden for mesh in self._meshes], bool)
            self._scene.hide(self._hiding[3])
            objects = self._objects
            scene, scene_shown = self._scene, replace(
                scene_shown, hovered=objects.get(scene_shown.hovered),
                selected=frozenset(objects[mesh] for mesh in scene_shown.selected if mesh in objects))
        self._pickable = None if scene_shown is None else self._pickable or ScenePickable(
            tuple(self._meshes), self._culls, *self._scene.boxes())
        return overlay.steps(shown, overlays.background, camera, overlays.overlay_marks(), scene, scene_shown)

    def pickable(self) -> ScenePickable | None:
        """What the last steps drew of the scene, None if none."""
        return self._pickable


__all__ = ["SceneOverlay"]
