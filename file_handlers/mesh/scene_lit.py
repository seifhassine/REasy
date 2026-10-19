"""See docs/internals/mesh-preview.md."""
from __future__ import annotations

import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from functools import partial

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP, QObject, Qt, Signal

from file_handlers.mmtr.engine_buffers import Camera, Placement
from file_handlers.mmtr.light_passes import LightPasses, captured_constants, light_pass_catalog_for, parsed_pass_file
from file_handlers.mmtr.material_preview import MaterialPreviewError, gbuffer_route
from file_handlers.mmtr.mesh_draw import MeshBuffer, SubmeshRange
from file_handlers.mmtr.mesh_render import (
    DEPTH_VIEWS, MeshBounds, MeshObject, MeshPart, MeshScene, ObjectBox, PartError, mesh_bounds, object_box,
)
from file_handlers.mmtr.meshlets import ROUTE as MESHLET_ROUTE, MeshletBuffer, meshlet_catalog_for
from file_handlers.mmtr.overlay import Overlay
from file_handlers.sdf.sdf_file import CULL_NONE
from file_handlers.mmtr.pass_choice import PassChoiceError
from file_handlers.mmtr.parsed_files import Lease
from file_handlers.mmtr.pretransform import (
    ROUTE, ChainLevel, Deformation, blend_weights, chain_level, deformed_route, masters_skinning_program,
    pretransform_catalog_for,
)
from file_handlers.mmtr.pretransform_passes import SystemPrograms
from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.mmtr.speedtree_catalog import speedtree_catalog_for
from file_handlers.mmtr.wrinkle import WrinkleObject, mesh_joints, wrinkle_catalog_for, wrinkle_object, wrinkle_proxy
lazy from file_handlers.stmesh.lit import drawn_chunks, drawn_cluster, engine_buffers as stmesh_engine_buffers
lazy from file_handlers.stmesh.stmesh_handler import StmeshHandler
from file_handlers.strands.chain_frame import Stepped
from file_handlers.strands.chain_scene import ChainScene
from file_handlers.strands.chain_view import StrandView
from file_handlers.tex.texture_quality import texture_quality_profile
lazy from ui.scene.mesh_scene import mesh_lod_parts
from ui.scene.scene_view import SceneGeometry, ScenePlacement, ScenePose, SceneView
from utils.resource_file_utils import PrefetchedFiles, resource_version_from_path

from .lit_preview import (
    DEFAULT_INTENSITY, NO_LIGHT_PASSES, UNKNOWN_SKINNING, GameLighting, LitDrawer, LitOverlays, LitPart, bind_part,
    camera_light, deformed, level_bounds, pass_files, scene_file, stencil_reference, submesh_groups,
)
from .material_resolver import MeshMaterialResolver
from .mesh_file import MeshFile
lazy from .scene_overlay import SceneOverlay
from .material_shader_preview import preview_material
from .scene_textures import SceneTexturesPreview

_UNMORPHED = QT_TRANSLATE_NOOP(
    "MaterialPreview", "The mesh is morphed, and REasy has no catalog of this build's mesh deformation passes.")
# Camera wind: m/s of wind per m/s of camera movement, and the share kept per ``_WIND_TIME``th of a second.
_WIND_PER_SPEED = 6.0
_WIND_KEPT, _WIND_TIME = 0.9, 60
_GRAVITY_GAIN = 2.0


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitMesh:
    """``groups`` maps each submesh to its mesh group."""
    skinned: bool
    bones: int
    stencil_reference: int
    bounds: MeshBounds | None
    level: ChainLevel | None
    groups: Mapping[int, int]
    channels: Mapping[str, tuple[int, ...]]
    meshlets: bool = False
    engine: Mapping[str, bytes] = frozendict()
    box: ObjectBox | None = None

    @property
    def route(self) -> str:
        if self.meshlets:
            return MESHLET_ROUTE
        return gbuffer_route(self.skinned) if self.level is None else ROUTE


@dataclass(frozen=True, slots=True)
class SceneLitPart:
    """``piece`` is ``SceneDrawBatch.part_index``."""
    number: int
    piece: int | None
    part: LitPart


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitObject:
    """``edge_excluded``, ``decal_receive_mode`` and ``transparent_bias`` are the component's ``NREdgeExcluded``,
    ``DecalReciveMode`` and ``TransparentBias``."""
    mesh: int
    of: SceneLitMesh
    parts: tuple[SceneLitPart, ...]
    batches: tuple[tuple[int | None, int], ...]
    edge_excluded: bool = False
    decal_receive_mode: int = 0
    transparent_bias: float = 0.0
    wrinkle: WrinkleObject | None = None



def _program(item: SceneLitObject) -> str | None:
    """None when the renderer skins as it draws."""
    return None if item.of.level is None else masters_skinning_program(item.of.level, item.edge_excluded)

