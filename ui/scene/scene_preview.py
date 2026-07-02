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
    GL_LINE_STRIP,
    GL_MODELVIEW,
    GL_MODELVIEW_MATRIX,
    GL_MODULATE,
    GL_NORMAL_ARRAY,
    GL_NORMALIZE,
    GL_ONE_MINUS_SRC_ALPHA,
    GL_POSITION,
    GL_PROJECTION,
    GL_PROJECTION_MATRIX,
    GL_QUADS,
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
    GL_UNSIGNED_INT,
    GL_VIEWPORT,
    GL_DYNAMIC_DRAW,
    GL_VERTEX_ARRAY,
    glBegin,
    glBindBuffer,
    glBindTexture,
    glBlendFunc,
    glBufferSubData,
    glClear,
    glClearColor,
    glColor4f,
    glColorMask,
    glColorPointer,
    glCullFace,
    glDeleteTextures,
    glDepthMask,
    glDisable,
    glDisableClientState,
    glDrawElements,
    glEnable,
    glEnableClientState,
    glEnd,
    glFrontFace,
    glGenTextures,
    glGetDoublev,
    glGetIntegerv,
    glLightfv,
    glLineWidth,
    glLoadIdentity,
    glMatrixMode,
    glMultMatrixf,
    glNormalPointer,
    glPixelStorei,
    glPolygonMode,
    glPopMatrix,
    glPushMatrix,
    glRotatef,
    glScalef,
    glShadeModel,
    glTexCoordPointer,
    glTexEnvi,
    glCompressedTexImage2D,
    glTexParameteri,
    glTranslatef,
    glVertex3f,
    glVertexPointer,
    glViewport,
)
from OpenGL.GLU import gluPerspective, gluProject, gluUnProject
from PySide6.QtCore import QEvent, QTimer, Qt, Signal
from PySide6.QtGui import QCursor, QSurfaceFormat
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
    scene_key_index_buffers,
)
from .scene_model import SceneDrawMesh


@dataclass(slots=True)
class _GlBufferSet:
    data: SceneBufferSet
    vertices_vbo: object | None = None
    normals_vbo: object | None = None
    colors: np.ndarray | None = None
    colors_vbo: object | None = None
    uvs_vbo: object | None = None
    indices_vbo: object | None = None
    line_indices_vbo: object | None = None
    batch_vbos: list[tuple[str, object, int]] = field(default_factory=list)
    index_count: int = 0
    line_count: int = 0
    lines_ready: bool = False


GIZMO_AXES = np.identity(3, dtype=np.float32)
GIZMO_COLORS = ((1.0, 0.18, 0.12), (0.2, 0.9, 0.25), (0.25, 0.45, 1.0))
GIZMO_BOX_FACES = ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0))
GIZMO_PLANES = ((0, 1, (1.0, 0.85, 0.12, 0.24)), (0, 2, (0.9, 0.15, 1.0, 0.22)), (1, 2, (0.15, 0.8, 1.0, 0.22)))
IDENTITY4 = np.identity(4, dtype=np.float32)
HOVER_PICK_INTERVAL = 1.0 / 15.0
HOVER_PICK_MIN_PIXELS = 4.0
HOVER_PICK_BATCH = 6
HOVER_DETECT_KEY = Qt.Key_H


