"""See docs/internals/textures-view.md."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP

from file_handlers.mmtr.light_passes import light_pass_catalog_for
from file_handlers.mmtr.material_context import MaterialInstanceContext
from file_handlers.mmtr.material_preview import MaterialPreviewError, PreviewMaterial, _texture_texels, gbuffer_route
from file_handlers.mmtr.overlay import Overlay
from file_handlers.sdf.sdf_file import CULL_NONE
from file_handlers.mmtr.parsed_files import Lease
from file_handlers.mmtr.profiles import mmtr_profile_for_game
from file_handlers.mmtr.resident import Resident
from file_handlers.mmtr.pass_choice import PassChoiceError
from file_handlers.mmtr.speedtree_catalog import speedtree_catalog_for
from file_handlers.mmtr.surface_meaning import surface_output_catalog_for
from file_handlers.mmtr.textures_states import ShownDraw, ordered, plain_draw, program_draw
from file_handlers.mmtr.textures_view import (
    CHECKER, TexturesView, UvPattern, VertexLight, ViewBatch, ViewGeometry, ViewLight,
)
from file_handlers.stmesh.lit import cluster_route
from file_handlers.stmesh.stmesh_handler import StmeshHandler
from file_handlers.tex.texture_quality import texture_quality_profile
from utils.resource_file_utils import PrefetchedFiles

from .device_preview import DevicePreview
from .lit_preview import UNKNOWN_SKINNING, DeviceDrawer, LitOverlays, LitView, bound_master
from .material_resolver import MeshMaterialResolver
from .material_shader_preview import preview_material


@dataclass(frozen=True, slots=True)
class TexturedView(LitView):
    light: ViewLight | VertexLight | None = None
    vertex_colours: bool = True
    pattern: UvPattern | None = None


@dataclass(frozen=True, slots=True, eq=False)
class TexturedPart:
    index: int
    key: str
    indices: np.ndarray
    texture_path: str
    tint: tuple[float, float, float, float]
    cutout: bool
    two_sided: bool
    transparent: bool
    mmtr_path: str
    context: MaterialInstanceContext | None
    material: PreviewMaterial | None
    checker: bool = False
    route: str | None = None

    @classmethod
    def of_surface(cls, index: int, key: str, indices: np.ndarray, surface, batch, exact: bool,
                   colour: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0), route: str | None = None) -> TexturedPart:
        textured = bool(surface.texture_path)
        tint = tuple(surface.tint) if textured else (1.0, 1.0, 1.0, 1.0)
        return cls(index, key, indices, surface.texture_path,
                   tuple(float(channel * whole) for channel, whole in zip(tint, colour, strict=True)),
                   textured and bool(batch.alpha_test), bool(surface.two_sided), bool(batch.transparent),
                   surface.mmtr_path, surface.instance_context if exact else None,
                   preview_material(surface) if exact else None, route=route)

    @classmethod
    def unmatched(cls, index: int, key: str, indices: np.ndarray, batch,
                  colour: tuple[float, ...] = (1.0, 1.0, 1.0, 1.0)) -> TexturedPart:
        return cls(index, key, indices, "", tuple(map(float, colour)), False, True, bool(batch.transparent), "", None,
                   None, checker=True)


def exact_states(profile) -> bool:
    return (profile is not None and light_pass_catalog_for(profile) is not None
            and surface_output_catalog_for(profile) is not None)


@dataclass(frozen=True, slots=True, eq=False)
class TexturedShape:
    positions: np.ndarray
    normals: np.ndarray
    colours: np.ndarray | None


@dataclass(frozen=True, slots=True, eq=False)
class TexturedContent:
    game: str
    geometry: tuple[str, int]
    parts: tuple[TexturedPart, ...]
    uvs: np.ndarray | None
    files: Mapping[str, bytes]
    largest_mip: int | None
    anisotropy: int
    exact: bool
    masters: Mapping[str, tuple[str, bytes]]
    skinned: bool


@dataclass(frozen=True, slots=True)
class TexturedScene:
    content: TexturedContent
    shape: TexturedShape

    @property
    def geometry(self) -> tuple[str, int]:
        return self.content.geometry


@dataclass(frozen=True, slots=True)
class TexturedFrame:
    view: TexturedView
    drawn: bool
    failures: Mapping[int, Exception]
    picked: int | None = field(default=None, kw_only=True)


def bind_parts(parts, game: str, exact: bool, skinned: bool, files: Mapping[str, bytes],
               masters: Mapping[str, tuple[str, bytes]], largest_mip: int | None, lease: Lease,
               decoded: dict[int, tuple | None] | None = None) -> dict[int, tuple[ShownDraw, tuple | None] | Exception]:
    """``decoded`` is keyed by id of the file bytes: the caller keeps them alive while reusing it."""
    profile = catalog = None
    if exact:
        profile = mmtr_profile_for_game(game)
        catalog = light_pass_catalog_for(profile)
    materials: dict[tuple[str, str | None], ShownDraw | Exception] = {}
    textures = {} if decoded is None else decoded
    bound = {}
    for part in parts:
        if (part.key, part.route) not in materials:
            try:
                materials[part.key, part.route] = _part_draw(part, game, skinned, masters, profile, catalog, lease)
            except (ValueError, FileNotFoundError) as exc:
                materials[part.key, part.route] = exc
        draw = materials[part.key, part.route]
        if isinstance(draw, Exception):
            bound[part.index] = draw
            continue
        path = part.texture_path
        if not path:
            bound[part.index] = draw, CHECKER if part.checker else None
            continue
        data = files[path]
        if id(data) not in textures:
            textures[id(data)] = _texture_texels(data, "texture2d", largest_mip)
        if textures[id(data)] is None:
            bound[part.index] = MaterialPreviewError(QT_TRANSLATE_NOOP(
                "MaterialPreview", "The texture {path} is not a 2D texture the preview reads."), {"path": path})
            continue
        bound[part.index] = draw, textures[id(data)]
    return bound


def _part_draw(part: TexturedPart, game: str, skinned: bool, masters, profile, catalog,
               lease: Lease) -> ShownDraw:
    if part.material is None:
        return plain_draw(part.two_sided, part.transparent)
    document = bound_master(game, masters, part.mmtr_path, part.context, profile, lease)
    draw = program_draw(document, part.material, catalog.master_target, part.route or gbuffer_route(skinned))
    if draw is None:
        raise MaterialPreviewError(QT_TRANSLATE_NOOP(
            "MaterialPreview", "The renderer does not draw the material's base colour: its program does not "
                               "write that target, or the master draws the material only in passes of its own."))
    return draw


class TexturesDrawer(DeviceDrawer):
    _drawn: tuple[tuple, tuple[TexturesView, Overlay] | None, dict[int, Exception]] | None

    def __init__(self, hardware: bool = True):
        super().__init__()
        self._hardware = hardware
        self._content: TexturedContent | None = None
        self._bound: dict[int, tuple[ShownDraw, tuple | None] | Exception] = {}
        self._held: TexturedShape | None = None

    @staticmethod
    def _overlay(drawn: tuple) -> Overlay:
        return drawn[1]

    def _hold(self, content) -> None:
        self._leave()
        self._lease.close()
        self._content, self._lease = content, Lease()
        self._bound = self._bind(content)

    def _bind(self, content: TexturedContent) -> dict[int, tuple[ShownDraw, tuple | None] | Exception]:
        return bind_parts(content.parts, content.game, content.exact, content.skinned, content.files,
                          content.masters, content.largest_mip, self._lease)

    def draw(self, scene: TexturedScene, view: TexturedView, overlays: LitOverlays) -> TexturedFrame:
        content, shape = scene.content, scene.shape
        if content is not self._content:
            self._hold(content)
        key = view.visible, view.size, view.vertex_colours
        if self._drawn is None or self._drawn[0] != key:
            self._leave()
            shown = {part.index: (part, self._bound[part.index]) for part in content.parts
                     if part.index in view.visible}
            failures = {index: item for index, (_part, item) in shown.items() if isinstance(item, Exception)}
            steps = ordered([(item[0], (part, item[1])) for part, item in shown.values()
                             if not isinstance(item, Exception)])
            drawn = None
            if steps:
                device = Resident(self._hardware)
                geometry = ViewGeometry(shape.positions, shape.normals, content.uvs, shape.colours)
                textures = TexturesView(device, view.size, [geometry], [
                    ViewBatch(part.indices, texture, part.tint, part.cutout, states,
                              coloured=view.vertex_colours or texture is None)
                    for states, (part, texture) in steps], anisotropy=content.anisotropy)
                drawn = textures, Overlay(device, view.size, textures.depth)
                self._held = shape
            self._drawn = (key, drawn, failures)
        _key, drawn, failures = self._drawn
        if drawn is None:
            return TexturedFrame(view, False, failures)
        textures, overlay = drawn
        if shape is not self._held:
            textures.write(0, positions=shape.positions, normals=shape.normals, colours=shape.colours)
            self._held = shape
        camera = view.camera()
        device = textures.device
        if overlays.filled:
            device.pending += textures.frame(camera, view.light, overlays.background, pattern=view.pattern)
        device.pending += self._shape.steps(overlay, textures.shown, overlays, camera, view.visible)
        device.flush()
        return TexturedFrame(view, True, failures)

    def _cull(self, index: int) -> tuple[int, bool]:
        item = self._bound.get(index)
        if isinstance(item, tuple):
            states = item[0].states
            return states.cull, bool(states.front_counter_clockwise)
        return CULL_NONE, True


def _shape(draw) -> TexturedShape:
    """Copies: the viewer moves its draw's arrays in place."""
    return TexturedShape(np.array(draw.vertices, np.float32).reshape(-1, 3),
                         np.array(draw.normals, np.float32).reshape(-1, 3),
                         None if draw.colors is None else np.array(draw.colors, np.float32).reshape(-1, 4))