class MaterialValues:
    def __init__(self) -> None:
        self.now: Mapping[str, Mapping[str, float]] = {}

    def of(self, key: str) -> Mapping[str, float]:
        return self.now.get(key, {})


@dataclass(frozen=True, slots=True, eq=False)
class SceneLitContent:
    geometry: int
    game: str
    meshes: tuple[SceneGeometry, ...]
    objects: tuple[SceneLitObject, ...]
    masters: Mapping[str, tuple[str, bytes]]
    files: Mapping[str, bytes]
    strands: ChainScene | None = None
    strands_object: int | None = None
    strands_refused: str = ""
    values: MaterialValues = field(default_factory=MaterialValues)
    wrinkles_refused: str = ""


@dataclass(frozen=True, slots=True)
class SceneLitFrame:
    view: SceneView
    drawn: bool
    failures: Mapping[int, Exception]
    missing_textures: tuple[str, ...]
    picked: int | None = field(default=None, kw_only=True)
    settling: bool = field(default=False, kw_only=True)
    stepped: Stepped | None = field(default=None, kw_only=True)


def _placement(of: SceneLitMesh, placement: ScenePlacement) -> Placement:
    world = placement.world
    if not of.skinned:
        return Placement(world)
    bones = np.tile(world, (of.bones, 1, 1))
    pose = placement.pose
    if pose is not None and pose.bones is not None:
        posed = np.asarray(pose.bones, np.float64)
        bones[:len(posed), :, :3] = np.matmul(world[:, :3], posed[:, :, :3])
        bones[:len(posed), :, 3] = posed[:, :, 3] @ world[:, :3].T + world[:, 3]
    return Placement(world, bones)


def _weights(item: SceneLitObject, pose: ScenePose | None) -> np.ndarray | None:
    of = item.of
    blend = None if of.level is None else of.level.blend
    morphs = () if pose is None else pose.morphs
    if blend is None:
        if any(weight and name in of.channels for name, weight in morphs):
            raise MaterialPreviewError(_UNMORPHED)
        return None
    weights = np.zeros(blend.channel_count, np.float32)
    for name, weight in morphs:
        weights[[index for index in of.channels.get(name, ()) if index < len(weights)]] = weight
    return blend_weights(blend, weights)


@dataclass(slots=True)
class _Entry:
    number: int
    placement: Placement
    deformation: Deformation | None
    skins: list[MeshPart]
    parts: list[tuple[int, MeshPart]]


@dataclass(slots=True)
class _Held:
    owners: list[int]
    placements: tuple[ScenePlacement, ...]
    worlds: np.ndarray
    poses: list[ScenePose | None]
    weights: list[np.ndarray | None]


def _strand_image(strands: StrandView, mesh: MeshScene, content: SceneLitContent,
                  placements: tuple[ScenePlacement, ...], rate: float, wind: np.ndarray | None,
                  turn: np.ndarray | None) -> None:
    placement = placements[content.objects[content.strands_object].mesh]
    world = np.eye(4)
    world[:3] = placement.world
    strands.image(mesh, rate, world.T, None if placement.pose is None else placement.pose.joints, wind, turn)


def _without(entries: list[_Entry], error: PartError, failures: dict[int, Exception]) -> list[_Entry]:
    given = {id(part) for entry in entries for part in (*(part for _number, part in entry.parts), *entry.skins)}
    if any(id(refused) not in given for refused, _reason in error.refusals):
        raise error
    for refused, reason in error.refusals:
        kept = []
        for entry in entries:
            drawn = any(part is refused for _number, part in entry.parts)
            if not drawn and all(part is not refused for part in entry.skins):
                kept.append(entry)
                continue
            # An object that only skins the refused part can't be deformed, so all its parts fail.
            failures.update((number, reason) for number, part in entry.parts if part is refused or not drawn)
            entry.parts = [(number, part) for number, part in entry.parts if drawn and part is not refused]
            if entry.parts:
                kept.append(entry)
        entries = kept
    return entries


