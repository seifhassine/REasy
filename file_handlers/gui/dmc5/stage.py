"""See docs/internals/gui-editor.md, Stage."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
from PySide6.QtCore import QCoreApplication, QPointF, QRectF
from PySide6.QtGui import QImage, QPainterPath, QPolygonF, QTransform

lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.mmtr.resident import Resident, warm_up
lazy from ui.field_form import Field
lazy from utils.resource_file_utils import resource_path_with_version

lazy from ..codec import parse_gui
lazy from ..dependencies import GuiDependencyCatalog
lazy from ..errors import GuiAssetError, GuiEditError, GuiSceneError
lazy from ..model import GuiAnimation, GuiTextureSet
lazy from ..passes import gui_pass_catalog_for
lazy from ..scene import GuiSceneNode, GuiWorkspace
lazy from .cameras import gui_view, placement, placements, projection
lazy from .device import GuiDevice
lazy from .icon_tags import Dmc5IconInputContext
lazy from .interaction import Dmc5GuiInteraction
lazy from .meshes import box_outline, covers, entity_world, mesh_triangles, mesh_world
from .playback import Dmc5GuiPlayback
lazy from .scene import Dmc5GuiScene
lazy from .text_typing import TextTyping
lazy from .vertices import bounds, circle_points, hit_polygon

_EPSILON = float(np.finfo(np.float32).eps)
_CONTROL = "via.gui.Control"
_UNBOUNDED = ("via.gui.Mesh", "via.gui.Effect")
_INPUT = ("Interactive", "HitVisible")


def _tr(text: str) -> str:
    return QCoreApplication.translate("GuiStage", text)


class Dmc5GuiStage:
    position_field, visible_field = "Position", "Visible"

    def __init__(self, dependencies: GuiDependencyCatalog, game: str = "") -> None:
        profile = self._profile = dependencies.profile
        self._dependencies, self._game = dependencies, game or profile.game
        self.scene: Dmc5GuiScene | None = None
        self.playback: Dmc5GuiPlayback | None = None
        self.device: GuiDevice | None = None
        self._image: QImage | None = None
        self._transient: tuple[GuiSceneNode, float, float] | None = None
        self.output_size: tuple[int, int] | None = None
        self.safe_area_ratio = profile.safe_area_ratio
        self.language = profile.language(profile.preview_language)
        self.icon_context = Dmc5IconInputContext(keyboard_mode=True)
        self.pointer = profile.try_pointer
        self.gaps: list[tuple[str, str]] = []
        self.interaction: Dmc5GuiInteraction | None = None
        self.live, self._scripted = False, 0
        self.placement, self.world, self._views, self._triangles = 0, None, {}, {}
        self.flat, self._window = False, None
        self.moment: float | None = None
        warm_up()

    def set_scene(self, scene: Dmc5GuiScene) -> None:
        self.scene, self.playback, self._scripted = scene, Dmc5GuiPlayback(scene), 0
        world, key = self._dependencies.world, scene.workspace.root_key
        scene.held_segment = lambda: world.segment(key) if self.in_game else None
        if self.in_game and self.placement >= len(placements(scene, world)):
            self.placement = 0
        self.interaction, self.typing = Dmc5GuiInteraction(self), TextTyping()
        self.layout()

    @property
    def in_game(self) -> bool:
        return self._dependencies.in_game

    @in_game.setter
    def in_game(self, on: bool) -> None:
        self._dependencies.in_game, self.placement, self.moment, self.output_size = on, 0, None, None
        self.scene._invalidate_runtime()
        self.layout(self._transient)
        self.interact(self.live)

    def interact(self, on: bool) -> None:
        self.live, self._image = on, None
        self.moment = self.world["moves"].start if on and self.world and self.world["moves"] else None

    def press(self, x: float, y: float) -> None:
        self.interaction.press(x, y)

    def hover(self, x: float, y: float) -> bool:
        return self.interaction.hover(x, y)

    def key(self, event) -> bool:
        return self.interaction.key(event)

    def release(self, event=None) -> None:
        self.interaction.release(event)

    @property
    def size(self) -> tuple[float, float]:
        return self.scene.viewport_size

    @property
    def frame(self) -> float:
        return self.playback.frame

    @property
    def commands(self) -> dict[str, tuple[int, ...]]:
        return self._dependencies.world.commands

    def show_state(self, node: GuiSceneNode | None, animation: GuiAnimation | None) -> None:
        symbol = node.prototype.symbol if node is not None and node.prototype is not None else None
        self.playback.select_animation(symbol, animation, node.path if node is not None else None)
        self.layout()

    def state(self, node: GuiSceneNode | None) -> GuiAnimation | None:
        held = self.playback.controllers.get(node.path) if node is not None else None
        return held.animation if held is not None else None

    def set_frame(self, frame: float) -> None:
        self.playback.set_selected_frame(frame)
        self.layout()

    def value(self, node: GuiSceneNode, name: str):
        return self.playback.overrides[node.path].get(name)

    def script(self, node: GuiSceneNode, name: str, value) -> None:
        self.playback.assign(node.path, name, value)
        self.interaction.assigned(node, name)
        self.typing.assigned(node, name, value)
        self._scripted += 1
        self.layout()

    def advance(self, seconds: float) -> None:
        rate = max(1.0, float(self.scene.root.render_properties["BaseFps"]))
        frames = min(seconds * rate, rate * 0.25)
        self.interaction.advance(frames, frames / rate)
        self.typing.advance(self.scene.tree_nodes, frames)
        self.playback.advance(frames)
        if self.live and self.moment is not None:
            self.moment = min(self.world["moves"].end, self.moment + frames / rate * self.world["moves"].rate)
        if self.device is not None and self.device.clock is not None:
            self.device.clock.step(frames / rate)
        self.layout()

    def layout(self, transient: tuple | None = None) -> None:
        self._transient, scene, held = transient, self.scene, self.playback.overrides
        placed = placement(scene, self._dependencies.world, held.get(scene.root.path),
                           self.placement) if self.in_game else None
        if placed is not None and placed["moves"] is not None and self.moment is not None:
            placed = {**placed, **self._dependencies.world.at(placed, self.moment)}
        if placed is not None and "of" in placed:
            view = self._view(placed["of"])
            placed, held = (None, held) if view is None else (placed, {**held, scene.root.path: {
                **held.get(scene.root.path, {}), **view}})
        self.world, own = placed, scene.runtime_properties(scene.root, held.get(scene.root.path))
        self.flat = placed is None and (self._profile.enum_label(own, "ViewType") != "Screen"
                                        or own["UseGUICamera"])
        unit = self.flat and scene.screen_size == (1.0, 1.0)
        if self.output_size is None or not unit:
            self._window = None
        if self.output_size is None:
            wide, high = size = self._profile.world_view_output
            if unit:
                scene.update_preview(held)
                seen = QRectF(-wide / 2, -high / 2, wide, high)
                for node in scene.draw_nodes:
                    if node.effective_visible:
                        seen |= self.outline(node).boundingRect()
                self._window = QRectF(seen.toAlignedRect()).getRect()
            self.output_size = tuple(round(side) for side in (
                size if placed is not None else self._window[2:] if unit else scene.screen_size))
        if self._window:
            (left, top, wide, high), (x, y, z) = self._window, own["Position"]
            held = {**held, scene.root.path: {**held.get(scene.root.path, {}), "ScreenSize": [wide, high],
                                              "Position": [x - left, y - top, z]}}
        catalog = None if placed is None else gui_pass_catalog_for(mmtr_profile_for_game(self._game))
        scene.update_preview(held, output_size=self.output_size,
                             safe_area_ratio=self.safe_area_ratio, transient=transient,
                             projection=None if placed is None else lambda screen, pixels: projection(
                                 catalog, placed, screen, pixels))
        self._image = None

    def _view(self, key: str) -> dict | None:
        if key not in self._views:
            found = self._dependencies.resource_data_loader(resource_path_with_version(
                key, "gui", self.scene.workspace.root.document.version))
            shown = found and GuiWorkspace.from_document(key, parse_gui(bytes(found[1]), str(found[0])),
                                                         profile=self._profile).instantiate()
            self._views[key] = shown and {name: value for name, value in shown.runtime_properties(shown.root).items()
                                          if name not in _INPUT}
        return self._views[key]

    def anchor(self, node: GuiSceneNode) -> tuple[float, float]:
        return node.world_position

    def local(self, node: GuiSceneNode, x: float, y: float) -> tuple[float, float]:
        try:
            return self.scene.local_position_for_scene_point(node, x, y)
        except GuiSceneError as error:
            raise GuiEditError(str(error)) from error

    def size_field(self, node: GuiSceneNode) -> str | None:
        name = self._profile.runtime_type(node.object.type_name).facts.get("size")
        return name if node.parent is not None and min(node.size) > 0.0 else None

    def handles(self, node: GuiSceneNode) -> dict[tuple[int, int], QPointF]:
        placed = QTransform(*node.world_transform)
        if self.size_field(node) is None or not placed.isInvertible():
            return {}
        left, top, right, bottom = bounds(node, node.size)
        across, down = (left, (left + right) / 2, right), (top, (top + bottom) / 2, bottom)
        return {(column - 1, row - 1): placed.map(QPointF(across[column], down[row]))
                for column in range(3) for row in range(3) if (column, row) != (1, 1)}

    def resize(self, node: GuiSceneNode, handle: tuple[int, int], x: float,
               y: float) -> tuple[tuple[float, float], tuple[float, float]]:
        placed = QTransform(*node.world_transform)
        at, (left, top, right, bottom) = placed.inverted()[0].map(QPointF(x, y)), bounds(node, node.size)
        width = max(1.0, round(right - at.x() if handle[0] < 0 else at.x() - left if handle[0] > 0 else right - left))
        height = max(1.0, round(bottom - at.y() if handle[1] < 0 else at.y() - top if handle[1] > 0 else bottom - top))
        edges = bounds(node, (width, height))
        origin = placed.map(QPointF((right - width if handle[0] < 0 else left) - edges[0],
                                    (bottom - height if handle[1] < 0 else top) - edges[1]))
        return (width, height), tuple(round(part, 3) for part in self.local(node, origin.x(), origin.y()))

    def settings(self, changed) -> list[Field]:
        def put(name: str, value) -> None:
            setattr(self, name, value)
            if self.device is not None:
                self.device.language, self.device.icon_context = self.language, self.icon_context
                self.device.forget()
            self.layout(self._transient)
            changed()

        def side(at: int, value: int) -> None:
            if not 1 <= value <= 16384:
                raise ValueError(value)
            put("output_size", tuple(value if place == at else held for place, held in enumerate(self.output_size)))

        def ratio(value: float) -> None:
            if not 0.01 <= value <= 1.0:
                raise ValueError(value)
            put("safe_area_ratio", value)

        languages = {value: label for label, value in sorted(self._profile.languages.items(), key=lambda item: item[1])}
        pointers = {value: label for label, value in self._profile.enum("MouseSelectType").items() if value}
        places = placements(self.scene, self._dependencies.world, self.playback.overrides.get(
            self.scene.root.path)) if self.in_game else ()
        parts, moves = places and "of" in places[0], self.world and self.world["moves"]

        def moment(value: float) -> None:
            if not moves.start <= value <= moves.end:
                raise ValueError(value)
            put("moment", value)

        timed = [Field(_tr("Timeline frame"), "float",
                       lambda: self.world["moment"] if self.moment is None else self.moment, moment,
                       tooltip=_tr("The frame of the game's timeline that moves this view's camera, shows or hides "
                                   "the view and sets its lights: the game plays it from {0} to {1}, and so does "
                                   "Try.").format(moves.start, moves.end))] if moves else []
        chosen = [Field(_tr("Placement"), "enum", lambda: self.placement, lambda value: put("placement", value),
                        {at: item["name"] for at, item in enumerate(places)},
                        _tr("The game shows this file's parts inside these GUIs: its view is drawn as theirs.")
                        if parts else _tr("Where the game puts this view: it is in more than one place."))
                  ] if len(places) > 1 or parts else []
        return [
            Field(_tr("Shown"), "enum", lambda: int(self.in_game), lambda value: put("in_game", bool(value)),
                  {1: _tr("As the game shows it"), 0: _tr("The file alone")},
                  _tr("As the game shows it: with what the game's scenes hold for this file, which the file does "
                      "not name: where they put its view, its camera, lights and timeline, their GUI "
                      "component's segment and the message files they add. The file alone: only what the file "
                      "and the game's GUI config name.")),
            *chosen, *timed,
            Field(_tr("Output"), "row", lambda: None, tooltip=_tr("The size of the window the GUI is drawn into."),
                  options={"fields": [Field("", "int", lambda: self.output_size[0], lambda value: side(0, value)),
                                      Field("x", "int", lambda: self.output_size[1], lambda value: side(1, value))]}),
            Field(_tr("Safe area"), "float", lambda: self.safe_area_ratio, ratio,
                  tooltip=_tr("GUISystem.SafeAreaRatio: the part of the screen elements with SafeAreaAdjust keep to. "
                              "The game sets it; a file does not hold it.")),
            Field(_tr("Language"), "enum", lambda: self.language, lambda value: put("language", value), languages,
                  _tr("The language messages are shown in. The game sets it; a file does not hold it.")),
            Field(_tr("Input"), "enum", lambda: int(not self.icon_context.keyboard_mode),
                  lambda value: put("icon_context", replace(self.icon_context, keyboard_mode=value == 0)),
                  {0: _tr("Keyboard"), 1: _tr("Gamepad")}, _tr("The device whose button icons texts show.")),
            Field(_tr("Pointer"), "enum", lambda: self._profile.enum("MouseSelectType").get(self.pointer, 0),
                  lambda value: put("pointer", pointers.get(value, "")), {0: _tr("As the file says"), **pointers},
                  _tr("How lists take the pointer in Try (MouseSelectType). A script can set it while the game "
                      "runs; a click on the selected item decides it.")),
        ]

    def sources(self) -> list[tuple[str, str, str]]:
        world, labels = self._dependencies.world, {
            "placement": _tr("Placement"), "camera": _tr("Camera"), "timeline": _tr("Timeline"),
            "lights": _tr("Lights"), "segment": _tr("Segment"), "messages": _tr("Added messages"),
            "commands": _tr("Key commands")}
        found = {**({} if self.world is None else world.behind(self.world)),
                 **{role: files for role, files in world.asked.items() if self.in_game or role == "commands"}}
        return [(label, path, world.found(path)) for role, label in labels.items() for path in found.get(role, ())]

    def image(self) -> QImage:
        if self._image is None:
            self.gaps = [*self._limits(), *self.interaction.gaps(self.live)]
            size = tuple(max(1, round(side)) for side in self.scene.display_size)
            loader = self._dependencies.resource_data_loader
            try:
                if self.device is None:
                    self.device = GuiDevice(Resident(True), mmtr_profile_for_game(self._game), self._profile,
                                            lambda path: (found := loader(path)) and found[1], self._dependencies,
                                            self.icon_context)
                    self.device.language = self.language
                self.device.typed = self.typing.typed
                texture, left = self.device.frame(self.scene, size, self._drawn, self.world)
            except (RuntimeError, ValueError, FileNotFoundError) as error:
                self.gaps.append((self.scene.root.path, str(error)))
                self._image = QImage()
                return self._image
            pixels, = self.device.device.flush([texture])
            self._image = QImage(pixels, *size, size[0] * 4, QImage.Format.Format_RGBX8888).copy()
            self.gaps += left
        return self._image

    def _drawn(self, node: GuiSceneNode) -> bool:
        return self._shown() and node.effective_visible and node.color_scale[3] >= _EPSILON

    def _shown(self) -> bool:
        return self.world is None or self.world["drawn"]

    def _limits(self) -> list[tuple[str, str]]:
        profile, root = self._profile, self.scene.root
        view, found = root.render_properties, []
        if self._scripted:
            found.append((root.path, _tr("{0} values are set as a script may set them: the GUI does not load so")
                          .format(self._scripted)))
        if self.flat:
            found.append((root.path, _tr("a world view is drawn flat: it follows the game's camera")
                          if not view["UseGUICamera"] else
                          _tr("a GUI camera view is drawn flat: where the game puts this file is not known")
                          if self.in_game else
                          _tr("a GUI camera view is drawn flat: the file alone does not say where the game puts it")))
        if self._window:
            found.append((root.path, _tr("the view's size is one unit: the frame is the game's screen size around "
                                         "its origin, widened to hold what it shows")))
        if not self._shown():
            found.append((root.path, _tr("the game does not draw this view at this frame of its timeline")))
        if not view["Overlay"]:
            found.append((root.path, _tr("the game's post-processing after this view is not drawn (its Detonemap "
                                         "undoes the tonemap's exposure and range)")
                          if view["Detonemap"] and not view["UseGUICamera"] else
                          _tr("the game's exposure, tonemap and post-processing after this view are not drawn")))
        if profile.enum_label(view, "ReprojectionType") != "Default":
            found.append((root.path, _tr("the view's reprojection pass is not drawn")))
        segment = profile.control_fields["segment"]
        return found + [(node.path, _tr("its segment is not followed: no game file holds this GUI, so the GUI's "
                                        "own segment is not known") if self.in_game else
                         _tr("its segment is not followed: the file alone does not hold the GUI's own segment"))
                        for node in self.scene.nodes if root.segment is None
                        and profile.enum_label(node.render_properties, segment, "Segment") != "Keep"]

    def outline(self, node: GuiSceneNode, hit: bool = False) -> QPainterPath:
        if "mesh" not in self._profile.runtime_type(node.object.type_name).facts:
            return QTransform(*node.world_transform).map(self._shape(node, hit))
        path, found = QPainterPath(), self._mesh(node)
        if found:
            path.addPolygon(QPolygonF([QPointF(*point) for point in box_outline(*found[1:])]))
            path.closeSubpath()
        return path

    def _mesh(self, node: GuiSceneNode) -> tuple | None:
        names, placed, scene = self._profile.runtime_type(node.object.type_name).facts["mesh"], self.world, self.scene
        reference = str(node.render_properties[names["mesh"]])
        try:
            mesh = placed and self._dependencies.assets.load_mesh(reference)[1]
        except GuiAssetError:
            return None
        if not mesh:
            return None
        catalog = gui_pass_catalog_for(mmtr_profile_for_game(self._game))
        sides = tuple(max(1.0, side) for side in scene.root.render_properties["ScreenSize"])
        world = mesh_world(node, names, catalog.mesh, self._profile.degree_factors, gui_view(
            catalog, sides, placed["world"]), entity_world(placed["world"], placed["rotation"]))
        return reference, mesh, projection(catalog, placed, sides, scene.display_size, world), placed["lens"].near

    def _under(self, node: GuiSceneNode, x: float, y: float) -> bool:
        if "mesh" not in self._profile.runtime_type(node.object.type_name).facts:
            return True
        reference, mesh, shown, near = self._mesh(node)
        if reference not in self._triangles:
            self._triangles[reference] = mesh_triangles(mesh)
        return covers(self._triangles[reference], shown, near, x, y)

    def pick(self, x: float, y: float) -> list[GuiSceneNode]:
        point, root = QPointF(x, y), self.scene.root
        return list(dict.fromkeys(
            node.origin() for node in reversed(self.scene.draw_nodes)
            if node is not root and node.effective_visible and self.outline(node, True).contains(point)
            and self._under(node, x, y)))

    def _rect(self, node: GuiSceneNode) -> QRectF:
        left, top, right, bottom = bounds(node, node.size)
        return QRectF(left, top, right - left, bottom - top)

    def _shape(self, node: GuiSceneNode, hit: bool) -> QPainterPath:
        path, kind, profile = QPainterPath(), node.object.type_name, self._profile
        special = node.object.special_data
        if kind == "via.gui.TextureSet" and isinstance(special, GuiTextureSet):
            scale = node.render_properties["Scale"]
            for entry in special.entries:
                left, top, right, bottom = entry.bounds
                path.addRect(QRectF(left * scale[0], top * scale[1], (right - left) * scale[0],
                                    (bottom - top) * scale[1]).normalized())
        elif kind == "via.gui.HitArea":
            rect, shape = self._rect(node), profile.enum_label(node.render_properties, "Shape", "HitAreaShape")
            if shape == "Triangle":
                path.addPolygon(QPolygonF([rect.bottomLeft(), QPointF(rect.center().x(), rect.top()),
                                           rect.bottomRight()]))
            elif shape in profile.runtime_type(kind).facts["polygons"]:
                path.addPolygon(QPolygonF([QPointF(*point) for point in hit_polygon(node, node.size)]))
            else:
                path.addRect(rect)
        elif kind == "via.gui.Circle":
            points = circle_points(node, node.size, hit_test=hit)
            if points is not None:
                outer, inner = ([QPointF(*point) for point in ring] for ring in points)
                path.addPolygon(QPolygonF([*outer, *reversed(inner)]))
        elif _CONTROL not in profile.runtime_type(kind).classes and kind not in _UNBOUNDED:
            path.addRect(self._rect(node))
        return path
