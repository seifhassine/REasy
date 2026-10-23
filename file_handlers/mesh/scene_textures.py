"""See docs/internals/textures-view.md."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass, replace

import numpy as np
from PySide6.QtCore import Qt

from file_handlers.mmtr.material_preview import MaterialPreviewError
from file_handlers.mmtr.mesh_render import BOX_CORNERS
from file_handlers.mmtr.overlay import Overlay, view_projection
from file_handlers.mmtr.pass_choice import PassChoiceError
from file_handlers.mmtr.profiles import mmtr_profile_for_game
from file_handlers.mmtr.resident import Resident
lazy from file_handlers.mmtr.speedtree_catalog import speedtree_catalog_for
from file_handlers.mmtr.textures_states import ordered
from file_handlers.mmtr.textures_view import DrawStates, ProbeLight, TexturesView, ViewBatch, ViewGeometry, ViewProbes
lazy from file_handlers.stmesh.lit import cluster_route
lazy from file_handlers.stmesh.stmesh_handler import StmeshHandler
from file_handlers.tex.texture_quality import texture_quality_profile
lazy from ui.scene.mesh_scene import mesh_lod_parts
from ui.scene.scene_view import SceneGeometry, ScenePlacement, SceneShape, SceneView, moved_placements
from utils.resource_file_utils import PrefetchedFiles

from .device_preview import DevicePreview
from .lit_preview import UNKNOWN_SKINNING, LitOverlays
from .material_resolver import MeshMaterialResolver
lazy from .material_session import MeshMaterialCollection
lazy from .scene_overlay import SceneOverlay
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
    ``shapes``, ``boxes``; ``batches`` gives each batch's object and ``pieces`` its (mesh key, piece)."""
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
    pieces: list[tuple[str, int]] = field(default_factory=list)


def _box(shape: SceneShape) -> tuple[np.ndarray, np.ndarray]:
    positions = np.asarray(shape.positions, np.float64)
    return positions.min(axis=0), positions.max(axis=0)


def _geometries(content: SceneContent, vertices: list[int], shapes: list[SceneShape]) -> list[ViewGeometry]:
    return [ViewGeometry(shape.positions, shape.normals, content.meshes[mesh].uvs, shape.colours,
                         content.meshes[mesh].bones, content.meshes[mesh].weights)
            for mesh, shape in zip(vertices, shapes, strict=True)]


def _same(value, other) -> bool:
    """Whether two gathered values hold the same: arrays by their elements, dataclasses and tuples by their items."""
    if value is other:
        return True
    if isinstance(value, np.ndarray) or isinstance(other, np.ndarray):
        return (isinstance(value, np.ndarray) and isinstance(other, np.ndarray) and value.dtype == other.dtype
                and np.array_equal(value, other, equal_nan=value.dtype.kind in "fc"))
    if isinstance(value, tuple):
        return isinstance(other, tuple) and len(value) == len(other) and all(map(_same, value, other))
    if is_dataclass(value):
        return type(other) is type(value) and all(_same(getattr(value, item.name), getattr(other, item.name))
                                                  for item in fields(value))
    return value == other


def _pieces(content: SceneContent, steps: list) -> list[tuple[str, int]]:
    return [(content.meshes[content.spots[part.index][0]].key, content.spots[part.index][1])
            for _states, (part, _texture) in steps]


