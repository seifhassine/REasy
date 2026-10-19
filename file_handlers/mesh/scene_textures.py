"""See docs/internals/textures-view.md."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

import numpy as np
from PySide6.QtCore import Qt, Signal

from file_handlers.mmtr.material_preview import MaterialPreviewError
from file_handlers.mmtr.mesh_render import BOX_CORNERS
from file_handlers.mmtr.overlay import Overlay, view_projection
from file_handlers.mmtr.pass_choice import PassChoiceError
from file_handlers.mmtr.profiles import mmtr_profile_for_game
from file_handlers.mmtr.resident import Resident
from file_handlers.mmtr.speedtree_catalog import speedtree_catalog_for
from file_handlers.mmtr.textures_states import ordered
from file_handlers.mmtr.textures_view import ProbeLight, TexturesView, ViewBatch, ViewGeometry, ViewProbes
from file_handlers.stmesh.lit import cluster_route
from file_handlers.stmesh.stmesh_handler import StmeshHandler
from file_handlers.tex.texture_quality import texture_quality_profile
from ui.scene.mesh_scene import mesh_lod_parts
from ui.scene.scene_view import SceneGeometry, ScenePlacement, SceneShape, SceneView
from utils.resource_file_utils import PrefetchedFiles

from .device_preview import DevicePreview
from .lit_preview import UNKNOWN_SKINNING, LitOverlays
from .material_resolver import MeshMaterialResolver
from .material_session import MeshMaterialCollection
from .scene_overlay import SceneOverlay
from .textures_preview import TexturedFrame, TexturedPart, TexturesDrawer, bind_parts, exact_states

# Culling margin beyond the view's sides, as a share of distance, for the device's single precision.
_SLACK = 1e-4


@dataclass(frozen=True, slots=True, eq=False)
class SceneMaterials:
    game: str
    exact: bool
    skinned: bool
    parts: tuple[TexturedPart, ...]
    files: Mapping[str, bytes]
    masters: Mapping[str, tuple[str, bytes]]


@dataclass(frozen=True, slots=True, eq=False)
class SceneContent:
    """``spots`` maps a part number to (mesh, part), part None when unnumbered."""
    geometry: int
    meshes: tuple[SceneGeometry, ...]
    materials: tuple[SceneMaterials, ...]
    spots: tuple[tuple[int, int | None], ...]
    largest_mip: int | None
    anisotropy: int


@dataclass(slots=True)
class _OnDevice:
    """Per object: ``meshes``, ``geometry``, ``culled``, ``worlds``, ``bones``, ``corners``; per geometry: ``vertices``,
    ``shapes``, ``boxes``; ``batches`` gives each batch's object."""
    meshes: list[int]
    vertices: list[int]
    geometry: np.ndarray
    batches: np.ndarray
    culled: np.ndarray
    shapes: list[SceneShape]
    boxes: np.ndarray
    worlds: np.ndarray
    bones: list
    corners: np.ndarray
    placements: tuple[ScenePlacement, ...] | None = None


def _box(shape: SceneShape) -> tuple[np.ndarray, np.ndarray]:
    positions = np.asarray(shape.positions, np.float64)
    return positions.min(axis=0), positions.max(axis=0)