class SceneLitDrawer(LitDrawer):
    """Camera wind and gravity are REasy's stand-ins (docs/internals/mesh-preview.md)."""

    def __init__(self):
        super().__init__()
        # Keyed by (id of the ``LitPart``, skinning program).
        self._bound: dict[int, tuple[MeshPart | None, Exception | None]] | None = None
        self._morphs: list = []
        self._weighed: tuple[ScenePlacement, ...] | None = None
        self._held: _Held | None = None
        self._scenes = SceneOverlay(self._scene_cull)
        self._faces: dict[int, tuple[int, bool] | None] = {}
        # World space.
        self._wind = np.zeros(3)
        self._eye: np.ndarray | None = None
        self._rate: float | None = None

    def _blow(self, camera: Camera, rate: float) -> np.ndarray:
        view = np.asarray(camera.view, np.float64).reshape(3, 4)
        eye = -view[:, :3].T @ view[:, 3]
        moved = np.zeros(3) if self._eye is None else (eye - self._eye) * rate
        self._eye = eye
        kept = _WIND_KEPT ** (_WIND_TIME / rate)
        self._wind = self._wind * kept + moved * (_WIND_PER_SPEED * (1 - kept))
        return self._wind

    @staticmethod
    def _fall(camera: Camera) -> np.ndarray:
        turn = np.asarray(camera.view, np.float64).reshape(3, 4)[:, :3].T
        return turn * (1 + _GRAVITY_GAIN * (1 - turn[1, 1]))

    def _leave(self) -> None:
        super()._leave()
        self._scenes.forget()
        self._eye = None
        self._wind = np.zeros(3)

    def _scene_cull(self, number: int) -> tuple[int, bool]:
        faces = self._faces.get(number)
        return (CULL_NONE, True) if faces is None else faces

    def _frame(self, drawn: tuple, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
               overlays: LitOverlays, camera: Camera, light) -> None:
        mesh, passes, overlay, strands = drawn
        meshes = [content.objects[number].mesh for number in self._held.owners]

        def after(shown: int | None, averaged: bool = True) -> list[int]:
            steps = []
            if averaged and overlays.filled:
                steps, shown = self._average(overlay, shown, overlays.background,
                                             None if strands is None else strands.chain.drawn_opacity)
            return steps + self._scenes.steps(overlay, shown, overlays, camera, content.meshes, meshes, placements,
                                              view.hidden_parts)
        if not overlays.filled:
            mesh.device.pending += after(None, averaged=False)
            mesh.device.flush()
        else:
            if strands is not None:
                _strand_image(strands, mesh, content, placements, self._rate, self._blow(camera, self._rate),
                              self._fall(camera))
            passes.drawn(light, after)

    def draw(self, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
             overlays: LitOverlays, rate: float, materials: Mapping[str, Mapping[str, float]] = frozendict()
             ) -> SceneLitFrame:
        content.values.now = materials
        self._rate = rate
        if content is not self._scene:
            self._leave()
            self._lease.close()
            self._scene, self._bound, self._lease = content, None, Lease()
            self._morphs, self._weighed = [None] * len(content.objects), None
        if not content.objects:
            return SceneLitFrame(view, False, {}, ())
        profile = mmtr_profile_for_game(content.game)
        catalog = light_pass_catalog_for(profile)
        if self._bound is None:
            self._bound = self._bind(content, profile, catalog)
        refused = self._weigh(content, placements)
        key = view.hidden, view.hidden_parts, frozenset(refused), view.strands_only
        made = self._drawn is None or self._drawn[0] != key
        if made:
            self._leave()
        camera = view.camera()
        drawn_by = self._antialias(camera, (view, placements), view.antialiased)
        if made:
            drawn, failures = self._build(content, view, placements, overlays, profile, catalog, refused, drawn_by)
            self._drawn = key, drawn, failures
        else:
            _key, drawn, failures = self._drawn
            if drawn is not None:
                mesh = drawn[0]
                if mesh.size != tuple(view.size):
                    self._resize(drawn, view.size)
                mesh.camera = drawn_by
                self._place(mesh, content, placements)
                self._frame(drawn, content, view, placements, overlays, camera, camera_light(camera, DEFAULT_INTENSITY))
        if drawn is None:
            return SceneLitFrame(view, False, failures, ())
        return SceneLitFrame(view, True, failures, drawn[0].missing_textures, picked=self._scenes.picked(),
                             settling=self._settling, stepped=None if drawn[3] is None else drawn[3].chain.stepped)

    def _bind(self, content: SceneLitContent, profile,
              catalog) -> dict[tuple[int, str | None], tuple[MeshPart | None, Exception | None]]:
        materials, bound = {}, {}
        for item in content.objects:
            route, program = item.of.route, _program(item)
            for spot in item.parts:
                part = spot.part
                if (id(part), program) in bound:
                    continue
                drawn_as = (part.key, part.route or route, part.meshlet_programs, program)
                if drawn_as not in materials:
                    materials[drawn_as] = bind_part(content.game, content.masters, route, part, profile, catalog,
                                                    self._lease, skinned=item.of.skinned, program=program)
                document, refusal = materials[drawn_as]
                skin = None
                if document is not None:
                    own = deformed_route(document, catalog.master_target, part.route or route, program,
                                         item.of.skinned)
                    skin = MeshPart(document, part.material, part.buffer, part.submesh,
                                    own if own != route else part.route, part.meshlet_programs)
                bound[id(part), program] = skin, refusal
        return bound

    def _weigh(self, content: SceneLitContent, placements: tuple[ScenePlacement, ...]) -> dict[int, Exception]:
        if placements is not self._weighed:
            for number, item in enumerate(content.objects):
                pose, held = placements[item.mesh].pose, self._morphs[number]
                if held is not None and held[0] is pose:
                    continue
                try:
                    self._morphs[number] = pose, _weights(item, pose)
                except MaterialPreviewError as exc:
                    self._morphs[number] = pose, exc
            self._weighed = placements
        return {number: held[1] for number, held in enumerate(self._morphs) if isinstance(held[1], Exception)}

    def _build(self, content: SceneLitContent, view: SceneView, placements: tuple[ScenePlacement, ...],
               overlays: LitOverlays, profile, catalog, refused: Mapping[int, Exception], drawn_by: Camera) -> tuple:
        failures: dict[int, Exception] = {}
        entries = []
        for number, item in enumerate(content.objects):
            name = content.meshes[item.mesh].key
            if name in view.hidden:
                continue
            shown = [spot for spot in item.parts if (name, spot.piece) not in view.hidden_parts]
            if number in refused:
                failures.update((spot.number, refused[number]) for spot in shown)
                continue
            parts = []
            for spot in shown:
                skin, refusal = self._bound[id(spot.part), _program(item)]
                if refusal is None:
                    parts.append((spot.number, skin))
                else:
                    failures[spot.number] = refusal
            if not parts:
                continue
            deformation, skins = None, []
            if item.of.level is not None:
                visible = [submesh for piece, submesh in item.batches if (name, piece) not in view.hidden_parts]
                held = {spot.part.index: self._bound[id(spot.part), _program(item)][0] for spot in item.parts
                        if self._bound[id(spot.part), _program(item)][0] is not None}
                try:
                    deformation, skins = deformed(content.game, item.of.level, item.of.groups, visible, held,
                                                  self._morphs[number][1], edge_excluded=item.edge_excluded)
                except MaterialPreviewError as exc:
                    failures.update((part_number, exc) for part_number, _part in parts)
                    continue
            entries.append(_Entry(number, _placement(item.of, placements[item.mesh]), deformation, skins, parts))
        alone = view.strands_only and content.strands is not None
        if alone:
            entries = [entry for entry in entries
                       if entry.number == content.strands_object and entry.deformation is not None]
        camera = view.camera()
        light = camera_light(camera, DEFAULT_INTENSITY)

        def pass_file(path: str, data: bytes):
            return self._lease.parsed(path, data, parsed_pass_file, profile.game, path)
        while entries:
            mesh = None
            objects = []
            for entry in entries:
                shown = content.objects[entry.number]
                of = shown.of
                drawn = [] if alone else [part for _number, part in entry.parts]
                skins = tuple(part for part in entry.skins if all(part is not item for item in drawn))
                objects.append(MeshObject(tuple(drawn), entry.placement, of.skinned, of.stencil_reference, of.bounds,
                                          entry.deformation, skins, of.engine, shown.decal_receive_mode, of.box,
                                          shown.transparent_bias, shown.wrinkle))
            try:
                mesh = MeshScene(objects, content.files.get, drawn_by, shader_target=catalog.master_target,
                                 hardware=True, phases={phase.key for phase in catalog.phases},
                                 pass_file=lambda path, data: pass_file(path, data).document,
                                 captured=captured_constants(catalog))
                passes = LightPasses(mesh, pass_file=pass_file)
                owner = next((owner for owner, entry in enumerate(entries)
                              if entry.number == content.strands_object), None)
                strands = None if content.strands is None or owner is None else StrandView(
                    content.strands, passes, SystemPrograms(profile, partial(scene_file, content)),
                    catalog.master_target, owner)
                overlay = Overlay(mesh.device, view.size, (DEPTH_VIEWS[mesh.depth_format][1], mesh.depth))
                given = [placements[content.objects[entry.number].mesh] for entry in entries]
                self._held = _Held(
                    [entry.number for entry in entries], placements,
                    np.array([placement.world for placement in given], np.float64).reshape(-1, 3, 4),
                    [placement.pose for placement in given],
                    [None if entry.deformation is None else entry.deformation.weights for entry in entries])
                self._faces = {number: mesh.faces(owner, part) for owner, entry in enumerate(entries)
                               for number, part in entry.parts}
                self._frame((mesh, passes, overlay, strands), content, view, placements, overlays, camera, light)
            except PartError as exc:
                if mesh is not None:
                    self._leave()
                    mesh.device.close()
                entries = _without(entries, exc, failures)
                continue
            return (mesh, passes, overlay, strands), failures
        return None, failures

    def _place(self, mesh: MeshScene, content: SceneLitContent, placements: tuple[ScenePlacement, ...]) -> None:
        held = self._held
        if placements is held.placements:
            return
        given = [placements[content.objects[number].mesh] for number in held.owners]
        worlds = np.array([placement.world for placement in given], np.float64).reshape(-1, 3, 4)
        moved = (worlds != held.worlds).any(axis=(1, 2))
        for owner, (number, placement) in enumerate(zip(held.owners, given, strict=True)):
            of = content.objects[number].of
            if moved[owner] or of.skinned and placement.pose is not held.poses[owner]:
                mesh.place(owner, _placement(of, placement))
                held.poses[owner] = placement.pose
            weights = self._morphs[number][1]
            if weights is not None and not np.array_equal(weights, held.weights[owner]):
                mesh.morph(owner, weights)
                held.weights[owner] = weights
        held.worlds, held.placements = worlds, placements


