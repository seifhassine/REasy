"""See docs/internals/mesh-preview.md."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.engine_buffers import Camera, Placement
from file_handlers.mmtr.light_passes import (
    DERIVED_READ, Light, LightPasses, captured_constants, derived_read, light_pass_catalog_for, light_pass_files,
    parsed_pass_file,
)
from file_handlers.mmtr.material_context import MaterialInstanceContext
from file_handlers.mmtr.material_family import bind_drawn_master
from file_handlers.mmtr.material_preview import MaterialPreviewError, PreviewMaterial, gbuffer_route
from file_handlers.mmtr.mesh_draw import MeshBuffer, SubmeshRange
from file_handlers.mmtr.mesh_render import (
    DEPTH_VIEWS, MeshBounds, MeshObject, MeshPart, MeshScene, ObjectBox, PartError, mesh_bounds, object_box, part_draws,
)
from file_handlers.mmtr.meshlets import ROUTE as MESHLET_ROUTE, MeshletBuffer, MeshletPrograms, meshlet_catalog_for
from file_handlers.mmtr.mmtr_file import parse_drawn_master
from file_handlers.mmtr.overlay import Marks, Overlay
from file_handlers.mmtr.temporal import FRAMES, TemporalImage, jittered
from file_handlers.mmtr.overlay_scene import OverlayGeometry, OverlayPart, OverlayScene, SceneShown
from file_handlers.mmtr.parsed_files import Lease
from file_handlers.mmtr.pretransform import (
    ROUTE, ChainLevel, Deformation, blend_weights, chain_level, deformed_route, masters_skinning_program,
    pretransform_catalog_for,
)
from file_handlers.mmtr.profiles import mmtr_profile_for_game, mmtr_profile_for_resource
from file_handlers.mmtr.shown_image import TargetImages
from file_handlers.mmtr.speedtree_catalog import speedtree_catalog_for
from file_handlers.mmtr.pass_choice import PassChoiceError
from file_handlers.mmtr.surface_meaning import GBUFFER_PURPOSE, surface_output_catalog_for
from file_handlers.stmesh.lit import drawn_chunks, drawn_cluster, engine_buffers as stmesh_engine_buffers
from file_handlers.stmesh.stmesh_handler import StmeshHandler
from file_handlers.sdf.sdf_file import CULL_NONE
from file_handlers.tex.texture_quality import texture_quality_profile
from ui.scene.device_frames import Presenter
from ui.scene.mesh_preview import MeshPreviewScene
from ui.scene.scene_buffers import VIEW_LIGHT_DIRECTION, mesh_bounds_points
from utils.resource_file_utils import PrefetchedFiles, resource_path_with_version, resource_version_from_path

from .device_preview import DevicePreview
from .material_resolver import MeshMaterialResolver
from .material_shader_preview import preview_material
from .mesh_inspector import MeshInspection

# The mesh viewer's vertical field of view (degrees) and clip distances (scaled units).
_FIELD_OF_VIEW = 45.0
_NEAR, _FAR = 0.1, 100.0
# Direction towards the viewers' default light, in view space.
_LIGHT_DIRECTION = np.array(VIEW_LIGHT_DIRECTION) / 1.5
DEFAULT_INTENSITY = 3.0
# MDF flag ``SSSProfileUsed``: objects using it get stencil bit value 2, which masked deferred lights test.
_SSS_PROFILE_USED = 1 << 27
_SSS_STENCIL = 2
NO_LIGHT_PASSES = QT_TRANSLATE_NOOP(
    "MaterialPreview", "REasy has no catalog of the renderer's light passes for this game build.")
UNKNOWN_SKINNING = QT_TRANSLATE_NOOP(
    "MaterialPreview", "This mesh format does not say whether the mesh is skinned, which picks the masters' "
                       "programs.")


def _rotation(rot_x: float, rot_y: float) -> np.ndarray:
    pitch, yaw = np.deg2rad((rot_x, rot_y))
    about_x = np.array([[1, 0, 0], [0, np.cos(pitch), -np.sin(pitch)], [0, np.sin(pitch), np.cos(pitch)]])
    about_y = np.array([[np.cos(yaw), 0, np.sin(yaw)], [0, 1, 0], [-np.sin(yaw), 0, np.cos(yaw)]])
    return about_x @ about_y


def _perspective(rotation: np.ndarray, origin: np.ndarray, near: float, far: float,
                 size: tuple[int, int]) -> Camera:
    vertical = 1 / np.tan(np.deg2rad(_FIELD_OF_VIEW / 2))
    width, height = size
    return Camera(np.hstack([rotation, origin[:, None]]), (vertical * height / width, vertical), near, far,
                  (width, height))


def free_camera(position, pitch: float, yaw: float, near: float, far: float, size: tuple[int, int]) -> Camera:
    """``pitch`` and ``yaw`` in degrees."""
    rotation = _rotation(-pitch, -yaw)
    return _perspective(rotation, -rotation @ np.asarray(position, np.float64), near, far, size)


def engine_camera(rot_x: float, rot_y: float, distance: float, center, scale: float,
                  size: tuple[int, int], orthographic: bool = False) -> Camera:
    """The mesh viewer's camera: it views through T(0, 0, -distance) Rx Ry S(scale) T(-center), so clip planes and
    orthographic extents scale by ``scale``."""
    rotation = _rotation(rot_x, rot_y)
    origin = -rotation @ np.asarray(center, np.float64) - (0.0, 0.0, distance / scale)
    width, height = size
    if orthographic:
        vertical = scale / (distance * np.tan(np.deg2rad(_FIELD_OF_VIEW / 2)))
        return Camera(np.hstack([rotation, origin[:, None]]), (vertical * height / width, vertical), -_FAR / scale,
                      _FAR / scale, (width, height), orthographic=True)
    return _perspective(rotation, origin, _NEAR / scale, _FAR / scale, size)


def camera_light(camera: Camera, intensity: float) -> Light:
    return Light(tuple(np.asarray(camera.view, np.float64)[:, :3].T @ _LIGHT_DIRECTION),
                 (intensity, intensity, intensity))


@dataclass(frozen=True, slots=True)
class LitView:
    """``geometry`` is (layer, LOD); ``morph`` is (channel, weight)."""
    rot_x: float
    rot_y: float
    distance: float
    center: tuple[float, float, float]
    scale: float
    size: tuple[int, int]
    geometry: tuple[str, int]
    visible: frozenset[int]
    morph: tuple[int, float] | None = field(default=None, kw_only=True)
    orthographic: bool = field(default=False, kw_only=True)
    antialiased: bool = field(default=False, kw_only=True)

    def camera(self) -> Camera:
        return engine_camera(self.rot_x, self.rot_y, self.distance, self.center, self.scale, self.size,
                             self.orthographic)


@dataclass(frozen=True, slots=True)
class LitPart:
    """``route`` and ``meshlet_programs`` are set only for SpeedTree parts, which name their own."""
    index: int
    key: str
    mmtr_path: str
    context: MaterialInstanceContext
    material: PreviewMaterial
    buffer: MeshBuffer | MeshletBuffer
    submesh: SubmeshRange
    route: str | None = None
    meshlet_programs: MeshletPrograms | None = None


@dataclass(frozen=True, slots=True)
class LitScene:
    game: str
    geometry: tuple[str, int]
    parts: tuple[LitPart, ...]
    masters: Mapping[str, tuple[str, bytes]]
    files: Mapping[str, bytes]
    skinned: bool
    bones: int
    stencil_reference: int
    bounds: MeshBounds | None = None
    level: ChainLevel | None = None
    groups: tuple[int, ...] = ()
    meshlets: bool = False
    engine: Mapping[str, bytes] = MappingProxyType({})
    box: ObjectBox | None = None

    @property
    def route(self) -> str:
        if self.meshlets:
            return MESHLET_ROUTE
        return gbuffer_route(self.skinned) if self.level is None else ROUTE


@dataclass(frozen=True, slots=True)
class LitFrame:
    view: LitView
    drawn: bool
    failures: Mapping[int, Exception]
    missing_textures: tuple[str, ...]
    settling: bool = field(default=False, kw_only=True)


@dataclass(frozen=True, slots=True, eq=False)
class LitLines:
    """``ends`` is n x 2 x 3; ``width`` in pixels; ``colour`` RGBA."""
    ends: np.ndarray
    width: float
    colour: tuple[int, int, int, int]
    tested: bool = True


@dataclass(frozen=True, slots=True, eq=False)
class LitPoints:
    """``size`` in pixels across; ``colour`` RGBA, one or one per point."""
    positions: np.ndarray
    size: float
    colour: tuple[int, int, int, int] | np.ndarray
    tested: bool = True


@dataclass(frozen=True, slots=True, eq=False)
class LitTriangles:
    """``corners`` is n x 3 x 3; ``colour`` RGBA."""
    corners: np.ndarray
    colour: tuple[int, int, int, int]
    tested: bool = True


@dataclass(frozen=True, slots=True, eq=False)
class LitLabels:
    """Rectangles of ``atlas`` as ``overlay.packed`` lays them out; each is drawn above and right of its anchor's texel,
    ``margin`` apart."""
    atlas: np.ndarray
    anchors: np.ndarray
    rectangles: np.ndarray
    margin: int


@dataclass(frozen=True, slots=True, eq=False)
class LitShape:
    """``positions`` are morphed; ``indices`` hold each part's triangles by part index."""
    positions: np.ndarray
    indices: Mapping[int, np.ndarray]