class SceneTexturesDrawer(TexturesDrawer):
    def __init__(self, hardware: bool = True):
        super().__init__(hardware)
        self._held: _OnDevice | None = None
        self._scenes = SceneOverlay(self._cull)

    def _leave(self) -> None:
        super()._leave()
        self._scenes.forget()

    def _bind(self, content: SceneContent) -> dict:
        # ``decoded`` is keyed by id of the file bytes, which the content keeps alive meanwhile.
        bound, decoded = {}, {}
        for materials in content.materials:
            bound.update(bind_parts(materials.parts, materials.game, materials.exact, materials.skinned,
                                    materials.files, materials.masters, content.largest_mip, self._lease, decoded))
        return bound

    def draw(self, content: SceneContent, view: SceneView, placements: tuple[ScenePlacement, ...],
             overlays: LitOverlays) -> TexturedFrame:
        if content is not self._content:
            self._hold(content)
        probes = view.light.probes if isinstance(view.light, ProbeLight) else None
        key = view.size, view.hidden, view.hidden_parts, probes
        if self._drawn is None or self._drawn[0] != key:
            self._leave()
            shown = {}
            for materials in content.materials:
                for part in materials.parts:
                    mesh, piece = content.spots[part.index]
                    name = content.meshes[mesh].key
                    if name not in view.hidden and (name, piece) not in view.hidden_parts:
                        shown[part.index] = part, self._bound[part.index]
            failures = {index: item for index, (_part, item) in shown.items() if isinstance(item, Exception)}
            steps = ordered([(item[0], (part, item[1])) for _index, (part, item) in sorted(shown.items())
                             if not isinstance(item, Exception)])
            drawn = None
            if steps:
                drawn = self._build(content, view, placements, steps, probes)
            self._drawn = (key, drawn, failures)
        _key, drawn, failures = self._drawn
        if drawn is None:
            return TexturedFrame(view, False, failures)
        textures, overlay = drawn
        if placements is not self._held.placements:
            self._place(textures, placements)
        camera = view.camera()
        device = textures.device
        held = self._held
        seen = self._seen(camera)
        if not overlays.filled:
            seen = [batch for batch in seen if content.meshes[held.meshes[held.batches[batch]]].solid]
        if seen:
            device.pending += textures.frame(camera, view.light, overlays.background, seen)
        device.pending += self._scenes.steps(overlay, textures.shown if seen else None, overlays, camera,
                                             content.meshes, held.meshes, placements, view.hidden_parts)
        device.flush()
        return TexturedFrame(view, True, failures, picked=self._scenes.picked())

    def _build(self, content: SceneContent, view: SceneView, placements: tuple[ScenePlacement, ...],
               steps: list, probes: ViewProbes | None) -> tuple[TexturesView, Overlay]:
        meshes = sorted({content.spots[part.index][0] for _states, (part, _texture) in steps})
        slots = {mesh: slot for slot, mesh in enumerate(meshes)}
        vertices = sorted({content.meshes[mesh].vertices for mesh in meshes})
        geometries = {mesh: number for number, mesh in enumerate(vertices)}
        shapes = [placements[mesh].shape for mesh in vertices]
        device = Resident(self._hardware)
        textures = TexturesView(device, view.size, [
            ViewGeometry(shape.positions, shape.normals, content.meshes[mesh].uvs, shape.colours,
                         content.meshes[mesh].bones, content.meshes[mesh].weights)
            for mesh, shape in zip(vertices, shapes, strict=True)], [
            ViewBatch(part.indices, texture, part.tint, part.cutout, states, slots[content.spots[part.index][0]],
                      texture is None)
            for states, (part, texture) in steps],
            objects=[geometries[content.meshes[mesh].vertices] for mesh in meshes], anisotropy=content.anisotropy,
            probes=probes)
        self._held = _OnDevice(
            meshes, vertices, np.array([geometries[content.meshes[mesh].vertices] for mesh in meshes], np.intp),
            np.array([slots[content.spots[part.index][0]] for _states, (part, _texture) in steps], np.intp),
            np.array([content.meshes[mesh].bones is None for mesh in meshes], bool), shapes,
            np.array([_box(shape) for shape in shapes]), np.tile(np.eye(3, 4), (len(meshes), 1, 1)),
            [None] * len(meshes), np.empty((len(meshes), 8, 3)))
        self._corner()
        return textures, Overlay(device, view.size, textures.depth)

    def _place(self, textures: TexturesView, placements: tuple[ScenePlacement, ...]) -> None:
        held = self._held
        for slot, mesh in enumerate(held.vertices):
            shape = placements[mesh].shape
            if shape is not held.shapes[slot]:
                textures.write(slot, positions=shape.positions, normals=shape.normals, colours=shape.colours)
                held.shapes[slot], held.boxes[slot] = shape, _box(shape)
        worlds = np.array([placements[mesh].world for mesh in held.meshes], np.float64).reshape(-1, 3, 4)
        for slot in np.flatnonzero((worlds != held.worlds).any(axis=(1, 2))).tolist():
            textures.place(slot, worlds[slot])
        held.worlds = worlds
        for slot, mesh in enumerate(held.meshes):
            bones = placements[mesh].bones
            if bones is not None and bones is not held.bones[slot]:
                textures.pose(slot, bones)
                held.bones[slot] = bones
        held.placements = placements
        self._corner()

    def _corner(self) -> None:
        held = self._held
        corners = held.boxes[held.geometry][:, BOX_CORNERS, np.arange(3)]
        held.corners = (np.einsum("nki,nji->nkj", corners, held.worlds[:, :, :3])
                        + held.worlds[:, None, :, 3])

    def _seen(self, camera) -> list[int]:
        held = self._held
        matrix = view_projection(camera)
        clip = held.corners @ matrix[:3] + matrix[3]
        w = clip[..., 3]
        beyond = np.zeros(len(held.meshes), bool)
        for along in (clip[..., 0], clip[..., 1]):
            slack = _SLACK * (np.abs(along) + np.abs(w))
            beyond |= (along + w < -slack).all(axis=1) | (w - along < -slack).all(axis=1)
        return np.flatnonzero(~(beyond & held.culled)[held.batches]).tolist()