@dataclass(frozen=True, slots=True, eq=False)
class _Lod:
    submeshes: tuple[tuple[SubmeshRange, MeshBuffer | MeshletBuffer], ...]
    level: ChainLevel | None
    groups: Mapping[int, int]
    channels: Mapping[str, tuple[int, ...]]
    bounds: MeshBounds | None


@dataclass(slots=True)
class _MeshRead:
    mesh: object
    bones: int
    buffers: dict[int, MeshBuffer | MeshletBuffer] = field(default_factory=dict)
    lods: dict[int, _Lod | str] = field(default_factory=dict)


def _read_stmesh_lod(handler, lod: int) -> _Lod | str:
    parts = mesh_lod_parts(handler.mesh, lod)
    try:
        if handler.stmesh.meshlets is not None:
            drawn = [handler.meshlet_draw(submesh)[::-1] for _group, submesh in parts]
        else:
            drawn = [(draw.submesh, draw.buffer) for draw in (handler.cluster_draw(lod, submesh)
                                                              for _group, submesh in parts)]
    except MaterialPreviewError as exc:
        return str(exc)
    payloads = handler.mesh.mesh_buffer.buffer_payloads
    bounds = mesh_bounds((payloads[index] for index in sorted({submesh.buffer_index for _group, submesh in parts})),
                         None)
    return _Lod(tuple(drawn), None, {}, {}, bounds)