_MARKS_MADE: dict[int, tuple] = {}
_MARKS_KEPT = 4


@dataclass(frozen=True, slots=True)
class LitOverlays:
    background: tuple[int, int, int]
    marks: tuple[LitLines | LitPoints | LitTriangles, ...] = ()
    labels: LitLabels | None = None
    scene: SceneShown | None = None
    shape: LitShape | None = None

    @property
    def filled(self) -> bool:
        return self.scene is None or self.scene.filled

    def overlay_marks(self) -> Marks:
        """Cached, so reused objects aren't uploaded again."""
        held = _MARKS_MADE.get(id(self))
        if held is not None and held[0] is self:
            return held[1]
        marks = self._made_marks()
        if len(_MARKS_MADE) >= _MARKS_KEPT:
            _MARKS_MADE.pop(next(iter(_MARKS_MADE)))
        _MARKS_MADE[id(self)] = (self, marks)
        return marks

    def _made_marks(self) -> Marks:
        labels = self.labels
        marks = Marks(None if labels is None else labels.atlas)
        for mark in self.marks:
            if isinstance(mark, LitLines):
                marks.lines(mark.ends, mark.width, mark.colour, mark.tested)
            elif isinstance(mark, LitPoints):
                marks.points(mark.positions, mark.size, mark.colour, mark.tested)
            else:
                marks.triangles(mark.corners, mark.colour, mark.tested)
        if labels is not None:
            marks.labels(labels.anchors, labels.rectangles, labels.margin)
        return marks


