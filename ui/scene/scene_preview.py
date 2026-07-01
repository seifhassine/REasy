from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass, field

import OpenGL
import numpy as np
for _flag in ("ERROR_CHECKING", "ERROR_LOGGING", "CONTEXT_CHECKING", "FULL_LOGGING"):
    setattr(OpenGL, _flag, False)

from OpenGL.arrays import vbo
from OpenGL.GL import (
    GL_AMBIENT,
    GL_BACK,
    GL_BLEND,
    GL_CCW,
    GL_COLOR_ARRAY,
    GL_COLOR_BUFFER_BIT,
    GL_COLOR_MATERIAL,
    GL_CULL_FACE,
    GL_DEPTH_BUFFER_BIT,
    GL_DEPTH_TEST,
    GL_DIFFUSE,
    GL_ARRAY_BUFFER,
    GL_ELEMENT_ARRAY_BUFFER,
    GL_FILL,
    GL_FLOAT,
    GL_FRONT_AND_BACK,
    GL_LIGHT0,
    GL_LIGHTING,
    GL_LINE,
    GL_LINES,
    GL_LINEAR,
    GL_MODELVIEW,
    GL_MODULATE,
    GL_NORMAL_ARRAY,
    GL_NORMALIZE,
    GL_ONE_MINUS_SRC_ALPHA,
    GL_POSITION,
    GL_PROJECTION,
    GL_RGBA,
    GL_SMOOTH,
    GL_SRC_ALPHA,
    GL_TEXTURE_2D,
    GL_TEXTURE_COORD_ARRAY,
    GL_TEXTURE_ENV,
    GL_TEXTURE_ENV_MODE,
    GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_MIN_FILTER,
    GL_TRIANGLES,
    GL_UNPACK_ALIGNMENT,
    GL_UNSIGNED_BYTE,
    GL_UNSIGNED_INT,
    GL_DYNAMIC_DRAW,
    GL_VERTEX_ARRAY,
    glBindTexture,
    glBlendFunc,
    glClear,
    glClearColor,
    glColor4f,
    glColorPointer,
    glCullFace,
    glDeleteTextures,
    glDepthMask,
    glDisable,
    glDisableClientState,
    glDrawElements,
    glEnable,
    glEnableClientState,
    glFrontFace,
    glGenTextures,
    glLightfv,
    glLineWidth,
    glLoadIdentity,
    glMatrixMode,
    glNormalPointer,
    glPixelStorei,
    glPolygonMode,
    glRotatef,
    glScalef,
    glShadeModel,
    glTexCoordPointer,
    glTexEnvi,
    glCompressedTexImage2D,
    glTexImage2D,
    glTexParameteri,
    glTranslatef,
    glVertexPointer,
    glViewport,
)
from OpenGL.GLU import gluPerspective
from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QCursor, QImage, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QSpinBox, QToolButton, QVBoxLayout, QWidget

from file_handlers.tex.qt_image_utils import TexPreviewUpload
from settings import save_settings
from ui.opengl_camera import OrbitCameraMixin
from .scene_buffers import (
    SceneBufferSet,
    build_scene_buffer_set,
    display_colors,
    scene_bounds,
    scene_index_buffers,
)
from .scene_model import SceneDrawMesh


@dataclass(slots=True)
class _GlBufferSet:
    data: SceneBufferSet
    vertices_vbo: object | None = None
    normals_vbo: object | None = None
    colors_vbo: object | None = None
    uvs_vbo: object | None = None
    indices_vbo: object | None = None
    line_indices_vbo: object | None = None
    batch_vbos: list[tuple[str, object, int]] = field(default_factory=list)
    index_count: int = 0
    line_count: int = 0
    lines_ready: bool = False