def _read_lod(mesh, lod: int, chains, skinned: bool, read: _MeshRead) -> _Lod:
    parts = mesh_lod_parts(mesh, lod)
    payloads = mesh.mesh_buffer.buffer_payloads
    for _group, submesh in parts:
        if submesh.buffer_index not in read.buffers:
            read.buffers[submesh.buffer_index] = (MeshletBuffer.from_mesh(mesh)
                                                  or MeshBuffer.from_payload(payloads[submesh.buffer_index]))
    # The game deforms LODs with blend shapes or normal-recalc data before drawing them.
    level = chain_level(mesh, lod, chains) if chains is not None and parts else None
    groups = {} if level is None else dict(enumerate(submesh_groups(level, (submesh for _group, submesh in parts))))
    body = None if mesh.blend_shape_data is None else next(
        (item for item in mesh.blend_shape_data.bodies if item.lod_index == lod), None)
    channels: dict[str, list[int]] = {}
    for channel in () if body is None else body.channels:
        if channel.name:
            channels.setdefault(channel.name, []).append(int(channel.index))
    bounds = level_bounds(
        level, (payloads[index] for index in sorted({submesh.buffer_index for _group, submesh in parts})),
        read.bones if skinned else None)
    return _Lod(tuple((SubmeshRange.from_submesh(submesh), read.buffers[submesh.buffer_index])
                      for _group, submesh in parts),
                level, groups, {name: tuple(numbers) for name, numbers in channels.items()}, bounds)


@dataclass(slots=True)
class _Gathered:
    game: str
    reason: str
    names: dict[str, str]
    placement: str | None = None
    resolve: Callable[[str], tuple[str, bytes] | None] | None = None
    cache: dict = field(default_factory=dict)
    meshes: dict[int, SceneLitMesh | str] = field(default_factory=dict)
    lods: dict[int, _Lod] = field(default_factory=dict)
    parts: dict[tuple[int, int], LitPart] = field(default_factory=dict)
    unusable: dict[str, str] = field(default_factory=dict)