class ShapeOverlay:
    def __init__(self, culls: Callable[[int], tuple[int, bool]]):
        self._culls = culls
        self._held: tuple | None = None

    def forget(self) -> None:
        self._held = None

    def steps(self, overlay: Overlay, shown: int, overlays: LitOverlays, camera: Camera,
              visible: Iterable[int]) -> list[int]:
        shape, scene = overlays.shape, None
        if overlays.scene is not None and shape is not None:
            parts = tuple(sorted(index for index in visible if index in shape.indices))
            key = (overlay, id(shape.indices), parts)
            if self._held is None or self._held[0] != key:
                geometry = OverlayGeometry(shape.positions, tuple(
                    OverlayPart(shape.indices[index], *self._culls(index)) for index in parts))
                self._held = (key, OverlayScene(overlay.device, overlay.size, [geometry]), shape.positions)
            key, scene, positions = self._held
            if scene.size != overlay.size:
                scene.resize(overlay.size)
            if shape.positions is not positions:
                scene.write(0, shape.positions)
                self._held = (key, scene, shape.positions)
        return overlay.steps(shown if overlays.filled else None, overlays.background, camera,
                             overlays.overlay_marks(), scene, overlays.scene)


def bound_master(game: str, masters: Mapping[str, tuple[str, bytes]], mmtr_path: str,
                 context: MaterialInstanceContext, profile, lease: Lease):
    def master(found: str, data: bytes):
        build = mmtr_profile_for_resource(resource_version_from_path(found, "mmtr"), game=game)
        return lease.parsed(found, data, parse_drawn_master, build.game, found)

    document = bind_drawn_master(context, mmtr_path, masters.get, master)
    if document.profile is not profile:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The master {path} is of another game build than the mesh."), {"path": mmtr_path})
    return document


