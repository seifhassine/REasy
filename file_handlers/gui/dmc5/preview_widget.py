from __future__ import annotations

from dataclasses import replace
from functools import lru_cache

import numpy as np
from PySide6.QtCore import QElapsedTimer, QPointF, QRectF, Qt, QTimer, Signal
lazy from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QPolygonF, QTransform
from PySide6.QtWidgets import QWidget

lazy from file_handlers.mmtr.profiles import mmtr_profile_for_game
lazy from file_handlers.native_math import f32
lazy from file_handlers.mmtr.resident import Resident, warm_up
lazy from ui.styles import current_colors

lazy from ..dependencies import GuiDependencyCatalog
lazy from ..model import GuiAnimation, GuiSymbol, GuiTextureSet
lazy from ..scene import GuiSceneNode
lazy from .controllers import Dmc5GuiControllerContext
lazy from .device import GuiDevice
lazy from .icon_tags import Dmc5IconInputContext
from .playback import Dmc5GuiPlayback
lazy from .scene import Dmc5GuiScene, project_gui_point
lazy from .text_layout import TextLayoutGap
lazy from .vertices import bounds, circle_points, hit_polygon, vertex_color


_FLOAT32_EPSILON = float(np.finfo(np.float32).eps)


class Dmc5GuiCanvas(QWidget):
    node_selected = Signal(object)
    node_moved = Signal(object, float, float)
    frame_changed = Signal(float, float)
    diagnostics_changed = Signal(str)

    def __init__(
        self,
        dependencies: GuiDependencyCatalog,
        controller_context: Dmc5GuiControllerContext | None = None,
        parent=None,
        game: str = "",
    ) -> None:
        """``game``: the game whose build's GUI programs draw the preview, the runtime's when empty."""
        super().__init__(parent)
        self._game = game or dependencies.profile.game
        self.setMinimumSize(520, 320)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._scene: Dmc5GuiScene | None = None
        self.playback: Dmc5GuiPlayback | None = None
        self._dependencies = dependencies
        self._device: GuiDevice | None = None
        self._image: QImage | None = None
        self._controller_context = (
            controller_context or Dmc5GuiControllerContext()
        )
        warm_up()
        self._preview = False
        self._interaction_enabled = False
        self._guides = False
        self._playing = False
        self._interaction_frames = 0.0
        self._selected: GuiSceneNode | None = None
        self._output_size: tuple[float, float] | None = None
        self._safe_area_ratio: float | None = None
        self._profile = dependencies.profile
        self._language = dependencies.profile.language(dependencies.profile.preview_language)
        self._icon_context = Dmc5IconInputContext(keyboard_mode=True)
        self._runtime_overrides: dict[str, dict[str, object]] = {}
        self._runtime_active_by_path: dict[str, bool] = {}
        self._zoom = 1.0
        self._pan = QPointF()
        self._drag_node: GuiSceneNode | None = None
        self._drag_start = QPointF()
        self._drag_origin = (0.0, 0.0)
        self._transient: tuple[GuiSceneNode, float, float] | None = None
        self._panning = False
        self._pan_start = QPointF()
        self._interaction_ready = False
        self._active_list: GuiSceneNode | None = None
        self._list_selection: dict[str, int] = {}
        self._list_items_by_owner: dict[str, tuple[GuiSceneNode, ...]] = {}
        self._static_boundaries: set[tuple[str, str]] = set()
        self._last_frame_boundaries: set[tuple[str, str]] = set()
        self._frame_boundaries: set[tuple[str, str]] | None = None
        # A child timer dies with the canvas.
        self._diagnostic_timer = QTimer(self)
        self._diagnostic_timer.setSingleShot(True)
        self._diagnostic_timer.setInterval(0)
        self._diagnostic_timer.timeout.connect(self._emit_diagnostics)
        self._timer = QTimer(self)
        self._timer.setInterval(16)
        self._timer.timeout.connect(self._tick)
        self._clock = QElapsedTimer()

    def set_document(self, scene: Dmc5GuiScene) -> None:
        self._scene = scene
        self.playback = Dmc5GuiPlayback(scene)
        self._selected = None
        self._output_size = scene.screen_size
        self._interaction_ready = False
        self._active_list = None
        self._list_selection.clear()
        self._list_items_by_owner.clear()
        self._runtime_overrides.clear()
        self._runtime_active_by_path.clear()
        self._static_boundaries.clear()
        self._last_frame_boundaries.clear()
        self._interaction_frames = 0.0
        self._forget()
        self._apply_layout()
        self.fit_document()

    def set_controller_context(
        self,
        context: Dmc5GuiControllerContext | None,
    ) -> None:
        self._controller_context = context or Dmc5GuiControllerContext()
        self._emit_diagnostics()
        self._image = None
        self.update()

    def set_runtime_properties(
        self,
        values_by_path: dict[str, dict[str, object]] | None,
    ) -> None:
        """For values GUIR files do not hold, such as save data and menu-controller labels, which the game's host and
        controllers set."""

        self._runtime_overrides = {
            str(path): dict(values)
            for path, values in (values_by_path or {}).items()
        }
        self._apply_layout()

    def set_runtime_active(
        self,
        values_by_path: dict[str, bool] | None,
    ) -> None:
        """Install native runtime-active flags for hit/input traversal."""

        self._runtime_active_by_path = {
            str(path): bool(value)
            for path, value in (values_by_path or {}).items()
        }
        self._image = None
        self.update()

    def set_runtime_playback(
        self,
        values_by_path: dict[str, dict[str, object]] | None,
    ) -> None:
        """Restore captured Control states without modifying authored clips."""

        if self.playback is None:
            return
        self.playback.reset_runtime()
        self.playback.apply_runtime_snapshot({
            str(path): dict(values)
            for path, values in (values_by_path or {}).items()
        })
        self._interaction_frames = 0.0
        self._apply_layout()

    def set_preview_enabled(self, enabled: bool) -> None:
        self._preview = bool(enabled)
        if not self._preview:
            self._interaction_enabled = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
        if self._preview and not self._interaction_ready:
            self._initialize_interaction()
        self._sync_timer()
        self._apply_layout()

    def set_interaction_enabled(self, enabled: bool) -> None:
        self._interaction_enabled = bool(enabled) and self._preview
        self.setCursor(Qt.CursorShape.ArrowCursor)
        if self._interaction_enabled:
            self.setFocus(Qt.FocusReason.OtherFocusReason)

    def set_playing(self, enabled: bool) -> None:
        self._playing = bool(enabled)
        self._sync_timer()

    def set_guides(self, enabled: bool) -> None:
        self._guides = bool(enabled)
        self.update()

    def set_output_size(self, width: int, height: int) -> None:
        self._output_size = max(1, int(width)), max(1, int(height))
        self._apply_layout()

    def set_safe_area_ratio(self, ratio: float) -> None:
        """Set the external ``GUISystem.SafeAreaRatio`` preview input."""

        self._safe_area_ratio = min(1.0, max(0.01, float(ratio)))
        self._apply_layout()

    def set_language(self, language: int) -> None:
        self._language = int(language)
        self._forget()

    def set_input_device(self, device: int) -> None:
        if device not in (0, 1):
            raise ValueError(f"unknown DMC5 GUI input device index {device}")
        self._icon_context = replace(
            self._icon_context,
            keyboard_mode=device == 0,
        )
        self._forget()

    def _forget(self) -> None:
        if self._device is not None:
            self._device.language, self._device.icon_context = self._language, self._icon_context
            self._device.forget()
        self._image = None
        self.update()

    def select_animation(
        self,
        symbol: GuiSymbol | None,
        animation: GuiAnimation | None,
        path: str | None = None,
    ) -> None:
        if self.playback is None:
            return
        self.playback.select_animation(symbol, animation, path)
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)

    def set_frame(self, frame: float) -> None:
        if self.playback is None:
            return
        self.playback.set_selected_frame(frame)
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)

    def restart_animation(self) -> None:
        if self.playback is None:
            return
        self.playback.restart_selected()
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)

    def document_changed(self) -> None:
        if self.playback is not None:
            self.playback.rebuild()
        self._static_boundaries.clear()
        self._last_frame_boundaries.clear()
        self._emit_diagnostics()
        self._apply_layout()

    def update_branch(self, _node: GuiSceneNode) -> None:
        self.document_changed()

    def fit_document(self) -> None:
        self._zoom = 1.0
        self._pan = QPointF()
        self.update()

    def select_node(self, node: GuiSceneNode) -> None:
        self._selected = node
        self.update()

    def _sync_timer(self) -> None:
        active = self._preview and (self._playing or self._interaction_frames > 0.0)
        if active and not self._timer.isActive():
            self._clock.restart()
            self._timer.start()
        elif not active:
            self._timer.stop()

    def _tick(self) -> None:
        if self.playback is None or self._scene is None:
            return
        elapsed = self._clock.restart() / 1000.0
        fps = max(1.0, float(self._scene.root.render_properties["BaseFps"]))
        delta = min(elapsed * fps, fps * 0.25)
        self.playback.advance(delta)
        if self._device is not None and self._device.clock is not None:
            self._device.clock.step(delta / fps)
        if self._interaction_frames > 0.0:
            self._interaction_frames = max(0.0, self._interaction_frames - delta)
        self._apply_layout()
        self.frame_changed.emit(self.playback.frame, self.playback.duration)
        self._sync_timer()

    def _apply_layout(self) -> None:
        if self._scene is None:
            return
        overrides = None
        if self._preview:
            overrides = {
                path: dict(values)
                for path, values in self._runtime_overrides.items()
            }
            if self.playback is not None:
                for path, values in self.playback.overrides.items():
                    overrides.setdefault(path, {}).update(values)
        self._scene.update_preview(
            overrides,
            output_size=self._output_size,
            safe_area_ratio=self._safe_area_ratio,
            transient=self._transient,
        )
        self._image = None
        self.update()

    def _boundary(self, node: GuiSceneNode, reason: str) -> None:
        boundary = node.path, reason
        target = (
            self._frame_boundaries
            if self._frame_boundaries is not None
            else self._static_boundaries
        )
        if boundary in target:
            return
        target.add(boundary)
        if self._frame_boundaries is None:
            self._diagnostic_timer.start()

    def _emit_diagnostics(self) -> None:
        reasons: dict[str, int] = {}
        for _path, reason in self._static_boundaries | self._last_frame_boundaries:
            reasons[reason] = reasons.get(reason, 0) + 1
        parts = [
            f"{count}× {reason}"
            for reason, count in sorted(reasons.items())
        ]
        parts.extend(self._controller_context.errors)
        parts.extend(self._controller_context.diagnostics)
        list_path = self._controller_context.active_list_path
        if list_path is not None and self._scene is not None:
            target = self._scene.nodes_by_path.get(list_path)
            if target is None or not self._controller_context.accepts_list(target):
                behavior = self._controller_context.runtime_list_behavior
                target_type = (
                    behavior.list_type.rsplit(".", 1)[-1]
                    if behavior is not None
                    else "runtime"
                )
                parts.append(
                    f"selected preview {target_type} target is unresolved"
                )
        self.diagnostics_changed.emit("; ".join(parts))

    def _stage(self) -> tuple[QRectF, float]:
        width, height = self._scene.viewport_size if self._scene else (1920.0, 1080.0)
        available = QRectF(10.0, 10.0, max(1.0, self.width() - 20.0), max(1.0, self.height() - 20.0))
        scale = min(available.width() / width, available.height() / height) * self._zoom
        size = QPointF(width * scale, height * scale)
        origin = QPointF(
            available.center().x() - size.x() * 0.5,
            available.center().y() - size.y() * 0.5,
        ) + self._pan
        return QRectF(origin, origin + size), scale

    def paintEvent(self, _event) -> None:  # noqa: N802
        colors = current_colors()
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.fillRect(self.rect(), QColor(colors["canvas_bg"]))
        stage, scale = self._stage()
        painter.fillRect(stage, QColor(2, 4, 6))
        painter.setPen(QPen(QColor(colors["border_strong"]), 1.0))
        painter.drawRect(stage)
        if self._scene is None:
            return
        painter.save()
        painter.setClipRect(stage)
        painter.translate(stage.left(), stage.top())
        painter.scale(scale, scale)
        if self._preview:
            self._draw_preview(painter)
        else:
            self._draw_layout(painter)
        self._draw_guides(painter)
        painter.restore()
        self._draw_navigation_hint(painter, stage, colors)

    def _draw_navigation_hint(self, painter: QPainter, stage: QRectF, colors: dict) -> None:
        if self.height() - stage.bottom() < 18:
            return
        painter.setPen(QColor(colors["text_subtle"]))
        painter.drawText(
            QRectF(10.0, stage.bottom() + 2.0, max(1.0, self.width() - 20.0), 16.0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            self.tr("{zoom}% · Ctrl+Wheel to zoom · Middle-drag to pan").format(zoom=round(self._zoom * 100)),
        )

    def _draw_preview(self, painter: QPainter) -> None:
        if self._image is None:
            current: set[tuple[str, str]] = set()
            self._frame_boundaries = current
            try:
                self._register_scene_boundaries()
                loader, size = self._dependencies.resource_data_loader, tuple(
                    max(1, round(side)) for side in self._scene.display_size)
                try:
                    if self._device is None:
                        self._device = GuiDevice(Resident(True), mmtr_profile_for_game(self._game), self._profile,
                                                 lambda path: (found := loader(path)) and found[1], self._dependencies,
                                                 self._icon_context)
                        self._device.language = self._language
                    texture, left = self._device.frame(self._scene, size, self._render_branch_enabled)
                except (RuntimeError, ValueError, FileNotFoundError) as exc:
                    if self._device is not None and not isinstance(exc, FileNotFoundError):
                        raise
                    self._boundary(self._scene.root, str(exc))
                    self._image = QImage()
                    return
                pixels, = self._device.device.flush([texture])
                self._image = QImage(pixels, *size, size[0] * 4, QImage.Format.Format_RGBX8888).copy()
                current.update(left)
            finally:
                self._frame_boundaries = None
                if current != self._last_frame_boundaries:
                    self._last_frame_boundaries = current
                    self._diagnostic_timer.start()
        width, height = self._scene.viewport_size
        painter.drawImage(QRectF(0.0, 0.0, width, height), self._image)

    def _register_scene_boundaries(self) -> None:
        """Expose engine-owned render context instead of silently guessing it."""

        assert self._scene is not None
        root = self._scene.root
        view = root.render_properties
        if (
            self._profile.enum_label(view, "ViewType") != "Screen"
            or bool(view["UseGUICamera"])
        ):
            self._boundary(root, "external world/GUI camera matrices")
        if (
            not bool(view["Overlay"])
            or bool(view["DepthTest"])
            or bool(view["StencilZeroFill"])
        ):
            self._boundary(root, "native depth/stencil/compositor integration")
        if bool(view["Detonemap"]):
            self._boundary(root, "native detonemap render pass")
        if self._profile.enum_label(view, "ReprojectionType") != "Default":
            self._boundary(root, "native GUI reprojection pass")

        for node in self._scene.nodes:
            properties = node.render_properties
            if self._profile.enum_label(properties, self._profile.control_fields["segment"], "Segment") != "Keep":
                self._boundary(node, "native multi-segment render routing")
        self._register_interaction_boundaries()

    def _register_interaction_boundaries(self) -> None:
        if not self._interaction_enabled or self._scene is None:
            return
        if not self._icon_context.keyboard_mode:
            self._boundary(self._scene.root, "external gamepad input-event source")
            return
        bindings = self._icon_context.keyboard_bindings
        for owner in self._interactive_lists():
            actions = self._profile.list_input.get(
                self._profile.enum_label(owner.render_properties, "InputType", "InputListType"))
            if actions is not None and any(action not in bindings for action in actions):
                self._boundary(owner, "external keyboard bindings for SimpleList input")

    def _draw_layout(self, painter: QPainter) -> None:
        assert self._scene is not None
        outline = QColor(current_colors()["text_muted"])
        outline.setAlpha(130)
        outline_pen = QPen(outline, 0.0)
        for node in self._scene.draw_nodes:
            if node is self._scene.root:
                continue
            path = self._world_path(node)
            fill = self._color(node)
            fill.setAlpha(35 if node.effective_visible else 10)
            painter.fillPath(path, fill)
            painter.setPen(outline_pen)
            painter.drawPath(path)

    def _draw_guides(self, painter: QPainter) -> None:
        if self._scene is None:
            return
        colors = current_colors()
        selected_color = QColor(colors["accent"])
        guide_color = QColor(colors["text_muted"])
        guide_color.setAlpha(90)
        for node in self._scene.draw_nodes:
            if node is self._scene.root or (not self._guides and node is not self._selected):
                continue
            pen = QPen(
                selected_color if node is self._selected else guide_color,
                0.0,
            )
            if not node.effective_visible:
                pen.setStyle(Qt.PenStyle.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(self._world_path(node))

    def _bounds(self, node: GuiSceneNode) -> QRectF:
        left, top, right, bottom = bounds(node, node.size)
        return QRectF(left, top, right - left, bottom - top)

    def _local_path(self, node: GuiSceneNode) -> QPainterPath:
        path = QPainterPath()
        if node.object.type_name == "via.gui.TextureSet" and isinstance(
            node.object.special_data, GuiTextureSet
        ):
            scale = node.render_properties["Scale"]
            for entry in node.object.special_data.entries:
                left, top, right, bottom = entry.bounds
                path.addRect(
                    QRectF(
                        left * scale[0],
                        top * scale[1],
                        (right - left) * scale[0],
                        (bottom - top) * scale[1],
                    ).normalized()
                )
        elif node.object.type_name == "via.gui.HitArea":
            bounds = self._bounds(node)
            shape = self._profile.enum_label(node.render_properties, "Shape", "HitAreaShape")
            if shape == "Triangle":
                path.addPolygon(
                    QPolygonF(
                        [
                            bounds.bottomLeft(),
                            QPointF(bounds.center().x(), bounds.top()),
                            bounds.bottomRight(),
                        ]
                    )
                )
            elif shape in self._profile.runtime_type(node.object.type_name).facts["polygons"]:
                path.addPolygon(QPolygonF([QPointF(*point) for point in hit_polygon(node, node.size)]))
            else:
                path.addRect(bounds)
        elif node.object.type_name == "via.gui.Circle":
            return self._circle_path(node)
        elif self._is_control(node) or node.object.type_name in ("via.gui.Mesh", "via.gui.Effect"):
            # These classes have no file-authored local hit/draw rectangle.
            # An arbitrary editor-sized quad would change native hit routing.
            return path
        else:
            path.addRect(self._bounds(node))
        return path

    def _circle_path(
        self,
        node: GuiSceneNode,
        *,
        hit_test: bool = False,
    ) -> QPainterPath:
        path = QPainterPath()
        points = self._circle_points(node, hit_test=hit_test)
        if points is None:
            return path
        outer, inner = points
        path.addPolygon(QPolygonF([*outer, *reversed(inner)]))
        return path

    def _circle_points(
        self,
        node: GuiSceneNode,
        *,
        hit_test: bool,
    ) -> tuple[list[QPointF], list[QPointF]] | None:
        points = circle_points(node, node.size, hit_test=hit_test)
        if points is None:
            self._boundary(node, "invalid authored Circle arc/subdivision combination")
            return None
        return tuple([QPointF(*point) for point in ring] for ring in points)

    def _world_path(
        self,
        node: GuiSceneNode,
        *,
        hit_test: bool = False,
    ) -> QPainterPath:
        local = (
            self._circle_path(node, hit_test=True)
            if hit_test and node.object.type_name == "via.gui.Circle"
            else self._local_path(node)
        )
        return QTransform(*node.world_transform).map(local)

    def _native_contains(self, node: GuiSceneNode, point: QPointF) -> bool:
        """Apply the recovered leaf hit-test geometry and strict edge rule."""

        matrix = node.world_matrix
        if matrix is None:
            return False

        def project(value: QPointF) -> tuple[float, float, float]:
            return project_gui_point((value.x(), value.y(), 0.0), matrix)

        def quad_contains(rect: QRectF) -> bool:
            quad = tuple(
                project(value)
                for value in (
                    rect.topLeft(), rect.topRight(),
                    rect.bottomLeft(), rect.bottomRight(),
                )
            )
            if all(value[2] > 1.0 for value in quad):
                return False
            target = point.x(), point.y()
            return _strict_triangle_contains(target, quad[0], quad[2], quad[1]) or _strict_triangle_contains(
                target, quad[2], quad[1], quad[3]
            )

        rect = self._bounds(node)
        kind = node.object.type_name
        if kind == "via.gui.Circle":
            if not quad_contains(rect):
                return False
            points = self._circle_points(node, hit_test=True)
            if points is None:
                return False
            outer, inner = points
            target = point.x(), point.y()
            for first in range(len(outer) - 1):
                vertices = tuple(
                    project(value)
                    for value in (
                        outer[first], inner[first],
                        outer[first + 1], inner[first + 1],
                    )
                )
                if all(value[2] > 1.0 for value in vertices):
                    continue
                if _strict_triangle_contains(
                    target,
                    vertices[0],
                    vertices[1],
                    vertices[2],
                ) or _strict_triangle_contains(
                    target,
                    vertices[1],
                    vertices[2],
                    vertices[3],
                ):
                    return True
            return False

        if kind == "via.gui.HitArea":
            shape = self._profile.enum_label(node.render_properties, "Shape", "HitAreaShape")
            center = rect.center()
            target = point.x(), point.y()
            if shape == "Triangle":
                vertices = tuple(
                    project(value)
                    for value in (
                        QPointF(center.x(), rect.top()),
                        rect.bottomLeft(),
                        rect.bottomRight(),
                    )
                )
                return _strict_triangle_contains(target, *vertices)
            if not quad_contains(rect):
                return False
            if shape == "Rect":
                return True
            polygon = tuple(project(QPointF(*point)) for point in hit_polygon(node, node.size))
            world_center = project(center)
            return any(
                _strict_triangle_contains(
                    target,
                    polygon[index],
                    polygon[(index + 1) % len(polygon)],
                    world_center,
                )
                for index in range(len(polygon))
            )

        if "hit_layout_size" in self._profile.runtime_type(kind).facts:
            if not rect.isEmpty() and quad_contains(rect):
                return True
            try:
                if self._device is None:
                    raise TextLayoutGap("no device frame")
                shown = self._device.display_size(node)
            except TextLayoutGap as gap:
                self._boundary(node, f"Text hit test without its DisplaySize ({gap})")
                return False
            left, top, right, bottom = bounds(node, shown)
            return min(shown) > 0.0 and quad_contains(QRectF(left, top, right - left, bottom - top))

        if kind == "via.gui.TextureSet" and isinstance(
            node.object.special_data, GuiTextureSet
        ):
            scale = node.render_properties["Scale"]
            return any(
                quad_contains(
                    QRectF(
                        entry.bounds[0] * scale[0],
                        entry.bounds[1] * scale[1],
                        (entry.bounds[2] - entry.bounds[0]) * scale[0],
                        (entry.bounds[3] - entry.bounds[1]) * scale[1],
                    ).normalized()
                )
                for entry in node.object.special_data.entries
            )
        return not rect.isEmpty() and quad_contains(rect)

    def _color(
        self,
        node: GuiSceneNode,
        source: QColor | None = None,
        *,
        apply_saturation: bool = True,
    ) -> QColor:
        if source is None:
            source = _qcolor(
                node.render_properties.get("Color"),
                QColor(255, 255, 255, 255),
            )
        return QColor.fromRgb(
            *_cached_display_rgba(
                source.getRgb(),
                tuple(node.color_scale),
                tuple(node.color_offset),
                float(node.saturation),
                bool(apply_saturation),
                node.runtime.srgb_to_linear,
            )
        )

    def _initialize_interaction(self) -> None:
        self._interaction_ready = True
        if self._scene is None or self.playback is None:
            return
        self._list_items_by_owner.clear()
        for node in self._scene.nodes:
            if node.object.type_name in {"via.gui.ScrollList", "via.gui.ScrollGrid"}:
                self._boundary(
                    node,
                    "native recycled list/grid clones require external item data",
                )
        for node in self._scene.nodes:
            if node.object.type_name != "via.gui.SimpleList":
                continue
            self._list_items_by_owner[node.path] = tuple(
                child
                for child in node.children
                if child.object.type_name == "via.gui.SelectItem"
            )
            items = self._list_items(node)
            if not items:
                continue
            selected = int(node.render_properties["SelectedIndex"])
            self._list_selection[node.path] = selected
            for index, item in enumerate(items):
                self.playback.overrides.setdefault(item.path, {})["ListIndex"] = index
                self.playback.overrides[item.path]["Selected"] = index == selected
                # SelectItem constructor starts unselected. SimpleList.awake
                # invokes the reflected Selected setter for every item, which
                # only changes state for the selected one.
                if (
                    index == selected
                    and bool(node.render_properties["UseFocus"])
                ):
                    self._start_state(item, self._select_state(item, True))
        self._sync_timer()

    def _list_items(self, node: GuiSceneNode) -> list[GuiSceneNode]:
        cached = self._list_items_by_owner.get(node.path)
        if cached is None:
            cached = tuple(
                child
                for child in node.children
                if child.object.type_name == "via.gui.SelectItem"
            )
            self._list_items_by_owner[node.path] = cached
        return list(cached)

    @staticmethod
    def _select_state(node: GuiSceneNode, selected: bool) -> str:
        enabled = bool(node.render_properties["Enabled"])
        if enabled:
            return "FOCUS" if selected else "UNFOCUS"
        return "DISABLE_FOCUS" if selected else "DISABLE_UNFOCUS"

    def _start_state(self, node: GuiSceneNode, state: str) -> bool:
        if self.playback is None or not self.playback.play_state(node.path, state):
            return False
        self._interaction_frames = max(
            self._interaction_frames,
            self.playback.state_remaining(node.path),
        )
        self._sync_timer()
        return True

    def _set_list_selection(self, owner: GuiSceneNode, index: int) -> bool:
        if self.playback is None or not bool(owner.render_properties["UseFocus"]):
            return False
        items = self._list_items(owner)
        if not 0 <= index < len(items):
            return False
        target = items[index]
        if not bool(target.render_properties["CanSelect"]):
            return False
        previous = self._list_selection.get(owner.path, -1)
        if previous == index:
            self._active_list = owner
            return False
        if 0 <= previous < len(items):
            self._start_state(items[previous], self._select_state(items[previous], False))
            self.playback.overrides.setdefault(items[previous].path, {})["Selected"] = False
        self._start_state(target, self._select_state(target, True))
        self.playback.overrides.setdefault(target.path, {})["Selected"] = True
        self._list_selection[owner.path] = index
        self.playback.overrides.setdefault(owner.path, {})["SelectedIndex"] = index
        self._active_list = owner
        self._apply_layout()
        return True

    def _move_list_selection(self, owner: GuiSceneNode, step: int) -> bool:
        items = self._list_items(owner)
        if not items:
            return False
        previous = self._list_selection.get(owner.path, 0)
        candidate = previous
        for _ in range(len(items)):
            candidate += step
            if candidate < 0 or candidate >= len(items):
                if not bool(owner.render_properties["Loop"]):
                    break
                candidate %= len(items)
            if candidate == previous:
                break
            if bool(items[candidate].render_properties["CanSelect"]):
                moved = self._set_list_selection(owner, candidate)
                self._start_state(owner, "PLUS_INPUT" if step > 0 else "MINUS_INPUT")
                return moved
        self._start_state(owner, "PLUS_INPUT" if step > 0 else "MINUS_INPUT")
        return False

    def _hit_node(self, point: QPointF) -> GuiSceneNode | None:
        if self._scene is None:
            return None
        return next(
            (
                node for node in reversed(self._scene.draw_nodes)
                if node is not self._scene.root
                and node.effective_visible
                and self._world_path(node, hit_test=True).contains(point)
            ),
            None,
        )

    def _runtime_branch_enabled(
        self,
        node: GuiSceneNode,
        *,
        require_hit_visible: bool,
    ) -> bool:
        """Apply native local visibility, alpha, active, and optional hit gates."""

        current: GuiSceneNode | None = node
        while current is not None:
            properties = current.render_properties
            alpha = float(properties["ColorScale"][3]) if "ColorScale" in properties else 1.0
            if (
                not bool(properties["Visible"])
                or alpha < _FLOAT32_EPSILON
                or not self._controller_context.runtime_active(current)
                or not self._runtime_active_by_path.get(current.path, True)
                or (
                    require_hit_visible
                    and not bool(properties["HitVisible"])
                )
            ):
                return False
            current = current.parent
        return True

    def _render_branch_enabled(self, node: GuiSceneNode) -> bool:
        """Apply authored visibility, inherited alpha, and external Active state."""

        if not node.effective_visible or node.color_scale[3] < _FLOAT32_EPSILON:
            return False
        current: GuiSceneNode | None = node
        while current is not None:
            if (
                not self._controller_context.runtime_active(current)
                or not self._runtime_active_by_path.get(current.path, True)
            ):
                return False
            current = current.parent
        return True

    def _input_branch_enabled(self, node: GuiSceneNode) -> bool:
        return self._runtime_branch_enabled(node, require_hit_visible=True)

    def _input_hit_node(self, point: QPointF) -> GuiSceneNode | None:
        """Return the frontmost leaf admitted by native Control hit traversal."""

        if self._scene is None:
            return None
        return next(
            (
                node for node in reversed(self._scene.draw_nodes)
                if node is not self._scene.root
                and not self._is_control(node)
                and self._input_branch_enabled(node)
                and self._native_contains(node, point)
            ),
            None,
        )

    @staticmethod
    def _select_item_ancestor(node: GuiSceneNode | None) -> GuiSceneNode | None:
        while node is not None and node.object.type_name != "via.gui.SelectItem":
            node = node.parent
        return node

    @staticmethod
    def _list_owner(item: GuiSceneNode | None) -> GuiSceneNode | None:
        owner = item.parent if item is not None else None
        return (
            owner
            if owner is not None and owner.object.type_name == "via.gui.SimpleList"
            else None
        )

    def _is_control(self, node: GuiSceneNode) -> bool:
        return "via.gui.Control" in self._profile.runtime_type(node.object.type_name).classes

    def _list_interactive(self, owner: GuiSceneNode) -> bool:
        current: GuiSceneNode | None = owner
        while current is not None:
            if self._is_control(current):
                authored = bool(current.render_properties["Interactive"])
                if not self._controller_context.interactive(current, authored):
                    return False
            current = current.parent
        return True

    def _mouse_select(
        self,
        point: QPointF,
        required_mode: str,
    ) -> GuiSceneNode | None:
        item = self._select_item_ancestor(self._input_hit_node(point))
        owner = self._list_owner(item)
        if item is None or owner is None:
            return item
        self._active_list = owner
        mode = self._controller_context.mouse_select_type(
            owner, self._profile.enum_value(owner.render_properties, "MouseSelectType"))
        if (
            self._runtime_branch_enabled(owner, require_hit_visible=False)
            and self._list_interactive(owner)
            and mode == self._profile.enum("MouseSelectType")[required_mode]
        ):
            self._set_list_selection(owner, self._list_items(owner).index(item))
        return item

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._preview or not self._interaction_enabled or self._scene is None:
            return super().keyPressEvent(event)
        if not self._icon_context.keyboard_mode:
            return super().keyPressEvent(event)
        key = _event_virtual_key(event)
        if key == self._profile.icons["menu"]["DECIDE"][0]:
            handled = False
            for owner in self._interactive_lists():
                items = self._list_items(owner)
                selected = self._list_selection.get(owner.path, -1)
                if 0 <= selected < len(items):
                    handled |= self._start_state(items[selected], "DECIDE")
            if handled:
                self._apply_layout()
                event.accept()
                return

        bindings = self._icon_context.keyboard_bindings
        handled = False
        for input_type, actions in self._profile.list_input.items():
            step = (
                -1 if bindings.get(actions[0]) == key
                else 1 if bindings.get(actions[1]) == key
                else 0
            )
            if not step:
                continue
            for owner in self._interactive_lists(input_type):
                self._move_list_selection(owner, step)
                handled = True
        if handled:
            event.accept()
            return
        super().keyPressEvent(event)

    def _interactive_lists(self, input_type: str | None = None) -> list[GuiSceneNode]:
        """Lists registered for the same global input event in native code."""

        if self._scene is None:
            return []
        return [
            node
            for node in self._scene.nodes
            if node.object.type_name == "via.gui.SimpleList"
            and self._runtime_branch_enabled(node, require_hit_visible=False)
            and self._list_interactive(node)
            and (
                input_type is None
                or self._profile.enum_label(node.render_properties, "InputType", "InputListType") == input_type
            )
        ]

    def _virtual_point(self, position: QPointF) -> QPointF | None:
        stage, scale = self._stage()
        if scale <= 0.0 or not stage.contains(position):
            return None
        return QPointF((position.x() - stage.left()) / scale, (position.y() - stage.top()) / scale)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if event.button() != Qt.MouseButton.LeftButton or self._scene is None:
            return super().mousePressEvent(event)
        point = self._virtual_point(event.position())
        if point is None:
            return
        if self._preview and self._interaction_enabled:
            item = self._mouse_select(point, "LeftClick")
            self.setFocus(Qt.FocusReason.MouseFocusReason)
            event.accept()
            return
        node = self._hit_node(point)
        if node is None:
            return
        self.select_node(node)
        self.node_selected.emit(node)
        if self._scene.can_move(node):
            self._drag_node = node
            self._drag_start = point
            self._drag_origin = node.world_position
            self.setCursor(Qt.CursorShape.SizeAllCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan += delta
            self._pan_start = event.position()
            self.update()
            return
        if self._preview and self._interaction_enabled and self._scene is not None:
            point = self._virtual_point(event.position())
            item = None
            if point is not None:
                item = self._mouse_select(point, "MouseOver")
            owner = self._list_owner(item)
            clickable = (
                item is not None
                and owner is not None
                and bool(item.render_properties["CanSelect"])
                and self._list_interactive(owner)
                and self._controller_context.mouse_select_type(
                    owner, self._profile.enum_value(owner.render_properties, "MouseSelectType"),
                ) in {self._profile.enum("MouseSelectType")[mode] for mode in ("MouseOver", "LeftClick")}
            )
            self.setCursor(
                Qt.CursorShape.PointingHandCursor
                if clickable
                else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        if self._drag_node is None or self._scene is None:
            return super().mouseMoveEvent(event)
        point = self._virtual_point(event.position())
        if point is None:
            return
        desired = (
            self._drag_origin[0] + point.x() - self._drag_start.x(),
            self._drag_origin[1] + point.y() - self._drag_start.y(),
        )
        x, y = self._scene.local_position_for_scene_point(self._drag_node, *desired)
        self._transient = self._drag_node, x, y
        self._apply_layout()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.MiddleButton:
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return
        if event.button() == Qt.MouseButton.LeftButton and self._drag_node is not None:
            node = self._drag_node
            transient = self._transient
            self._drag_node = None
            self._transient = None
            self.setCursor(Qt.CursorShape.ArrowCursor)
            if transient is not None:
                self.node_moved.emit(node, transient[1], transient[2])
            self._apply_layout()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self._zoom = min(12.0, max(0.1, self._zoom * (1.15 if event.angleDelta().y() > 0 else 1 / 1.15)))
            self.update()
            event.accept()
            return
        # Mouse-wheel-to-list movement is not a DMC5 GUI behavior. A game
        # controller may map it to an input action, but GUIR alone does not.
        super().wheelEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        if self._interaction_enabled:
            self.setCursor(Qt.CursorShape.ArrowCursor)
        super().leaveEvent(event)


def _event_virtual_key(event) -> int:
    """Translate a Qt key event to the Windows virtual-key domain used by DMC5."""

    native = int(event.nativeVirtualKey())
    if native:
        return native
    key = event.key()
    translated = {
        Qt.Key.Key_Backspace: 8,
        Qt.Key.Key_Tab: 9,
        Qt.Key.Key_Return: 13,
        Qt.Key.Key_Enter: 146,
        Qt.Key.Key_Escape: 27,
        Qt.Key.Key_Space: 32,
        Qt.Key.Key_PageUp: 33,
        Qt.Key.Key_PageDown: 34,
        Qt.Key.Key_End: 35,
        Qt.Key.Key_Home: 36,
        Qt.Key.Key_Left: 37,
        Qt.Key.Key_Up: 38,
        Qt.Key.Key_Right: 39,
        Qt.Key.Key_Down: 40,
        Qt.Key.Key_Insert: 45,
        Qt.Key.Key_Delete: 46,
    }
    return translated.get(key, int(key) if 0 <= int(key) <= 0xFF else 0)


def _qcolor(value: object, fallback: QColor) -> QColor:
    if isinstance(value, (list, tuple)) and len(value) >= 4:
        try:
            return QColor(*(int(item) for item in value[:4]))
        except (TypeError, ValueError):
            pass
    return QColor(fallback)


def _strict_triangle_contains(
    point: tuple[float, float],
    first: tuple[float, float, float],
    second: tuple[float, float, float],
    third: tuple[float, float, float],
) -> bool:
    """DMC5 requires all three triangle edge signs to be strictly equal."""

    x, y = f32(point[0]), f32(point[1])

    def edge(start, end) -> float:
        return f32(f32(f32(y - end[1]) * f32(end[0] - start[0])) - f32(f32(x - end[0]) * f32(end[1] - start[1])))

    signs = edge(first, second), edge(second, third), edge(third, first)
    return all(value > 0.0 for value in signs) or all(value < 0.0 for value in signs)


@lru_cache(maxsize=16_384)
def _cached_display_rgba(
    rgba: tuple[int, int, int, int],
    scale: tuple[float, float, float, float],
    offset: tuple[float, float, float],
    saturation: float,
    apply_saturation: bool,
    srgb: tuple[float, float, float, float, float],
) -> tuple[int, int, int, int]:
    *rgb, alpha = vertex_color(rgba, scale, offset, srgb)
    channels = [value / 255.0 for value in rgb]
    if apply_saturation:
        luminance = 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]
        channels = [
            luminance + (value - luminance) * saturation
            for value in channels
        ]
    return (
        *(round(_linear_to_srgb(value) * 255.0) for value in channels),
        alpha,
    )


def _linear_to_srgb(value: float) -> float:
    value = min(1.0, max(0.0, float(value)))
    return value * 12.92 if value <= 0.0031308 else 1.055 * value ** (1.0 / 2.4) - 0.055
