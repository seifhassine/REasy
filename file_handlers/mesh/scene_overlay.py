from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

import numpy as np

from file_handlers.mmtr.engine_buffers import Camera
from file_handlers.mmtr.overlay import Overlay
from file_handlers.mmtr.overlay_scene import OverlayGeometry, OverlayPart, OverlayScene
from ui.scene.scene_view import SceneGeometry, ScenePlacement

from .lit_preview import LitOverlays


class SceneOverlay:
    def __init__(self, culls: Callable[[int], tuple[int, bool]]):
        self._culls = culls
        self._key: tuple | None = None
        self._scene: OverlayScene | None = None
        self._meshes: list[int] = []
        self._objects: dict[int, int] = {}
        self._firsts: list[int] = []
        self._given: tuple[list, np.ndarray, list] | None = None
        self._placed: Sequence[ScenePlacement] | None = None
        self._picking = False

    def forget(self) -> None:
        """Call on leaving its device: the held scene's buffers live on it."""
        self._key = self._scene = self._given = self._placed = None

    def _made(self, overlay: Overlay, meshes: Sequence[SceneGeometry], drawn: Sequence[int],
              placements: Sequence[ScenePlacement], hidden_parts: frozenset) -> None:
        key = (overlay, id(meshes), tuple(drawn), hidden_parts)
        if key == self._key:
            if self._scene.size != overlay.size:
                self._scene.resize(overlay.size)
            return
        groups: dict[tuple, int] = {}
        geometries, numbers, firsts = [], [], []
        offsets = np.cumsum([0, *(len(geometry.batches) for geometry in meshes)])
        for mesh in drawn:
            geometry = meshes[mesh]
            batches = [(batch, self._culls(int(offsets[mesh]) + batch)) for batch, item in enumerate(geometry.batches)
                       if (geometry.key, item.part_index) not in hidden_parts]
            group = (geometry.vertices, tuple(batches), geometry.solid)
            if group not in groups:
                groups[group] = len(geometries)
                firsts.append(len(numbers))
                of = meshes[geometry.vertices]
                geometries.append(OverlayGeometry(
                    placements[geometry.vertices].shape.positions,
                    tuple(OverlayPart(geometry.batches[batch].indices, *cull) for batch, cull in batches),
                    of.bones, of.weights, not geometry.solid))
            numbers.append(groups[group])
        self._scene = OverlayScene(overlay.device, overlay.size, geometries, numbers)
        self._key, self._meshes, self._firsts = key, list(drawn), firsts
        self._objects = {mesh: number for number, mesh in enumerate(self._meshes)}
        self._placed = None
        self._given = ([placements[meshes[mesh].vertices].shape for mesh in (drawn[first] for first in firsts)],
                       np.tile(np.eye(3, 4), (len(numbers), 1, 1)), [None] * len(numbers))

    def _place(self, meshes: Sequence[SceneGeometry], placements: Sequence[ScenePlacement]) -> None:
        if placements is self._placed:
            return
        scene, (held_shapes, held_worlds, held_bones) = self._scene, self._given
        shapes = [placements[meshes[self._meshes[first]].vertices].shape for first in self._firsts]
        worlds = np.array([placements[mesh].world for mesh in self._meshes], np.float64).reshape(-1, 3, 4)
        bones = [placements[mesh].bones for mesh in self._meshes]
        for number, shape in enumerate(shapes):
            if shape is not held_shapes[number]:
                scene.write(number, shape.positions)
        for number in np.flatnonzero((worlds != held_worlds).any(axis=(1, 2))).tolist():
            scene.place(number, worlds[number])
        for number, (posed, held) in enumerate(zip(bones, held_bones)):
            if posed is not None and posed is not held:
                scene.pose(number, posed)
        self._given, self._placed = (shapes, worlds, bones), placements

    def steps(self, overlay: Overlay, shown: int | None, overlays: LitOverlays, camera: Camera,
              meshes: Sequence[SceneGeometry], drawn: Sequence[int], placements: Sequence[ScenePlacement],
              hidden_parts: frozenset) -> list[int]:
        scene, scene_shown = None, overlays.scene
        if scene_shown is not None:
            self._made(overlay, meshes, drawn, placements, hidden_parts)
            self._place(meshes, placements)
            objects = self._objects
            scene, scene_shown = self._scene, replace(
                scene_shown, hovered=objects.get(scene_shown.hovered),
                selected=frozenset(objects[mesh] for mesh in scene_shown.selected if mesh in objects))
        self._picking = scene_shown is not None and scene_shown.pick is not None
        return overlay.steps(shown, overlays.background, camera, overlays.overlay_marks(), scene, scene_shown)

    def picked(self) -> int | None:
        if not self._picking:
            return None
        data, = self._scene.device.scene.run([], [self._scene.picked])
        seen = OverlayScene.picked_of(data)
        return None if seen is None else self._meshes[seen[0]]


__all__ = ["SceneOverlay"]