class SceneTexturesDrawer(TexturesDrawer):
    def __init__(self, hardware: bool = True):
        super().__init__(hardware)
        self._held: _OnDevice | None = None
        # The meshes held, those hidden and whether each is; the batches held, the parts hidden and whether each is
        # of one shown.
        self._hiding: tuple | None = None
        self._showing: tuple | None = None
        self._scenes = SceneOverlay(self._cull)
        self._decoded: dict[int, tuple | None] = {}
        # The view's batch of each part drawn, and of its depth prepass.
        self._batches: dict[tuple[int, DrawStates], ViewBatch] = {}

    def _leave(self) -> None:
        super()._leave()
        self._scenes.forget()
        self._batches = {}

    def _bind(self, content: SceneContent) -> dict:
        # ``_decoded`` is keyed by id of the file bytes, which the content keeps alive meanwhile.
        bound, self._decoded = {}, {}
        for materials in content.materials:
            bound.update(bind_parts(materials.parts, materials.game, materials.exact, materials.skinned,
                                    materials.files, materials.masters, content.largest_mip, self._lease,
                                    self._decoded))
        return bound

    def _kept(self, content: SceneContent) -> bool:
        """Whether ``content`` holds the content held first, as it was, and only adds meshes and parts after."""
        held = self._content
        if not isinstance(held, SceneContent) or (content.largest_mip, content.anisotropy) != (
                held.largest_mip, held.anisotropy) or len(content.meshes) < len(held.meshes) \
                or len(content.materials) < len(held.materials) or content.spots[:len(held.spots)] != held.spots \
                or any(mesh is not was for mesh, was in zip(content.meshes, held.meshes)):
            return False
        return all((new.game, new.exact, new.skinned) == (old.game, old.exact, old.skinned)
                   and new.files is old.files and len(new.parts) >= len(old.parts)
                   and all(part is was for part, was in zip(new.parts, old.parts))
                   and all(new.masters.get(path) is master for path, master in old.masters.items())
                   for new, old in zip(content.materials, held.materials))

    def _add(self, content: SceneContent) -> None:
        """Binds the parts ``content`` adds to the content held."""
        held = self._content
        for number, materials in enumerate(content.materials):
            start = len(held.materials[number].parts) if number < len(held.materials) else 0
            self._bound.update(bind_parts(materials.parts[start:], materials.game, materials.exact,
                                          materials.skinned, materials.files, materials.masters, content.largest_mip,
                                          self._lease, self._decoded))
        self._content = content

    def _shown(self, content: SceneContent) -> tuple[dict[int, Exception], list]:
        """The failures of the parts, and the steps of the others in order; frames leave out those hidden."""
        shown = {part.index: (part, self._bound[part.index]) for materials in content.materials
                 for part in materials.parts}
        failures = {index: item for index, (_part, item) in shown.items() if isinstance(item, Exception)}
        return failures, ordered([(item[0], (part, item[1])) for _index, (part, item) in sorted(shown.items())
                                  if not isinstance(item, Exception)])

    def draw(self, content: SceneContent, view: SceneView, placements: tuple[ScenePlacement, ...],
             overlays: LitOverlays) -> TexturedFrame:
        held = None
        if content is not self._content:
            if self._kept(content):
                held = self._content
                self._add(content)
            else:
                self._hold(content)
        probes = view.light.probes if isinstance(view.light, ProbeLight) else None
        # Meshes and parts hidden are drawn hidden (``_seen``), not left out of the view.
        key = probes
        grown = held is not None and self._drawn is not None and self._drawn[1] is not None \
            and self._drawn[0] == key and self._grow(content, view, placements, key)
        if not grown and (self._drawn is None or self._drawn[0] != key or held is not None):
            self._leave()
            failures, steps = self._shown(content)
            drawn = None
            if steps:
                drawn = self._build(content, view, placements, steps, probes)
            self._drawn = (key, drawn, failures)
        _key, drawn, failures = self._drawn
        if view.hidden or view.hidden_parts:
            failures = {index: item for index, item in failures.items()
                        if (name := content.meshes[content.spots[index][0]].key) not in view.hidden
                        and (name, content.spots[index][1]) not in view.hidden_parts}
        if drawn is None:
            return TexturedFrame(view, False, failures)
        textures, overlay = drawn
        if textures.size != tuple(view.size):
            textures.resize(view.size)
            overlay.resize(view.size, textures.depth)
        if placements is not self._held.placements:
            self._place(textures, placements)
        camera = view.camera()
        device = textures.device
        held = self._held
        seen = self._seen(camera, self._hidden(content, view), self._parts_shown(view))
        if not overlays.filled:
            seen = [batch for batch in seen if content.meshes[held.meshes[held.batches[batch]]].solid]
        if seen:
            device.pending += textures.frame(camera, view.light, overlays.background, seen)
        device.pending += self._scenes.steps(overlay, textures.shown if seen else None, overlays, camera,
                                             content.meshes, held.meshes, placements, view.hidden_parts,
                                             view.hidden)
        device.flush()
        return TexturedFrame(view, True, failures, pickable=self._scenes.pickable())

    def _build(self, content: SceneContent, view: SceneView, placements: tuple[ScenePlacement, ...],
               steps: list, probes: ViewProbes | None) -> tuple[TexturesView, Overlay]:
        meshes = sorted({content.spots[part.index][0] for _states, (part, _texture) in steps})
        slots = {mesh: slot for slot, mesh in enumerate(meshes)}
        vertices = sorted({content.meshes[mesh].vertices for mesh in meshes})
        geometries = {mesh: number for number, mesh in enumerate(vertices)}
        shapes = [placements[mesh].shape for mesh in vertices]
        device = Resident(self._hardware)
        textures = TexturesView(device, view.size, _geometries(content, vertices, shapes),
                                self._view_batches(content, steps, slots),
                                objects=[geometries[content.meshes[mesh].vertices] for mesh in meshes],
                                anisotropy=content.anisotropy, probes=probes)
        self._held = _OnDevice(
            meshes, vertices, np.array([geometries[content.meshes[mesh].vertices] for mesh in meshes], np.intp),
            np.array([slots[content.spots[part.index][0]] for _states, (part, _texture) in steps], np.intp),
            np.array([content.meshes[mesh].bones is None for mesh in meshes], bool), shapes,
            np.array([_box(shape) for shape in shapes]), np.tile(np.eye(3, 4), (len(meshes), 1, 1)),
            [None] * len(meshes), np.empty((len(meshes), 8, 3)), pieces=_pieces(content, steps))
        self._corner()
        return textures, Overlay(device, view.size, textures.depth)

    def _view_batches(self, content: SceneContent, steps: list, slots: dict[int, int]) -> list[ViewBatch]:
        """The batch of each step, as made for a step of the part before."""
        batches = []
        for states, (part, texture) in steps:
            made = self._batches.get((part.index, states))
            if made is None:
                made = self._batches[part.index, states] = ViewBatch(
                    part.indices, texture, part.tint, part.cutout, states, slots[content.spots[part.index][0]],
                    texture is None)
            batches.append(made)
        return batches

    def _grow(self, content: SceneContent, view: SceneView, placements: tuple[ScenePlacement, ...],
              key: tuple) -> bool:
        """Adds the meshes and parts drawn of ``content`` after those of the content drawn before to the view drawn;
        False when it draws others: the view is made again."""
        textures, _overlay = self._drawn[1]
        held = self._held
        failures, steps = self._shown(content)
        meshes = sorted({content.spots[part.index][0] for _states, (part, _texture) in steps})
        if meshes[:len(held.meshes)] != held.meshes:
            return False
        added = meshes[len(held.meshes):]
        before = len(held.vertices)
        geometries = {mesh: number for number, mesh in enumerate(held.vertices)}
        fresh = sorted({content.meshes[mesh].vertices for mesh in added} - geometries.keys())
        geometries.update((mesh, number) for number, mesh in enumerate(fresh, before))
        shapes = [placements[mesh].shape for mesh in fresh]
        numbers = [geometries[content.meshes[mesh].vertices] for mesh in added]
        slots = {mesh: slot for slot, mesh in enumerate(meshes)}
        try:
            textures.add(_geometries(content, fresh, shapes), self._view_batches(content, steps, slots),
                         objects=numbers)
        except ValueError:
            # The view made again meets it as a view made with the meshes does.
            return False
        held.meshes, held.vertices = meshes, held.vertices + fresh
        held.geometry = np.concatenate([held.geometry, np.array(numbers, np.intp)])
        held.batches = np.array([slots[content.spots[part.index][0]] for _states, (part, _texture) in steps],
                                np.intp)
        held.pieces = _pieces(content, steps)
        held.culled = np.concatenate([held.culled, np.array([content.meshes[mesh].bones is None for mesh in added],
                                                            bool)])
        held.shapes = held.shapes + shapes
        held.boxes = np.concatenate([held.boxes, np.array([_box(shape) for shape in shapes]).reshape(-1, 2, 3)])
        held.worlds = np.concatenate([held.worlds, np.tile(np.eye(3, 4), (len(added), 1, 1))])
        held.bones = held.bones + [None] * len(added)
        held.placements = None
        for slot, mesh in enumerate(held.vertices[:before]):
            # The viewport makes the shapes again for the meshes added: the same ones write nothing.
            shape = placements[mesh].shape
            if shape is not held.shapes[slot] and _same(shape, held.shapes[slot]):
                held.shapes[slot] = shape
        self._corner()
        self._drawn = key, self._drawn[1], failures
        return True

    def _place(self, textures: TexturesView, placements: tuple[ScenePlacement, ...]) -> None:
        held = self._held
        moved = moved_placements(placements, held.placements)
        for slot in np.flatnonzero(moved[held.vertices]).tolist():
            shape = placements[held.vertices[slot]].shape
            if shape is not held.shapes[slot]:
                textures.write(slot, positions=shape.positions, normals=shape.normals, colours=shape.colours)
                held.shapes[slot], held.boxes[slot] = shape, _box(shape)
        for slot in np.flatnonzero(moved[held.meshes]).tolist():
            placement = placements[held.meshes[slot]]
            world = np.asarray(placement.world, np.float64).reshape(3, 4)
            if (world != held.worlds[slot]).any():
                textures.place(slot, world)
                held.worlds[slot] = world
            if placement.bones is not None and placement.bones is not held.bones[slot]:
                textures.pose(slot, placement.bones)
                held.bones[slot] = placement.bones
        held.placements = placements
        self._corner()

    def _corner(self) -> None:
        held = self._held
        corners = held.boxes[held.geometry][:, BOX_CORNERS, np.arange(3)]
        held.corners = (np.einsum("nki,nji->nkj", corners, held.worlds[:, :, :3])
                        + held.worlds[:, None, :, 3])

    def _hidden(self, content: SceneContent, view: SceneView) -> np.ndarray | bool:
        """Whether each mesh held is hidden."""
        held = self._held
        if not view.hidden:
            return False
        if self._hiding is None or self._hiding[0] is not held.meshes or self._hiding[1] != view.hidden:
            self._hiding = held.meshes, view.hidden, np.array(
                [content.meshes[mesh].key in view.hidden for mesh in held.meshes], bool)
        return self._hiding[2]

    def _parts_shown(self, view: SceneView) -> np.ndarray | bool:
        """Whether each batch held is of a part shown."""
        held = self._held
        if not view.hidden_parts:
            return True
        if self._showing is None or self._showing[0] is not held.pieces or self._showing[1] != view.hidden_parts:
            self._showing = held.pieces, view.hidden_parts, np.array(
                [piece not in view.hidden_parts for piece in held.pieces], bool)
        return self._showing[2]

    def _seen(self, camera, hidden: np.ndarray | bool = False, shown: np.ndarray | bool = True) -> list[int]:
        held = self._held
        matrix = view_projection(camera)
        clip = held.corners @ matrix[:3] + matrix[3]
        w = clip[..., 3]
        beyond = np.zeros(len(held.meshes), bool)
        for along in (clip[..., 0], clip[..., 1]):
            slack = _SLACK * (np.abs(along) + np.abs(w))
            beyond |= (along + w < -slack).all(axis=1) | (w - along < -slack).all(axis=1)
        return np.flatnonzero(~(beyond & held.culled | hidden)[held.batches] & shown).tolist()


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
    prefetched: PrefetchedFiles | None = None