@dataclass(slots=True)
class _Gathered:
    game: str = ""
    exact: bool = False
    skinned: bool = False
    reason: str = ""
    names: dict[str, str] = field(default_factory=dict)
    parts: list[TexturedPart] = field(default_factory=list)
    files: dict[str, bytes] = field(default_factory=dict)
    masters: dict[str, tuple[str, bytes]] = field(default_factory=dict)
    unusable: dict[str, str] = field(default_factory=dict)
    cache: dict = field(default_factory=dict)


class SceneTexturesPreview(DevicePreview):
    view_type = SceneView
    _game = False
    picked = Signal(int, object, object)

    def __init__(self, viewport, materials, parent=None):
        super().__init__(None, None, lambda: None, parent)
        self._viewport, self._materials = viewport, materials
        self._gathered: int | None = None
        self._labels: list[str] = []
        self._objects: list[str] = []
        # Sent while the viewport paints, so handled after.
        viewport.device_view_changed.connect(self.view_changed, Qt.ConnectionType.QueuedConnection)
        # Sent before the window is destroyed, so handled at once.
        viewport.device_window_changed.connect(self.set_window)
        self.active_changed.connect(viewport.set_device_frames)
        self.frame_changed.connect(viewport.set_device_frame)
        self.picked.connect(viewport.device_picked)

    def view_changed(self, view, overlays=None) -> None:
        if isinstance(view, SceneView) and view.game != self._game:
            view = ""
        super().view_changed(view, overlays)
        if self._active and isinstance(view, SceneView) and view.content != self._gathered:
            self.scene_changed()

    def _make_drawer(self) -> SceneTexturesDrawer:
        return SceneTexturesDrawer()

    def _drawable(self) -> bool:
        # Skipping loses nothing: the viewport sends a view of its content when it paints.
        return super()._drawable() and self._viewport.device_scene()[0] == self._scene.geometry

    def _request(self) -> tuple:
        return self._scene, self._view, self._viewport.device_motion(), self._overlays

    def _on_finished(self, thread, request: tuple, result) -> None:
        if not isinstance(result, Exception) and request[0] is not self._scene:
            result = self._of_other_content(result)
        super()._on_finished(thread, request, result)
        shown = request[3].scene
        if thread is self._thread and shown is not None and shown.pick is not None:
            mesh = None if isinstance(result, Exception) or request[0] is not self._scene else result.picked
            self.picked.emit(request[0].geometry, shown.pick, mesh)

    @staticmethod
    def _of_other_content(frame: TexturedFrame) -> TexturedFrame:
        """Parts gathered since are numbered differently."""
        return replace(frame, failures={})

    def failed_parts(self) -> list[tuple[str, str, str]]:
        return [(self._objects[index], self._labels[index], reason) for index, reason in sorted(self.failures().items())]

    def _failure_lines(self, failures: dict[int, str]) -> list[str]:
        return list(dict.fromkeys(
            self.tr("{part}: not drawn. {reason}").format(part=self._labels[index], reason=reason)
            for index, reason in sorted(failures.items())))

    def _told(self) -> tuple[int, tuple]:
        number, meshes = self._viewport.device_scene()
        self._gathered, self._labels, self._objects = number, [], []
        if self._frame is not None:
            self._frame = self._of_other_content(self._frame)
        return number, meshes or ()

    def _gather(self) -> SceneContent:
        number, meshes = self._told()
        quality = texture_quality_profile(self._materials.texture_quality)
        gathered: dict[object, _Gathered] = {}
        scopes: dict[tuple, dict] = {}
        spots = []
        for mesh_number, mesh in enumerate(meshes):
            for batch in mesh.batches:
                index = len(spots)
                spots.append((mesh_number, batch.part_index))
                name = batch.material_name
                session = self._materials.owner(name) if name else None
                if session not in gathered:
                    gathered[session] = _Gathered() if session is None else self._gathered_of(session, scopes)
                group = gathered[session]
                self._labels.append(group.names.get(name, name))
                self._objects.append(mesh.key)
                reason = self._gather_part(session, group, index, mesh, batch, quality.prefer_streaming)
                if reason:
                    self._failures[index] = reason
        return SceneContent(
            number, meshes, tuple(
                SceneMaterials(group.game, group.exact, group.skinned, tuple(group.parts), PrefetchedFiles(group.files),
                               group.masters)
                for group in gathered.values()),
            tuple(spots), quality.max_dimension, max(1, int(quality.anisotropy)))

    def _gathered_of(self, session, scopes: dict[tuple, dict]) -> _Gathered:
        handler = session.handler
        game = MeshMaterialResolver.material_profile(handler).game
        profile = mmtr_profile_for_game(game)
        exact = exact_states(profile)
        flags = handler.mesh.header.decoded_content_flags
        return _Gathered(
            game, exact, bool(exact and flags is not None and flags.is_skinning),
            str(MaterialPreviewError(UNKNOWN_SKINNING)) if exact and flags is None else "",
            {session.material_key(binding.mesh_material_name): binding.mesh_material_name
             for binding in session.bindings},
            cache=scopes.setdefault(MeshMaterialResolver.resource_scope(handler), {}))

    def _gather_part(self, session, group: _Gathered, index: int, mesh: SceneGeometry, batch,
                     streaming: bool) -> str:
        name = batch.material_name
        if session is None and not name:
            group.parts.append(TexturedPart(index, "", batch.indices, "", mesh.colour, False, False,
                                            bool(batch.transparent), "", None, None))
            return ""
        if session is None:
            group.parts.append(TexturedPart.unmatched(index, name, batch.indices, batch, mesh.colour))
            return ""
        if group.reason:
            return group.reason
        surface = session.profiles.get(name)
        if surface is None or group.exact and surface.instance_context is None:
            group.parts.append(TexturedPart.unmatched(index, name, batch.indices, batch, mesh.colour))
            return ""
        if name not in group.unusable:
            group.unusable[name] = self._gather_material(
                session.handler, surface, group.masters if group.exact else None, group.files, streaming, group.cache)
        if group.unusable[name]:
            return group.unusable[name]
        route, exact = None, group.exact
        handler = session.handler
        if exact and isinstance(handler, StmeshHandler) and batch.submesh is not None:
            # SpeedTree cluster programs follow the batch type.
            lod, number = batch.submesh
            try:
                route = cluster_route(handler, lod, mesh_lod_parts(handler.mesh, lod)[number][1],
                                      int(surface.material_flags),
                                      speedtree_catalog_for(mmtr_profile_for_game(group.game)),
                                      mesh.speedtree_placement)
            except (MaterialPreviewError, PassChoiceError):
                exact = False
        group.parts.append(TexturedPart.of_surface(index, name, batch.indices, surface, batch, exact, mesh.colour,
                                                   route=route))
        return ""


def plain_preview(viewport, parent) -> SceneTexturesPreview:
    preview = SceneTexturesPreview(viewport, MeshMaterialCollection(viewport, parent=parent), parent)
    preview.set_active(True)
    return preview


__all__ = ["SceneContent", "SceneMaterials", "SceneTexturesDrawer", "SceneTexturesPreview", "plain_preview"]