class SceneLitPreview(GameLighting, SceneTexturesPreview):
    _game = True

    def __init__(self, viewport, materials, parent=None):
        super().__init__(viewport, materials, parent)
        self._read: dict[int, tuple[object, object, _MeshRead]] = {}
        self.set_limit(viewport.fps_limit)
        viewport.fps_limit_changed.connect(self.set_limit)
        viewport.stepped_time = self.stepped_time

    def set_active(self, active: bool) -> None:
        super().set_active(active)
        if not active:
            self._read.clear()
            self._viewport.set_strands_stepped(None)

    def _on_finished(self, thread, request: tuple, result) -> None:
        super()._on_finished(thread, request, result)
        if thread is self._thread:
            self._viewport.set_strands_stepped(None if isinstance(result, Exception) else result.stepped)

    def _stepping(self) -> bool:
        return self._active and self._scene is not None and self._scene.strands is not None

    def status(self) -> list[str]:
        refused = "" if self._scene is None else self._scene.strands_refused
        wrinkles = "" if self._scene is None else self._scene.wrinkles_refused
        return (super().status() + ([self.tr("Strands not drawn: {reason}").format(reason=refused)] if refused else [])
                + ([self.tr("Wrinkles not drawn: {reason}").format(reason=wrinkles)] if wrinkles else []))

    def _make_drawer(self) -> SceneLitDrawer:
        return SceneLitDrawer()

    def _request(self) -> tuple:
        return (*super()._request(), self.step_rate, self._viewport.device_materials())

    def _gather(self) -> SceneLitContent:
        number, meshes = self._told()
        streaming = texture_quality_profile(self._materials.texture_quality).prefer_streaming
        owners = [[self._materials.owner(batch.material_name) if batch.material_name else None
                   for batch in mesh.batches] for mesh in meshes]
        wanted: dict[object, set[int]] = {}
        for mesh, sessions in zip(meshes, owners, strict=True):
            for batch, session in zip(mesh.batches, sessions, strict=True):
                if session is not None and batch.submesh is not None:
                    wanted.setdefault(session, set()).add(batch.submesh[0])
        gathered: dict[object, _Gathered] = {}
        scopes: dict[tuple, dict] = {}
        read: dict[int, tuple[object, object, _MeshRead]] = {}
        masters: dict[str, tuple[str, bytes]] = {}
        files: dict[str, bytes] = {}
        values, animated = MaterialValues(), self._viewport.device_materials().keys()
        first: _Gathered | None = None
        objects, sources = [], []
        for mesh_number, (mesh, sessions) in enumerate(zip(meshes, owners, strict=True)):
            made: dict[tuple, tuple[list, list]] = {}
            for batch, session in zip(mesh.batches, sessions, strict=True):
                index = len(self._labels)
                name = batch.material_name
                self._objects.append(mesh.key)
                if session is None:
                    self._labels.append(name or mesh.key)
                    self._failures[index] = self.tr("No MDF material.")
                    continue
                grouped = (session, mesh.speedtree_placement)
                if grouped not in gathered:
                    gathered[grouped] = self._session_gathered(session, mesh.speedtree_placement,
                                                               wanted.get(session, set()), scopes, read)
                group = gathered[grouped]
                self._labels.append(group.names.get(name, name))
                if first is None and not group.reason:
                    first = group
                reason = group.reason or ("" if group.game == first.game else self.tr(
                    "Game lighting draws the meshes of one game build at a time."))
                if batch.submesh is None:
                    self._failures[index] = reason or self.tr("Game lighting draws only the meshes' main geometry.")
                    continue
                reason = reason or self._unread(group, batch.submesh)
                if not reason:
                    parts, batches = made.setdefault((grouped, batch.submesh[0]), ([], []))
                    batches.append((batch.part_index, batch.submesh[1]))
                    reason = self._gather_lit_part(session, group, batch, masters, files, streaming,
                                                   values if name in animated else None)
                if reason:
                    self._failures[index] = reason
                    continue
                parts.append(SceneLitPart(index, batch.part_index, group.parts[batch.submesh]))
            for (grouped, lod), (parts, batches) in made.items():
                if parts:
                    objects.append(SceneLitObject(mesh_number, gathered[grouped].meshes[lod], tuple(parts),
                                                  tuple(batches), mesh.edge_excluded, mesh.decal_receive_mode,
                                                  mesh.transparent_bias))
                    sources.append((grouped, lod))
        self._read = read
        if first is None:
            uncatalogued = str(MaterialPreviewError(NO_LIGHT_PASSES))
            if gathered and all(group.reason == uncatalogued for group in gathered.values()):
                self._viewport.refuse_game_lighting(uncatalogued)
            return SceneLitContent(number, "", meshes, (), {}, {})
        profile = mmtr_profile_for_game(first.game)
        chains = pretransform_catalog_for(profile)
        wrinkled, wrinkles_refused = self._gather_wrinkles(objects, sources, meshes, gathered, read, profile, files)
        drawn = pass_files(profile, light_pass_catalog_for(profile),
                           chains if any(item.of.level is not None for item in objects) else None, first.resolve,
                           meshlet_catalog_for(profile) if any(item.of.meshlets for item in objects) else None,
                           wrinkle_catalog_for(profile) if wrinkled else None)
        drawn.update(files)
        strands, owner, refused, told = None, None, "", self._viewport.device_strands()
        if told is not None:
            key, handler, shown, edited = told
            owner = next((number for number, (item, ((session, _placement), _lod))
                          in enumerate(zip(objects, sources))
                          if meshes[item.mesh].key == key and session.handler is handler), None)
            if owner is None:
                refused = self.tr("The mesh they are over is not drawn.")
            else:
                (session, placement), lod = sources[owner]
                # Strands need the whole mesh file (bone boxes, mesh groups).
                strands, refused = self._gather_strands(handler, read[id(handler)][2].mesh, first.game, profile,
                                                        shown, lod, objects[owner].of.level,
                                                        gathered[session, placement].resolve, masters, drawn,
                                                        edited)
        return SceneLitContent(number, first.game, meshes, tuple(objects), masters, PrefetchedFiles(drawn), strands,
                               owner, refused, values, wrinkles_refused)

    def _gather_wrinkles(self, objects: list[SceneLitObject], sources: list, meshes, gathered: dict, read: dict,
                         profile, files: dict) -> tuple[bool, str]:
        told = {item.key: item for item in self._viewport.device_wrinkles()}
        catalog = wrinkle_catalog_for(profile) if told else None
        chains = pretransform_catalog_for(profile) if told else None
        refused, proxies = [], {}
        for number, item in enumerate(objects):
            wrinkle = told.get(meshes[item.mesh].key)
            if wrinkle is None:
                continue
            (session, placement), _lod = sources[number]
            resolve = gathered[session, placement].resolve
            try:
                if wrinkle.refused:
                    raise MaterialPreviewError(wrinkle.refused)
                if catalog is None or chains is None:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "REasy has no catalog of the wrinkle passes of this game build."))
                if not item.of.skinned or id(session.handler) not in read:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The wrinkle passes skin their proxy by the bones of a skinned mesh."))
                if wrinkle.proxy not in proxies:
                    proxies[wrinkle.proxy] = self._wrinkle_proxy(resolve, wrinkle.proxy, chains)
                target = resolve(wrinkle.render_target)
                if target is None:
                    raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                        "MaterialPreview", "The render texture file {path} was not found."),
                        {"path": wrinkle.render_target})
                files[wrinkle.render_target] = target[1]
                joints, binds = mesh_joints(read[id(session.handler)][2].mesh)
                objects[number] = replace(item, wrinkle=wrinkle_object(
                    proxies[wrinkle.proxy], joints, binds, wrinkle.render_target, wrinkle.spread))
            except MaterialPreviewError as exc:
                refused.append(str(exc))
        return any(item.wrinkle is not None for item in objects), "; ".join(dict.fromkeys(refused))

    @staticmethod
    def _wrinkle_proxy(resolve, path: str, chains):
        found = resolve(path)
        if found is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The wrinkle proxy mesh {path} was not found."), {"path": path})
        mesh = MeshFile()
        try:
            mesh.read(found[1], file_version=resource_version_from_path(found[0], "mesh"))
        except (ValueError, IndexError, struct.error) as exc:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The wrinkle proxy mesh {path} could not be read: {reason}"),
                {"path": path, "reason": str(exc)}) from exc
        return wrinkle_proxy(mesh, chains)

    def _session_gathered(self, session, placement: str | None, lods: set[int], scopes: dict[tuple, dict],
                          read: dict[int, tuple[object, object, _MeshRead]]) -> _Gathered:
        handler = session.handler
        game = MeshMaterialResolver.material_profile(handler).game
        cache = scopes.setdefault(MeshMaterialResolver.resource_scope(handler), {})
        group = _Gathered(game, "", {session.material_key(binding.mesh_material_name): binding.mesh_material_name
                                     for binding in session.bindings}, placement, self._resolver(handler, cache),
                          cache)
        profile = mmtr_profile_for_game(game)
        flags = handler.mesh.header.decoded_content_flags
        if profile is None or light_pass_catalog_for(profile) is None:
            group.reason = str(MaterialPreviewError(NO_LIGHT_PASSES))
        elif isinstance(handler, StmeshHandler):
            engine = frozendict(stmesh_engine_buffers(handler.stmesh, speedtree_catalog_for(profile),
                                                      placement))
            stencil = stencil_reference(session)
            for lod in lods:
                held = _read_stmesh_lod(handler, lod)
                group.meshes[lod] = held if isinstance(held, str) else SceneLitMesh(
                    False, 1, stencil, held.bounds, None, {}, {}, handler.stmesh.meshlets is not None, engine)
                if not isinstance(held, str):
                    group.lods[lod] = held
        elif flags is None:
            group.reason = str(MaterialPreviewError(UNKNOWN_SKINNING))
        else:
            whole = self._read_whole(handler, lods, pretransform_catalog_for(profile), bool(flags.is_skinning), read)
            if isinstance(whole, str):
                group.reason = whole
                return group
            stencil = stencil_reference(session)
            for lod in lods:
                held = whole.lods[lod]
                group.meshes[lod] = held if isinstance(held, str) else SceneLitMesh(
                    bool(flags.is_skinning), whole.bones, stencil, held.bounds, held.level, held.groups,
                    held.channels, any(isinstance(buffer, MeshletBuffer) for _range, buffer in held.submeshes),
                    box=object_box(whole.mesh))
                if not isinstance(held, str):
                    group.lods[lod] = held
        return group

    def _read_whole(self, handler, lods: set[int], chains, skinned: bool,
                    read: dict[int, tuple[object, object, _MeshRead]]) -> _MeshRead | str:
        held = read.get(id(handler)) or self._read.get(id(handler))
        entry = held[2] if held is not None and held[0] is handler and held[1] is handler.mesh else None
        if entry is None or not lods <= entry.lods.keys():
            try:
                whole = handler.whole_mesh()
            except ValueError as exc:
                return str(exc)
            if whole is None:
                return self.tr("The mesh file {path} could not be read whole.").format(path=handler.filepath)
            if entry is None:
                entry = _MeshRead(whole, max(int(whole.joint_count), int(whole.bone_remap_count), 1))
            for lod in lods - entry.lods.keys():
                try:
                    entry.lods[lod] = _read_lod(whole, lod, chains, skinned, entry)
                except MaterialPreviewError as exc:
                    entry.lods[lod] = str(exc)
        read[id(handler)] = handler, handler.mesh, entry
        return entry

    def _unread(self, group: _Gathered, submesh: tuple[int, int]) -> str:
        lod, number = submesh
        if isinstance(group.meshes[lod], str):
            return group.meshes[lod]
        if number >= len(group.lods[lod].submeshes):
            return self.tr("The mesh file read again is not the one shown.")
        return ""

    def _gather_lit_part(self, session, group: _Gathered, batch, masters: dict, files: dict,
                         streaming: bool, values: MaterialValues | None) -> str:
        lod, number = batch.submesh
        name = batch.material_name
        surface = session.profiles.get(name)
        if surface is None or surface.instance_context is None:
            return self.tr("No MDF material.")
        if name not in group.unusable:
            drawn_masters, drawn_files = {}, {}
            group.unusable[name] = self._gather_drawn(
                session.handler, surface, group.resolve, drawn_masters, drawn_files, streaming, group.cache
            ) or self._merged(masters, drawn_masters, files, drawn_files)
        if group.unusable[name]:
            return group.unusable[name]
        if batch.submesh not in group.parts:
            submesh, buffer = group.lods[lod].submeshes[number]
            route = programs = None
            handler = session.handler
            if isinstance(handler, StmeshHandler):
                speedtree = speedtree_catalog_for(mmtr_profile_for_game(group.game))
                viewed = mesh_lod_parts(handler.mesh, lod)[number][1]
                try:
                    if handler.stmesh.meshlets is not None:
                        _buffer, _range, programs = drawn_chunks(handler, lod, viewed, int(surface.material_flags),
                                                                 speedtree, group.placement)
                    else:
                        _draw, route = drawn_cluster(handler, lod, viewed, int(surface.material_flags), speedtree,
                                                     group.placement)
                except (MaterialPreviewError, PassChoiceError) as exc:
                    return str(exc)
            group.parts[batch.submesh] = LitPart(number, name, surface.mmtr_path, surface.instance_context,
                                                 preview_material(surface, None if values is None else
                                                                  partial(values.of, name)), buffer, submesh, route,
                                                 programs)
        return ""

    def _merged(self, masters: dict, more_masters: dict, files: dict, more_files: dict) -> str:
        for held, more in ((masters, more_masters), (files, more_files)):
            clash = next((path for path, value in more.items() if held.get(path, value) != value), None)
            if clash is not None:
                return self.tr("Another mesh of the scene reads another file by the path {path}.").format(path=clash)
        masters.update(more_masters)
        files.update(more_files)
        return ""