def bind_part(game: str, masters: Mapping[str, tuple[str, bytes]], route: str, part: LitPart, profile, catalog,
              lease: Lease, *, gbuffer: bool = False, skinned: bool = False,
              program: str | None = None) -> tuple[object, Exception | None]:
    try:
        document = bound_master(game, masters, part.mmtr_path, part.context, profile, lease)
    except (ValueError, FileNotFoundError) as exc:
        return None, exc
    route = deformed_route(document, catalog.master_target, part.route or route, program, skinned)
    try:
        if gbuffer:
            _check_gbuffer(route, part, document, catalog, skinned)
        else:
            _check_drawn(route, part, document, profile, catalog, skinned)
    except (ValueError, FileNotFoundError) as exc:
        return document, exc
    return document, None


def _check_gbuffer(route: str, part: LitPart, document, catalog, skinned: bool = False) -> None:
    main, _drawn = part_draws(document, part.material, catalog.master_target, route, (), part.meshlet_programs,
                              skinned)
    if main is None or main[0] == "forward":
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The renderer draws this material after the G-buffer, so it writes no G-buffer "
                               "target."))


def _check_drawn(route: str, part: LitPart, document, profile, catalog, skinned: bool = False) -> None:
    phases = {phase.key: phase for phase in catalog.phases}
    main, drawn = part_draws(document, part.material, catalog.master_target, route, phases, part.meshlet_programs,
                             skinned)
    if main is not None:
        purpose, program = main
        if purpose == "forward" and (purpose, ()) not in phases:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The master has no G-buffer program for this material, and REasy's light pass "
                                   "catalog for this game build does not cover forward programs."))
        if (purpose, ()) in phases:
            drawn[purpose, ()] = program
    contract = profile.shader_graph_profile.shader_contract
    for key, program in drawn.items():
        name = derived_read(phases[key], [resource.name for shader in program.shaders if shader.present
                                          for resource in contract(document, program, shader.role).resources])
        if name is not None:
            raise MaterialPreviewError(DERIVED_READ, {"name": name})


def _weights(scene: LitScene, morph: tuple[int, float] | None) -> np.ndarray | None:
    blend = None if scene.level is None else scene.level.blend
    if blend is None:
        if morph is not None and morph[1]:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "Game lighting draws this mesh without morphs; set the morph weight to 0."))
        return None
    weights = np.zeros(blend.channel_count, np.float32)
    if morph is not None:
        weights[morph[0]] = morph[1]
    return blend_weights(blend, weights)


def submesh_groups(level: ChainLevel, submeshes: Iterable) -> list[int]:
    ranges = {SubmeshRange(item.first_index, item.index_count, item.first): item.group for item in level.submeshes}
    return [ranges[SubmeshRange.from_submesh(submesh)] for submesh in submeshes]


def level_bounds(level: ChainLevel | None, payloads: Iterable, bones: int | None) -> MeshBounds | None:
    """Blend shapes can move vertices outside the mesh bounds."""
    return None if level is not None and level.blend is not None else mesh_bounds(payloads, bones)


def deformed(game: str, level: ChainLevel, groups: Mapping[int, int], visible: Iterable[int],
             skins: Mapping[int, MeshPart], weights: np.ndarray | None,
             edge_excluded: bool = False) -> tuple[Deformation, list[MeshPart]]:
    enabled = frozenset(groups[index] for index in visible)
    held = [part for index, part in skins.items() if groups[index] in enabled]
    materials = {SubmeshRange(item.first_index, item.index_count, item.first): item.material
                 for item in level.submeshes if item.group in enabled}
    unskinned = frozenset(materials[part.submesh] for part in held if part.route not in (None, ROUTE))
    deformation = Deformation(level, pretransform_catalog_for(mmtr_profile_for_game(game)), enabled,
                              edge_excluded, weights, unskinned)
    skinning = {step.material for step in deformation.steps() if step.material is not None}
    if not {materials[part.submesh] for part in held} >= skinning:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The renderer deforms this mesh with its materials' masters before drawing it, and a "
                               "material of the shown mesh groups has no usable master."))
    return deformation, held


class DeviceDrawer:
    """``place`` may be called from another thread and stops showing before it returns."""

    def __init__(self):
        self._lease = Lease()
        self._drawn: tuple[tuple, tuple | None, dict[int, Exception]] | None = None
        self._shape = ShapeOverlay(self._cull)
        self._presenter = Presenter()

    def place(self, window: int | None) -> None:
        self._presenter.place(window)

    def show(self) -> None:
        drawn = None if self._drawn is None else self._drawn[1]
        if drawn is not None:
            self._presenter.show(drawn[0].device.scene, self._overlay(drawn).image)

    def close(self) -> None:
        self.place(None)
        self._leave()
        self._lease.close()

    def _leave(self) -> None:
        self._presenter.hide()
        drawn = None if self._drawn is None else self._drawn[1]
        self._drawn = None
        self._shape.forget()
        if drawn is not None:
            drawn[0].device.close()

    @staticmethod
    def _overlay(drawn: tuple) -> Overlay:
        raise NotImplementedError