class ScenePreviewWidget(OrbitCameraMixin, QOpenGLWidget):
    SETTINGS_DEFAULTS = {
        "mesh_viewer_fps_limit": 60,
        "mesh_viewer_wireframe_mode": "off",
        "mesh_viewer_lighting_mode": "fixed",
        "mesh_viewer_line_width": 1.5,
        "mesh_viewer_ambient": 0.35,
        "mesh_viewer_diffuse": 0.65,
        "mesh_viewer_show_bones": False,
    }
    WIREFRAME_MODES = ("off", "polygon", "lines_depth", "lines_overlay")
    LIGHTING_MODES = ("off", "fixed", "software")
    FREECAM_MOD_KEYS = {Qt.Key_Shift, Qt.Key_Control}
    FREECAM_SCANCODES = {17: "forward", 31: "back", 30: "left", 32: "right", 16: "down", 18: "up"}
    FREECAM_TEXT_FALLBACK = {"w": "forward", "s": "back", "a": "left", "d": "right", "q": "down", "e": "up"}

    def __init__(
        self,
        parent=None,
        *,
        controls: str = "scene",
        settings: dict | None = None,
        initial_rotation: tuple[float, float] = (20.0, -30.0),
        initial_distance: float = 8.0,
        background: tuple[float, float, float, float] = (0.08, 0.08, 0.08, 1.0),
    ):
        fmt = QSurfaceFormat()
        fmt.setDepthBufferSize(24)
        fmt.setSwapInterval(0)
        fmt.setVersion(2, 1)
        fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        QSurfaceFormat.setDefaultFormat(fmt)
        super().__init__(parent)
        self.setFormat(fmt)

        self._settings = settings if isinstance(settings, dict) else None
        self._controls = controls
        self._background = background
        self._init_orbit_camera(
            rot_x=float(initial_rotation[0]),
            rot_y=float(initial_rotation[1]),
            distance=float(initial_distance),
        )
        self.scale = 1.0
        self.extent = 1.0
        self.center = np.zeros(3, dtype=np.float32)
        self.freecam_pos = np.zeros(3, dtype=np.float32)
        self.freecam_yaw = 0.0
        self.freecam_pitch = -10.0
        self.freecam_speed = 1.0
        self._freecam_keys: set[str] = set()
        self._freecam_mods: set[int] = set()
        self._last_freecam_time = time.perf_counter()
        self._cursor_lock_pos = None
        self._drag_overlay = self._drag_offset = self._resize_overlay = self._fullscreen_restore = None

        self._meshes: list[SceneDrawMesh] = []
        self._highlighted_keys: set[str] = set()
        self._regular_set: _GlBufferSet | None = None
        self._solid_set: _GlBufferSet | None = None
        self._regular_data: SceneBufferSet | None = None
        self._solid_data: SceneBufferSet | None = None
        self._texture_ids: dict[str, int] = {}
        self._texture_sources: dict[str, str] = {}
        self._texture_source_ids: dict[str, int] = {}
        self._pending_material_images: dict[str, tuple[str, TexPreviewUpload]] = {}
        self._hidden_keys: set[str] = set()
        self._gl_cleanup_context = None

        self.render_mode = "wire"
        self.show_only_highlighted = False
        self._fps_limit = self._setting_int("mesh_viewer_fps_limit", 60, 0, 240)
        self.wireframe_mode = self._setting_choice("mesh_viewer_wireframe_mode", "off", self.WIREFRAME_MODES)
        self.lighting_mode = self._setting_choice("mesh_viewer_lighting_mode", "fixed", self.LIGHTING_MODES)
        self.line_width = self._setting_float("mesh_viewer_line_width", 1.5, 0.5, 8.0)
        self.color_source = "vertex"
        self.ambient = self._setting_float("mesh_viewer_ambient", 0.35, 0.0, 1.0)
        self.diffuse = self._setting_float("mesh_viewer_diffuse", 0.65, 0.0, 1.0)
        self.show_bone_labels = self._setting_bool("mesh_viewer_show_bones", False)
        self._colors_dirty = True

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self.update)

        self.setFocusPolicy(Qt.StrongFocus)
        self._build_overlay()
        self._update_timer_state()

    def _build_overlay(self):
        self.overlay = QFrame(self)
        self.overlay.setObjectName("viewportHud")
        self.overlay.setStyleSheet("""
            QFrame#viewportHud { background:rgba(11,15,20,190); border:1px solid rgba(92,110,128,170); border-radius:6px; color:#dce3ea; }
            QWidget#viewportHudBody { background-color:transparent; border:0; }
            QLabel { color:#dce3ea; background-color:transparent; font-size:11px; }
            QLabel#overlayResizeGrip { color:#5d6f80; background-color:transparent; font-size:10px; }
            QToolButton { background-color:transparent; border:1px solid #33414e; color:#dce3ea; border-radius:3px; padding:0px; }
            QToolButton:hover { background:#253342; border-color:#4eb4a6; }
            QComboBox, QSpinBox, QDoubleSpinBox { background:#151b22; border:1px solid #33414e; color:#e8eef5; padding:2px 4px; min-height:18px; }
            QCheckBox { color:#dce3ea; background-color:transparent; font-size:11px; }
        """)
        layout = QVBoxLayout(self.overlay)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        header = QHBoxLayout()
        self.fps_label = QLabel("0 FPS", self.overlay)
        self.overlay_fold_button = self._overlay_button("v", "Fold panel")
        self.fullscreen_button = self._overlay_button("⛶", "Fullscreen viewport", lambda: self._leave_view_fullscreen() if self._fullscreen_restore else self._enter_view_fullscreen())
        header.addWidget(self.fps_label, 1)
        header.addWidget(self.fullscreen_button)
        header.addWidget(self.overlay_fold_button)
        layout.addLayout(header)
        self.overlay_body = QWidget(self.overlay)
        self.overlay_body.setObjectName("viewportHudBody")
        self.overlay_body.setAttribute(Qt.WA_StyledBackground, True)
        body_layout = QVBoxLayout(self.overlay_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(4)
        layout.addWidget(self.overlay_body)

        if self._controls == "mesh":
            self._build_mesh_controls(body_layout)
        else:
            self._build_scene_controls(body_layout)

        self.setup_viewport_overlay(self.overlay, self.overlay_body, self.overlay_fold_button)
        self.overlay.adjustSize()
        self.place_viewport_overlays()

    def _overlay_button(self, text: str, tip: str, slot=None) -> QToolButton:
        button = QToolButton(self.overlay)
        button.setText(text)
        button.setToolTip(tip)
        button.setFixedSize(18, 18)
        if slot:
            button.clicked.connect(slot)
        return button

    def setup_viewport_overlay(self, widget: QWidget, body: QWidget | None = None, fold_button: QToolButton | None = None):
        widget._viewport_body = body
        if fold_button is not None:
            widget._viewport_fold_button = fold_button
            fold_button.clicked.connect(lambda: self._toggle_overlay_fold(widget))
        grip = QLabel("///", widget)
        grip.setObjectName("overlayResizeGrip")
        grip.setAlignment(Qt.AlignRight)
        grip.setCursor(Qt.SizeFDiagCursor)
        grip.setFixedHeight(10)
        grip._viewport_resize_overlay = widget
        if widget.layout() is not None:
            widget.layout().addWidget(grip)
        for child in (widget, *widget.findChildren(QWidget)):
            child._viewport_drag_overlay = widget
            child.installEventFilter(self)

    def _enter_view_fullscreen(self):
        if self._fullscreen_restore is not None:
            return
        parent = self.parentWidget()
        parent_layout = parent.layout() if parent is not None else None
        index = parent_layout.indexOf(self) if parent_layout is not None else -1
        if index >= 0:
            parent_layout.takeAt(index)
        elif parent is not None and hasattr(parent, "indexOf"):
            index = parent.indexOf(self)
        self._fullscreen_restore = (parent, parent_layout, index)
        self.setParent(None, Qt.Window)
        self.setWindowTitle("Scene Preview")
        self.fullscreen_button.setText("x")
        self.showFullScreen()
        self.setFocus(Qt.OtherFocusReason)
        QTimer.singleShot(0, self.place_viewport_overlays)

    def _leave_view_fullscreen(self):
        if self._fullscreen_restore is None:
            return
        parent, layout, index = self._fullscreen_restore or (None, None, -1)
        self._fullscreen_restore = None
        self.showNormal()
        if parent is not None and layout is not None and hasattr(layout, "insertWidget"):
            layout.insertWidget(index if index >= 0 else layout.count(), self, 1)
        elif parent is not None and hasattr(parent, "insertWidget"):
            parent.insertWidget(index if index >= 0 else parent.count(), self)
        self.fullscreen_button.setText("⛶")
        self.show()
        QTimer.singleShot(0, self.place_viewport_overlays)

    def eventFilter(self, obj, event):
        overlay = getattr(obj, "_viewport_drag_overlay", None)
        if not overlay:
            return super().eventFilter(obj, event)
        kind = event.type()
        active = "resize" if self._resize_overlay is overlay else "drag" if self._drag_overlay is overlay else ""
        if active and kind == QEvent.Type.MouseMove and event.buttons() & Qt.LeftButton:
            if active == "resize":
                self._resize_overlay_to(overlay, event.globalPosition().toPoint())
            else:
                self._move_overlay(overlay, self.mapFromGlobal(event.globalPosition().toPoint()) - self._drag_offset)
            return True
        if active and kind == QEvent.Type.MouseButtonRelease:
            overlay.releaseMouse()
            self._resize_overlay = None
            self._drag_overlay = self._drag_offset = None
            return True
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.LeftButton:
            if getattr(obj, "_viewport_resize_overlay", None) is overlay:
                self._resize_overlay = overlay
            elif self._can_drag_overlay_from(obj):
                self._drag_overlay = overlay
                self._drag_offset = overlay.mapFromGlobal(event.globalPosition().toPoint())
            else:
                return super().eventFilter(obj, event)
            overlay.viewport_anchor = "manual"
            overlay.raise_()
            overlay.grabMouse()
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _can_drag_overlay_from(widget) -> bool:
        blocked = ("QAbstractButton", "QAbstractSpinBox", "QComboBox", "QAbstractItemView", "QTextEdit", "QScrollBar")
        while widget is not None:
            if any(widget.inherits(name) for name in blocked):
                return False
            if getattr(widget, "_viewport_drag_overlay", None) is widget:
                return True
            widget = widget.parentWidget()
        return True

    def _move_overlay(self, overlay: QWidget, pos):
        margin = 4
        overlay.viewport_anchor = "manual"
        overlay.move(
            max(margin, min(pos.x(), max(margin, self.width() - overlay.width() - margin))),
            max(margin, min(pos.y(), max(margin, self.height() - overlay.height() - margin))),
        )

    def _resize_overlay_to(self, overlay: QWidget, global_pos):
        margin = 4
        local = overlay.mapFromGlobal(global_pos)
        max_w = min(overlay.maximumWidth(), self.width() - overlay.x() - margin)
        max_h = min(overlay.maximumHeight(), self.height() - overlay.y() - margin)
        overlay.resize(
            max(overlay.minimumWidth(), min(local.x(), max_w)),
            max(overlay.minimumHeight(), min(local.y(), max_h)),
        )

    def _toggle_overlay_fold(self, overlay: QWidget):
        body = getattr(overlay, "_viewport_body", None)
        if body is None:
            return
        body.setVisible(not body.isVisible())
        button = getattr(overlay, "_viewport_fold_button", None)
        if button is not None:
            button.setText(">" if not body.isVisible() else "v")
        if body.isVisible():
            overlay.resize(max(overlay.width(), overlay.sizeHint().width()), max(overlay.height(), overlay.sizeHint().height()))
        else:
            overlay.adjustSize()
        self.place_viewport_overlays()

    def _build_scene_controls(self, layout: QVBoxLayout):
        row = QHBoxLayout()
        row.addWidget(QLabel("Mode", self.overlay))
        self.scene_mode_combo = QComboBox(self.overlay)
        self.scene_mode_combo.addItem("Wireframe", "wire")
        self.scene_mode_combo.addItem("Solid + Wire", "hybrid")
        self.scene_mode_combo.addItem("Solid", "solid")
        self.scene_mode_combo.currentIndexChanged.connect(lambda _: self._set_render_mode(self.scene_mode_combo.currentData()))
        row.addWidget(self.scene_mode_combo)
        layout.addLayout(row)

        self._add_fps_limit_control(layout)

        self.highlight_only_check = QCheckBox("View only highlighted", self.overlay)
        self.highlight_only_check.toggled.connect(self._set_show_only_highlighted)
        layout.addWidget(self.highlight_only_check)

    def _add_fps_limit_control(self, layout: QVBoxLayout):
        limit_layout = QHBoxLayout()
        limit_layout.addWidget(QLabel("Limit", self.overlay))
        fps_spin = QSpinBox(self.overlay)
        fps_spin.setRange(0, 240)
        fps_spin.setFixedWidth(50)
        fps_spin.setValue(self._fps_limit)
        fps_spin.valueChanged.connect(self._change_fps_limit)
        limit_layout.addWidget(fps_spin)
        layout.addLayout(limit_layout)

    def _build_mesh_controls(self, layout: QVBoxLayout):
        self._add_fps_limit_control(layout)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("WF Mode", self.overlay))
        self.wf_combo = QComboBox(self.overlay)
        self.wf_combo.addItems(["off", "polygon", "lines_depth", "lines_overlay"])
        self.wf_combo.setCurrentText(self.wireframe_mode)
        self.wf_combo.currentTextChanged.connect(self._set_wireframe_mode)
        row1.addWidget(self.wf_combo)
        row1.addWidget(QLabel("Line", self.overlay))
        self.line_spin = QDoubleSpinBox(self.overlay)
        self.line_spin.setRange(0.5, 8.0)
        self.line_spin.setSingleStep(0.1)
        self.line_spin.setValue(self.line_width)
        self.line_spin.valueChanged.connect(self._set_line_width)
        row1.addWidget(self.line_spin)
        layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Light", self.overlay))
        self.light_combo = QComboBox(self.overlay)
        self.light_combo.addItems(["off", "fixed", "software"])
        self.light_combo.setCurrentText(self.lighting_mode)
        self.light_combo.currentTextChanged.connect(self._set_lighting_mode)
        row2.addWidget(self.light_combo)
        mesh = getattr(self, "mesh", None)
        if getattr(mesh, "streaming_buffer_count", 0):
            stream_status = "Loaded" if getattr(mesh, "streaming_data_loaded", False) else "Missing"
            row2.addWidget(QLabel(f"Stream {stream_status}", self.overlay))
        row2.addWidget(QLabel("Amb", self.overlay))
        self.amb_spin = QDoubleSpinBox(self.overlay)
        self.amb_spin.setRange(0.0, 1.0)
        self.amb_spin.setSingleStep(0.05)
        self.amb_spin.setValue(self.ambient)
        self.amb_spin.valueChanged.connect(self._set_ambient)
        row2.addWidget(self.amb_spin)
        row2.addWidget(QLabel("Diff", self.overlay))
        self.diff_spin = QDoubleSpinBox(self.overlay)
        self.diff_spin.setRange(0.0, 1.0)
        self.diff_spin.setSingleStep(0.05)
        self.diff_spin.setValue(self.diffuse)
        self.diff_spin.valueChanged.connect(self._set_diffuse)
        row2.addWidget(self.diff_spin)
        layout.addLayout(row2)

        row3 = QHBoxLayout()
        self.bone_labels_check = QCheckBox("Bones", self.overlay)
        self.bone_labels_check.setChecked(self.show_bone_labels)
        self.bone_labels_check.toggled.connect(self._set_show_bone_labels)
        row3.addWidget(self.bone_labels_check)
        layout.addLayout(row3)

    def _setting_value(self, key: str):
        if self._settings is None:
            return self.SETTINGS_DEFAULTS[key]
        return self._settings.get(key, self.SETTINGS_DEFAULTS[key])

    def _setting_bool(self, key: str, default: bool) -> bool:
        return bool(self._setting_value(key)) if key in self.SETTINGS_DEFAULTS else default

    def _setting_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self._setting_value(key))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _setting_float(self, key: str, default: float, minimum: float, maximum: float) -> float:
        try:
            value = float(self._setting_value(key))
        except (TypeError, ValueError):
            value = default
        return max(minimum, min(maximum, value))

    def _setting_choice(self, key: str, default: str, choices: tuple[str, ...]) -> str:
        value = str(self._setting_value(key))
        return value if value in choices else default

    def _save_view_setting(self, key: str, value):
        if self._settings is None:
            return
        self._settings[key] = value
        save_settings(self._settings)

    def set_scene(self, meshes: list[SceneDrawMesh], highlighted_keys: set[str] | None = None, *, reset_camera: bool = True):
        self._meshes = meshes
        self._highlighted_keys = set(highlighted_keys or set())
        self._recompute_bounds()
        if reset_camera:
            self._reset_freecam()
        self._upload_buffers()
        self.update()

    def set_hidden_keys(self, keys: set[str], *, refresh: bool = True):
        self._hidden_keys = set(keys)
        if not refresh:
            return
        if self.context() is None:
            self.update()
            return
        self.makeCurrent()
        self._upload_index_vbos(self._regular_set)
        self._upload_index_vbos(self._solid_set)
        self.doneCurrent()
        self.update()

    def set_material_images(self, images: dict[str, tuple[str, TexPreviewUpload]]):
        self._pending_material_images = dict(images)
        self._sync_material_images()

    def update_material_images(self, images: dict[str, tuple[str, TexPreviewUpload]]):
        self._pending_material_images.update(images)
        self._sync_material_images(images)

    def _sync_material_images(self, names=None):
        if self.context() is None:
            self.update()
            return
        self.makeCurrent()
        self._sync_gl_textures(names)
        self.doneCurrent()
        self.update()

    def _set_render_mode(self, mode: str):
        self.render_mode = str(mode or "wire")
        self.update()

    def set_render_mode(self, mode: str):
        mode = str(mode or "wire")
        combo = getattr(self, "scene_mode_combo", None)
        if combo is not None:
            index = combo.findData(mode)
            if index >= 0:
                combo.setCurrentIndex(index)
                return
        self._set_render_mode(mode)

    def mousePressEvent(self, event):
        self.setFocus(Qt.MouseFocusReason)
        if self._controls == "mesh" or event.button() not in (Qt.LeftButton, Qt.RightButton):
            return super().mousePressEvent(event)
        self._lock_scene_cursor(event.globalPosition().toPoint())
        event.accept()

    def mouseMoveEvent(self, event):
        if self._controls == "mesh":
            return super().mouseMoveEvent(event)
        buttons = event.buttons()
        if not (buttons & (Qt.LeftButton | Qt.RightButton)):
            self._unlock_scene_cursor()
            return event.accept()
        if self._cursor_lock_pos is None:
            self._lock_scene_cursor(event.globalPosition().toPoint())
            return event.accept()
        pos = event.globalPosition().toPoint()
        dx = pos.x() - self._cursor_lock_pos.x()
        dy = pos.y() - self._cursor_lock_pos.y()
        if dx or dy:
            QCursor.setPos(self._cursor_lock_pos)
            self._move_scene_camera(dx, dy, buttons)
            self._update_after_camera_change()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._controls == "mesh" or event.buttons() & (Qt.LeftButton | Qt.RightButton):
            return super().mouseReleaseEvent(event)
        self._unlock_scene_cursor()
        event.accept()

    def wheelEvent(self, event):
        if self._controls == "mesh":
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120.0
        self._move_freecam_local(np.array((0.0, 0.0, -steps * self.freecam_speed * 0.08), dtype=np.float32))
        self._update_after_camera_change()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_F11 or (self._fullscreen_restore and event.key() == Qt.Key_Escape):
            self._leave_view_fullscreen() if self._fullscreen_restore else self._enter_view_fullscreen()
            event.accept()
            return
        action = self._freecam_key_action(event)
        if self._controls != "mesh" and action:
            self._freecam_keys.add(action)
            event.accept()
            self._update_after_camera_change()
            return
        if self._controls != "mesh" and event.key() in self.FREECAM_MOD_KEYS:
            self._freecam_mods.add(event.key())
            event.accept()
            return
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        action = self._freecam_key_action(event)
        if self._controls != "mesh" and action:
            if not event.isAutoRepeat():
                self._freecam_keys.discard(action)
            event.accept()
            return
        if self._controls != "mesh" and event.key() in self.FREECAM_MOD_KEYS:
            if not event.isAutoRepeat():
                self._freecam_mods.discard(event.key())
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._unlock_scene_cursor()
        self._freecam_keys.clear()
        self._freecam_mods.clear()
        super().focusOutEvent(event)

    def _move_scene_camera(self, dx: float, dy: float, buttons) -> None:
        pan = self.freecam_speed * 0.004
        if buttons == Qt.RightButton:
            self.freecam_yaw -= dx * 0.18
            self.freecam_pitch = max(-80.0, min(80.0, self.freecam_pitch - dy * 0.18))
        elif buttons == Qt.LeftButton:
            self._move_freecam_local(np.array((dx, 0.0, -dy), dtype=np.float32) * pan)
        elif (buttons & Qt.LeftButton) and (buttons & Qt.RightButton):
            self._move_freecam_local(np.array((dx, -dy, 0.0), dtype=np.float32) * pan)

    def _lock_scene_cursor(self, pos) -> None:
        self._cursor_lock_pos = pos
        self.setCursor(Qt.BlankCursor)
        self.grabMouse()

    def _unlock_scene_cursor(self) -> None:
        if self._cursor_lock_pos is None:
            return
        self.releaseMouse()
        self.unsetCursor()
        self._cursor_lock_pos = None

    def _set_show_only_highlighted(self, enabled: bool):
        self.show_only_highlighted = bool(enabled)
        self._upload_buffers()
        self.update()

    def _recompute_bounds(self):
        self.center, self.extent = scene_bounds(self._meshes)
        self.scale = 1.0 / self.extent if self.extent > 1e-6 else 1.0

    def _reset_freecam(self) -> None:
        self.freecam_yaw = 0.0
        self.freecam_pitch = -10.0
        self.freecam_speed = max(self.extent * 0.6, 1.0)
        self.freecam_pos = self.center + np.array((0.0, self.extent * 0.2, self.extent * 1.4), dtype=np.float32)
        self._last_freecam_time = time.perf_counter()

    def _freecam_rotation(self) -> np.ndarray:
        yaw = np.deg2rad(self.freecam_yaw)
        pitch = np.deg2rad(self.freecam_pitch)
        cy, sy = np.cos(yaw), np.sin(yaw)
        cp, sp = np.cos(pitch), np.sin(pitch)
        return (
            np.array(((cy, 0.0, sy), (0.0, 1.0, 0.0), (-sy, 0.0, cy)), dtype=np.float32)
            @ np.array(((1.0, 0.0, 0.0), (0.0, cp, -sp), (0.0, sp, cp)), dtype=np.float32)
        )

    def _move_freecam_local(self, local_delta: np.ndarray) -> None:
        self.freecam_pos += self._freecam_rotation() @ local_delta

    def _freecam_key_action(self, event) -> str:
        try:
            scan_code = int(event.nativeScanCode())
        except Exception:
            scan_code = 0
        if scan_code:
            return self.FREECAM_SCANCODES.get(scan_code, "")
        return self.FREECAM_TEXT_FALLBACK.get((event.text() or "").lower(), "")

    def _freecam_move_vector(self) -> np.ndarray:
        move = np.zeros(3, dtype=np.float32)
        if "forward" in self._freecam_keys:
            move[2] -= 1.0
        if "back" in self._freecam_keys:
            move[2] += 1.0
        if "right" in self._freecam_keys:
            move[0] += 1.0
        if "left" in self._freecam_keys:
            move[0] -= 1.0
        if "up" in self._freecam_keys:
            move[1] += 1.0
        if "down" in self._freecam_keys:
            move[1] -= 1.0
        length = float(np.linalg.norm(move))
        return move / length if length > 1e-6 else move

    def _step_freecam(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._last_freecam_time, 0.05)
        self._last_freecam_time = now
        if self._controls == "mesh" or not self._freecam_keys:
            return
        move = self._freecam_move_vector()
        if np.any(move):
            speed = self.freecam_speed * (3.0 if Qt.Key_Shift in self._freecam_mods else 1.0)
            speed *= 0.25 if Qt.Key_Control in self._freecam_mods else 1.0
            self._move_freecam_local(move * speed * dt)

    def _build_buffer_set(self, *, force_solid: bool) -> SceneBufferSet | None:
        return build_scene_buffer_set(
            self._meshes,
            self._highlighted_keys,
            show_only_highlighted=self.show_only_highlighted,
            force_solid=force_solid,
        )

    def _rebuild_buffer_data(self):
        self._regular_data = self._build_buffer_set(force_solid=False)
        self._solid_data = self._build_buffer_set(force_solid=True)

    @staticmethod
    def _dispose_vbo(handle) -> None:
        if handle is None:
            return
        with suppress(Exception):
            handle.delete()
        buffers = getattr(handle, "buffers", None)
        if hasattr(buffers, "clear"):
            buffers.clear()

    def _delete_buffer_set(self, buffer_set: _GlBufferSet | None):
        if buffer_set is None:
            return
        for name in ("vertices_vbo", "normals_vbo", "colors_vbo", "uvs_vbo"):
            self._dispose_vbo(getattr(buffer_set, name))
            setattr(buffer_set, name, None)
        self._delete_index_vbos(buffer_set)

    def _delete_index_vbos(self, buffer_set: _GlBufferSet | None):
        if buffer_set is None:
            return
        for handle in (buffer_set.indices_vbo, buffer_set.line_indices_vbo):
            self._dispose_vbo(handle)
        for _, batch_vbo, _ in buffer_set.batch_vbos:
            self._dispose_vbo(batch_vbo)
        buffer_set.indices_vbo = None
        buffer_set.line_indices_vbo = None
        buffer_set.batch_vbos.clear()
        buffer_set.index_count = 0
        buffer_set.line_count = 0
        buffer_set.lines_ready = False

    def _display_colors(self, buffer_set: SceneBufferSet) -> np.ndarray | None:
        return display_colors(
            buffer_set,
            color_source=self.color_source,
            lighting_mode=self.lighting_mode,
            ambient=self.ambient,
            diffuse=self.diffuse,
        )

    def _upload_buffer_set(self, data: SceneBufferSet | None) -> _GlBufferSet | None:
        if data is None:
            return None
        buffer_set = _GlBufferSet(data=data)
        buffer_set.vertices_vbo = self._array_vbo(data.vertices) if len(data.vertices) else None
        buffer_set.normals_vbo = self._array_vbo(data.normals) if data.normals is not None else None
        colors = self._display_colors(data)
        buffer_set.colors_vbo = self._array_vbo(colors) if colors is not None else None
        buffer_set.uvs_vbo = self._array_vbo(data.uvs) if data.uvs is not None else None
        self._upload_index_vbos(buffer_set)
        return buffer_set

    def _upload_index_vbos(self, buffer_set: _GlBufferSet | None):
        if buffer_set is None:
            return
        self._delete_index_vbos(buffer_set)
        need_lines = self._needs_line_indices()
        indices, batches, line_indices = scene_index_buffers(buffer_set.data, self._hidden_keys, include_lines=need_lines)
        buffer_set.index_count = len(indices)
        buffer_set.line_count = len(line_indices)
        buffer_set.lines_ready = need_lines
        buffer_set.indices_vbo = self._element_vbo(indices) if len(indices) else None
        buffer_set.line_indices_vbo = self._element_vbo(line_indices) if len(line_indices) else None
        buffer_set.batch_vbos = [
            (material_name, self._element_vbo(indices), len(indices))
            for material_name, indices in sorted(batches, key=lambda item: item[0])
            if len(indices)
        ]

    def _needs_line_indices(self) -> bool:
        return self.render_mode in {"wire", "hybrid"} or (
            self._controls == "mesh" and self.wireframe_mode in {"lines_depth", "lines_overlay"}
        )

    def _ensure_line_indices(self, buffer_set: _GlBufferSet | None):
        if buffer_set is None or buffer_set.lines_ready:
            return
        _indices, _batches, line_indices = scene_index_buffers(buffer_set.data, self._hidden_keys, include_lines=True)
        buffer_set.line_count = len(line_indices)
        buffer_set.line_indices_vbo = self._element_vbo(line_indices) if len(line_indices) else None
        buffer_set.lines_ready = True

    @staticmethod
    def _array_vbo(data: np.ndarray):
        return vbo.VBO(data, usage=GL_DYNAMIC_DRAW, target=GL_ARRAY_BUFFER)

    @staticmethod
    def _element_vbo(data: np.ndarray):
        return vbo.VBO(data, usage=GL_DYNAMIC_DRAW, target=GL_ELEMENT_ARRAY_BUFFER)

    def _refresh_color_vbos(self):
        for buffer_set in (self._regular_set, self._solid_set):
            if buffer_set is None:
                continue
            if buffer_set.colors_vbo is not None:
                self._dispose_vbo(buffer_set.colors_vbo)
            colors = self._display_colors(buffer_set.data)
            buffer_set.colors_vbo = self._array_vbo(colors) if colors is not None else None
        self._colors_dirty = False

    def _upload_buffers(self, *, rebuild: bool = True):
        if rebuild:
            self._rebuild_buffer_data()
        if self.context() is None:
            return
        self.makeCurrent()
        self._delete_buffer_set(self._regular_set)
        self._delete_buffer_set(self._solid_set)
        self._regular_set = self._upload_buffer_set(self._regular_data)
        self._solid_set = self._upload_buffer_set(self._solid_data)
        self._colors_dirty = False
        self.doneCurrent()

    def _clear_gl_textures(self):
        if self._texture_source_ids:
            with suppress(Exception):
                glDeleteTextures(list(self._texture_source_ids.values()))
            self._texture_source_ids.clear()
        self._texture_sources.clear()
        self._texture_ids.clear()

    def _sync_gl_textures(self, names=None):
        if names is None:
            for name in set(self._texture_sources) - set(self._pending_material_images):
                self._texture_ids.pop(name, None)
                self._texture_sources.pop(name, None)
            self._delete_unused_source_textures()
            names = self._pending_material_images
        for name in names:
            source_path, texture = self._pending_material_images[name]
            if self._texture_sources.get(name) == source_path and source_path in self._texture_source_ids:
                self._texture_ids[name] = self._texture_source_ids[source_path]
                continue
            old_source = self._texture_sources.pop(name, None)
            self._texture_ids.pop(name, None)
            if old_source and old_source not in self._texture_sources.values():
                old_id = self._texture_source_ids.pop(old_source, None)
                if old_id:
                    glDeleteTextures([old_id])
            texture_id = self._texture_source_ids.get(source_path)
            if texture_id is None:
                texture_id = glGenTextures(1)
                self._texture_source_ids[source_path] = texture_id
                self._upload_texture(texture_id, texture)
            self._texture_ids[name] = texture_id
            self._texture_sources[name] = source_path

    def _delete_unused_source_textures(self):
        active_sources = set(self._texture_sources.values())
        for source_path, texture_id in list(self._texture_source_ids.items()):
            if source_path not in active_sources:
                glDeleteTextures([texture_id])
                self._texture_source_ids.pop(source_path, None)

    @staticmethod
    def _upload_texture(texture_id: int, texture: TexPreviewUpload):
        glBindTexture(GL_TEXTURE_2D, texture_id)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glCompressedTexImage2D(GL_TEXTURE_2D, 0, texture.gl_format, texture.width, texture.height, 0, texture.data)
        glBindTexture(GL_TEXTURE_2D, 0)

    @staticmethod
    def _upload_qimage_texture(texture_id: int, image: QImage):
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        bits = rgba.bits()
        size = rgba.sizeInBytes()
        glBindTexture(GL_TEXTURE_2D, texture_id)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexImage2D(
            GL_TEXTURE_2D,
            0,
            GL_RGBA,
            rgba.width(),
            rgba.height(),
            0,
            GL_RGBA,
            GL_UNSIGNED_BYTE,
            bytes(bits[:size]),
        )
        glBindTexture(GL_TEXTURE_2D, 0)

    def _cleanup_gl(self):
        made_current = False
        with suppress(Exception):
            if self.context() is not None:
                self.makeCurrent()
                made_current = True
                self._clear_gl_textures()
        self._delete_buffer_set(self._regular_set)
        self._delete_buffer_set(self._solid_set)
        with suppress(Exception):
            self._cleanup_extra_gl()
        if made_current:
            with suppress(Exception):
                self.doneCurrent()
        self._regular_set = self._solid_set = self._gl_cleanup_context = None

    def cleanup(self):
        self._leave_view_fullscreen()
        self._cleanup_gl()

    def closeEvent(self, event):
        if self._fullscreen_restore is not None:
            self._leave_view_fullscreen()
            event.ignore()
            return
        self._cleanup_gl()
        super().closeEvent(event)

    def _cleanup_extra_gl(self):
        pass

    def _after_gl_initialized(self):
        pass

    def _after_scene_draw(self):
        pass

    def initializeGL(self):
        glClearColor(*self._background)
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_LIGHT0)
        glEnable(GL_COLOR_MATERIAL)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glFrontFace(GL_CCW)
        glEnable(GL_NORMALIZE)
        glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_MODULATE)
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glShadeModel(GL_SMOOTH)
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (0.8, 0.8, 0.8, 1.0))
        glLightfv(GL_LIGHT0, GL_AMBIENT, (0.3, 0.3, 0.3, 1.0))
        context = self.context()
        if context is not None and context is not self._gl_cleanup_context:
            context.aboutToBeDestroyed.connect(self._cleanup_gl)
            self._gl_cleanup_context = context
        if self._regular_data is None and self._solid_data is None:
            self._rebuild_buffer_data()
        self._regular_set = self._upload_buffer_set(self._regular_data)
        self._solid_set = self._upload_buffer_set(self._solid_data)
        self._sync_gl_textures()
        self._after_gl_initialized()

    def resizeGL(self, w: int, h: int):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(w * dpr), int(h * dpr))
        self.place_viewport_overlays()

    def place_viewport_overlays(self):
        margin = 12
        self.overlay.adjustSize()
        if getattr(self.overlay, "viewport_anchor", "") != "manual":
            self.overlay.move(margin, margin)
        self.overlay.raise_()
        for widget in self.children():
            if not isinstance(widget, QWidget) or getattr(widget, "viewport_anchor", "") != "right":
                continue
            width = min(max(widget.width(), widget.minimumWidth()), widget.maximumWidth(), self.width() - margin * 2)
            height = min(max(widget.height(), widget.minimumHeight()), widget.maximumHeight(), self.height() - margin * 2)
            widget.setGeometry(max(margin, self.width() - width - margin), margin, width, height)
            widget.raise_()

    def _apply_projection(self, w: int, h: int):
        glViewport(0, 0, max(int(w), 1), max(int(h), 1))
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        safe_h = max(float(h), 1.0)
        near_plane = 0.1 if self._controls == "mesh" else 0.01
        far_plane = 100.0 if self._controls == "mesh" else max(self.extent * 6.0, 1000.0)
        gluPerspective(45.0, float(w) / safe_h, near_plane, far_plane)
        glMatrixMode(GL_MODELVIEW)

    def _apply_render_state(self):
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        glDepthMask(True)

        if self.lighting_mode == "fixed":
            glEnable(GL_LIGHTING)
            glEnable(GL_LIGHT0)
            glEnable(GL_COLOR_MATERIAL)
            glEnable(GL_NORMALIZE)
            glShadeModel(GL_SMOOTH)
            glLightfv(GL_LIGHT0, GL_DIFFUSE, (self.diffuse, self.diffuse, self.diffuse, 1.0))
            glLightfv(GL_LIGHT0, GL_AMBIENT, (self.ambient, self.ambient, self.ambient, 1.0))
        else:
            glDisable(GL_LIGHTING)
            glDisable(GL_LIGHT0)

        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glLineWidth(1.0)

    def _apply_camera_transform(self):
        if self._controls == "mesh":
            glTranslatef(0.0, 0.0, -self.distance)
            glRotatef(self.rot_x, 1.0, 0.0, 0.0)
            glRotatef(self.rot_y, 0.0, 1.0, 0.0)
            glScalef(self.scale, self.scale, self.scale)
            glTranslatef(-self.center[0], -self.center[1], -self.center[2])
            return
        self._step_freecam()
        glRotatef(-self.freecam_pitch, 1.0, 0.0, 0.0)
        glRotatef(-self.freecam_yaw, 0.0, 1.0, 0.0)
        glTranslatef(-self.freecam_pos[0], -self.freecam_pos[1], -self.freecam_pos[2])

    def _bind_arrays(self, buffer_set: _GlBufferSet, *, use_textures: bool):
        buffer_set.vertices_vbo.bind()
        glEnableClientState(GL_VERTEX_ARRAY)
        glVertexPointer(3, GL_FLOAT, 0, None)

        if buffer_set.normals_vbo is not None and self.lighting_mode == "fixed":
            buffer_set.normals_vbo.bind()
            glEnableClientState(GL_NORMAL_ARRAY)
            glNormalPointer(GL_FLOAT, 0, None)
        else:
            glDisableClientState(GL_NORMAL_ARRAY)

        if self._colors_dirty:
            self._refresh_color_vbos()
        if buffer_set.colors_vbo is not None:
            buffer_set.colors_vbo.bind()
            glEnableClientState(GL_COLOR_ARRAY)
            glColorPointer(4, GL_FLOAT, 0, None)
        else:
            glDisableClientState(GL_COLOR_ARRAY)

        uvs_vbo = buffer_set.uvs_vbo
        if use_textures and uvs_vbo is not None and self._texture_ids:
            uvs_vbo.bind()
            glEnableClientState(GL_TEXTURE_COORD_ARRAY)
            glTexCoordPointer(2, GL_FLOAT, 0, None)
            return True

        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        return False

    def _unbind_arrays(self, buffer_set: _GlBufferSet, *, textured: bool):
        if textured and buffer_set.uvs_vbo is not None:
            glDisableClientState(GL_TEXTURE_COORD_ARRAY)
            buffer_set.uvs_vbo.unbind()
        if buffer_set.colors_vbo is not None:
            glDisableClientState(GL_COLOR_ARRAY)
            buffer_set.colors_vbo.unbind()
        if buffer_set.normals_vbo is not None:
            glDisableClientState(GL_NORMAL_ARRAY)
            buffer_set.normals_vbo.unbind()
        glDisableClientState(GL_VERTEX_ARRAY)
        buffer_set.vertices_vbo.unbind()

    def _draw_triangles(self, buffer_set: _GlBufferSet | None, *, use_textures: bool = True):
        if buffer_set is None:
            return
        if buffer_set.vertices_vbo is not None and buffer_set.indices_vbo is not None:
            textured = self._bind_arrays(buffer_set, use_textures=use_textures)
            glColor4f(1.0, 1.0, 1.0, 1.0)

            if textured and buffer_set.batch_vbos:
                for material_name, batch_vbo, count in buffer_set.batch_vbos:
                    tex_id = self._texture_ids.get(material_name)
                    if tex_id:
                        glEnable(GL_TEXTURE_2D)
                        glBindTexture(GL_TEXTURE_2D, tex_id)
                    else:
                        glBindTexture(GL_TEXTURE_2D, 0)
                        glDisable(GL_TEXTURE_2D)
                    batch_vbo.bind()
                    glDrawElements(GL_TRIANGLES, count, GL_UNSIGNED_INT, None)
                    batch_vbo.unbind()
            else:
                glBindTexture(GL_TEXTURE_2D, 0)
                glDisable(GL_TEXTURE_2D)
                buffer_set.indices_vbo.bind()
                glDrawElements(GL_TRIANGLES, buffer_set.index_count, GL_UNSIGNED_INT, None)
                buffer_set.indices_vbo.unbind()

            glBindTexture(GL_TEXTURE_2D, 0)
            glDisable(GL_TEXTURE_2D)
            self._unbind_arrays(buffer_set, textured=textured)

    def _draw_lines(self, buffer_set: _GlBufferSet | None, *, overlay: bool):
        if buffer_set is None:
            return
        glDisable(GL_LIGHTING)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_CULL_FACE)
        glDisableClientState(GL_NORMAL_ARRAY)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        if overlay:
            glDisable(GL_DEPTH_TEST)
            glDepthMask(False)
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glLineWidth(self.line_width if self._controls == "mesh" else 1.4)
        self._ensure_line_indices(buffer_set)
        if buffer_set.vertices_vbo is not None and buffer_set.line_indices_vbo is not None:
            if self._colors_dirty:
                self._refresh_color_vbos()
            use_vertex_colors = self._controls != "mesh" and buffer_set.colors_vbo is not None
            if use_vertex_colors:
                buffer_set.colors_vbo.bind()
                glEnableClientState(GL_COLOR_ARRAY)
                glColorPointer(4, GL_FLOAT, 0, None)
                glColor4f(1.0, 1.0, 1.0, 1.0)
            else:
                glDisableClientState(GL_COLOR_ARRAY)
                glColor4f(0.2, 1.0, 0.3, 1.0)
            buffer_set.vertices_vbo.bind()
            glEnableClientState(GL_VERTEX_ARRAY)
            glVertexPointer(3, GL_FLOAT, 0, None)
            buffer_set.line_indices_vbo.bind()
            glDrawElements(GL_LINES, buffer_set.line_count, GL_UNSIGNED_INT, None)
            buffer_set.line_indices_vbo.unbind()
            glDisableClientState(GL_VERTEX_ARRAY)
            buffer_set.vertices_vbo.unbind()
            if use_vertex_colors:
                glDisableClientState(GL_COLOR_ARRAY)
                buffer_set.colors_vbo.unbind()
        if overlay:
            glDepthMask(True)
            glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        if self.lighting_mode == "fixed":
            glEnable(GL_LIGHTING)

    def _draw_regular_scene(self):
        if self._controls == "mesh":
            if self.wireframe_mode == "polygon":
                glPolygonMode(GL_FRONT_AND_BACK, GL_LINE)
                glLineWidth(self.line_width)
                self._draw_triangles(self._regular_set, use_textures=False)
                glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
            else:
                self._draw_triangles(self._regular_set)
            if self.wireframe_mode in ("lines_depth", "lines_overlay"):
                self._draw_lines(self._regular_set, overlay=self.wireframe_mode == "lines_overlay")
            return

        if self.render_mode in {"solid", "hybrid"}:
            self._draw_triangles(self._regular_set, use_textures=True)
        if self.render_mode in {"wire", "hybrid"}:
            self._draw_lines(self._regular_set, overlay=False)

    def paintGL(self):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(self.width() * dpr), int(self.height() * dpr))
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self._apply_render_state()
        glLoadIdentity()
        glLightfv(GL_LIGHT0, GL_POSITION, (0.5, 1.0, 1.0, 0.0))
        self._apply_camera_transform()

        self._draw_regular_scene()
        self._draw_triangles(self._solid_set, use_textures=False)
        self._after_scene_draw()
        self._record_frame()

    def _update_timer_state(self):
        if not self.isVisible():
            self._timer.stop()
            return
        interval = 0 if self._fps_limit == 0 else max(1, round(1000 / self._fps_limit))
        self._timer.start(interval)

    def _update_after_camera_change(self) -> None:
        if self._fps_limit == 0:
            self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_timer_state()

    def hideEvent(self, event):
        self._unlock_scene_cursor()
        self._timer.stop()
        self._freecam_keys.clear()
        self._freecam_mods.clear()
        super().hideEvent(event)

    def _change_fps_limit(self, value: int):
        self._fps_limit = int(value)
        self._save_view_setting("mesh_viewer_fps_limit", self._fps_limit)
        self._update_timer_state()

    def _set_wireframe_mode(self, mode: str):
        if mode not in self.WIREFRAME_MODES:
            return
        self.wireframe_mode = mode
        self._save_view_setting("mesh_viewer_wireframe_mode", mode)
        self.update()

    def _set_lighting_mode(self, mode: str):
        if mode not in self.LIGHTING_MODES:
            return
        self.lighting_mode = mode
        self._save_view_setting("mesh_viewer_lighting_mode", mode)
        self._colors_dirty = True
        self.update()

    def _set_line_width(self, value: float):
        self.line_width = float(value)
        self._save_view_setting("mesh_viewer_line_width", self.line_width)
        self.update()

    def _set_ambient(self, value: float):
        self.ambient = float(value)
        self._save_view_setting("mesh_viewer_ambient", self.ambient)
        self._colors_dirty = True
        self.update()

    def _set_diffuse(self, value: float):
        self.diffuse = float(value)
        self._save_view_setting("mesh_viewer_diffuse", self.diffuse)
        self._colors_dirty = True
        self.update()

    def _set_show_bone_labels(self, checked: bool):
        self.show_bone_labels = bool(checked)
        self._save_view_setting("mesh_viewer_show_bones", self.show_bone_labels)
        self.update()