class SceneDevicePreview(QObject):
    status_changed = Signal()

    def __init__(self, viewport, materials, parent=None):
        super().__init__(parent)
        self._viewport = viewport
        self._active = False
        self._textures = SceneTexturesPreview(viewport, materials, self)
        self._lit = SceneLitPreview(viewport, materials, self)
        self._logged: set[tuple[str, str, str]] = set()
        for preview in (self._textures, self._lit):
            preview.status_changed.connect(self._log_failures)
            preview.status_changed.connect(self.status_changed)
        # Handled after the previews get the view: its lighting picks which one draws.
        viewport.device_view_changed.connect(self._pick, Qt.ConnectionType.QueuedConnection)
        viewport.offer_game_lighting()

    def set_active(self, active: bool) -> None:
        self._active = active
        self._pick()

    def status(self) -> list[str]:
        refusal = self._viewport.game_lighting_refusal
        told = [self.tr("{reason} The meshes are drawn with their textures.").format(reason=refusal)] if refusal else []
        return [*told, *self._textures.status(), *self._lit.status()]

    def refused(self) -> set[str]:
        return {key for preview in (self._textures, self._lit) for key, _label, _reason in preview.failed_parts()}

    def _log_failures(self) -> None:
        for failed in (*self._textures.failed_parts(), *self._lit.failed_parts()):
            if failed not in self._logged:
                self._logged.add(failed)
                key, label, reason = failed
                print(f"Scene [warning] not_drawn: {label}: {reason} {key}")

    def strands_changed(self) -> None:
        self._lit.scene_changed()

    def _pick(self, *_told) -> None:
        game = self._viewport.lighting_mode == "game"
        shown, other = (self._lit, self._textures) if game else (self._textures, self._lit)
        other.set_active(False)
        shown.set_active(self._active)


__all__ = ["MaterialValues", "SceneDevicePreview", "SceneLitContent", "SceneLitDrawer", "SceneLitFrame", "SceneLitMesh",
           "SceneLitObject", "SceneLitPart", "SceneLitPreview"]