class LitDrawer(DeviceDrawer):
    _drawn: tuple[tuple, tuple[MeshScene, LightPasses | TargetImages, Overlay] | None, dict[int, Exception]] | None

    def __init__(self):
        super().__init__()
        self._scene: LitScene | None = None
        self._bound: dict[bool, dict[int, MeshPart | Exception]] = {}
        self._skins: dict[int, MeshPart] = {}
        self._gbuffer = False
        self._mesh: MeshScene | None = None
        self._still = None
        self._averaged = 0
        self._temporal: TemporalImage | None = None

    def _antialias(self, camera: Camera, still, antialiased: bool) -> Camera:
        if not antialiased:
            self._still = None
            return camera
        if still != self._still:
            self._still, self._averaged = still, 0
        return jittered(camera, self._averaged)

    def _average(self, overlay: Overlay, shown: int, background,
                 responsive: tuple[int, int] | None = None) -> tuple[list[int], int]:
        if self._still is None:
            return [], shown
        if self._temporal is None:
            # Released with the overlay's scene.
            self._temporal = TemporalImage(overlay.device, overlay.size)
        steps = self._temporal.steps(shown, background, self._averaged, responsive)
        self._averaged += 1
        return steps, self._temporal.image

    @property
    def _settling(self) -> bool:
        return self._still is not None and self._averaged < FRAMES

    @staticmethod
    def _overlay(drawn: tuple) -> Overlay:
        return drawn[2]

    def _leave(self) -> None:
        super()._leave()
        self._mesh = self._temporal = self._still = None

    def draw(self, scene: LitScene, view: LitView, intensity: float, overlays: LitOverlays,
             target: tuple[int, str] | None = None) -> LitFrame:
        profile = mmtr_profile_for_game(scene.game)
        catalog = light_pass_catalog_for(profile)
        gbuffer = self._gbuffer = target is not None
        if scene is not self._scene:
            self._leave()
            self._lease.close()
            self._scene, self._bound, self._lease = scene, {}, Lease()
        if gbuffer not in self._bound:
            materials, bound, skins = {}, {}, {}
            # The mesh viewer keeps edge normals.
            program = None if scene.level is None else masters_skinning_program(scene.level, False)
            for part in scene.parts:
                drawn_as = (part.key, part.route, part.meshlet_programs)
                if drawn_as not in materials:
                    materials[drawn_as] = bind_part(scene.game, scene.masters, scene.route, part, profile, catalog,
                                                    self._lease, gbuffer=gbuffer, skinned=scene.skinned,
                                                    program=program)
                document, refusal = materials[drawn_as]
                if document is not None:
                    route = deformed_route(document, catalog.master_target, part.route or scene.route, program,
                                           scene.skinned)
                    skins[part.index] = MeshPart(document, part.material, part.buffer, part.submesh,
                                                 route if route != scene.route else part.route, part.meshlet_programs)
                bound[part.index] = skins[part.index] if refusal is None else refusal
            self._bound[gbuffer], self._skins = bound, skins
        camera = view.camera()
        light = camera_light(camera, intensity)
        weights = _weights(scene, view.morph)
        kept = self._drawn is not None and self._drawn[0] == (view.visible, gbuffer)
        if not kept:
            self._leave()
        drawn_by = self._antialias(camera, (view, intensity), view.antialiased and not gbuffer)
        if kept:
            _key, drawn, failures = self._drawn
            if drawn is not None:
                mesh = drawn[0]
                if mesh.size != tuple(view.size):
                    self._resize(drawn, view.size)
                mesh.camera = drawn_by
                if weights is not None and not np.array_equal(weights, mesh.objects[0].deformation.weights):
                    mesh.morph(0, weights)
                self._run(drawn, light, target, overlays, camera, view.visible)
        else:
            shown ={index: item for index, item in self._bound[gbuffer].items() if index in view.visible}
            parts = [item for item in shown.values() if isinstance(item, MeshPart)]
            failures = {index: item for index, item in shown.items() if not isinstance(item, MeshPart)}
            placement = Placement(np.eye(4)[:3],
                                  np.tile(np.eye(4)[:3], (scene.bones, 1, 1)) if scene.skinned else None)
            deformation, skins = None, []
            if scene.level is not None and parts:
                try:
                    deformation, skins = deformed(scene.game, scene.level, dict(enumerate(scene.groups)),
                                                  view.visible, self._skins, weights)
                except MaterialPreviewError as exc:
                    failures.update((index, exc) for index, item in shown.items() if isinstance(item, MeshPart))
                    parts = []

            def pass_file(path: str, data: bytes):
                return self._lease.parsed(path, data, parsed_pass_file, profile.game, path)
            drawn = None
            while parts:
                mesh = None
                try:
                    shown_object = MeshObject(tuple(parts), placement, scene.skinned, scene.stencil_reference,
                                              scene.bounds, deformation, tuple(part for part in skins
                                                                               if part not in parts), scene.engine,
                                              box=scene.box)
                    # The G-buffer view draws every G-buffer program, blending ones too.
                    mesh = MeshScene([shown_object], scene.files.get, drawn_by, shader_target=catalog.master_target,
                                     hardware=True,
                                     phases=() if gbuffer else {phase.key for phase in catalog.phases},
                                     pass_file=lambda path, data: pass_file(path, data).document,
                                     captured=captured_constants(catalog))
                    frame = TargetImages(mesh) if gbuffer else LightPasses(mesh, pass_file=pass_file)
                    overlay = Overlay(mesh.device, view.size, (DEPTH_VIEWS[mesh.depth_format][1], mesh.depth))
                    # Kept once its frame has run: running may refuse a part too.
                    self._run((mesh, frame, overlay), light, target, overlays, camera, view.visible)
                    drawn = mesh, frame, overlay
                    break
                except PartError as exc:
                    if mesh is not None:
                        self._leave()
                        mesh.device.close()
                    for refused, reason in exc.refusals:
                        # A refused part outside ``parts`` only skins the others; without it none can be deformed.
                        for part in [refused] if refused in parts else list(parts):
                            parts.remove(part)
                            failures[next(index for index, item in shown.items() if item is part)] = reason
            self._drawn = ((view.visible, gbuffer), drawn, failures)
        if drawn is None:
            return LitFrame(view, False, failures, ())
        return LitFrame(view, True, failures, drawn[0].missing_textures, settling=self._settling)

    def _resize(self, drawn: tuple, size: Sequence[int]) -> None:
        mesh, frame, overlay = drawn[:3]
        mesh.resize(size)
        frame.resize()
        overlay.resize(size, (DEPTH_VIEWS[mesh.depth_format][1], mesh.depth))
        if self._temporal is not None:
            self._temporal.release()
            self._temporal = None

    def image(self) -> np.ndarray | None:
        drawn = None if self._drawn is None else self._drawn[1]
        if drawn is None:
            return None
        mesh, _frame, overlay = drawn[:3]
        width, height = overlay.size
        shown, = mesh.device.flush([overlay.image])
        return np.frombuffer(shown, np.uint8).reshape(height, width, 4)

    def _run(self, drawn: tuple, light: Light, target: tuple[int, str] | None, overlays: LitOverlays, camera: Camera,
             visible: Iterable[int]) -> None:
        mesh, frame, overlay = drawn
        self._mesh = mesh

        def after(shown: int) -> list[int]:
            averaged, shown = self._average(overlay, shown, overlays.background) if overlays.filled else ([], shown)
            return averaged + self._shape.steps(overlay, shown, overlays, camera, visible)
        if not overlays.filled:
            mesh.device.pending += after(0)
            mesh.device.flush()
        elif target is not None:
            frame.drawn(*target, after)
        else:
            frame.drawn(light, after)

    def _cull(self, index: int) -> tuple[int, bool]:
        """(cull mode, winding)."""
        part = self._bound.get(self._gbuffer, {}).get(index)
        faces = self._mesh.faces(0, part) if isinstance(part, MeshPart) else None
        return (CULL_NONE, True) if faces is None else faces