class TexturesPreview(DevicePreview):
    view_type = TexturedView

    def __init__(self, handler, session, inspection, shown, parent=None):
        super().__init__(handler, session, inspection, parent)
        self._shown = shown

    def geometry_changed(self, preview=None) -> None:
        scene = self._scene
        super().geometry_changed(preview)
        if self._active and scene is not None and self._scene is scene and preview is not None:
            self._scene = replace(scene, shape=_shape(preview.scene[0]))
            self._ask()

    def _make_drawer(self) -> TexturesDrawer:
        return TexturesDrawer()

    def _request(self) -> tuple:
        return self._scene, self._view, self._overlays

    def _gather(self) -> TexturedScene:
        handler, session, inspection, preview = self._handler, self._session, self._inspection(), self._shown()
        if inspection is None or preview is None or (preview.layer, preview.lod_index) != inspection.geometry:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The mesh has no geometry shown."))
        draw = preview.scene[0]
        if draw.normals is None:
            raise MaterialPreviewError(QT_TRANSLATE_NOOP("MaterialPreview", "The mesh has no normals."))
        game = MeshMaterialResolver.material_profile(handler).game
        profile = mmtr_profile_for_game(game)
        exact = exact_states(profile)
        content = handler.mesh.header.decoded_content_flags
        if exact and content is None:
            raise MaterialPreviewError(UNKNOWN_SKINNING)
        quality = texture_quality_profile(session.texture_quality)
        cache: dict = {}
        files: dict[str, bytes] = {}
        masters: dict[str, tuple[str, bytes]] = {}
        unusable: dict[str, str] = {}
        parts = []
        for index, (submesh, batch) in enumerate(zip(inspection.submeshes, draw.batches, strict=True)):
            binding = (session.bindings[submesh.material_index]
                       if 0 <= submesh.material_index < len(session.bindings) else None)
            surface = None if binding is None else binding.surface
            key = "" if binding is None else session.material_key(binding.mesh_material_name)
            if surface is None or exact and surface.instance_context is None:
                parts.append(TexturedPart.unmatched(index, key, np.array(batch.indices, np.uint32), batch))
                continue
            if key not in unusable:
                unusable[key] = self._gather_material(handler, surface, masters if exact else None, files,
                                                      quality.prefer_streaming, cache)
            if unusable[key]:
                self._failures[index] = unusable[key]
                continue
            route, programs = None, exact
            if exact and isinstance(handler, StmeshHandler) and handler.stmesh.meshlets is not None:
                # Meshlet SpeedTree chunks have no vertex layout.
                programs = False
            elif exact and isinstance(handler, StmeshHandler):
                # SpeedTree clusters use their batch type's programs.
                try:
                    route = cluster_route(handler, inspection.geometry[1], submesh, int(surface.material_flags),
                                          speedtree_catalog_for(profile), None)
                except (MaterialPreviewError, PassChoiceError):
                    programs = False
            parts.append(TexturedPart.of_surface(index, key, np.array(batch.indices, np.uint32), surface, batch,
                                                 programs, route=route))
        uvs = None if draw.uvs is None else np.array(draw.uvs, np.float32).reshape(-1, 2)
        return TexturedScene(
            TexturedContent(game, inspection.geometry, tuple(parts), uvs, PrefetchedFiles(files), quality.max_dimension,
                            max(1, int(quality.anisotropy)), exact, masters, bool(exact and content.is_skinning)),
            _shape(draw))


__all__ = ["TexturedContent", "TexturedFrame", "TexturedPart", "TexturedScene", "TexturedShape", "TexturedView",
           "TexturesDrawer", "TexturesPreview", "bind_parts", "exact_states"]