class SceneTexturesPreview(DevicePreview):
    view_type = SceneView
    _game = False

    def __init__(self, viewport, materials, parent=None):
        super().__init__(None, None, lambda: None, parent)
        self._viewport, self._materials = viewport, materials
        self._gathered: int | None = None
        self._holding = False
        # Kept while active, so a gather after an added SCN keeps what it gathered of the meshes before.
        self._groups: dict[object, tuple[tuple, _Gathered]] = {}
        self._parts_made: dict[int, tuple] = {}
        self._before: SceneContent | None = None
        self._labels: list[str] = []
        self._objects: list[str] = []
        # By the ``failures()`` they were made of, the last two.
        self._told_parts: list[tuple[Mapping[int, str], list[tuple[str, str, str]]]] = []
        # Sent while the viewport paints, so handled after.
        viewport.device_view_changed.connect(self.view_changed, Qt.ConnectionType.QueuedConnection)
        # Sent before the window is destroyed, so handled at once.
        viewport.device_window_changed.connect(self.set_window)
        viewport.device_image_wanted.connect(self.read_image)
        self.image_read.connect(viewport.show_device_image)
        self.active_changed.connect(viewport.set_device_frames)
        self.frame_changed.connect(viewport.set_device_frame)

    def set_active(self, active: bool) -> None:
        super().set_active(active)
        if not active:
            self._groups.clear()
            self._parts_made.clear()
            self._before = None

    def view_changed(self, view, overlays=None) -> None:
        if isinstance(view, SceneView) and view.game != self._game:
            view = ""
        super().view_changed(view, overlays)
        if self._active and not self._holding and isinstance(view, SceneView) and view.content != self._gathered:
            self.scene_changed()

    def hold(self, held: bool) -> None:
        """While held, the scene gathered is drawn on as the viewport's meshes change; they are gathered once it is
        let go. Added meshes come after the others, so the scene's still index the viewport's."""
        if held == self._holding:
            return
        self._holding = held
        if not held and self._active and isinstance(self._view, SceneView) and self._view.content != self._gathered:
            self.scene_changed()

    def _make_drawer(self) -> SceneTexturesDrawer:
        return SceneTexturesDrawer()

    def _drawable(self) -> bool:
        if self._holding:
            return self._view is not None and self._scene is not None
        # Skipping loses nothing: the viewport sends a view of its content when it paints.
        return super()._drawable() and self._viewport.device_scene()[0] == self._scene.geometry

    def _request(self) -> tuple:
        return self._scene, self._view, self._viewport.device_motion(), self._overlays

    def _on_finished(self, thread, request: tuple, result) -> None:
        if not isinstance(result, Exception) and request[0] is not self._scene:
            result = self._of_other_content(result)
        super()._on_finished(thread, request, result)

    @staticmethod
    def _of_other_content(frame: TexturedFrame) -> TexturedFrame:
        """Parts gathered since are numbered differently."""
        return replace(frame, failures={})

    def failed_parts(self) -> list[tuple[str, str, str]]:
        """Not to be changed: shared until ``failures()`` changes, as only a gather changes the labels."""
        failures = self.failures()
        for held, parts in self._told_parts:
            if held is failures:
                return parts
        parts = [(self._objects[index], self._labels[index], reason) for index, reason in sorted(failures.items())]
        self._told_parts = [(failures, parts), *self._told_parts[:1]]
        return parts

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
        before = self._before
        if before is not None:
            # The viewport makes its meshes again for an added SCN.
            meshes = tuple(before.meshes[at] if at < len(before.meshes) and _same(mesh, before.meshes[at]) else mesh
                           for at, mesh in enumerate(meshes))
        gathered: dict[object, _Gathered] = {}
        scopes: dict[tuple, dict] = {}
        spots, made = [], {}
        for mesh_number, mesh in enumerate(meshes):
            for batch in mesh.batches:
                index = len(spots)
                spots.append((mesh_number, batch.part_index))
                name = batch.material_name
                session = self._materials.owner(name) if name else None
                if session not in gathered:
                    gathered[session] = self._group(session, scopes, quality.prefer_streaming)
                group = gathered[session]
                self._labels.append(group.names.get(name, name))
                self._objects.append(mesh.key)
                held = self._parts_made.get(index)
                if held is not None and held[0] is mesh and held[1] is batch and held[2] is group:
                    part, reason = held[3:]
                    if part is not None:
                        group.parts.append(part)
                else:
                    reason = self._gather_part(session, group, index, mesh, batch, quality.prefer_streaming)
                    part = None if reason else group.parts[-1]
                made[index] = mesh, batch, group, part, reason
                if reason:
                    self._failures[index] = reason
        self._parts_made = made
        self._groups = {session: kept for session, kept in self._groups.items() if session in gathered}
        for group in gathered.values():
            if group.prefetched is not None and group.prefetched.holds(group.files):
                group.prefetched.add(group.files)
            else:
                group.prefetched = PrefetchedFiles(group.files)
        self._before = SceneContent(
            number, meshes, tuple(
                SceneMaterials(group.game, group.exact, group.skinned, tuple(group.parts), group.prefetched,
                               frozendict(group.masters))
                for group in gathered.values()),
            tuple(spots), quality.max_dimension, max(1, int(quality.anisotropy)))
        return self._before

    def _group(self, session, scopes: dict[tuple, dict], streaming: bool) -> _Gathered:
        """The session's group as gathered before, while its mesh file and materials and the texture quality are the
        same."""
        state = (None, None, streaming) if session is None else (session.profiles, session.handler.mesh, streaming)
        kept = self._groups.get(session)
        if kept is not None and kept[0][0] is state[0] and kept[0][1] is state[1] and kept[0][2] == state[2]:
            group = kept[1]
            group.parts = []
            return group
        group = _Gathered() if session is None else self._gathered_of(session, scopes)
        self._groups[session] = state, group
        return group

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
        if exact and isinstance(handler, StmeshHandler) and handler.stmesh.meshlets is not None:
            # Meshlet SpeedTree chunks have no vertex layout.
            exact = False
        elif exact and isinstance(handler, StmeshHandler) and batch.submesh is not None:
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