def scene_file(scene, path: str) -> bytes | None:
    return scene.files[path] if path in scene.files else scene.masters[path][1] if path in scene.masters else None


def gbuffer_targets(profile) -> tuple[tuple[int, str, int], ...]:
    catalog = None if profile is None else surface_output_catalog_for(profile)
    if catalog is None:
        return ()
    found = []
    for row in sorted(catalog.formats, key=lambda item: item.target):
        if row.purpose == GBUFFER_PURPOSE and not row.blending:
            meaning = catalog.target(GBUFFER_PURPOSE, "SV_Target", row.target)
            found.append((row.target, "" if meaning is None else meaning.slot, row.format))
    return tuple(found)


class GameLighting:
    def _frame_lines(self, frame) -> list[str]:
        if not frame.missing_textures:
            return []
        return [self.tr("Textures not used (not found, or not the shape the shader reads): {names}.")
                .format(names=", ".join(frame.missing_textures))]


class LitPreview(GameLighting, DevicePreview):
    view_type = LitView

    def __init__(self, handler, session, inspection: Callable[[], object], parent=None):
        super().__init__(handler, session, inspection, parent)
        self._intensity = DEFAULT_INTENSITY
        self._target: tuple[int, str] | None = None

    @property
    def intensity(self) -> float:
        return self._intensity

    def set_intensity(self, intensity: float) -> None:
        self._intensity = float(intensity)
        if self._active:
            self._ask()

    def targets(self) -> tuple[tuple[int, str, int], ...]:
        profile = MeshMaterialResolver.material_profile(self._handler)
        return gbuffer_targets(None if profile is None else mmtr_profile_for_game(profile.game))

    def set_target(self, target: tuple[int, str] | None) -> None:
        if target != self._target:
            self._target = target
            if self._active:
                self._ask()

    def unavailable(self) -> str:
        scene, frame = self._scene, self._frame
        if not self._active or scene is None:
            return self._error if self._active else ""
        refused = (not scene.parts and self._failures) or (frame is not None and not frame.drawn and frame.failures)
        return self.tr("None of the mesh's parts can be drawn.") if refused else ""

    def _make_drawer(self) -> LitDrawer:
        return LitDrawer()

    def _request(self) -> tuple:
        return self._scene, self._view, self._intensity, self._overlays, self._target

    def _gather(self) -> LitScene:
        handler, session, inspection = self._handler, self._session, self._inspection()
        mesh = handler.mesh
        game = MeshMaterialResolver.material_profile(handler).game
        profile = mmtr_profile_for_game(game)
        catalog = None if profile is None else light_pass_catalog_for(profile)
        if catalog is None:
            raise MaterialPreviewError(NO_LIGHT_PASSES)
        content = mesh.header.decoded_content_flags
        if content is None:
            raise MaterialPreviewError(UNKNOWN_SKINNING)
        if inspection is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The mesh has no geometry shown."))

        def resolve(path: str) -> tuple[str, bytes] | None:
            return MeshMaterialResolver._resolve_resource(handler, path)

        # The game deforms LODs with blend shapes or normal-recalc data before drawing them.
        chains = pretransform_catalog_for(profile)
        layer, lod = inspection.geometry
        level = chain_level(mesh, lod, chains) if chains is not None and layer == "main" else None
        groups = () if level is None else tuple(submesh_groups(level, inspection.submeshes))
        meshlets = layer == "main" and MeshletBuffer.from_mesh(mesh)
        stmesh_meshlets = isinstance(handler, StmeshHandler) and handler.stmesh.meshlets is not None
        files = pass_files(profile, catalog, chains if level is not None else None, resolve,
                           meshlet_catalog_for(profile) if meshlets or stmesh_meshlets else None)
        streaming = texture_quality_profile(session.texture_quality).prefer_streaming
        cache: dict = {}
        masters: dict[str, tuple[str, bytes]] = {}
        unusable: dict[str, str] = {}
        buffers: dict[int, MeshBuffer | MeshletBuffer] = {}
        payloads: set[int] = set()
        speedtree = isinstance(handler, StmeshHandler)
        speedtree_catalog = speedtree_catalog_for(profile) if speedtree else None
        parts = []
        for index, submesh in enumerate(inspection.submeshes):
            binding = (session.bindings[submesh.material_index]
                       if 0 <= submesh.material_index < len(session.bindings) else None)
            surface = None if binding is None else binding.surface
            if surface is None or surface.instance_context is None:
                self._failures[index] = self.tr("No MDF material.")
                continue
            key = session.material_key(binding.mesh_material_name)
            if key not in unusable:
                unusable[key] = self._gather_drawn(handler, surface, resolve, masters, files, streaming, cache)
            if unusable[key]:
                self._failures[index] = unusable[key]
                continue
            if speedtree:
                try:
                    if handler.stmesh.meshlets is not None:
                        buffer, found, programs = drawn_chunks(handler, lod, submesh, int(surface.material_flags),
                                                               speedtree_catalog, None)
                        drawn = LitPart(index, key, surface.mmtr_path, surface.instance_context,
                                        preview_material(surface), buffer, found, None, programs)
                    else:
                        draw, route = drawn_cluster(handler, lod, submesh, int(surface.material_flags),
                                                    speedtree_catalog, None)
                        drawn = LitPart(index, key, surface.mmtr_path, surface.instance_context,
                                        preview_material(surface), draw.buffer, draw.submesh, route)
                except (MaterialPreviewError, PassChoiceError) as exc:
                    self._failures[index] = str(exc)
                    continue
                payloads.add(submesh.buffer_index)
                parts.append(drawn)
                continue
            if submesh.buffer_index not in buffers:
                buffers[submesh.buffer_index] = meshlets or MeshBuffer.from_payload(
                    mesh.mesh_buffer.buffer_payloads[submesh.buffer_index])
            payloads.add(submesh.buffer_index)
            parts.append(LitPart(index, key, surface.mmtr_path, surface.instance_context, preview_material(surface),
                                 buffers[submesh.buffer_index], SubmeshRange.from_submesh(submesh)))
        skinned, bones = bool(content.is_skinning), max(int(mesh.joint_count), int(mesh.bone_remap_count), 1)
        bounds = level_bounds(level, (mesh.mesh_buffer.buffer_payloads[index] for index in sorted(payloads)),
                              bones if skinned else None)
        return LitScene(game, inspection.geometry, tuple(parts), masters, PrefetchedFiles(files), skinned, bones,
                        stencil_reference(session), bounds, level, groups,
                        bool(meshlets) or speedtree and handler.stmesh.meshlets is not None,
                        stmesh_engine_buffers(handler.stmesh, speedtree_catalog, None) if speedtree
                        else MappingProxyType({}), None if speedtree else object_box(mesh))