class ScenePreviewWidget(OrbitCameraMixin, QOpenGLWidget):
    object_clicked = Signal(str)

    SETTINGS_DEFAULTS = {
        "mesh_viewer_fps_limit": 60,
        "mesh_viewer_wireframe_mode": "off",
        "mesh_viewer_lighting_mode": "fixed",
        "mesh_viewer_line_width": 1.5,
        "mesh_viewer_ambient": 0.35,
        "mesh_viewer_diffuse": 0.65,
        "mesh_viewer_show_bones": False,
        "scene_render_mode": "solid",
        "scene_gizmo_mode": "position",
        "scene_show_only_highlighted": False,
        "scene_camera_speed": 1.0,
        "scene_camera_look": 0.18,
        "scene_camera_wheel": 0.08,
        "scene_camera_boost": 3.0,
        "scene_camera_slow": 0.25,
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
        fmt.setAlphaBufferSize(0)
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
        self._freecam_base_speed = 1.0
        self.freecam_speed = 1.0
        self._freecam_keys: set[str] = set()
        self._freecam_mods: set[int] = set()
        self._last_freecam_time = time.perf_counter()
        self._cursor_lock_pos = None
        self._drag_overlay = self._drag_offset = self._resize_overlay = self._fullscreen_restore = None

        self._meshes: list[SceneDrawMesh] = []
        self._highlighted_keys: set[str] = set()
        self._selection_keys: set[str] = set()
        self._selection_center: np.ndarray | None = None
        self._selection_extent = 1.0
        self._gizmo_hover_axis: int | None = None
        self._gizmo_drag = None
        self._gizmo_projection = None
        self._regular_set: _GlBufferSet | None = None
        self._solid_set: _GlBufferSet | None = None
        self._selection_regular_set: _GlBufferSet | None = None
        self._selection_solid_set: _GlBufferSet | None = None
        self._hover_key = self._hover_block_key = ""
        self._hover_detect_down = False
        self._hover_vertex_cache: dict[int, dict[str, np.ndarray]] = {}
        self._pick_entries: list[tuple[str, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
        self._pick_bound_keys: list[str] = []
        self._pick_bound_mins = np.zeros((0, 3), dtype=np.float32)
        self._pick_bound_maxs = np.zeros((0, 3), dtype=np.float32)
        self._last_hover_pick_time = 0.0
        self._last_hover_pick_pos: np.ndarray | None = None
        self._scene_matrix = IDENTITY4.copy()
        self._regular_data: SceneBufferSet | None = None
        self._solid_data: SceneBufferSet | None = None
        self._texture_ids: dict[str, int] = {}
        self._texture_sources: dict[str, str] = {}
        self._texture_source_ids: dict[str, int] = {}
        self._pending_material_images: dict[str, tuple[str, TexPreviewUpload]] = {}
        self._material_tints: dict[str, tuple[float, float, float, float]] = {}
        self._two_sided_materials: set[str] = set()
        self._hidden_keys: set[str] = set()
        self._gl_cleanup_context = None
        self._needs_gl_upload = False

        self.render_mode = "wire" if self._controls == "mesh" else self._setting_choice("scene_render_mode", "solid", ("wire", "hybrid", "solid"))
        self._gizmo_mode = self._setting_choice("scene_gizmo_mode", "position", ("position", "rotation", "scale"))
        self.show_only_highlighted = self._setting_bool("scene_show_only_highlighted", False)
        self._fps_limit = self._setting_int("mesh_viewer_fps_limit", 60, 0, 240)
        self.wireframe_mode = self._setting_choice("mesh_viewer_wireframe_mode", "off", self.WIREFRAME_MODES)
        self.lighting_mode = self._setting_choice("mesh_viewer_lighting_mode", "fixed", self.LIGHTING_MODES)
        self.line_width = self._setting_float("mesh_viewer_line_width", 1.5, 0.5, 8.0)
        self.color_source = "vertex"
        self.ambient = self._setting_float("mesh_viewer_ambient", 0.35, 0.0, 1.0)
        self.diffuse = self._setting_float("mesh_viewer_diffuse", 0.65, 0.0, 1.0)
        self.show_bone_labels = self._setting_bool("mesh_viewer_show_bones", False)
        self.camera_speed = self._setting_float("scene_camera_speed", 1.0, 0.01, 50.0)
        self.camera_look = self._setting_float("scene_camera_look", 0.18, 0.01, 2.0)
        self.camera_wheel = self._setting_float("scene_camera_wheel", 0.08, 0.001, 2.0)
        self.camera_boost = self._setting_float("scene_camera_boost", 3.0, 1.0, 20.0)
        self.camera_slow = self._setting_float("scene_camera_slow", 0.25, 0.01, 1.0)
        self._colors_dirty = True

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self.update)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
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
        self.fullscreen_button = self._overlay_button("⛶", "Fullscreen viewport", self._toggle_view_fullscreen)
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
        button.setFocusPolicy(Qt.NoFocus)
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

    def _toggle_view_fullscreen(self):
        owner = getattr(self, "_external_fullscreen_owner", None)
        if owner is not None:
            owner.leave_view_fullscreen() if owner.is_view_fullscreen() else owner.enter_view_fullscreen()
        else:
            self._leave_view_fullscreen() if self._fullscreen_restore else self._enter_view_fullscreen()

    def _enter_view_fullscreen(self):
        if self._fullscreen_restore is not None:
            return
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        index = layout.indexOf(self) if layout is not None else -1
        self._cleanup_gl()
        if index >= 0:
            layout.takeAt(index)
        elif parent is not None and hasattr(parent, "indexOf"):
            index = parent.indexOf(self)
        self._fullscreen_restore = (parent, layout, index)
        self.setParent(None)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setWindowTitle("Scene Preview")
        self.fullscreen_button.setText("x")
        self.showFullScreen()
        QTimer.singleShot(0, self._after_fullscreen_change)

    def _leave_view_fullscreen(self):
        if self._fullscreen_restore is None:
            return
        parent, layout, index = self._fullscreen_restore
        self._fullscreen_restore = None
        self._cleanup_gl()
        self.hide()
        self.setWindowFlags(Qt.Widget)
        if parent is not None and layout is not None and hasattr(layout, "insertWidget"):
            layout.insertWidget(index if index >= 0 else layout.count(), self, 1)
        elif parent is not None and hasattr(parent, "insertWidget"):
            parent.insertWidget(index if index >= 0 else parent.count(), self)
        elif parent is not None:
            self.setParent(parent)
        self.fullscreen_button.setText("⛶")
        self.show()
        QTimer.singleShot(0, self._after_fullscreen_change)

    def _after_fullscreen_change(self):
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        if self.isWindow():
            self.raise_()
            self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        self._gizmo_projection = None
        self._rebuild_pick_entries()
        missing_regular = self._regular_set is None and self._regular_data is not None
        missing_solid = self._solid_set is None and self._solid_data is not None
        self._needs_gl_upload = self._needs_gl_upload or missing_regular or missing_solid
        self.place_viewport_overlays()
        self.update()

    def eventFilter(self, obj, event):
        overlay = getattr(obj, "_viewport_drag_overlay", None)
        if not overlay:
            return super().eventFilter(obj, event)
        kind = event.type()
        if kind in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease) and self._controls != "mesh" and event.key() == HOVER_DETECT_KEY:
            (self.keyPressEvent if kind == QEvent.Type.KeyPress else self.keyReleaseEvent)(event)
            return True
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
        self.scene_mode_combo = self._data_combo((("Wireframe", "wire"), ("Solid + Wire", "hybrid"), ("Solid", "solid")), self._set_render_mode, self.render_mode)
        self._add_control_row(layout, "Mode", self.scene_mode_combo)
        self._add_fps_limit_control(layout)
        self._add_control_row(
            layout,
            "Speed", self._camera_spin("camera_speed", 0.01, 50.0, 0.1),
            "Look", self._camera_spin("camera_look", 0.01, 2.0, 0.01),
        )
        self._add_control_row(
            layout,
            "Wheel", self._camera_spin("camera_wheel", 0.001, 2.0, 0.01),
            "Fast", self._camera_spin("camera_boost", 1.0, 20.0, 0.25),
            "Slow", self._camera_spin("camera_slow", 0.01, 1.0, 0.05),
        )
        self.gizmo_mode_combo = self._data_combo((("Position", "position"), ("Rotation", "rotation"), ("Scale", "scale")), self.set_gizmo_mode, self._gizmo_mode)
        self._add_control_row(layout, "Gizmo", self.gizmo_mode_combo)
        self.highlight_only_check = QCheckBox("View only highlighted", self.overlay)
        self.highlight_only_check.setChecked(self.show_only_highlighted)
        self.highlight_only_check.toggled.connect(self._set_show_only_highlighted)
        layout.addWidget(self.highlight_only_check)
        note = QLabel("Hold H to hover/select viewport objects.", self.overlay)
        note.setStyleSheet("color:#7f8b96; background-color:transparent; font-size:10px;")
        layout.addWidget(note)

    def _add_fps_limit_control(self, layout: QVBoxLayout):
        fps_spin = QSpinBox(self.overlay)
        fps_spin.setRange(0, 240)
        fps_spin.setFixedWidth(50)
        fps_spin.setValue(self._fps_limit)
        fps_spin.valueChanged.connect(self._change_fps_limit)
        self._add_control_row(layout, "Limit", fps_spin)

    def _add_control_row(self, layout: QVBoxLayout, *items):
        row = QHBoxLayout()
        for item in items:
            row.addWidget(QLabel(item, self.overlay) if isinstance(item, str) else item)
        layout.addLayout(row)
        return row

    def _data_combo(self, items, slot, current=None) -> QComboBox:
        combo = QComboBox(self.overlay)
        for label, data in items:
            combo.addItem(label, data)
        if current is not None and (index := combo.findData(current)) >= 0:
            combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(lambda _: slot(combo.currentData()))
        return combo

    def _text_combo(self, items, current: str, slot) -> QComboBox:
        combo = QComboBox(self.overlay)
        combo.addItems(items)
        combo.setCurrentText(current)
        combo.currentTextChanged.connect(slot)
        return combo

    def _float_spin(self, minimum: float, maximum: float, step: float, value: float, slot) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self.overlay)
        spin.setRange(minimum, maximum)
        spin.setDecimals(3 if step < 0.01 else 2)
        spin.setSingleStep(step)
        spin.setFixedWidth(58)
        spin.setValue(value)
        spin.valueChanged.connect(slot)
        return spin

    def _camera_spin(self, attr: str, minimum: float, maximum: float, step: float) -> QDoubleSpinBox:
        return self._float_spin(minimum, maximum, step, getattr(self, attr), lambda value: self._set_camera_setting(attr, f"scene_{attr}", value))

    def _build_mesh_controls(self, layout: QVBoxLayout):
        self._add_fps_limit_control(layout)

        self.wf_combo = self._text_combo(["off", "polygon", "lines_depth", "lines_overlay"], self.wireframe_mode, self._set_wireframe_mode)
        self.line_spin = self._float_spin(0.5, 8.0, 0.1, self.line_width, self._set_line_width)
        self._add_control_row(layout, "WF Mode", self.wf_combo, "Line", self.line_spin)

        self.light_combo = self._text_combo(["off", "fixed", "software"], self.lighting_mode, self._set_lighting_mode)
        row2 = self._add_control_row(layout, "Light", self.light_combo)
        mesh = getattr(self, "mesh", None)
        if getattr(mesh, "streaming_buffer_count", 0):
            stream_status = "Loaded" if getattr(mesh, "streaming_data_loaded", False) else "Missing"
            row2.addWidget(QLabel(f"Stream {stream_status}", self.overlay))
        self.amb_spin = self._float_spin(0.0, 1.0, 0.05, self.ambient, self._set_ambient)
        self.diff_spin = self._float_spin(0.0, 1.0, 0.05, self.diffuse, self._set_diffuse)
        for item in ("Amb", self.amb_spin, "Diff", self.diff_spin):
            row2.addWidget(QLabel(item, self.overlay) if isinstance(item, str) else item)

        self.bone_labels_check = QCheckBox("Bones", self.overlay)
        self.bone_labels_check.setChecked(self.show_bone_labels)
        self.bone_labels_check.toggled.connect(self._set_show_bone_labels)
        self._add_control_row(layout, self.bone_labels_check)

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
        self._highlighted_keys = set(self._selection_keys if highlighted_keys is None else highlighted_keys)
        self._hover_key = self._hover_block_key = ""
        self._recompute_bounds()
        self._refresh_selection_bounds()
        if reset_camera:
            self._reset_freecam()
        self._upload_buffers()
        self.update()

    def set_selected_keys(self, keys: set[str], *, focus: bool = False) -> None:
        keys = set(keys)
        if keys == self._selection_keys and not focus:
            return
        self._finish_gizmo_drag(commit=False)
        self._selection_keys = keys
        self._highlighted_keys = set(keys)
        self._gizmo_hover_axis = None
        self._refresh_selection_bounds()
        if self.show_only_highlighted:
            self._upload_buffers()
        else:
            self._refresh_selection_buffer_sets()
        if focus:
            self.focus_selection()
        self.update()

    def set_gizmo_mode(self, mode: str) -> None:
        self._gizmo_mode = mode if mode in {"position", "rotation", "scale"} else "position"
        self._save_view_setting("scene_gizmo_mode", self._gizmo_mode)
        self.update()

    def focus_selection(self) -> None:
        if self._selection_center is None:
            return
        extent = max(float(self._selection_extent), 1.0)
        if self._controls == "mesh":
            self.center = self._selection_center.copy()
            self.distance = extent * 2.2
        else:
            forward = self._freecam_rotation() @ np.array((0.0, 0.0, -1.0), dtype=np.float32)
            forward /= np.linalg.norm(forward) or 1.0
            self.freecam_pos = self._selection_center - forward * max(extent * 1.8, 1.0)
            self._last_freecam_time = time.perf_counter()

    def set_hidden_keys(self, keys: set[str], *, refresh: bool = True):
        keys = set(keys)
        if keys == self._hidden_keys:
            return
        self._hidden_keys = keys
        if self._hover_key in self._hidden_keys:
            self._hover_key = ""
        self._refresh_selection_bounds()
        if not refresh:
            return
        if self.context() is None:
            self.update()
            return
        self.makeCurrent()
        self._upload_index_vbos(self._regular_set)
        self._upload_index_vbos(self._solid_set)
        self._refresh_selection_buffer_sets(current=True)
        self._refresh_hover_colors(current=True)
        self.doneCurrent()
        self.update()

    def set_material_images(self, images: dict[str, tuple[str, TexPreviewUpload]]):
        self._pending_material_images = dict(images)
        self._sync_material_images()

    def update_material_images(self, images: dict[str, tuple[str, TexPreviewUpload]]):
        self._pending_material_images.update(images)
        self._sync_material_images(images)

    def set_material_profiles(self, profiles: dict[str, object]):
        self._material_tints, self._two_sided_materials = {}, set()
        self.update_material_profiles(profiles)

    def update_material_profiles(self, profiles: dict[str, object]):
        for name, profile in profiles.items():
            tint = tuple(float(value) for value in getattr(profile, "tint", (1.0, 1.0, 1.0, 1.0)))
            self._material_tints[name] = (tint + (1.0, 1.0, 1.0, 1.0))[:4]
            (self._two_sided_materials.add if getattr(profile, "two_sided", False) else self._two_sided_materials.discard)(name)
        self.update()

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
        if self._controls != "mesh":
            self._save_view_setting("scene_render_mode", self.render_mode)
        self.update()

    def mousePressEvent(self, event):
        self.setFocus(Qt.MouseFocusReason)
        if self._controls != "mesh" and event.button() == Qt.LeftButton:
            if self._begin_gizmo_drag(event):
                event.accept()
                return
            if self._hover_detect_down and (key := self._pick_scene_key(self._screen_pos(event), precise=True)):
                self.object_clicked.emit(key)
                self._hover_block_key = key
                self._set_hover_key("")
            else:
                self.object_clicked.emit("")
            self._set_gizmo_hover(self._pick_gizmo_axis(self._screen_pos(event)))
            event.accept()
            return
        if self._controls == "mesh" or event.button() not in (Qt.LeftButton, Qt.RightButton):
            return super().mousePressEvent(event)
        self._lock_scene_cursor(event.globalPosition().toPoint())
        event.accept()

    def mouseMoveEvent(self, event):
        if self._gizmo_drag is not None:
            self._drag_gizmo(event)
            event.accept()
            return
        if self._controls == "mesh":
            return super().mouseMoveEvent(event)
        buttons = event.buttons()
        if not (buttons & Qt.RightButton):
            self._update_scene_hover(self._screen_pos(event)) if self._hover_detect_down else self._set_hover_key("")
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
        if self._gizmo_drag is not None:
            self._finish_gizmo_drag(commit=True)
            event.accept()
            return
        if self._controls == "mesh":
            return super().mouseReleaseEvent(event)
        if not (event.buttons() & Qt.RightButton):
            self._unlock_scene_cursor()
        event.accept()

    def wheelEvent(self, event):
        if self._controls == "mesh":
            super().wheelEvent(event)
            return
        steps = event.angleDelta().y() / 120.0
        self._move_freecam_local(np.array((0.0, 0.0, -steps * self.freecam_speed * self.camera_wheel), dtype=np.float32))
        self._update_after_camera_change()

    def keyPressEvent(self, event):
        owner = getattr(self, "_external_fullscreen_owner", None)
        fullscreen = owner.is_view_fullscreen() if owner is not None else self._fullscreen_restore is not None
        if event.key() == Qt.Key_F11 or (fullscreen and event.key() == Qt.Key_Escape):
            self._toggle_view_fullscreen()
            event.accept()
            return
        if self._controls != "mesh" and event.key() == HOVER_DETECT_KEY:
            if not event.isAutoRepeat():
                self._hover_detect_down, self._last_hover_pick_time, self._last_hover_pick_pos = True, 0.0, None
                pos = self.mapFromGlobal(QCursor.pos())
                if self.rect().contains(pos):
                    self._update_scene_hover(np.array((pos.x(), pos.y()), dtype=np.float32), force=True)
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
        if self._controls != "mesh" and event.key() == HOVER_DETECT_KEY:
            if not event.isAutoRepeat():
                self._hover_detect_down, self._last_hover_pick_pos = False, None
                self._set_hover_key("")
            event.accept()
            return
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
        self._finish_gizmo_drag(commit=True)
        self._set_hover_key("")
        self._hover_detect_down, self._last_hover_pick_pos = False, None
        self._freecam_keys.clear()
        self._freecam_mods.clear()
        super().focusOutEvent(event)

    def leaveEvent(self, event):
        self._set_hover_key("")
        self._last_hover_pick_pos = None
        super().leaveEvent(event)

    def event(self, event):
        if event.type() == QEvent.HoverMove and getattr(self, "_hover_detect_down", False) and getattr(self, "_controls", "mesh") != "mesh" and getattr(self, "_gizmo_drag", None) is None and getattr(self, "_cursor_lock_pos", None) is None:
            self._update_scene_hover(self._screen_pos(event))
            return True
        return super().event(event)

    def _set_gizmo_hover(self, axis: int | None) -> None:
        if axis == self._gizmo_hover_axis:
            return
        self._gizmo_hover_axis = axis
        self.update()

    def _update_scene_hover(self, pos: np.ndarray, *, force: bool = False) -> None:
        now = time.perf_counter()
        if not force and self._last_hover_pick_pos is not None and float(np.linalg.norm(pos - self._last_hover_pick_pos)) < HOVER_PICK_MIN_PIXELS:
            return
        if not force and now - self._last_hover_pick_time < HOVER_PICK_INTERVAL:
            return
        self._last_hover_pick_time = now
        self._last_hover_pick_pos = pos.copy()
        axis = self._pick_gizmo_axis(pos)
        self._set_gizmo_hover(axis)
        self._set_hover_key("" if axis is not None else self._pick_scene_key(pos, precise=False))

    def _move_scene_camera(self, dx: float, dy: float, buttons) -> None:
        if buttons & Qt.RightButton:
            self.freecam_yaw -= dx * self.camera_look
            self.freecam_pitch = max(-80.0, min(80.0, self.freecam_pitch - dy * self.camera_look))

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
        self._save_view_setting("scene_show_only_highlighted", self.show_only_highlighted)
        self._upload_buffers()
        self.update()

    def _recompute_bounds(self):
        self.center, self.extent = scene_bounds(self._meshes)
        self.scale = 1.0 / self.extent if self.extent > 1e-6 else 1.0

    def _refresh_selection_bounds(self) -> None:
        meshes = [mesh for mesh in self._meshes if mesh.key in self._selection_keys and mesh.key not in self._hidden_keys]
        self._selection_center, self._selection_extent = scene_bounds(meshes) if meshes else (None, 1.0)

    def _selection_is_whole_scene(self) -> bool:
        visible = {mesh.key for mesh in self._meshes if mesh.key not in self._hidden_keys}
        return bool(visible) and visible <= self._selection_keys

    def _reset_freecam(self) -> None:
        self.freecam_yaw = 0.0
        self.freecam_pitch = -10.0
        self._freecam_base_speed = max(self.extent * 0.6, 1.0)
        self.freecam_speed = self._freecam_base_speed * self.camera_speed
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
        keys = self._freecam_keys
        move = np.array((
            ("right" in keys) - ("left" in keys),
            ("up" in keys) - ("down" in keys),
            ("back" in keys) - ("forward" in keys),
        ), dtype=np.float32)
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
            speed = self.freecam_speed * (self.camera_boost if Qt.Key_Shift in self._freecam_mods else 1.0)
            speed *= self.camera_slow if Qt.Key_Control in self._freecam_mods else 1.0
            self._move_freecam_local(move * speed * dt)

    def _gizmo_size(self) -> float:
        if self._controls != "mesh" and self._selection_center is not None:
            distance = max(float(np.linalg.norm(self._selection_center - self.freecam_pos)), 1.0)
            return max(2.0 * distance * np.tan(np.deg2rad(22.5)) * 120.0 / max(self.height(), 1), self.extent * 0.004, 0.1)
        return max(float(self._selection_extent) * 0.45, max(float(self.extent) * 0.025, 0.25))

    def _cache_gizmo_projection(self) -> None:
        self._gizmo_projection = (
            glGetDoublev(GL_MODELVIEW_MATRIX),
            glGetDoublev(GL_PROJECTION_MATRIX),
            glGetIntegerv(GL_VIEWPORT),
            float(self.devicePixelRatioF()),
        )

    def _project_gizmo_point(self, point) -> np.ndarray | None:
        if self._gizmo_projection is None:
            return None
        model, projection, viewport, dpr = self._gizmo_projection
        try:
            x, y, _z = gluProject(float(point[0]), float(point[1]), float(point[2]), model, projection, viewport)
        except Exception:
            return None
        return np.array((x / dpr, self.height() - y / dpr), dtype=np.float32)

    @staticmethod
    def _screen_pos(event) -> np.ndarray:
        pos = event.position()
        return np.array((pos.x(), pos.y()), dtype=np.float32)

    @staticmethod
    def _screen_segment_distance(pos: np.ndarray, a: np.ndarray, b: np.ndarray) -> float:
        ab = b - a
        denom = float(ab @ ab)
        if denom <= 1e-6:
            return float(np.linalg.norm(pos - a))
        t = max(0.0, min(1.0, float(((pos - a) @ ab) / denom)))
        return float(np.linalg.norm(pos - (a + ab * t)))

    @staticmethod
    def _screen_triangle_contains(pos: np.ndarray, a: np.ndarray, b: np.ndarray, c: np.ndarray) -> bool:
        v0, v1, v2 = c - a, b - a, pos - a
        d00, d01, d11 = float(v0 @ v0), float(v0 @ v1), float(v1 @ v1)
        denom = d00 * d11 - d01 * d01
        if abs(denom) <= 1e-6:
            return False
        u = (d11 * float(v2 @ v0) - d01 * float(v2 @ v1)) / denom
        v = (d00 * float(v2 @ v1) - d01 * float(v2 @ v0)) / denom
        return u >= 0.0 and v >= 0.0 and u + v <= 1.0

    @staticmethod
    def _gizmo_plane_corners(center: np.ndarray, size: float, a: int, b: int):
        u, v = GIZMO_AXES[a], GIZMO_AXES[b]
        return (
            center + (u + v) * size * 0.22,
            center + (u * 0.42 + v * 0.22) * size,
            center + (u + v) * size * 0.42,
            center + (u * 0.22 + v * 0.42) * size,
        )

    def _pick_gizmo_axis(self, pos: np.ndarray) -> int | None:
        if self._selection_center is None:
            return None
        center = self._selection_center
        size = self._gizmo_size()
        if self._gizmo_mode != "rotation":
            origin = self._project_gizmo_point(center)
            if origin is None:
                return None
            if self._gizmo_mode == "position":
                for a, b, _color in GIZMO_PLANES:
                    screen = [self._project_gizmo_point(point) for point in self._gizmo_plane_corners(center, size, a, b)]
                    if all(point is not None for point in screen) and (
                        self._screen_triangle_contains(pos, screen[0], screen[1], screen[2])
                        or self._screen_triangle_contains(pos, screen[0], screen[2], screen[3])
                    ):
                        return a, b
            hits = [
                (self._screen_segment_distance(pos, origin, end), axis_index)
                for axis_index, axis in enumerate(GIZMO_AXES)
                for end in [self._project_gizmo_point(center + axis * size)]
                if end is not None
            ]
        else:
            hits = []
            for axis_index, points in enumerate(self._gizmo_circle_points(center, size)):
                screen = [self._project_gizmo_point(point) for point in points]
                screen = [point for point in screen if point is not None]
                hits.extend((self._screen_segment_distance(pos, a, b), axis_index) for a, b in zip(screen, screen[1:]))
        best = min(hits, default=(11.0, None))
        return best[1] if best[0] <= 10.0 else None

    def _set_hover_key(self, key: str) -> None:
        if key and key == self._hover_block_key:
            key = ""
        elif key:
            self._hover_block_key = ""
        key = key if key and key not in self._active_hidden_keys() else ""
        if key == self._hover_key:
            return
        old_key = self._hover_key
        self._hover_key = key
        self._refresh_hover_colors(old_key, key)
        self.update()

    def _pick_scene_key(self, pos: np.ndarray, *, precise: bool) -> str:
        ray = self._scene_pick_ray(pos)
        if ray is None:
            return ""
        origin, direction = ray
        hidden = self._active_hidden_keys()
        if not precise:
            return self._pick_hover_key(origin, direction, hidden)
        best_key, best_t = "", float("inf")
        for key, mins, maxs, vertices, triangles in self._pick_entries:
            if key in hidden:
                continue
            box_t = self._ray_aabb_distance(origin, direction, mins, maxs)
            if box_t is None or box_t > best_t:
                continue
            tri_t = self._ray_triangles(origin, direction, vertices, triangles)
            if tri_t is not None and tri_t < best_t:
                best_key, best_t = key, tri_t
        return best_key

    def _pick_hover_key(self, origin: np.ndarray, direction: np.ndarray, hidden: set[str]) -> str:
        distances = self._pick_bound_distances(origin, direction, hidden)
        if distances is None:
            return ""
        candidates = np.flatnonzero(np.isfinite(distances))
        if not len(candidates):
            return ""
        best_key, best_t = "", float("inf")
        order = candidates[np.argsort(distances[candidates])]
        for start in range(0, len(order), HOVER_PICK_BATCH):
            batch = order[start:start + HOVER_PICK_BATCH]
            if distances[batch[0]] > best_t:
                break
            key, tri_t = self._ray_entry_batch(origin, direction, batch)
            if tri_t is not None and tri_t < best_t:
                best_key, best_t = key, tri_t
        return best_key

    def _ray_entry_batch(self, origin: np.ndarray, direction: np.ndarray, entry_indices: np.ndarray):
        tris, owners = [], []
        for entry_index in entry_indices:
            _key, _mins, _maxs, vertices, triangles = self._pick_entries[int(entry_index)]
            tris.append(vertices[triangles])
            owners.append(np.full(len(triangles), int(entry_index), dtype=np.int32))
        hits = self._ray_triangle_distances(origin, direction, np.concatenate(tris, axis=0))
        if not np.isfinite(hits).any():
            return "", None
        hit_index = int(np.argmin(hits))
        return self._pick_entries[int(np.concatenate(owners)[hit_index])][0], float(hits[hit_index])

    def _pick_bound_distances(self, origin: np.ndarray, direction: np.ndarray, hidden: set[str]) -> np.ndarray | None:
        if not self._pick_bound_keys:
            return None
        mins, maxs = self._pick_bound_mins, self._pick_bound_maxs
        valid = np.ones(len(self._pick_bound_keys), dtype=bool) if not hidden else np.fromiter((key not in hidden for key in self._pick_bound_keys), dtype=bool, count=len(self._pick_bound_keys))
        lo = np.full(len(self._pick_bound_keys), -np.inf, dtype=np.float32)
        hi = np.full(len(self._pick_bound_keys), np.inf, dtype=np.float32)
        for axis in range(3):
            d = float(direction[axis])
            if abs(d) < 1e-8:
                valid &= (origin[axis] >= mins[:, axis]) & (origin[axis] <= maxs[:, axis])
                continue
            a = (mins[:, axis] - origin[axis]) / d
            b = (maxs[:, axis] - origin[axis]) / d
            lo = np.maximum(lo, np.minimum(a, b))
            hi = np.minimum(hi, np.maximum(a, b))
        valid &= hi >= np.maximum(lo, 0.0)
        if not np.any(valid):
            return None
        return np.where(valid, np.maximum(lo, 0.0), np.inf)

    def _scene_pick_ray(self, pos: np.ndarray):
        if self._gizmo_projection is None:
            return None
        model, projection, viewport, dpr = self._gizmo_projection
        try:
            x, y = float(pos[0] * dpr), float((self.height() - pos[1]) * dpr)
            origin = np.array(gluUnProject(x, y, 0.0, model, projection, viewport), dtype=np.float32)
            far = np.array(gluUnProject(x, y, 1.0, model, projection, viewport), dtype=np.float32)
            if (matrix := self._active_scene_matrix()) is not None:
                inv = np.linalg.inv(matrix).astype(np.float32)
                origin, far = self._transform_point(origin, inv), self._transform_point(far, inv)
        except Exception:
            return None
        direction = far - origin
        length = float(np.linalg.norm(direction))
        return (origin, direction / length) if length > 1e-6 else None

    @staticmethod
    def _ray_aabb_distance(origin: np.ndarray, direction: np.ndarray, mins: np.ndarray, maxs: np.ndarray):
        t0, t1 = 0.0, float("inf")
        for axis in range(3):
            d = float(direction[axis])
            if abs(d) < 1e-8:
                if origin[axis] < mins[axis] or origin[axis] > maxs[axis]:
                    return None
                continue
            a, b = (float(mins[axis] - origin[axis]) / d, float(maxs[axis] - origin[axis]) / d)
            if a > b:
                a, b = b, a
            t0, t1 = max(t0, a), min(t1, b)
            if t1 < t0:
                return None
        return t0

    @staticmethod
    def _ray_triangles(origin: np.ndarray, direction: np.ndarray, vertices: np.ndarray, triangles: np.ndarray):
        hits = ScenePreviewWidget._ray_triangle_distances(origin, direction, vertices[triangles])
        hit = float(hits.min()) if len(hits) else float("inf")
        return hit if np.isfinite(hit) else None

    @staticmethod
    def _ray_triangle_distances(origin: np.ndarray, direction: np.ndarray, tris: np.ndarray) -> np.ndarray:
        hits = np.full(len(tris), np.inf, dtype=np.float32)
        if not len(tris):
            return hits
        v0, e1, e2 = tris[:, 0], tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0]
        p = np.cross(direction, e2)
        det = np.einsum("ij,ij->i", e1, p)
        idx = np.flatnonzero(np.abs(det) > 1e-6)
        if not len(idx):
            return hits
        tvec, inv = origin - v0[idx], 1.0 / det[idx]
        p = p[idx]
        u = np.einsum("ij,ij->i", tvec, p) * inv
        keep = np.flatnonzero((u >= 0.0) & (u <= 1.0))
        if not len(keep):
            return hits
        idx, tvec, inv, u = idx[keep], tvec[keep], inv[keep], u[keep]
        q = np.cross(tvec, e1[idx])
        v = np.einsum("j,ij->i", direction, q) * inv
        keep = np.flatnonzero((v >= 0.0) & (u + v <= 1.0))
        if not len(keep):
            return hits
        idx, q, inv = idx[keep], q[keep], inv[keep]
        t = np.einsum("ij,ij->i", e2[idx], q) * inv
        keep = t > 1e-4
        hits[idx[keep]] = t[keep]
        return hits

    def _begin_gizmo_drag(self, event) -> bool:
        handle = self._pick_gizmo_axis(self._screen_pos(event))
        if handle is None or self._selection_center is None:
            return False
        origin = self._project_gizmo_point(self._selection_center)
        if origin is None:
            return False
        if isinstance(handle, tuple):
            ends = [self._project_gizmo_point(self._selection_center + GIZMO_AXES[axis] * self._gizmo_size()) for axis in handle]
            if self._gizmo_mode != "position" or any(end is None for end in ends):
                return False
            basis = np.column_stack([end - origin for end in ends]).astype(np.float32)
            if abs(float(np.linalg.det(basis))) <= 1e-6:
                return False
            screen_axis = None
        else:
            end = self._project_gizmo_point(self._selection_center + GIZMO_AXES[handle] * self._gizmo_size())
            if end is None:
                return False
            screen_axis = end - origin
            if np.linalg.norm(screen_axis) <= 1e-6:
                return False
            if self._gizmo_mode == "rotation":
                screen_axis = np.array((-screen_axis[1], screen_axis[0]), dtype=np.float32)
        self._gizmo_drag = {
            "mode": self._gizmo_mode,
            "axis": handle,
            "start": self._screen_pos(event),
            "center": self._selection_center.copy(),
            "size": self._gizmo_size(),
            "extent": max(float(self._selection_extent), 1e-3),
            "screen_axis": None if screen_axis is None else screen_axis / np.linalg.norm(screen_axis),
            "matrices": {mesh.key: np.asarray(mesh.transform_matrix if mesh.transform_matrix is not None else np.identity(4), dtype=np.float32) for mesh in self._meshes if mesh.key in self._selection_keys},
        }
        if isinstance(handle, tuple):
            self._gizmo_drag["plane_axes"] = handle
            self._gizmo_drag["screen_basis_inv"] = np.linalg.inv(basis)
        if not self._gizmo_drag["matrices"]:
            self._gizmo_drag = None
            return False
        self._set_hover_key("")
        self._gizmo_drag["whole_scene"] = self._selection_is_whole_scene()
        if not self._gizmo_drag["whole_scene"] and len(self._gizmo_drag["matrices"]) == 1:
            self._gizmo_drag["regular_geometry"] = self._capture_gizmo_geometry(self._regular_set)
            self._gizmo_drag["solid_geometry"] = self._capture_gizmo_geometry(self._solid_set)
            if not (self._gizmo_drag["regular_geometry"] or self._gizmo_drag["solid_geometry"]):
                self._gizmo_drag = None
                raise RuntimeError("Selected renderable is missing from scene buffers")
        self._gizmo_drag["deferred_geometry"] = not self._gizmo_drag["whole_scene"] and len(self._gizmo_drag["matrices"]) > 1
        self._gizmo_hover_axis = handle
        if self._gizmo_drag["deferred_geometry"]:
            self._refresh_selection_buffer_sets()
            self._refresh_main_index_sets()
        return True

    def _drag_gizmo(self, event) -> None:
        drag = self._gizmo_drag
        if not drag:
            return
        delta = self._screen_pos(event) - drag["start"]
        if "plane_axes" in drag:
            coeff = drag["screen_basis_inv"] @ delta
            axes = drag["plane_axes"]
            matrix = self._translation_matrix((GIZMO_AXES[axes[0]] * coeff[0] + GIZMO_AXES[axes[1]] * coeff[1]) * drag["size"])
        else:
            axis = GIZMO_AXES[drag["axis"]]
            amount = float(delta @ drag["screen_axis"]) / 120.0 * drag["size"]
            if drag["mode"] == "position":
                matrix = self._translation_matrix(axis * amount)
            elif drag["mode"] == "scale":
                scale = np.ones(3, dtype=np.float32)
                scale[drag["axis"]] = max(0.05, 1.0 + amount / drag["extent"])
                matrix = self._around_pivot(drag["center"], self._scale_matrix(scale))
            else:
                matrix = self._around_pivot(drag["center"], self._rotation_matrix(axis, np.deg2rad(float(delta @ drag["screen_axis"]) * 0.8)))
        matrix = matrix.astype(np.float32, copy=False)
        self._selection_center = self._transform_point(drag["center"], matrix)
        self._selection_extent = drag["extent"] if drag["mode"] != "scale" else max(drag["extent"] * np.linalg.norm(matrix[:3, :3], axis=0).max(), 1.0)
        drag["matrix"] = matrix
        if not drag.get("whole_scene") and not drag.get("deferred_geometry"):
            self._apply_gizmo_drag_matrix(matrix)
        self.update()

    def _finish_gizmo_drag(self, *, commit: bool) -> None:
        drag = self._gizmo_drag
        if drag is None:
            return
        matrix = drag.get("matrix")
        if commit and matrix is not None:
            for mesh in self._meshes:
                start = drag["matrices"].get(mesh.key)
                if start is not None:
                    mesh.transform_matrix = (matrix @ start).astype(np.float32, copy=False)
            if drag.get("whole_scene"):
                self._scene_matrix = (matrix @ self._scene_matrix).astype(np.float32, copy=False)
            elif drag.get("deferred_geometry"):
                self._commit_deferred_gizmo_geometry(matrix)
        elif not commit and not drag.get("whole_scene") and not drag.get("deferred_geometry"):
            self._apply_gizmo_drag_matrix(IDENTITY4)
        deferred = bool(drag.get("deferred_geometry"))
        moved_geometry = bool(matrix is not None and not drag.get("whole_scene"))
        self._gizmo_drag = None
        if deferred:
            self._refresh_main_index_sets()
        if moved_geometry:
            self._rebuild_pick_entries()
            self._refresh_hover_colors()
        self._refresh_selection_bounds()
        self.update()

    @staticmethod
    def _transform_point(point: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        hom = np.append(np.asarray(point, dtype=np.float32)[:3], np.float32(1.0))
        return (np.asarray(matrix, dtype=np.float32) @ hom)[:3].astype(np.float32)

    @staticmethod
    def _transform_points(points: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        hom = np.concatenate([np.asarray(points, dtype=np.float32), np.ones((len(points), 1), dtype=np.float32)], axis=1)
        return (hom @ np.asarray(matrix, dtype=np.float32).T)[:, :3].astype(np.float32, copy=False)

    def _capture_gizmo_geometry(self, buffer_set: _GlBufferSet | None) -> dict | None:
        if buffer_set is None or buffer_set.data is None:
            return None
        indices, _batches, _lines = scene_key_index_buffers(buffer_set.data, self._selection_keys, include_lines=False)
        if not len(indices):
            return None
        vertices = np.unique(indices)
        return {
            "indices": vertices,
            "vertices": buffer_set.data.vertices[vertices].copy(),
            "normals": buffer_set.data.normals[vertices].copy() if buffer_set.data.normals is not None else None,
        }

    def _buffer_space_matrix(self, matrix: np.ndarray) -> np.ndarray:
        if np.array_equal(self._scene_matrix, IDENTITY4):
            return matrix
        try:
            return (np.linalg.inv(self._scene_matrix) @ matrix @ self._scene_matrix).astype(np.float32, copy=False)
        except np.linalg.LinAlgError:
            return matrix

    def _apply_gizmo_drag_matrix(self, matrix: np.ndarray) -> None:
        drag = self._gizmo_drag or {}
        matrix = self._buffer_space_matrix(matrix)
        made_current = self.context() is not None
        if made_current:
            self.makeCurrent()
        self._apply_gizmo_snapshot(self._regular_set, drag.get("regular_geometry"), matrix, current=made_current)
        self._apply_gizmo_snapshot(self._solid_set, drag.get("solid_geometry"), matrix, current=made_current)
        if made_current:
            self.doneCurrent()

    def _commit_deferred_gizmo_geometry(self, matrix: np.ndarray) -> None:
        matrix = self._buffer_space_matrix(matrix)
        made_current = self.context() is not None
        if made_current:
            self.makeCurrent()
        self._apply_gizmo_snapshot(self._regular_set, self._capture_gizmo_geometry(self._regular_set), matrix, current=made_current)
        self._apply_gizmo_snapshot(self._solid_set, self._capture_gizmo_geometry(self._solid_set), matrix, current=made_current)
        if made_current:
            self.doneCurrent()

    def _apply_gizmo_snapshot(self, buffer_set: _GlBufferSet | None, snapshot: dict | None, matrix: np.ndarray, *, current: bool = False) -> None:
        if buffer_set is None or snapshot is None:
            return
        indices = snapshot["indices"]
        buffer_set.data.vertices[indices] = self._transform_points(snapshot["vertices"], matrix)
        if snapshot["normals"] is not None and buffer_set.data.normals is not None:
            try:
                normal_matrix = np.linalg.inv(np.asarray(matrix, dtype=np.float32)[:3, :3]).T.astype(np.float32)
            except np.linalg.LinAlgError:
                normal_matrix = np.identity(3, dtype=np.float32)
            buffer_set.data.normals[indices] = (snapshot["normals"] @ normal_matrix.T).astype(np.float32, copy=False)
        if self.context() is not None:
            if not current:
                self.makeCurrent()
            self._upload_changed_geometry(buffer_set, indices)
            if not current:
                self.doneCurrent()

    @staticmethod
    def _translation_matrix(offset: np.ndarray) -> np.ndarray:
        matrix = np.identity(4, dtype=np.float32)
        matrix[:3, 3] = np.asarray(offset, dtype=np.float32)[:3]
        return matrix

    @staticmethod
    def _scale_matrix(scale: np.ndarray) -> np.ndarray:
        matrix = np.identity(4, dtype=np.float32)
        matrix[0, 0], matrix[1, 1], matrix[2, 2] = float(scale[0]), float(scale[1]), float(scale[2])
        return matrix

    @staticmethod
    def _rotation_matrix(axis: np.ndarray, angle: float) -> np.ndarray:
        x, y, z = np.asarray(axis, dtype=np.float32)
        c, s, t = np.cos(angle), np.sin(angle), 1.0 - np.cos(angle)
        matrix = np.identity(4, dtype=np.float32)
        matrix[:3, :3] = np.array(
            ((t*x*x + c, t*x*y - s*z, t*x*z + s*y), (t*x*y + s*z, t*y*y + c, t*y*z - s*x), (t*x*z - s*y, t*y*z + s*x, t*z*z + c)),
            dtype=np.float32,
        )
        return matrix

    def _around_pivot(self, pivot: np.ndarray, matrix: np.ndarray) -> np.ndarray:
        return self._translation_matrix(pivot) @ matrix @ self._translation_matrix(-pivot)

    def _gizmo_circle_points(self, center: np.ndarray, radius: float):
        angles = np.linspace(0.0, np.pi * 2.0, 49, dtype=np.float32)
        circle = np.stack((np.cos(angles), np.sin(angles)), axis=1) * radius
        return (
            center + np.column_stack((np.zeros(len(circle)), circle[:, 0], circle[:, 1])),
            center + np.column_stack((circle[:, 0], np.zeros(len(circle)), circle[:, 1])),
            center + np.column_stack((circle[:, 0], circle[:, 1], np.zeros(len(circle)))),
        )

    @staticmethod
    def _axis_perps(axis_index: int) -> tuple[np.ndarray, np.ndarray]:
        axes = GIZMO_AXES
        return axes[(axis_index + 1) % 3], axes[(axis_index + 2) % 3]

    def _gizmo_color(self, axis_index: int) -> tuple[float, float, float, float]:
        color = np.array(GIZMO_COLORS[axis_index], dtype=np.float32)
        if axis_index == self._gizmo_active_axis():
            color = np.clip(color * 1.25 + 0.35, 0.0, 1.0)
        return float(color[0]), float(color[1]), float(color[2]), 1.0

    def _gizmo_active_axis(self) -> int | None:
        return self._gizmo_drag["axis"] if self._gizmo_drag else self._gizmo_hover_axis

    def _draw_axis_arrow(self, center: np.ndarray, axis_index: int, size: float) -> None:
        axis = GIZMO_AXES[axis_index]
        p1, p2 = self._axis_perps(axis_index)
        end = center + axis * size
        base = end - axis * size * 0.16
        radius = size * (0.07 if axis_index == self._gizmo_active_axis() else 0.05)
        glLineWidth(4.0 if axis_index == self._gizmo_active_axis() else 2.4)
        glColor4f(*self._gizmo_color(axis_index))
        glBegin(GL_LINES)
        glVertex3f(float(center[0]), float(center[1]), float(center[2]))
        glVertex3f(float(base[0]), float(base[1]), float(base[2]))
        glEnd()
        ring = (base + p1 * radius, base + p2 * radius, base - p1 * radius, base - p2 * radius)
        glBegin(GL_TRIANGLES)
        for a, b in zip(ring, ring[1:] + ring[:1]):
            for point in (end, a, b):
                glVertex3f(float(point[0]), float(point[1]), float(point[2]))
        glEnd()

    def _draw_box(self, corners) -> None:
        glBegin(GL_QUADS)
        for face in GIZMO_BOX_FACES:
            for index in face:
                point = corners[index]
                glVertex3f(float(point[0]), float(point[1]), float(point[2]))
        glEnd()

    def _draw_scale_axis(self, center: np.ndarray, axis_index: int, size: float) -> None:
        self._draw_axis_arrow(center, axis_index, size * 0.82)
        axis = GIZMO_AXES[axis_index]
        p1, p2 = self._axis_perps(axis_index)
        end = center + axis * size
        half = size * (0.085 if axis_index == self._gizmo_active_axis() else 0.065)
        glColor4f(*self._gizmo_color(axis_index))
        corners = (
            end + axis * half + p1 * half + p2 * half, end + axis * half + p1 * half - p2 * half,
            end + axis * half - p1 * half - p2 * half, end + axis * half - p1 * half + p2 * half,
            end - axis * half + p1 * half + p2 * half, end - axis * half + p1 * half - p2 * half,
            end - axis * half - p1 * half - p2 * half, end - axis * half - p1 * half + p2 * half,
        )
        self._draw_box(corners)

    def _draw_translate_planes(self, center: np.ndarray, size: float) -> None:
        active = self._gizmo_active_axis()
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        for a, b, color in GIZMO_PLANES:
            selected = active == (a, b) or active == (b, a)
            rgba = np.array(color, dtype=np.float32)
            if selected:
                rgba[:3] = np.clip(rgba[:3] * 1.2 + 0.25, 0.0, 1.0)
                rgba[3] = min(float(rgba[3]) * 1.9, 0.55)
            glColor4f(float(rgba[0]), float(rgba[1]), float(rgba[2]), float(rgba[3]))
            glBegin(GL_QUADS)
            for point in self._gizmo_plane_corners(center, size, a, b):
                glVertex3f(float(point[0]), float(point[1]), float(point[2]))
            glEnd()

    def _draw_center_cube(self, center: np.ndarray, size: float) -> None:
        x, y, z = GIZMO_AXES * size
        corners = (
            center + x + y + z, center + x + y - z, center + x - y - z, center + x - y + z,
            center - x + y + z, center - x + y - z, center - x - y - z, center - x - y + z,
        )
        glColor4f(0.95, 0.95, 0.9, 1.0)
        self._draw_box(corners)

    def _draw_gizmo(self) -> None:
        if self._controls == "mesh" or self._selection_center is None:
            return
        center = self._selection_center
        size = self._gizmo_size()
        glDisable(GL_LIGHTING)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_CULL_FACE)
        glDepthMask(False)
        if self._gizmo_mode == "rotation":
            for axis_index, points in enumerate(self._gizmo_circle_points(center, size)):
                glLineWidth(4.5 if axis_index == self._gizmo_active_axis() else 2.4)
                glColor4f(*self._gizmo_color(axis_index))
                glBegin(GL_LINE_STRIP)
                for point in points:
                    glVertex3f(float(point[0]), float(point[1]), float(point[2]))
                glEnd()
        elif self._gizmo_mode == "scale":
            for axis_index in range(3):
                self._draw_scale_axis(center, axis_index, size)
        else:
            self._draw_translate_planes(center, size)
            for axis_index in range(3):
                self._draw_axis_arrow(center, axis_index, size)
        self._draw_center_cube(center, size * 0.045)
        glLineWidth(1.0)
        glDepthMask(True)
        glDisable(GL_BLEND)
        glEnable(GL_CULL_FACE)
        glEnable(GL_DEPTH_TEST)
        if self.lighting_mode == "fixed":
            glEnable(GL_LIGHTING)

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
        self._hover_vertex_cache.clear()
        self._rebuild_pick_entries()

    def _rebuild_pick_entries(self) -> None:
        self._pick_entries.clear()
        mins_list, maxs_list, keys = [], [], []
        for data in (self._regular_data, self._solid_data):
            if data is None or not len(data.vertices):
                continue
            by_key: dict[str, list[np.ndarray]] = {}
            for key, _material, indices in data.triangle_chunks:
                if key and len(indices) >= 3:
                    by_key.setdefault(str(key), []).append(indices)
            for key, chunks in by_key.items():
                indices = np.concatenate(chunks)
                usable = (len(indices) // 3) * 3
                if usable < 3:
                    continue
                triangles = np.asarray(indices[:usable], dtype=np.uint32).reshape(-1, 3)
                points = data.vertices[np.unique(triangles)]
                if len(points):
                    mins, maxs = points.min(axis=0), points.max(axis=0)
                    self._pick_entries.append((key, mins, maxs, data.vertices, triangles))
                    keys.append(key)
                    mins_list.append(mins)
                    maxs_list.append(maxs)
        self._pick_bound_keys = keys
        self._pick_bound_mins = np.asarray(mins_list, dtype=np.float32) if mins_list else np.zeros((0, 3), dtype=np.float32)
        self._pick_bound_maxs = np.asarray(maxs_list, dtype=np.float32) if maxs_list else np.zeros((0, 3), dtype=np.float32)

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
        buffer_set.colors = None
        self._delete_index_vbos(buffer_set)

    def _delete_selection_buffer_sets(self) -> None:
        self._delete_index_vbos(self._selection_regular_set)
        self._delete_index_vbos(self._selection_solid_set)
        self._selection_regular_set = self._selection_solid_set = None

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
        buffer_set.index_count = buffer_set.line_count = 0
        buffer_set.lines_ready = False

    def _display_colors(self, buffer_set: SceneBufferSet) -> np.ndarray | None:
        colors = self._base_display_colors(buffer_set)
        if self._controls != "mesh" and self._hover_key:
            used = self._hover_vertices(buffer_set, self._hover_key)
            if len(used):
                colors = np.ones((len(buffer_set.vertices), 4), dtype=np.float32) if colors is None else colors.copy()
                self._tint_hover_rows(colors, used)
        return colors

    def _base_display_colors(self, buffer_set: SceneBufferSet) -> np.ndarray | None:
        return display_colors(
            buffer_set,
            color_source=self.color_source,
            lighting_mode=self.lighting_mode,
            ambient=self.ambient,
            diffuse=self.diffuse,
        )

    def _refresh_hover_colors(self, old_key: str = "", new_key: str = "", *, current: bool = False) -> None:
        if self.context() is None:
            self._colors_dirty = True
            return
        if not current:
            self.makeCurrent()
        if old_key or new_key:
            for buffer_set in (self._regular_set, self._solid_set):
                self._update_hover_colors(buffer_set, old_key, new_key)
        else:
            self._refresh_color_vbos()
        if not current:
            self.doneCurrent()

    def _update_hover_colors(self, buffer_set: _GlBufferSet | None, old_key: str, new_key: str) -> None:
        if buffer_set is None or buffer_set.data is None:
            return
        old_rows = self._hover_vertices(buffer_set.data, old_key)
        new_rows = self._hover_vertices(buffer_set.data, new_key)
        if not len(old_rows) and not len(new_rows):
            return
        if not new_key and not self._needs_base_color_buffer(buffer_set.data):
            self._dispose_vbo(buffer_set.colors_vbo)
            buffer_set.colors_vbo = buffer_set.colors = None
            return
        self._ensure_color_buffer(buffer_set)
        if buffer_set.colors is None or buffer_set.colors_vbo is None:
            return
        changed = np.unique(np.concatenate([old_rows, new_rows]))
        buffer_set.colors[changed] = self._display_color_rows(buffer_set.data, changed)
        if len(new_rows):
            self._tint_hover_rows(buffer_set.colors, new_rows)
        self._upload_changed_colors(buffer_set, changed)

    def _ensure_color_buffer(self, buffer_set: _GlBufferSet) -> None:
        if buffer_set.colors is not None and buffer_set.colors_vbo is not None:
            return
        colors = self._base_display_colors(buffer_set.data)
        if colors is None:
            colors = np.ones((len(buffer_set.data.vertices), 4), dtype=np.float32)
            for _key, material_name, indices in buffer_set.data.triangle_chunks:
                if tint := self._material_tints.get(material_name):
                    colors[indices] = tint
        buffer_set.colors = colors
        buffer_set.colors_vbo = self._array_vbo(buffer_set.colors)

    def _needs_base_color_buffer(self, buffer_set: SceneBufferSet) -> bool:
        return self.lighting_mode == "software" or (self.color_source == "vertex" and buffer_set.base_colors is not None)

    def _hover_vertices(self, buffer_set: SceneBufferSet, key: str) -> np.ndarray:
        if not key:
            return np.zeros((0,), dtype=np.uint32)
        by_key = self._hover_vertex_cache.setdefault(id(buffer_set), {})
        if key not in by_key:
            chunks = [idx for chunk_key, _material, idx in buffer_set.triangle_chunks if chunk_key == key]
            by_key[key] = np.unique(np.concatenate(chunks)).astype(np.uint32, copy=False) if chunks else np.zeros((0,), dtype=np.uint32)
        return by_key[key]

    def _display_color_rows(self, buffer_set: SceneBufferSet, rows: np.ndarray) -> np.ndarray:
        if self.color_source == "vertex" and buffer_set.base_colors is not None:
            base = buffer_set.base_colors[rows].copy()
        else:
            base = np.ones((len(rows), 4), dtype=np.float32)
            for _key, material_name, indices in buffer_set.triangle_chunks:
                if tint := self._material_tints.get(material_name):
                    base[np.isin(rows, indices)] = tint
        if self.lighting_mode != "software":
            return base
        normals = buffer_set.normals[rows] if buffer_set.normals is not None else np.tile(np.array((0.0, 0.0, 1.0), dtype=np.float32), (len(rows), 1))
        light_dir = np.array((0.4, 0.8, 0.4), dtype=np.float32)
        light_dir /= np.linalg.norm(light_dir) or 1.0
        base[:, :3] *= np.clip((normals @ light_dir) * float(self.diffuse) + float(self.ambient), 0.0, 1.0)[:, np.newaxis]
        base[:, 3] = 1.0
        return base

    @staticmethod
    def _tint_hover_rows(colors: np.ndarray, rows: np.ndarray) -> None:
        colors[rows, :3] = colors[rows, :3] * 0.65 + np.array((0.05, 1.0, 0.25), dtype=np.float32) * 0.35
        colors[rows, 3] = 1.0

    def _upload_buffer_set(self, data: SceneBufferSet | None) -> _GlBufferSet | None:
        if data is None:
            return None
        buffer_set = _GlBufferSet(data=data)
        buffer_set.vertices_vbo = self._array_vbo(data.vertices) if len(data.vertices) else None
        buffer_set.normals_vbo = self._array_vbo(data.normals) if data.normals is not None else None
        colors = self._display_colors(data)
        buffer_set.colors = colors
        buffer_set.colors_vbo = self._array_vbo(colors) if colors is not None else None
        buffer_set.uvs_vbo = self._array_vbo(data.uvs) if data.uvs is not None else None
        self._upload_index_vbos(buffer_set)
        return buffer_set

    def _upload_index_view(self, source: _GlBufferSet | None, keys: set[str]) -> _GlBufferSet | None:
        if source is None or source.data is None or not keys:
            return None
        view = _GlBufferSet(
            data=source.data,
            vertices_vbo=source.vertices_vbo,
            normals_vbo=source.normals_vbo,
            colors=source.colors,
            colors_vbo=source.colors_vbo,
            uvs_vbo=source.uvs_vbo,
        )
        indices, batches, line_indices = scene_key_index_buffers(source.data, keys, include_lines=self._needs_line_indices())
        self._set_index_buffers(view, indices, batches, line_indices, self._needs_line_indices())
        return view

    def _refresh_selection_buffer_sets(self, *, current: bool = False) -> None:
        if self.context() is None:
            return
        if not current:
            self.makeCurrent()
        self._delete_selection_buffer_sets()
        if not self._selection_is_whole_scene():
            keys = self._selection_keys - self._hidden_keys
            self._selection_regular_set = self._upload_index_view(self._regular_set, keys)
            self._selection_solid_set = self._upload_index_view(self._solid_set, keys)
        if not current:
            self.doneCurrent()

    def _refresh_main_index_sets(self, *, current: bool = False) -> None:
        if self.context() is None:
            self.update()
            return
        if not current:
            self.makeCurrent()
        self._upload_index_vbos(self._regular_set)
        self._upload_index_vbos(self._solid_set)
        self._refresh_hover_colors(current=True)
        if not current:
            self.doneCurrent()

    def _active_hidden_keys(self) -> set[str]:
        drag = self._gizmo_drag
        if drag and drag.get("deferred_geometry"):
            return self._hidden_keys | self._selection_keys
        return self._hidden_keys

    def _upload_index_vbos(self, buffer_set: _GlBufferSet | None):
        if buffer_set is None:
            return
        self._delete_index_vbos(buffer_set)
        need_lines = self._needs_line_indices()
        indices, batches, line_indices = scene_index_buffers(buffer_set.data, self._active_hidden_keys(), include_lines=need_lines)
        self._set_index_buffers(buffer_set, indices, batches, line_indices, need_lines)

    def _set_index_buffers(self, buffer_set: _GlBufferSet, indices: np.ndarray, batches, line_indices: np.ndarray, need_lines: bool) -> None:
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
        _indices, _batches, line_indices = scene_index_buffers(buffer_set.data, self._active_hidden_keys(), include_lines=True)
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
            buffer_set.colors = colors
            buffer_set.colors_vbo = self._array_vbo(colors) if colors is not None else None
        self._colors_dirty = False

    @staticmethod
    def _index_ranges(indices: np.ndarray):
        if not len(indices):
            return
        start = prev = int(indices[0])
        for value in indices[1:]:
            value = int(value)
            if value != prev + 1:
                yield start, prev + 1
                start = value
            prev = value
        yield start, prev + 1

    def _upload_changed_geometry(self, buffer_set: _GlBufferSet | None, vertices: np.ndarray | None) -> None:
        if buffer_set is None or vertices is None or not len(vertices):
            return
        for handle, data in ((buffer_set.vertices_vbo, buffer_set.data.vertices), (buffer_set.normals_vbo, buffer_set.data.normals)):
            self._upload_changed_array(handle, data, vertices)

    def _upload_changed_colors(self, buffer_set: _GlBufferSet, rows: np.ndarray) -> None:
        self._upload_changed_array(buffer_set.colors_vbo, buffer_set.colors, rows)

    def _upload_changed_array(self, handle, data: np.ndarray | None, rows: np.ndarray) -> None:
        if handle is None or data is None or not len(rows):
            return
        handle.bind()
        stride = int(data.strides[0])
        for start, end in self._index_ranges(rows):
            chunk = np.ascontiguousarray(data[start:end])
            glBufferSubData(GL_ARRAY_BUFFER, start * stride, chunk.nbytes, chunk)
        glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _upload_buffers(self, *, rebuild: bool = True, current: bool = False):
        if rebuild:
            self._rebuild_buffer_data()
            if not (self._gizmo_drag and self._gizmo_drag.get("whole_scene")):
                self._scene_matrix = IDENTITY4.copy()
        if self.context() is None:
            return
        if not current:
            self.makeCurrent()
        self._delete_selection_buffer_sets()
        self._delete_buffer_set(self._regular_set)
        self._delete_buffer_set(self._solid_set)
        self._regular_set = self._upload_buffer_set(self._regular_data)
        self._solid_set = self._upload_buffer_set(self._solid_data)
        self._refresh_selection_buffer_sets(current=True)
        self._colors_dirty = False
        if not current:
            self.doneCurrent()
        self._needs_gl_upload = False

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

    def _cleanup_gl(self, context=None):
        if context is not None and context is not self._gl_cleanup_context:
            return
        current = self.context()
        made_current = False
        with suppress(Exception):
            if current is not None:
                self.makeCurrent()
                made_current = True
                self._clear_gl_textures()
        self._delete_buffer_set(self._regular_set)
        self._delete_buffer_set(self._solid_set)
        self._delete_selection_buffer_sets()
        with suppress(Exception):
            self._cleanup_extra_gl()
        if made_current:
            with suppress(Exception):
                self.doneCurrent()
        self._regular_set = self._solid_set = self._gl_cleanup_context = None
        self._needs_gl_upload = self._regular_data is not None or self._solid_data is not None

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
        glClearColor(self._background[0], self._background[1], self._background[2], 1.0)
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
            context.aboutToBeDestroyed.connect(lambda ctx=context: self._cleanup_gl(ctx))
            self._gl_cleanup_context = context
        if self._regular_data is None and self._solid_data is None:
            self._rebuild_buffer_data()
        elif not self._pick_entries:
            self._rebuild_pick_entries()
        self._regular_set = self._upload_buffer_set(self._regular_data)
        self._solid_set = self._upload_buffer_set(self._solid_data)
        self._refresh_selection_buffer_sets(current=True)
        self._sync_gl_textures()
        self._needs_gl_upload = False
        self._after_gl_initialized()

    def resizeGL(self, w: int, h: int):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(w * dpr), int(h * dpr))
        self.place_viewport_overlays()

    def place_viewport_overlays(self):
        margin = 12
        self.overlay.adjustSize()
        if getattr(self.overlay, "viewport_anchor", "") == "manual":
            self._clamp_overlay(self.overlay, margin)
        else:
            self.overlay.move(margin, margin)
        self.overlay.raise_()
        for widget in self.children():
            if not isinstance(widget, QWidget):
                continue
            anchor = getattr(widget, "viewport_anchor", "")
            if anchor == "right":
                width = min(max(widget.width(), widget.minimumWidth()), widget.maximumWidth(), self.width() - margin * 2)
                height = min(max(widget.height(), widget.minimumHeight()), widget.maximumHeight(), self.height() - margin * 2)
                widget.setGeometry(max(margin, self.width() - width - margin), margin, width, height)
                widget.raise_()
            elif anchor == "manual":
                self._clamp_overlay(widget, margin)

    def _clamp_overlay(self, widget: QWidget, margin: int) -> None:
        x = max(margin, min(widget.x(), max(margin, self.width() - widget.width() - margin)))
        y = max(margin, min(widget.y(), max(margin, self.height() - widget.height() - margin)))
        widget.move(x, y)
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
        glDisable(GL_BLEND)
        glColorMask(True, True, True, False)
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
        if buffer_set.colors_vbo is not None:
            glDisableClientState(GL_COLOR_ARRAY)
        if buffer_set.normals_vbo is not None:
            glDisableClientState(GL_NORMAL_ARRAY)
        glDisableClientState(GL_VERTEX_ARRAY)
        glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _draw_triangles(self, buffer_set: _GlBufferSet | None, *, use_textures: bool = True):
        if buffer_set is None:
            return
        if buffer_set.vertices_vbo is not None and buffer_set.indices_vbo is not None:
            textured = self._bind_arrays(buffer_set, use_textures=use_textures)
            glColor4f(1.0, 1.0, 1.0, 1.0)

            if buffer_set.batch_vbos and (textured or self._material_tints or self._two_sided_materials):
                for material_name, batch_vbo, count in buffer_set.batch_vbos:
                    tex_id = self._texture_ids.get(material_name)
                    tint = self._material_tints.get(material_name, (1.0, 1.0, 1.0, 1.0))
                    glColor4f(tint[0], tint[1], tint[2], 1.0)
                    glDisable(GL_CULL_FACE) if material_name in self._two_sided_materials else glEnable(GL_CULL_FACE)
                    if textured and tex_id:
                        glEnable(GL_TEXTURE_2D)
                        glBindTexture(GL_TEXTURE_2D, tex_id)
                    else:
                        glBindTexture(GL_TEXTURE_2D, 0)
                        glDisable(GL_TEXTURE_2D)
                    batch_vbo.bind()
                    glDrawElements(GL_TRIANGLES, count, GL_UNSIGNED_INT, None)
                    glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)
                glEnable(GL_CULL_FACE)
                glColor4f(1.0, 1.0, 1.0, 1.0)
            else:
                glBindTexture(GL_TEXTURE_2D, 0)
                glDisable(GL_TEXTURE_2D)
                buffer_set.indices_vbo.bind()
                glDrawElements(GL_TRIANGLES, buffer_set.index_count, GL_UNSIGNED_INT, None)
                glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)

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
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)
            glDisableClientState(GL_VERTEX_ARRAY)
            if use_vertex_colors:
                glDisableClientState(GL_COLOR_ARRAY)
            glBindBuffer(GL_ARRAY_BUFFER, 0)
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

    def _draw_deferred_selection(self) -> bool:
        drag = self._gizmo_drag
        if not (drag and drag.get("deferred_geometry") and drag.get("matrix") is not None):
            return False
        glPushMatrix()
        glMultMatrixf(np.ascontiguousarray(self._buffer_space_matrix(drag["matrix"]).T, dtype=np.float32))
        if self.render_mode in {"solid", "hybrid"}:
            self._draw_triangles(self._selection_regular_set, use_textures=True)
        if self.render_mode in {"wire", "hybrid"}:
            self._draw_lines(self._selection_regular_set, overlay=False)
        self._draw_triangles(self._selection_solid_set, use_textures=False)
        self._draw_selection_glow()
        glPopMatrix()
        return True

    def _draw_selection_glow(self):
        if not self._selection_keys or (self._selection_regular_set is None and self._selection_solid_set is None):
            return
        if sum(buffer_set.index_count for buffer_set in (self._selection_regular_set, self._selection_solid_set) if buffer_set) > 700_000:
            return
        alpha = 0.12 + 0.10 * (0.5 + 0.5 * np.sin(time.perf_counter() * 3.5))
        self._draw_overlay_sets((self._selection_regular_set, self._selection_solid_set), (1.0, 0.86, 0.18, float(alpha)))

    def _draw_overlay_sets(self, buffer_sets, color):
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(False)
        glDisable(GL_DEPTH_TEST)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_LIGHTING)
        glDisableClientState(GL_NORMAL_ARRAY)
        glDisableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        glColor4f(*color)
        for buffer_set in buffer_sets:
            if buffer_set is None or buffer_set.vertices_vbo is None or buffer_set.indices_vbo is None:
                continue
            buffer_set.vertices_vbo.bind()
            glEnableClientState(GL_VERTEX_ARRAY)
            glVertexPointer(3, GL_FLOAT, 0, None)
            buffer_set.indices_vbo.bind()
            glDrawElements(GL_TRIANGLES, buffer_set.index_count, GL_UNSIGNED_INT, None)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)
            glDisableClientState(GL_VERTEX_ARRAY)
            glBindBuffer(GL_ARRAY_BUFFER, 0)
        glDepthMask(True)
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        if self.lighting_mode == "fixed":
            glEnable(GL_LIGHTING)

    def _active_scene_matrix(self) -> np.ndarray | None:
        matrix = self._scene_matrix
        drag = self._gizmo_drag
        if drag and drag.get("whole_scene") and drag.get("matrix") is not None:
            matrix = drag["matrix"] @ matrix
        return None if np.allclose(matrix, IDENTITY4) else matrix

    def _restore_gl_content(self) -> None:
        missing_regular = self._regular_set is None and self._regular_data is not None
        missing_solid = self._solid_set is None and self._solid_data is not None
        if self._needs_gl_upload or missing_regular or missing_solid:
            self._upload_buffers(rebuild=False, current=True)
            self._sync_gl_textures()

    def paintGL(self):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(self.width() * dpr), int(self.height() * dpr))
        self._restore_gl_content()
        glColorMask(True, True, True, True)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self._apply_render_state()
        glLoadIdentity()
        glLightfv(GL_LIGHT0, GL_POSITION, (0.5, 1.0, 1.0, 0.0))
        self._apply_camera_transform()
        self._cache_gizmo_projection()

        scene_matrix = self._active_scene_matrix()
        if scene_matrix is not None:
            glPushMatrix()
            glMultMatrixf(np.ascontiguousarray(scene_matrix.T, dtype=np.float32))
        self._draw_regular_scene()
        self._draw_triangles(self._solid_set, use_textures=False)
        if not self._draw_deferred_selection():
            self._draw_selection_glow()
        if scene_matrix is not None:
            glPopMatrix()
        self._draw_gizmo()
        glColorMask(True, True, True, True)
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

    def _set_camera_setting(self, attr: str, key: str, value: float):
        setattr(self, attr, float(value))
        if attr == "camera_speed":
            self.freecam_speed = self._freecam_base_speed * self.camera_speed
        self._save_view_setting(key, float(value))

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