def stencil_reference(session) -> int:
    surfaces = session.resolved_mdf.surfaces.values() if session.resolved_mdf is not None else ()
    return _SSS_STENCIL if any(int(surface.material_flags) & _SSS_PROFILE_USED for surface in surfaces) else 0


def pass_files(profile, catalog, chains, resolve: Callable[[str], tuple[str, bytes] | None],
               meshlets=None, wrinkles=None) -> dict[str, bytes]:
    outputs = surface_output_catalog_for(profile)
    bound = () if outputs is None else outputs.engine_textures
    files = {}
    for path in dict.fromkeys((*light_pass_files(catalog), *(() if chains is None else chains.files()),
                               *(() if meshlets is None else meshlets.files()),
                               *(() if wrinkles is None else wrinkles.files()), *(item.file for item in bound))):
        found = resolve(resource_path_with_version(path, "sdf", catalog.resource_version)
                        if path.endswith(".sdf") else path)
        if found is not None:
            files[path] = found[1]
    return files


def thumbnail(handler, session, size: int, background: tuple[int, int, int]) -> np.ndarray | None:
    preview = MeshPreviewScene(handler.mesh)
    points = mesh_bounds_points(preview.scene)
    if not len(points):
        return None
    inspection = MeshInspection(handler.mesh, preview)
    try:
        scene = LitPreview(handler, session, lambda: inspection)._gather()
    except MaterialPreviewError:
        return None
    minimum, maximum = points.min(axis=0), points.max(axis=0)
    view = LitView(0.0, -25.0, 1.55, tuple(map(float, (minimum + maximum) * 0.5)),
                   1.0 / max(float(np.max(maximum - minimum)), 1e-6), (size, size), inspection.geometry,
                   inspection.all_parts)
    drawer = LitDrawer()
    try:
        drawer.draw(scene, view, DEFAULT_INTENSITY, LitOverlays(background))
        return drawer.image()
    finally:
        drawer.close()


__all__ = ["DEFAULT_INTENSITY", "NO_LIGHT_PASSES", "UNKNOWN_SKINNING", "GameLighting", "LitDrawer", "LitFrame",
           "LitLabels", "LitLines", "LitOverlays", "LitPart", "LitPoints", "LitPreview", "LitScene", "LitShape",
           "LitTriangles", "LitView", "ShapeOverlay", "bind_part", "camera_light", "deformed", "engine_camera",
           "free_camera", "gbuffer_targets", "pass_files", "scene_file", "stencil_reference", "thumbnail"]
