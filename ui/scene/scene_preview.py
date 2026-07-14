from __future__ import annotations

import time
from contextlib import suppress
from ctypes import c_void_p
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
    GL_DITHER,
    GL_ARRAY_BUFFER,
    GL_ELEMENT_ARRAY_BUFFER,
    GL_EXTENSIONS,
    GL_FILL,
    GL_FLOAT,
    GL_FRONT_AND_BACK,
    GL_LIGHT0,
    GL_LIGHTING,
    GL_LINE,
    GL_LINES,
    GL_LINEAR,
    GL_LINEAR_MIPMAP_LINEAR,
    GL_LEQUAL,
    GL_LESS,
    GL_LINE_STRIP,
    GL_MODELVIEW,
    GL_MODELVIEW_MATRIX,
    GL_MODULATE,
    GL_MULTISAMPLE,
    GL_NO_ERROR,
    GL_NORMAL_ARRAY,
    GL_NORMALIZE,
    GL_ONE_MINUS_SRC_ALPHA,
    GL_POSITION,
    GL_POINTS,
    GL_PROJECTION,
    GL_PROJECTION_MATRIX,
    GL_QUADS,
    GL_RGBA,
    GL_SCISSOR_TEST,
    GL_SMOOTH,
    GL_SRC_ALPHA,
    GL_TEXTURE_2D,
    GL_TEXTURE_COORD_ARRAY,
    GL_TEXTURE_ENV,
    GL_TEXTURE_ENV_MODE,
    GL_TEXTURE_MAG_FILTER,
    GL_TEXTURE_MAX_LEVEL,
    GL_TEXTURE_MIN_FILTER,
    GL_TRIANGLES,
    GL_UNPACK_ALIGNMENT,
    GL_UNSIGNED_BYTE,
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
    glDepthFunc,
    glDisable,
    glDisableClientState,
    glDrawElements,
    glEnable,
    glEnableClientState,
    glEnd,
    glFrontFace,
    glGenTextures,
    glGetDoublev,
    glGetError,
    glGetFloatv,
    glGetIntegerv,
    glGetString,
    glLightfv,
    glLineWidth,
    glLoadIdentity,
    glMatrixMode,
    glMultMatrixf,
    glNormalPointer,
    glPixelStorei,
    glPointSize,
    glPolygonMode,
    glPopMatrix,
    glPushMatrix,
    glReadPixels,
    glRotatef,
    glScissor,
    glScalef,
    glShadeModel,
    glTexCoordPointer,
    glTexEnvi,
    glCompressedTexImage2D,
    glTexImage2D,
    glTexParameterf,
    glTexParameteri,
    glTranslatef,
    glVertex3f,
    glVertexPointer,
    glViewport,
)
from OpenGL.GLU import gluPerspective, gluProject
from PySide6.QtCore import QEvent, QThreadPool, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCursor, QImage
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from file_handlers.tex.qt_image_utils import TexPreviewUpload
from file_handlers.tex.texture_quality import (
    DEFAULT_TEXTURE_QUALITY,
    TEXTURE_QUALITY_PROFILES,
    normalize_texture_quality,
    texture_quality_profile,
)
from settings import save_settings
from ui.opengl_camera import OrbitCameraMixin
from .opengl_setup import mesh_surface_format
from .freecam_controller import FreecamController
from .scene_buffers import (
    SceneBufferSet,
    build_scene_buffer_set,
    display_colors,
    mesh_bounds_points,
    point_bounds,
    scene_bounds,
    scene_index_buffers,
    scene_key_index_buffers,
    transform_normals,
    transform_points,
)
from .lightprobe_preview import SceneLightProbeSet
from .lightprobe_shading import (
    ProbeShadeInput,
    ProbeShadeRequest,
    ProbeShadeWorker,
    snapshot_probe_boxes,
)
from .scene_model import SceneDrawMesh
from .viewport_overlay import ViewportOverlayManager


@dataclass(slots=True)
class _GlBufferSet:
    data: SceneBufferSet
    vertices_vbo: object | None = None
    normals_vbo: object | None = None
    colors: np.ndarray | None = None
    colors_vbo: object | None = None
    uvs_vbo: object | None = None
    span_indices_vbo: object | None = None
    indices_vbo: object | None = None
    line_indices_vbo: object | None = None
    batch_vbos: list[tuple[str, object, int]] = field(default_factory=list)
    index_count: int = 0
    line_count: int = 0
    lines_ready: bool = False


GIZMO_AXES = np.identity(3, dtype=np.float32)
GIZMO_COLORS = ((1.0, 0.18, 0.12), (0.2, 0.9, 0.25), (0.25, 0.45, 1.0))
GIZMO_BOX_FACES = ((0, 1, 2, 3), (4, 7, 6, 5), (0, 4, 5, 1), (1, 5, 6, 2), (2, 6, 7, 3), (3, 7, 4, 0))
BOX_EDGES = ((0, 1), (1, 2), (2, 3), (3, 0), (4, 5), (5, 6), (6, 7), (7, 4), (0, 4), (1, 5), (2, 6), (3, 7))
GIZMO_PLANES = ((0, 1, (1.0, 0.85, 0.12, 0.24)), (0, 2, (0.9, 0.15, 1.0, 0.22)), (1, 2, (0.15, 0.8, 1.0, 0.22)))
IDENTITY4 = np.identity(4, dtype=np.float32)
HOVER_PICK_INTERVAL = 1.0 / 15.0
HOVER_PICK_MIN_PIXELS = 4.0
HOVER_DETECT_KEY = Qt.Key_H
GL_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FE
GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT = 0x84FF


class ScenePreviewWidget(OrbitCameraMixin, QOpenGLWidget):
    object_clicked = Signal(str)
    gizmo_transform_committed = Signal(object)
    texture_quality_changed = Signal(str)
    texture_upload_status_changed = Signal()

    SETTINGS_DEFAULTS = {
        "mesh_viewer_fps_limit": 60,
        "mesh_viewer_wireframe_mode": "off",
        "mesh_viewer_lighting_mode": "fixed",
        "mesh_viewer_line_width": 1.5,
        "mesh_viewer_ambient": 0.35,
        "mesh_viewer_diffuse": 0.65,
        "scene_probe_exposure": 0.12,
        "scene_probe_viz_mode": "all",
        "scene_probe_viz_points": 12000,
        "mesh_viewer_show_bones": False,
        "renderer_texture_quality": DEFAULT_TEXTURE_QUALITY,
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
    LIGHTING_MODES = ("off", "fixed", "software", "probes")
    PROBE_VIZ_MODES = ("off", "volumes", "points", "all")
    PROBE_AUTO_ENABLE_VERTEX_LIMIT = 120000

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
        fmt = mesh_surface_format()
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
        self.freecam = FreecamController()
        self._cursor_lock_pos = self._fullscreen_restore = None

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
        self._pending_scene_pick: tuple[str, np.ndarray] | None = None
        self._last_hover_pick_time = 0.0
        self._last_hover_pick_pos: np.ndarray | None = None
        self._scene_matrix = IDENTITY4.copy()
        self._regular_data: SceneBufferSet | None = None
        self._solid_data: SceneBufferSet | None = None
        self._texture_ids: dict[str, int] = {}
        self._texture_sources: dict[str, str] = {}
        self._texture_source_ids: dict[str, int] = {}
        self._texture_upload_failures: set[str] = set()
        self._max_texture_anisotropy = 1.0
        self._anisotropy_limit_logs: set[tuple[str, float]] = set()
        self._pending_material_images: dict[str, tuple[str, TexPreviewUpload]] = {}
        self._material_tints: dict[str, tuple[float, float, float, float]] = {}
        self._two_sided_materials: set[str] = set()
        self._hidden_keys: set[str] = set()
        self._gl_cleanup_context = None
        self._needs_gl_upload = False

        self.render_mode = "wire" if self._controls == "mesh" else self._setting_choice("scene_render_mode", ("wire", "hybrid", "solid"))
        self._gizmo_mode = self._setting_choice("scene_gizmo_mode", ("position", "rotation", "scale"))
        self.show_only_highlighted = self._setting_bool("scene_show_only_highlighted")
        self._fps_limit = self._setting_int("mesh_viewer_fps_limit", 0, 240)
        self.texture_quality = self._setting_choice("renderer_texture_quality", tuple(TEXTURE_QUALITY_PROFILES))
        self.wireframe_mode = self._setting_choice("mesh_viewer_wireframe_mode", self.WIREFRAME_MODES)
        self.lighting_mode = self._setting_choice("mesh_viewer_lighting_mode", self.LIGHTING_MODES)
        self._light_probe_set: SceneLightProbeSet | None = None
        self._light_probe_status = ""
        self._light_probe_key = ""
        self._light_probe_obbs: list[object] = []
        self._probe_viz_candidate_indices_cache_key = None
        self._probe_viz_candidate_indices_cache: np.ndarray | None = None
        self._probe_viz_point_cache_key = None
        self._probe_viz_point_cache: tuple[np.ndarray, np.ndarray] | None = None
        self._probe_shade_pool = QThreadPool(self)
        self._probe_shade_pool.setMaxThreadCount(1)
        self._probe_shade_worker: ProbeShadeWorker | None = None
        self._probe_shade_job_id = 0
        self._probe_shade_result: tuple[tuple, dict[int, np.ndarray]] | None = None
        self._probe_shade_error = ""
        self._probe_shade_percent = 0
        self.line_width = self._setting_float("mesh_viewer_line_width", 0.5, 8.0)
        self.color_source = "vertex"
        self.ambient = self._setting_float("mesh_viewer_ambient", 0.0, 1.0)
        self.diffuse = self._setting_float("mesh_viewer_diffuse", 0.0, 1.0)
        self.probe_exposure = self._setting_float("scene_probe_exposure", 0.01, 2.0)
        self.probe_viz_mode = self._setting_choice("scene_probe_viz_mode", self.PROBE_VIZ_MODES)
        self.probe_viz_points = self._setting_int("scene_probe_viz_points", 100, 50000)
        self.show_bone_labels = self._setting_bool("mesh_viewer_show_bones")
        self.camera_speed = self._setting_float("scene_camera_speed", 0.01, 50.0)
        self.camera_look = self._setting_float("scene_camera_look", 0.01, 2.0)
        self.camera_wheel = self._setting_float("scene_camera_wheel", 0.001, 2.0)
        self.camera_boost = self._setting_float("scene_camera_boost", 1.0, 20.0)
        self.camera_slow = self._setting_float("scene_camera_slow", 0.01, 1.0)
        self._colors_dirty = True

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self.update)
        self._overlays = ViewportOverlayManager(self, HOVER_DETECT_KEY)

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
            QProgressBar { background:#151b22; border:1px solid #33414e; border-radius:3px; color:#e8eef5; text-align:center; font-size:9px; }
            QProgressBar::chunk { background:#328f83; border-radius:2px; }
        """)
        layout = QVBoxLayout(self.overlay)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        header = QHBoxLayout()
        self.fps_label = QLabel(self.tr("0 FPS"), self.overlay)
        self.overlay_fold_button = self._overlay_button("v", self.tr("Fold panel"))
        self.fullscreen_button = self._overlay_button(
            "⛶", self.tr("Fullscreen viewport"), self._toggle_view_fullscreen
        )
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

        builders = {
            "mesh": self._build_mesh_controls,
            "rcol": self._build_rcol_controls,
            "scene": self._build_scene_controls,
        }
        builders.get(self._controls, self._build_scene_controls)(body_layout)

        self.setup_viewport_overlay(self.overlay, self.overlay_body, self.overlay_fold_button)
        self.overlay.adjustSize()
        self.place_viewport_overlays()

    def _overlay_button(self, text: str, tip: str, slot=None) -> QToolButton:
        button = QToolButton(self.overlay)
        button.setText(text)
        button.setToolTip(tip)
        button.setFixedSize(max(18, 8 * len(str(text)) + 12), 18)
        if slot:
            button.clicked.connect(slot)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def setup_viewport_overlay(self, widget: QWidget, body: QWidget | None = None, fold_button: QToolButton | None = None):
        self._overlays.setup(widget, body, fold_button)

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
        self.setWindowTitle(self.tr("Scene Preview"))
        self.fullscreen_button.setText("x")
        self.showFullScreen()
        QTimer.singleShot(0, self, self._after_fullscreen_change)

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
        QTimer.singleShot(0, self, self._after_fullscreen_change)

    def _after_fullscreen_change(self):
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        if self.isWindow():
            self.raise_()
            self.activateWindow()
        self.setFocus(Qt.OtherFocusReason)
        self._gizmo_projection = None
        missing_regular = self._regular_set is None and self._regular_data is not None
        missing_solid = self._solid_set is None and self._solid_data is not None
        self._needs_gl_upload = self._needs_gl_upload or missing_regular or missing_solid
        self.place_viewport_overlays()
        self.update()

    def eventFilter(self, obj, event):
        handled = self._overlays.event_filter(obj, event)
        return super().eventFilter(obj, event) if handled is None else handled

    def _build_scene_controls(self, layout: QVBoxLayout):
        self._add_scene_mode_control(layout)
        self._add_texture_quality_control(layout)
        self._add_fps_limit_control(layout)
        self._add_control_row(
            layout,
            self.tr("Speed"), self._camera_spin("camera_speed", 0.01, 50.0, 0.1),
            self.tr("Look"), self._camera_spin("camera_look", 0.01, 2.0, 0.01),
        )
        self._add_control_row(
            layout,
            self.tr("Wheel"), self._camera_spin("camera_wheel", 0.001, 2.0, 0.01),
            self.tr("Fast"), self._camera_spin("camera_boost", 1.0, 20.0, 0.25),
            self.tr("Slow"), self._camera_spin("camera_slow", 0.01, 1.0, 0.05),
        )
        self.scene_light_combo = self._data_combo(
            (
                (self.tr("Fixed"), "fixed"),
                (self.tr("Software"), "software"),
                (self.tr("Probes"), "probes"),
                (self.tr("Off"), "off"),
            ),
            self._set_lighting_mode,
            self.lighting_mode,
        )
        self._add_control_row(layout, self.tr("Light"), self.scene_light_combo)
        self.probe_exposure_spin = self._float_spin(0.01, 2.0, 0.01, self.probe_exposure, self._set_probe_exposure)
        self._add_control_row(layout, self.tr("Probe Exp"), self.probe_exposure_spin)
        self.probe_viz_combo = self._data_combo(
            (
                (self.tr("Off"), "off"),
                (self.tr("Volumes"), "volumes"),
                (self.tr("Points"), "points"),
                (self.tr("All"), "all"),
            ),
            self._set_probe_viz_mode,
            self.probe_viz_mode,
        )
        self.probe_viz_points_spin = QSpinBox(self.overlay)
        self.probe_viz_points_spin.setRange(100, 50000)
        self.probe_viz_points_spin.setSingleStep(500)
        self.probe_viz_points_spin.setFixedWidth(62)
        self.probe_viz_points_spin.setValue(self.probe_viz_points)
        self.probe_viz_points_spin.valueChanged.connect(self._set_probe_viz_points)
        self._add_control_row(
            layout,
            self.tr("Viz"),
            self.probe_viz_combo,
            self.tr("Pts"),
            self.probe_viz_points_spin,
        )
        self.probe_status_label = QLabel("", self.overlay)
        self.probe_status_label.setWordWrap(True)
        self.probe_status_label.setStyleSheet("color:#7fced6; background-color:transparent; font-size:10px;")
        layout.addWidget(self.probe_status_label)
        self.probe_progress_bar = QProgressBar(self.overlay)
        self.probe_progress_bar.setRange(0, 100)
        self.probe_progress_bar.setValue(0)
        self.probe_progress_bar.setFormat(self.tr("Calculating probe lighting… %p%"))
        self.probe_progress_bar.setFixedHeight(14)
        self.probe_progress_bar.hide()
        layout.addWidget(self.probe_progress_bar)
        self._refresh_probe_status_label()
        self.gizmo_mode_combo = self._data_combo(
            (
                (self.tr("Position"), "position"),
                (self.tr("Rotation"), "rotation"),
                (self.tr("Scale"), "scale"),
            ),
            self.set_gizmo_mode,
            self._gizmo_mode,
        )
        self._add_control_row(layout, self.tr("Gizmo"), self.gizmo_mode_combo)
        self._add_highlight_filter_control(layout)
        note = QLabel(self.tr("Hold H to hover/select viewport objects."), self.overlay)
        note.setStyleSheet("color:#7f8b96; background-color:transparent; font-size:10px;")
        layout.addWidget(note)
        shortcut_note = QLabel(
            self.tr("Main REasy shortcuts are disabled in Scene tabs."), self.overlay
        )
        shortcut_note.setStyleSheet("color:#7f8b96; background-color:transparent; font-size:10px;")
        layout.addWidget(shortcut_note)

    def _build_rcol_controls(self, layout: QVBoxLayout):
        self._add_scene_mode_control(layout)
        self._add_highlight_filter_control(layout)

    def _add_scene_mode_control(self, layout: QVBoxLayout):
        modes = (
            (self.tr("Wireframe"), "wire"),
            (self.tr("Solid + Wire"), "hybrid"),
            (self.tr("Solid"), "solid"),
        )
        self.scene_mode_combo = self._data_combo(modes, self._set_render_mode, self.render_mode)
        self._add_control_row(layout, self.tr("Mode"), self.scene_mode_combo)

    def _add_highlight_filter_control(self, layout: QVBoxLayout):
        self.highlight_only_check = QCheckBox(
            self.tr("View only highlighted"), self.overlay
        )
        self.highlight_only_check.setChecked(self.show_only_highlighted)
        self.highlight_only_check.toggled.connect(self._set_show_only_highlighted)
        layout.addWidget(self.highlight_only_check)

    def _add_fps_limit_control(self, layout: QVBoxLayout):
        fps_spin = QSpinBox(self.overlay)
        fps_spin.setRange(0, 240)
        fps_spin.setFixedWidth(50)
        fps_spin.setValue(self._fps_limit)
        fps_spin.valueChanged.connect(self._change_fps_limit)
        self._add_control_row(layout, self.tr("Limit"), fps_spin)

    def _add_texture_quality_control(self, layout: QVBoxLayout):
        labels = {
            "low": self.tr("Low"),
            "balanced": self.tr("Balanced"),
            "high": self.tr("High"),
        }
        descriptions = {
            "low": self.tr("Resident TEX up to 256 px; 1x sampling"),
            "balanced": self.tr("Resident TEX up to 512 px; up to 4x sampling"),
            "high": self.tr(
                "Full resolution, prefers streaming TEX; up to 16x sampling"
            ),
        }
        options = tuple(
            (labels.get(name, profile.label), name)
            for name, profile in TEXTURE_QUALITY_PROFILES.items()
        )
        combo = self._data_combo(options, self._set_texture_quality, self.texture_quality)
        combo.setToolTip(
            "\n".join(
                descriptions.get(name, profile.description)
                for name, profile in TEXTURE_QUALITY_PROFILES.items()
            )
        )
        self._add_control_row(layout, self.tr("Quality"), combo)

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
        self._add_texture_quality_control(layout)
        self._add_fps_limit_control(layout)

        self.wf_combo = self._data_combo(
            (
                (self.tr("Off"), "off"),
                (self.tr("Polygon"), "polygon"),
                (self.tr("Depth Lines"), "lines_depth"),
                (self.tr("Overlay Lines"), "lines_overlay"),
            ),
            self._set_wireframe_mode,
            self.wireframe_mode,
        )
        self.line_spin = self._float_spin(0.5, 8.0, 0.1, self.line_width, self._set_line_width)
        self._add_control_row(
            layout,
            self.tr("WF Mode"),
            self.wf_combo,
            self.tr("Line"),
            self.line_spin,
        )

        self.light_combo = self._data_combo(
            (
                (self.tr("Off"), "off"),
                (self.tr("Fixed"), "fixed"),
                (self.tr("Software"), "software"),
            ),
            self._set_lighting_mode,
            self.lighting_mode,
        )
        row2 = self._add_control_row(layout, self.tr("Light"), self.light_combo)
        mesh = getattr(self, "mesh", None)
        if getattr(mesh, "streaming_buffer_count", 0):
            stream_status = (
                self.tr("Loaded")
                if getattr(mesh, "streaming_data_loaded", False)
                else self.tr("Missing")
            )
            row2.addWidget(
                QLabel(
                    self.tr("Stream {status}").format(status=stream_status),
                    self.overlay,
                )
            )
        self.amb_spin = self._float_spin(0.0, 1.0, 0.05, self.ambient, self._set_ambient)
        self.diff_spin = self._float_spin(0.0, 1.0, 0.05, self.diffuse, self._set_diffuse)
        for item in (self.tr("Amb"), self.amb_spin, self.tr("Diff"), self.diff_spin):
            row2.addWidget(QLabel(item, self.overlay) if isinstance(item, str) else item)

        self.bone_labels_check = QCheckBox(self.tr("Bones"), self.overlay)
        self.bone_labels_check.setChecked(self.show_bone_labels)
        self.bone_labels_check.toggled.connect(self._set_show_bone_labels)
        self._add_control_row(layout, self.bone_labels_check)

    def _setting_value(self, key: str):
        if self._settings is None:
            return self.SETTINGS_DEFAULTS[key]
        return self._settings.get(key, self.SETTINGS_DEFAULTS[key])

    def _setting_bool(self, key: str) -> bool:
        return bool(self._setting_value(key))

    def _setting_int(self, key: str, minimum: int, maximum: int) -> int:
        try:
            value = int(self._setting_value(key))
        except (TypeError, ValueError):
            value = int(self.SETTINGS_DEFAULTS[key])
        return max(minimum, min(maximum, value))

    def _setting_float(self, key: str, minimum: float, maximum: float) -> float:
        try:
            value = float(self._setting_value(key))
        except (TypeError, ValueError):
            value = float(self.SETTINGS_DEFAULTS[key])
        return max(minimum, min(maximum, value))

    def _setting_choice(self, key: str, choices: tuple[str, ...]) -> str:
        value = str(self._setting_value(key))
        return value if value in choices else str(self.SETTINGS_DEFAULTS[key])

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
            self.freecam.reset(self.center, self.extent, self.camera_speed)
        self._upload_buffers()
        self.update()

    def set_light_probe_set(self, probe_set: SceneLightProbeSet | None, status: str = "", obbs: list[object] | None = None, key: str = "") -> None:
        self._cancel_probe_shading()
        self._probe_shade_error = ""
        self._light_probe_set = probe_set
        self._light_probe_status = str(status or "")
        self._light_probe_key = str(key or "")
        self._light_probe_obbs = list(obbs or [])
        self._invalidate_probe_viz_points()
        if probe_set is not None and self._controls != "mesh":
            vertex_count = self._buffer_vertex_count()
            target_mode = None
            if 0 < vertex_count <= self.PROBE_AUTO_ENABLE_VERTEX_LIMIT:
                target_mode = "probes"
            if target_mode is not None:
                self.lighting_mode = target_mode
                combo = getattr(self, "scene_light_combo", None)
                if combo is not None:
                    combo.blockSignals(True)
                    target_index = combo.findData(target_mode)
                    if target_index >= 0:
                        combo.setCurrentIndex(target_index)
                    combo.blockSignals(False)
        self._colors_dirty = True
        self._refresh_probe_status_label()
        self.update()

    def _buffer_vertex_count(self) -> int:
        total = 0
        for data in (self._regular_data, self._solid_data):
            if data is not None:
                total += len(data.vertices)
        return total

    def _refresh_probe_status_label(self) -> None:
        label = getattr(self, "probe_status_label", None)
        if label is None:
            return
        label.setToolTip(self._probe_shade_error)
        if self._light_probe_set is None:
            label.setText(
                self.tr("Probe: none")
                if not self._light_probe_status
                else self.tr("Probe: {status}").format(status=self._light_probe_status)
            )
            return
        boxes = self._light_probe_boxes()
        mode = (
            self.tr("active")
            if self.lighting_mode == "probes"
            else self.tr("mode {mode}").format(mode=self.lighting_mode)
        )
        if self._probe_shade_worker is not None:
            mode = self.tr("calculating {percent}%").format(percent=self._probe_shade_percent)
        elif self._probe_shade_result is not None:
            mode = self.tr("applying")
        elif self._probe_shade_error:
            mode = self.tr("error")
        candidate_indices = self._probe_viz_candidate_indices()
        probe_count = 0 if candidate_indices is None else len(candidate_indices)
        label.setText(
            self.tr("Probe: {mode} | OBB {boxes} | probes {probes}").format(
                mode=mode, boxes=len(boxes), probes=probe_count
            )
        )

    def _show_probe_shade_progress(self, visible: bool, percent: int = 0) -> None:
        bar = getattr(self, "probe_progress_bar", None)
        if bar is None:
            return
        bar.setValue(max(0, min(100, int(percent))))
        bar.setVisible(bool(visible))

    def _light_probe_boxes(self) -> list[object]:
        return [box for box in self._light_probe_obbs if not getattr(box, "is_default_unit_box", lambda: False)()]

    def _set_probe_exposure(self, value: float) -> None:
        self.probe_exposure = float(value)
        self._save_view_setting("scene_probe_exposure", self.probe_exposure)
        self._invalidate_probe_viz_points()
        self._invalidate_probe_shading()
        self.update()

    def _set_probe_viz_mode(self, mode: str) -> None:
        if mode not in self.PROBE_VIZ_MODES:
            return
        self.probe_viz_mode = mode
        self._save_view_setting("scene_probe_viz_mode", mode)
        self._refresh_probe_status_label()
        self.update()

    def _set_probe_viz_points(self, value: int) -> None:
        self.probe_viz_points = max(100, min(50000, int(value)))
        self._save_view_setting("scene_probe_viz_points", self.probe_viz_points)
        self._invalidate_probe_viz_points()
        self.update()

    def _invalidate_probe_viz_points(self) -> None:
        self._probe_viz_candidate_indices_cache_key = None
        self._probe_viz_candidate_indices_cache = None
        self._probe_viz_point_cache_key = None
        self._probe_viz_point_cache = None

    def _mesh_transform_affects_display_colors(self) -> bool:
        return self.lighting_mode in {"software", "probes"}

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
        if focus:
            self.focus_selection()
        self.update()

    def update_mesh_transforms(self, matrices: dict[str, np.ndarray]) -> None:
        if not matrices:
            return
        deltas: dict[str, np.ndarray] = {}
        for mesh in self._meshes:
            if mesh.key in matrices:
                old = np.asarray(mesh.transform_matrix if mesh.transform_matrix is not None else IDENTITY4, dtype=np.float32)
                new = np.asarray(matrices[mesh.key], dtype=np.float32)
                if not np.allclose(old, new):
                    deltas[mesh.key] = new @ np.linalg.inv(old)
                mesh.transform_matrix = new
        self._recompute_bounds()
        self._refresh_selection_bounds()
        if deltas:
            if self._mesh_transform_affects_display_colors():
                self._invalidate_probe_shading()
            if not self._scene_matrix_is_identity():
                self._upload_buffers()
            elif not self._apply_transform_deltas(deltas):
                raise RuntimeError("Incremental mesh transform upload failed")
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
            self.freecam.focus(self._selection_center, extent)

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
        self.doneCurrent()
        self.update()

    def set_material_images(self, images: dict[str, tuple[str, TexPreviewUpload]]):
        self._pending_material_images = dict(images)
        self._texture_upload_failures.intersection_update(images)
        self._sync_material_images()
        self.texture_upload_status_changed.emit()

    def update_material_images(self, images: dict[str, tuple[str, TexPreviewUpload]]):
        self._pending_material_images.update(images)
        self._sync_material_images(images)
        self.texture_upload_status_changed.emit()

    def texture_upload_counts(self) -> tuple[int, int, int]:
        return len(self._pending_material_images), len(self._texture_ids), len(self._texture_upload_failures)

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

    def _set_texture_quality(self, quality: str):
        quality = normalize_texture_quality(quality)
        if quality == self.texture_quality:
            return
        self.texture_quality = quality
        self._save_view_setting("renderer_texture_quality", quality)
        self.texture_quality_changed.emit(quality)

    def mousePressEvent(self, event):
        self.setFocus(Qt.MouseFocusReason)
        if self._controls != "mesh" and event.button() == Qt.LeftButton:
            if self._begin_gizmo_drag(event):
                event.accept()
                return
            if self._hover_detect_down:
                self._queue_scene_pick("click", self._screen_pos(event))
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
        self.freecam.move_local(np.array((0.0, 0.0, -steps * self.freecam.speed * self.camera_wheel), dtype=np.float32))
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
        action = self.freecam.key_action(event)
        if self._controls != "mesh" and action:
            self.freecam.keys.add(action)
            event.accept()
            self._update_after_camera_change()
            return
        if self._controls != "mesh" and event.key() in self.freecam.MOD_KEYS:
            self.freecam.mods.add(event.key())
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
        action = self.freecam.key_action(event)
        if self._controls != "mesh" and action:
            if not event.isAutoRepeat():
                self.freecam.keys.discard(action)
            event.accept()
            return
        if self._controls != "mesh" and event.key() in self.freecam.MOD_KEYS:
            if not event.isAutoRepeat():
                self.freecam.mods.discard(event.key())
            event.accept()
            return
        super().keyReleaseEvent(event)

    def focusOutEvent(self, event):
        self._unlock_scene_cursor()
        self._finish_gizmo_drag(commit=True)
        self._set_hover_key("")
        self._hover_detect_down, self._last_hover_pick_pos = False, None
        self.freecam.clear_input()
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
        if axis is None:
            self._queue_scene_pick("hover", pos)
        else:
            self._set_hover_key("")

    def _queue_scene_pick(self, kind: str, pos: np.ndarray) -> None:
        self._pending_scene_pick = (kind, np.asarray(pos, dtype=np.float32).copy())
        self.update()

    def _move_scene_camera(self, dx: float, dy: float, buttons) -> None:
        if buttons & Qt.RightButton:
            self.freecam.look(dx, dy, self.camera_look)

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
        chunks = []
        mesh_points = mesh_bounds_points(meshes)
        if len(mesh_points):
            chunks.append(mesh_points)
        probe_points = self._selected_light_probe_points()
        if len(probe_points):
            chunks.append(probe_points)
        if not chunks:
            self._selection_center, self._selection_extent = None, 1.0
            return
        self._selection_center, self._selection_extent = point_bounds(np.concatenate(chunks, axis=0))

    def _selected_light_probe_points(self) -> np.ndarray:
        chunks = []
        for box in self._selected_light_probe_boxes():
            try:
                corners = np.asarray(box.corners(), dtype=np.float32).reshape(8, 3)
            except Exception:
                continue
            if np.isfinite(corners).all():
                chunks.append(corners)
        return np.concatenate(chunks, axis=0) if chunks else np.zeros((0, 3), dtype=np.float32)

    def _selected_light_probe_boxes(self) -> list[object]:
        if not self._light_probe_key or self._light_probe_key not in self._selection_keys or self._light_probe_key in self._hidden_keys:
            return []
        return self._light_probe_boxes()

    def _selection_is_whole_scene(self) -> bool:
        visible = {mesh.key for mesh in self._meshes if mesh.key not in self._hidden_keys}
        return bool(visible) and visible <= self._selection_keys

    def _gizmo_size(self) -> float:
        if self._controls != "mesh" and self._selection_center is not None:
            distance = max(float(np.linalg.norm(self._selection_center - self.freecam.pos)), 1.0)
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
        self._hover_key = key
        self.update()

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
            "probe_matrices": [(box, box.matrix()) for box in self._selected_light_probe_boxes() if hasattr(box, "matrix")],
        }
        if isinstance(handle, tuple):
            self._gizmo_drag["plane_axes"] = handle
            self._gizmo_drag["screen_basis_inv"] = np.linalg.inv(basis)
        if not self._gizmo_drag["matrices"] and not self._gizmo_drag["probe_matrices"]:
            self._gizmo_drag = None
            return False
        if self._mesh_transform_affects_display_colors() or self._gizmo_drag["probe_matrices"]:
            self._cancel_probe_shading()
        self._set_hover_key("")
        self._gizmo_drag["whole_scene"] = self._selection_is_whole_scene()
        if not self._gizmo_drag["whole_scene"] and len(self._gizmo_drag["matrices"]) == 1 and not self._gizmo_drag["probe_matrices"]:
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
        self._selection_center = transform_points(np.asarray(drag["center"]).reshape(1, 3), matrix)[0]
        self._selection_extent = drag["extent"] if drag["mode"] != "scale" else max(drag["extent"] * np.linalg.norm(matrix[:3, :3], axis=0).max(), 1.0)
        drag["matrix"] = matrix
        if not drag.get("whole_scene") and not drag.get("deferred_geometry"):
            self._apply_gizmo_drag_matrix(matrix)
        self._apply_light_probe_drag_matrix(matrix)
        self.update()

    def _finish_gizmo_drag(self, *, commit: bool) -> None:
        drag = self._gizmo_drag
        if drag is None:
            return
        matrix = drag.get("matrix")
        committed = {}
        rebuild_after_drag = False
        display_colors_changed = self._uses_probe_shading() and bool(
            drag.get("matrices") or drag.get("probe_matrices")
        )
        if commit and matrix is not None:
            for mesh in self._meshes:
                start = drag["matrices"].get(mesh.key)
                if start is not None:
                    mesh.transform_matrix = (matrix @ start).astype(np.float32, copy=False)
                    committed[mesh.key] = mesh.transform_matrix.copy()
            if drag.get("whole_scene"):
                self._scene_matrix = (matrix @ self._scene_matrix).astype(np.float32, copy=False)
            elif drag.get("deferred_geometry"):
                self._commit_deferred_gizmo_geometry(matrix)
            if drag.get("matrices") and self._mesh_transform_affects_display_colors():
                display_colors_changed = True
                rebuild_after_drag = bool(drag.get("whole_scene"))
            if drag.get("probe_matrices") and self._light_probe_key:
                self._apply_light_probe_drag_matrix(matrix)
                committed[self._light_probe_key] = np.asarray(drag["probe_matrices"][0][0].matrix(), dtype=np.float32).copy()
                display_colors_changed = True
        elif not commit and not drag.get("whole_scene") and not drag.get("deferred_geometry"):
            self._apply_gizmo_drag_matrix(IDENTITY4)
        if not commit and drag.get("probe_matrices"):
            self._restore_light_probe_drag_matrices()
            display_colors_changed = True
        deferred = bool(drag.get("deferred_geometry"))
        self._gizmo_drag = None
        if rebuild_after_drag:
            self._scene_matrix = IDENTITY4.copy()
            self._upload_buffers()
        elif display_colors_changed:
            self._invalidate_probe_shading()
        if committed:
            self.gizmo_transform_committed.emit((committed, bool(drag.get("whole_scene"))))
        if deferred:
            self._refresh_main_index_sets()
        if committed or drag.get("probe_matrices"):
            self._invalidate_probe_viz_points()
            self._refresh_probe_status_label()
        self._refresh_selection_bounds()
        self.update()

    def _apply_light_probe_drag_matrix(self, matrix: np.ndarray) -> None:
        drag = self._gizmo_drag
        if not drag:
            return
        changed = False
        for box, start in drag.get("probe_matrices", ()):
            try:
                box.set_from_matrix(np.asarray(matrix, dtype=np.float32) @ np.asarray(start, dtype=np.float32))
                changed = True
            except Exception:
                continue
        if changed:
            self._invalidate_probe_viz_points()

    def _restore_light_probe_drag_matrices(self) -> None:
        drag = self._gizmo_drag
        if not drag:
            return
        changed = False
        for box, start in drag.get("probe_matrices", ()):
            try:
                box.set_from_matrix(start)
                changed = True
            except Exception:
                continue
        if changed:
            self._invalidate_probe_viz_points()

    def _capture_gizmo_geometry(self, buffer_set: _GlBufferSet | None) -> dict | None:
        if buffer_set is None:
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
        self._apply_gizmo_snapshots(matrix, drag.get("regular_geometry"), drag.get("solid_geometry"))

    def _commit_deferred_gizmo_geometry(self, matrix: np.ndarray) -> None:
        self._apply_gizmo_snapshots(matrix, self._capture_gizmo_geometry(self._regular_set), self._capture_gizmo_geometry(self._solid_set))

    def _apply_gizmo_snapshots(self, matrix: np.ndarray, regular_snapshot: dict | None, solid_snapshot: dict | None) -> None:
        matrix = self._buffer_space_matrix(matrix)
        made_current = self.context() is not None
        if made_current:
            self.makeCurrent()
        self._apply_gizmo_snapshot(self._regular_set, regular_snapshot, matrix, current=made_current)
        self._apply_gizmo_snapshot(self._solid_set, solid_snapshot, matrix, current=made_current)
        if made_current:
            self.doneCurrent()

    def _apply_gizmo_snapshot(self, buffer_set: _GlBufferSet | None, snapshot: dict | None, matrix: np.ndarray, *, current: bool = False) -> None:
        if buffer_set is None or snapshot is None:
            return
        indices = snapshot["indices"]
        buffer_set.data.vertices[indices] = transform_points(snapshot["vertices"], matrix)
        if snapshot["normals"] is not None and buffer_set.data.normals is not None:
            buffer_set.data.normals[indices] = transform_normals(snapshot["normals"], matrix)
        if self.context() is not None:
            if not current:
                self.makeCurrent()
            self._upload_changed_geometry(buffer_set, indices)
            if not current:
                self.doneCurrent()

    def _scene_matrix_is_identity(self) -> bool:
        return np.array_equal(self._scene_matrix, IDENTITY4)

    def _apply_transform_deltas(self, deltas: dict[str, np.ndarray]) -> bool:
        if not deltas:
            return self._scene_matrix_is_identity()
        made_current = self.context() is not None
        if made_current:
            try:
                self.makeCurrent()
            except Exception:
                return False
        data_seen = False
        for buffer_set in (self._regular_set, self._solid_set):
            if buffer_set is None:
                continue
            data_seen = True
            for key, matrix in deltas.items():
                rows = self._hover_vertices(buffer_set.data, key)
                if len(rows):
                    self._apply_gizmo_snapshot(buffer_set, {
                        "indices": rows,
                        "vertices": buffer_set.data.vertices[rows].copy(),
                        "normals": buffer_set.data.normals[rows].copy() if buffer_set.data.normals is not None else None,
                    }, matrix, current=made_current)
        if made_current:
            self.doneCurrent()
        return data_seen

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

    def _draw_wire_box(self, corners) -> None:
        glBegin(GL_LINES)
        for a, b in BOX_EDGES:
            for index in (a, b):
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

    def _draw_light_probe_overlay(self) -> None:
        if self._controls == "mesh" or self.probe_viz_mode == "off":
            return
        draw_volumes = self.probe_viz_mode in {"volumes", "all"}
        draw_points = self.probe_viz_mode in {"points", "all"}
        if not draw_volumes and not draw_points:
            return
        glDisable(GL_LIGHTING)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_CULL_FACE)
        glDisable(GL_DEPTH_TEST)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(False)
        if draw_volumes:
            self._draw_light_probe_obb_boxes()
        if draw_points:
            glEnable(GL_DEPTH_TEST)
            glDepthMask(False)
            self._draw_light_probe_points()
        glPointSize(1.0)
        glLineWidth(1.0)
        glDepthMask(True)
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        if self.lighting_mode == "fixed":
            glEnable(GL_LIGHTING)

    def _draw_light_probe_obb_boxes(self) -> None:
        boxes = self._light_probe_boxes()
        if not boxes:
            return
        glLineWidth(2.0)
        for index, box in enumerate(boxes):
            try:
                corners = np.asarray(box.corners(), dtype=np.float32).reshape(8, 3)
            except Exception:
                continue
            if not np.isfinite(corners).all():
                continue
            alpha = 0.9 if index == 0 else 0.55
            glColor4f(0.1, 0.9, 1.0, alpha)
            self._draw_wire_box(corners)
            center = np.asarray(getattr(box, "center", ()), dtype=np.float32).reshape(3)
            axes = np.asarray(getattr(box, "axes", ()), dtype=np.float32).reshape(3, 3)
            extent = np.asarray(getattr(box, "extent", ()), dtype=np.float32).reshape(3)
            if not np.isfinite(center).all() or not np.isfinite(axes).all() or not np.isfinite(extent).all():
                continue
            glLineWidth(1.5)
            glBegin(GL_LINES)
            for axis_index, color in enumerate(((1.0, 0.25, 0.2), (0.25, 1.0, 0.35), (0.35, 0.55, 1.0))):
                endpoint = center + axes[axis_index] * max(float(extent[axis_index]), 0.0)
                glColor4f(color[0], color[1], color[2], alpha)
                glVertex3f(float(center[0]), float(center[1]), float(center[2]))
                glVertex3f(float(endpoint[0]), float(endpoint[1]), float(endpoint[2]))
            glEnd()

    def _probe_viz_candidate_indices(self) -> np.ndarray | None:
        probe_set = self._light_probe_set
        if probe_set is None or not len(probe_set.probe_positions):
            return None
        boxes = self._light_probe_boxes()
        key = (id(probe_set), tuple(id(box) for box in boxes))
        if self._probe_viz_candidate_indices_cache_key == key:
            return self._probe_viz_candidate_indices_cache
        if not boxes:
            indices = np.arange(probe_set.probe_count, dtype=np.int64)
        else:
            points = np.asarray(probe_set.probe_positions, dtype=np.float32).reshape(-1, 3)
            inside = np.zeros(len(points), dtype=bool)
            for box in boxes:
                inside |= self._points_inside_light_probe_box(points, box)
            indices = np.flatnonzero(inside).astype(np.int64, copy=False)
        self._probe_viz_candidate_indices_cache_key = key
        self._probe_viz_candidate_indices_cache = indices
        return indices

    def _probe_viz_points(self) -> tuple[np.ndarray, np.ndarray]:
        probe_set = self._light_probe_set
        if probe_set is None:
            return np.zeros((0, 3), dtype=np.float32), np.zeros((0, 4), dtype=np.float32)
        candidate_indices = self._probe_viz_candidate_indices()
        candidate_key = None if candidate_indices is None else (len(candidate_indices), int(candidate_indices[0]) if len(candidate_indices) else -1, int(candidate_indices[-1]) if len(candidate_indices) else -1)
        key = (id(probe_set), int(self.probe_viz_points), round(float(self.probe_exposure), 4), candidate_key)
        if self._probe_viz_point_cache_key != key or self._probe_viz_point_cache is None:
            self._probe_viz_point_cache = probe_set.probe_point_cloud(
                max_points=self.probe_viz_points,
                normal=(0.0, 1.0, 0.0),
                exposure=self.probe_exposure,
                candidate_indices=candidate_indices,
                normalize_display=True,
            )
            self._probe_viz_point_cache_key = key
        return self._probe_viz_point_cache

    def _draw_light_probe_points(self) -> None:
        points, colors = self._probe_viz_points()
        if not len(points):
            return
        glPointSize(4.0)
        glBegin(GL_POINTS)
        for point, color in zip(points, colors):
            rgb = np.maximum(np.asarray(color[:3], dtype=np.float32), 0.08)
            glColor4f(float(rgb[0]), float(rgb[1]), float(rgb[2]), 0.88)
            glVertex3f(float(point[0]), float(point[1]), float(point[2]))
        glEnd()

    @staticmethod
    def _points_inside_light_probe_box(points: np.ndarray, box) -> np.ndarray:
        try:
            axes = np.asarray(getattr(box, "axes"), dtype=np.float32).reshape(3, 3)
            center = np.asarray(getattr(box, "center"), dtype=np.float32).reshape(3)
            extent = np.asarray(getattr(box, "extent"), dtype=np.float32).reshape(3)
        except Exception:
            return np.zeros(len(points), dtype=bool)
        if not np.isfinite(axes).all() or not np.isfinite(center).all() or not np.isfinite(extent).all():
            return np.zeros(len(points), dtype=bool)
        local = (np.asarray(points, dtype=np.float32).reshape(-1, 3) - center) @ axes.T
        return np.all(np.abs(local) <= (extent + 1e-4), axis=1)

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

    @staticmethod
    def _dispose_vbo(handle, *, delete_gl: bool = True) -> None:
        if handle is None:
            return
        if delete_gl:
            with suppress(Exception):
                handle.delete()
        buffers = getattr(handle, "buffers", None)
        if hasattr(buffers, "clear"):
            buffers.clear()

    def _delete_buffer_set(self, buffer_set: _GlBufferSet | None, *, delete_gl: bool = True):
        if buffer_set is None:
            return
        for name in ("vertices_vbo", "normals_vbo", "colors_vbo", "uvs_vbo", "span_indices_vbo"):
            self._dispose_vbo(getattr(buffer_set, name), delete_gl=delete_gl)
            setattr(buffer_set, name, None)
        buffer_set.colors = None
        self._delete_index_vbos(buffer_set, delete_gl=delete_gl)

    def _delete_selection_buffer_sets(self, *, delete_gl: bool = True) -> None:
        self._delete_index_vbos(self._selection_regular_set, delete_gl=delete_gl)
        self._delete_index_vbos(self._selection_solid_set, delete_gl=delete_gl)
        self._selection_regular_set = self._selection_solid_set = None

    def _delete_index_vbos(self, buffer_set: _GlBufferSet | None, *, delete_gl: bool = True):
        if buffer_set is None:
            return
        for handle in (buffer_set.indices_vbo, buffer_set.line_indices_vbo):
            self._dispose_vbo(handle, delete_gl=delete_gl)
        for _, batch_vbo, _ in buffer_set.batch_vbos:
            self._dispose_vbo(batch_vbo, delete_gl=delete_gl)
        buffer_set.indices_vbo = None
        buffer_set.line_indices_vbo = None
        buffer_set.batch_vbos.clear()
        buffer_set.index_count = buffer_set.line_count = 0
        buffer_set.lines_ready = False

    def _display_colors(self, buffer_set: SceneBufferSet) -> np.ndarray | None:
        # Probe colors are calculated asynchronously. Until they are ready,
        # keep the normal unlit material colors visible in the viewport.
        return self._base_display_colors(buffer_set)

    def _uses_probe_shading(self) -> bool:
        return self.lighting_mode == "probes" and self._light_probe_set is not None

    def _probe_shade_request(self) -> ProbeShadeRequest | None:
        probe_set = self._light_probe_set
        if probe_set is None:
            return None
        inputs = tuple(
            ProbeShadeInput(
                key=id(buffer_set.data),
                vertices=buffer_set.data.vertices,
                normals=buffer_set.data.normals,
                base_colors=buffer_set.data.base_colors,
            )
            for buffer_set in (self._regular_set, self._solid_set)
            if buffer_set is not None and len(buffer_set.data.vertices)
        )
        if not inputs:
            return None
        boxes = snapshot_probe_boxes(self._light_probe_boxes())
        return ProbeShadeRequest(probe_set, inputs, self.probe_exposure, boxes)

    def _start_probe_shading(self, request: ProbeShadeRequest) -> None:
        self._cancel_probe_shading()
        self._probe_shade_job_id += 1
        worker = ProbeShadeWorker(self._probe_shade_job_id, request)
        worker.signals.progress.connect(self._on_probe_shade_progress)
        worker.signals.finished.connect(self._on_probe_shade_finished)
        worker.signals.cancelled.connect(self._on_probe_shade_cancelled)
        worker.signals.failed.connect(self._on_probe_shade_failed)
        self._probe_shade_worker = worker
        self._probe_shade_error = ""
        self._probe_shade_percent = 0
        self._show_probe_shade_progress(True, 0)
        self._refresh_probe_status_label()
        self._probe_shade_pool.start(worker)

    def _cancel_probe_shading(self) -> None:
        if self._probe_shade_worker is not None:
            self._probe_shade_worker.cancel()
        self._probe_shade_pool.clear()
        self._probe_shade_worker = None
        self._probe_shade_result = None
        self._probe_shade_percent = 0
        self._show_probe_shade_progress(False)

    def _invalidate_probe_shading(self) -> None:
        self._cancel_probe_shading()
        self._probe_shade_error = ""
        self._colors_dirty = True
        self._refresh_probe_status_label()

    @Slot(int, int, int)
    def _on_probe_shade_progress(self, job_id: int, completed: int, total: int) -> None:
        worker = self._probe_shade_worker
        if worker is None or worker.job_id != job_id:
            return
        percent = 100 if total <= 0 else int((max(0, completed) * 100) / total)
        percent = max(0, min(100, percent))
        if percent == self._probe_shade_percent:
            return
        self._probe_shade_percent = percent
        self._show_probe_shade_progress(True, percent)
        self._refresh_probe_status_label()

    @Slot(int, object)
    def _on_probe_shade_finished(self, job_id: int, results: object) -> None:
        worker = self._probe_shade_worker
        if worker is None or worker.job_id != job_id or not isinstance(results, dict):
            return
        self._probe_shade_result = (worker.request.key, results)
        self._probe_shade_worker = None
        self._probe_shade_percent = 100
        self._show_probe_shade_progress(True, 100)
        self._colors_dirty = True
        self._refresh_probe_status_label()
        self.update()

    @Slot(int)
    def _on_probe_shade_cancelled(self, job_id: int) -> None:
        worker = self._probe_shade_worker
        if worker is None or worker.job_id != job_id:
            return
        self._probe_shade_worker = None
        self._show_probe_shade_progress(False)
        self._refresh_probe_status_label()

    @Slot(int, str)
    def _on_probe_shade_failed(self, job_id: int, message: str) -> None:
        worker = self._probe_shade_worker
        if worker is None or worker.job_id != job_id:
            return
        self._probe_shade_worker = None
        self._probe_shade_error = str(message)
        self._show_probe_shade_progress(False)
        self._colors_dirty = False
        self._refresh_probe_status_label()
        print(f"Probe lighting calculation failed: {message}")

    def _refresh_probe_color_vbos(self) -> None:
        request = self._probe_shade_request()
        if request is None:
            self._cancel_probe_shading()
            self._colors_dirty = False
            return
        key = request.key
        if self._probe_shade_result is not None:
            result_key, results = self._probe_shade_result
            if result_key == key and self._upload_probe_color_results(results):
                self._probe_shade_result = None
                self._probe_shade_error = ""
                self._show_probe_shade_progress(False)
                self._colors_dirty = False
                self._refresh_probe_status_label()
                return
            self._probe_shade_result = None
        if self._probe_shade_worker is None or self._probe_shade_worker.request.key != key:
            self._start_probe_shading(request)
        self._colors_dirty = False

    def _upload_probe_color_results(self, results: dict[int, np.ndarray]) -> bool:
        active_sets = tuple(
            buffer_set
            for buffer_set in (self._regular_set, self._solid_set)
            if buffer_set is not None and len(buffer_set.data.vertices)
        )
        if any(id(buffer_set.data) not in results for buffer_set in active_sets):
            return False
        for buffer_set in active_sets:
            colors = results[id(buffer_set.data)]
            if buffer_set.colors_vbo is not None:
                self._dispose_vbo(buffer_set.colors_vbo)
            buffer_set.colors = colors
            buffer_set.colors_vbo = self._array_vbo(colors)
        self._sync_selection_color_buffers()
        return True

    def _sync_selection_color_buffers(self) -> None:
        for selection, source in (
            (self._selection_regular_set, self._regular_set),
            (self._selection_solid_set, self._solid_set),
        ):
            if selection is not None and source is not None:
                selection.colors = source.colors
                selection.colors_vbo = source.colors_vbo

    def _base_display_colors(self, buffer_set: SceneBufferSet) -> np.ndarray | None:
        return display_colors(
            buffer_set,
            color_source=self.color_source,
            lighting_mode=self.lighting_mode,
            ambient=self.ambient,
            diffuse=self.diffuse,
        )

    def _hover_vertices(self, buffer_set: SceneBufferSet, key: str) -> np.ndarray:
        if not key:
            return np.zeros((0,), dtype=np.uint32)
        by_key = self._hover_vertex_cache.setdefault(id(buffer_set), {})
        if key not in by_key:
            chunks = [idx for chunk_key, _material, idx in buffer_set.triangle_chunks if chunk_key == key]
            by_key[key] = np.unique(np.concatenate(chunks)).astype(np.uint32, copy=False) if chunks else np.zeros((0,), dtype=np.uint32)
        return by_key[key]

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
        if source is None or not keys:
            return None
        view = _GlBufferSet(
            data=source.data,
            vertices_vbo=source.vertices_vbo,
            normals_vbo=source.normals_vbo,
            colors=source.colors,
            colors_vbo=source.colors_vbo,
            uvs_vbo=source.uvs_vbo,
        )
        need_lines = self._needs_line_indices()
        indices, batches, line_indices = scene_key_index_buffers(source.data, keys, include_lines=need_lines)
        self._set_index_buffers(view, indices, batches, line_indices, need_lines)
        return view

    def _refresh_selection_buffer_sets(self, *, current: bool = False) -> None:
        if self.context() is None:
            return
        if not current and not self._make_current_or_queue_upload():
            return
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
        if not current and not self._make_current_or_queue_upload():
            return
        self._upload_index_vbos(self._regular_set)
        self._upload_index_vbos(self._solid_set)
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
        if self._uses_probe_shading():
            self._refresh_probe_color_vbos()
            return
        if self._probe_shade_worker is not None or self._probe_shade_result is not None:
            self._cancel_probe_shading()
            self._probe_shade_error = ""
        for buffer_set in (self._regular_set, self._solid_set):
            if buffer_set is None:
                continue
            if buffer_set.colors_vbo is not None:
                self._dispose_vbo(buffer_set.colors_vbo)
            colors = self._display_colors(buffer_set.data)
            buffer_set.colors = colors
            buffer_set.colors_vbo = self._array_vbo(colors) if colors is not None else None
        self._sync_selection_color_buffers()
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
            self._cancel_probe_shading()
            self._rebuild_buffer_data()
            if not (self._gizmo_drag and self._gizmo_drag.get("whole_scene")):
                self._scene_matrix = IDENTITY4.copy()
        if self.context() is None:
            return
        if not current and not self._make_current_or_queue_upload():
            return
        self._delete_selection_buffer_sets()
        self._delete_buffer_set(self._regular_set)
        self._delete_buffer_set(self._solid_set)
        self._regular_set = self._upload_buffer_set(self._regular_data)
        self._solid_set = self._upload_buffer_set(self._solid_data)
        self._refresh_selection_buffer_sets(current=True)
        self._colors_dirty = self._uses_probe_shading()
        if not current:
            self.doneCurrent()
        self._needs_gl_upload = False

    def _make_current_or_queue_upload(self) -> bool:
        try:
            self.makeCurrent()
            return True
        except Exception:
            self._needs_gl_upload = True
            self.update()
            return False

    def _clear_gl_textures(self):
        if self._texture_source_ids:
            with suppress(Exception):
                glDeleteTextures(list(self._texture_source_ids.values()))
            self._texture_source_ids.clear()
        self._texture_sources.clear()
        self._texture_ids.clear()

    def _sync_gl_textures(self, names=None):
        if names is None:
            self._texture_upload_failures.intersection_update(self._pending_material_images)
            for name in set(self._texture_sources) - set(self._pending_material_images):
                self._texture_ids.pop(name, None)
                self._texture_sources.pop(name, None)
            self._delete_unused_source_textures()
            names = self._pending_material_images
        for name in names:
            source_path, texture = self._pending_material_images[name]
            if self._texture_sources.get(name) == source_path and source_path in self._texture_source_ids:
                self._texture_ids[name] = self._texture_source_ids[source_path]
                self._texture_upload_failures.discard(name)
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
                try:
                    self._upload_texture(texture_id, texture, self._texture_anisotropy())
                except Exception as exc:
                    with suppress(Exception):
                        glDeleteTextures([texture_id])
                    print(f"Texture upload failed: material={name!r}, path={source_path!r}: {exc}")
                    self._texture_upload_failures.add(name)
                    continue
                self._texture_source_ids[source_path] = texture_id
            self._texture_ids[name] = texture_id
            self._texture_sources[name] = source_path
            self._texture_upload_failures.discard(name)

    def _delete_unused_source_textures(self):
        active_sources = set(self._texture_sources.values())
        for source_path, texture_id in list(self._texture_source_ids.items()):
            if source_path not in active_sources:
                glDeleteTextures([texture_id])
                self._texture_source_ids.pop(source_path, None)

    @staticmethod
    def _upload_texture(texture_id: int, texture: TexPreviewUpload, anisotropy: float = 1.0):
        for _ in range(16):
            if glGetError() == GL_NO_ERROR:
                break
        glBindTexture(GL_TEXTURE_2D, texture_id)
        try:
            glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR_MIPMAP_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
            glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAX_LEVEL, len(texture.levels) - 1)
            if anisotropy > 1.0:
                glTexParameterf(GL_TEXTURE_2D, GL_TEXTURE_MAX_ANISOTROPY_EXT, anisotropy)
            error = glGetError()
            if error != GL_NO_ERROR:
                raise RuntimeError(f"texture setup GL error 0x{error:04X}")
            for mip, level in enumerate(texture.levels):
                if texture.compressed:
                    glCompressedTexImage2D(
                        GL_TEXTURE_2D,
                        mip,
                        texture.gl_format,
                        level.width,
                        level.height,
                        0,
                        level.data,
                    )
                else:
                    glTexImage2D(
                        GL_TEXTURE_2D,
                        mip,
                        texture.gl_format,
                        level.width,
                        level.height,
                        0,
                        GL_RGBA,
                        GL_UNSIGNED_BYTE,
                        level.data,
                    )
                error = glGetError()
                if error != GL_NO_ERROR:
                    raise RuntimeError(
                        f"mip {mip} ({level.width}x{level.height}) GL error 0x{error:04X}"
                    )
        finally:
            glBindTexture(GL_TEXTURE_2D, 0)

    @staticmethod
    def _query_max_texture_anisotropy() -> float:
        extensions = glGetString(GL_EXTENSIONS) or b""
        if isinstance(extensions, str):
            extensions = extensions.encode()
        supported = (b"GL_EXT_texture_filter_anisotropic", b"GL_ARB_texture_filter_anisotropic")
        if not any(name in extensions for name in supported):
            return 1.0
        value = np.asarray(glGetFloatv(GL_MAX_TEXTURE_MAX_ANISOTROPY_EXT)).reshape(-1)
        return max(1.0, float(value[0])) if len(value) else 1.0

    def _texture_anisotropy(self) -> float:
        profile = texture_quality_profile(self.texture_quality)
        available = min(profile.anisotropy, self._max_texture_anisotropy)
        if available < profile.anisotropy:
            key = self.texture_quality, self._max_texture_anisotropy
            if key not in self._anisotropy_limit_logs:
                self._anisotropy_limit_logs.add(key)
                print(
                    f"Texture anisotropy limited: quality={profile.label}, "
                    f"requested={profile.anisotropy:g}x, available={self._max_texture_anisotropy:g}x"
                )
        return available

    @staticmethod
    def _upload_qimage_texture(texture_id: int, image):
        if image is None or image.isNull():
            return
        rgba = image.convertToFormat(QImage.Format.Format_RGBA8888)
        bits = rgba.constBits()
        if hasattr(bits, "setsize"):
            bits.setsize(rgba.sizeInBytes())
        glBindTexture(GL_TEXTURE_2D, texture_id)
        glPixelStorei(GL_UNPACK_ALIGNMENT, 1)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
        glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA, rgba.width(), rgba.height(), 0, GL_RGBA, GL_UNSIGNED_BYTE, bytes(bits))
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
        self._delete_buffer_set(self._regular_set, delete_gl=made_current)
        self._delete_buffer_set(self._solid_set, delete_gl=made_current)
        self._delete_selection_buffer_sets(delete_gl=made_current)
        if made_current:
            with suppress(Exception):
                self._cleanup_extra_gl()
            with suppress(Exception):
                self.doneCurrent()
        self._regular_set = self._solid_set = self._gl_cleanup_context = None
        self._needs_gl_upload = self._regular_data is not None or self._solid_data is not None

    @Slot()
    def _on_gl_context_about_to_be_destroyed(self):
        self._cleanup_gl(self.sender())

    def cleanup(self):
        self._timer.stop()
        self._cancel_probe_shading()
        self._leave_view_fullscreen()
        self._cleanup_gl()
        self._clear_scene_memory()

    def _clear_scene_memory(self):
        with suppress(Exception):
            self._unlock_scene_cursor()
        self.freecam.clear_input()
        self._meshes.clear()
        self._highlighted_keys.clear()
        self._selection_keys.clear()
        self._hidden_keys.clear()
        self._material_tints.clear()
        self._two_sided_materials.clear()
        self._pending_material_images.clear()
        self._texture_upload_failures.clear()
        self._texture_ids.clear()
        self._texture_sources.clear()
        self._texture_source_ids.clear()
        self._hover_vertex_cache.clear()
        self._light_probe_set = None
        self._light_probe_status = self._light_probe_key = ""
        self._light_probe_obbs.clear()
        self._probe_shade_error = ""
        self._invalidate_probe_viz_points()
        self._hover_key = self._hover_block_key = ""
        self._hover_detect_down = False
        self._pending_scene_pick = self._last_hover_pick_pos = None
        self._selection_center = self._gizmo_drag = self._gizmo_projection = None
        self._regular_data = self._solid_data = None
        self._scene_matrix = IDENTITY4.copy()
        self._needs_gl_upload = self._colors_dirty = False

    def closeEvent(self, event):
        if self._fullscreen_restore is not None:
            self._leave_view_fullscreen()
            event.ignore()
            return
        self.cleanup()
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
        try:
            self._max_texture_anisotropy = self._query_max_texture_anisotropy()
        except Exception as exc:
            self._max_texture_anisotropy = 1.0
            print(f"Texture anisotropy query failed: {exc}")
        context = self.context()
        if context is not None and context is not self._gl_cleanup_context:
            context.aboutToBeDestroyed.connect(self._on_gl_context_about_to_be_destroyed)
            self._gl_cleanup_context = context
        if self._regular_data is None and self._solid_data is None:
            self._rebuild_buffer_data()
        self._regular_set = self._upload_buffer_set(self._regular_data)
        self._solid_set = self._upload_buffer_set(self._solid_data)
        self._refresh_selection_buffer_sets(current=True)
        self._colors_dirty = self._uses_probe_shading()
        self._sync_gl_textures()
        self.texture_upload_status_changed.emit()
        self._needs_gl_upload = False
        self._after_gl_initialized()

    def resizeGL(self, w: int, h: int):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(w * dpr), int(h * dpr))
        self.place_viewport_overlays()

    def place_viewport_overlays(self):
        self._overlays.place()

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
        self.freecam.step(self.camera_boost, self.camera_slow)
        glRotatef(-self.freecam.pitch, 1.0, 0.0, 0.0)
        glRotatef(-self.freecam.yaw, 0.0, 1.0, 0.0)
        glTranslatef(-self.freecam.pos[0], -self.freecam.pos[1], -self.freecam.pos[2])

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
        keys = self._selection_keys - self._hidden_keys
        if not keys:
            return
        if len(keys) > 256 or self._span_index_count(keys) > 700_000:
            return
        alpha = 0.12 + 0.10 * (0.5 + 0.5 * np.sin(time.perf_counter() * 3.5))
        self._draw_overlay_sets((self._regular_set, self._solid_set), (1.0, 0.86, 0.18, float(alpha)), keys)

    def _draw_hover_glow(self):
        if self._hover_key:
            self._draw_overlay_sets((self._regular_set, self._solid_set), (0.05, 1.0, 0.25, 0.38), {self._hover_key}, through=False)

    def _span_index_count(self, keys: set[str]) -> int:
        return sum(
            count
            for buffer_set in (self._regular_set, self._solid_set)
            if buffer_set is not None
            for key in keys
            for _offset, count in buffer_set.data.key_spans.get(key, ())
        )

    def _draw_overlay_sets(self, buffer_sets, color, keys: set[str] | None = None, *, through: bool = True):
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glDepthMask(False)
        if through:
            glDisable(GL_DEPTH_TEST)
        else:
            glDepthFunc(GL_LEQUAL)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_LIGHTING)
        glDisableClientState(GL_NORMAL_ARRAY)
        glDisableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        glColor4f(*color)
        for buffer_set in buffer_sets:
            if buffer_set is None or buffer_set.vertices_vbo is None:
                continue
            index_vbo = self._span_index_vbo(buffer_set) if keys else buffer_set.indices_vbo
            if index_vbo is None:
                continue
            buffer_set.vertices_vbo.bind()
            glEnableClientState(GL_VERTEX_ARRAY)
            glVertexPointer(3, GL_FLOAT, 0, None)
            index_vbo.bind()
            spans = (span for key in keys for span in buffer_set.data.key_spans.get(key, ())) if keys else ((0, buffer_set.index_count),)
            for offset, count in spans:
                glDrawElements(GL_TRIANGLES, count, GL_UNSIGNED_INT, c_void_p(offset * 4) if keys else None)
            glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)
            glDisableClientState(GL_VERTEX_ARRAY)
            glBindBuffer(GL_ARRAY_BUFFER, 0)
        glDepthMask(True)
        glDisable(GL_BLEND)
        if through:
            glEnable(GL_DEPTH_TEST)
        else:
            glDepthFunc(GL_LESS)
        if self.lighting_mode == "fixed":
            glEnable(GL_LIGHTING)

    def _span_index_vbo(self, buffer_set: _GlBufferSet):
        if not self._active_hidden_keys():
            return buffer_set.indices_vbo
        if buffer_set.span_indices_vbo is None and len(buffer_set.data.indices):
            buffer_set.span_indices_vbo = self._element_vbo(buffer_set.data.indices)
        return buffer_set.span_indices_vbo

    def _draw_scene_pick(self, pos: np.ndarray, scene_matrix: np.ndarray | None, dpr: float) -> str:
        key_ids, id_keys = self._pick_color_ids()
        if not key_ids:
            return ""
        width, height = max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))
        x = max(0, min(width - 1, int(pos[0] * dpr)))
        y = max(0, min(height - 1, height - 1 - int(pos[1] * dpr)))
        glDisable(GL_DITHER)
        glDisable(GL_MULTISAMPLE)
        glDisable(GL_BLEND)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_LIGHTING)
        glDisable(GL_CULL_FACE)
        glDisableClientState(GL_NORMAL_ARRAY)
        glDisableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        glDepthFunc(GL_LESS)
        glDepthMask(True)
        glEnable(GL_SCISSOR_TEST)
        glScissor(x, y, 1, 1)
        if scene_matrix is not None:
            glPushMatrix()
            glMultMatrixf(np.ascontiguousarray(scene_matrix.T, dtype=np.float32))
        for buffer_set in (self._regular_set, self._solid_set):
            self._draw_pick_buffer(buffer_set, key_ids)
        if scene_matrix is not None:
            glPopMatrix()
        rgba = np.frombuffer(glReadPixels(x, y, 1, 1, GL_RGBA, GL_UNSIGNED_BYTE), dtype=np.uint8, count=4)
        glDisable(GL_SCISSOR_TEST)
        glEnable(GL_DITHER)
        glEnable(GL_MULTISAMPLE)
        pick_id = int(rgba[0]) | (int(rgba[1]) << 8) | (int(rgba[2]) << 16)
        return id_keys.get(pick_id, "")

    def _pick_color_ids(self) -> tuple[dict[str, int], dict[int, str]]:
        hidden = self._active_hidden_keys()
        key_ids: dict[str, int] = {}
        for buffer_set in (self._regular_set, self._solid_set):
            if buffer_set is None:
                continue
            for key in buffer_set.data.key_spans:
                if key not in hidden and key not in key_ids:
                    key_ids[key] = len(key_ids) + 1
        return key_ids, {index: key for key, index in key_ids.items()}

    def _draw_pick_buffer(self, buffer_set: _GlBufferSet | None, key_ids: dict[str, int]) -> None:
        if buffer_set is None or buffer_set.vertices_vbo is None or not key_ids:
            return
        index_vbo = self._span_index_vbo(buffer_set)
        if index_vbo is None:
            return
        buffer_set.vertices_vbo.bind()
        glEnableClientState(GL_VERTEX_ARRAY)
        glVertexPointer(3, GL_FLOAT, 0, None)
        index_vbo.bind()
        for key, spans in buffer_set.data.key_spans.items():
            if not (pick_id := key_ids.get(key)):
                continue
            glColor4f((pick_id & 255) / 255.0, ((pick_id >> 8) & 255) / 255.0, ((pick_id >> 16) & 255) / 255.0, 1.0)
            for offset, count in spans:
                glDrawElements(GL_TRIANGLES, count, GL_UNSIGNED_INT, c_void_p(offset * 4))
        glBindBuffer(GL_ELEMENT_ARRAY_BUFFER, 0)
        glDisableClientState(GL_VERTEX_ARRAY)
        glBindBuffer(GL_ARRAY_BUFFER, 0)

    def _finish_scene_pick(self, kind: str, key: str) -> None:
        if kind == "hover":
            self._set_hover_key(key if self._hover_detect_down else "")
            return
        if key:
            self.object_clicked.emit(key)
            self._hover_block_key = key
            self._set_hover_key("")
        else:
            self.object_clicked.emit("")

    def _active_scene_matrix(self) -> np.ndarray | None:
        matrix = self._scene_matrix
        drag = self._gizmo_drag
        if drag and drag.get("whole_scene") and drag.get("matrix") is not None:
            matrix = drag["matrix"] @ matrix
        return None if np.allclose(matrix, IDENTITY4) else matrix

    def _prepare_scene_view(self) -> None:
        self._apply_render_state()
        glLoadIdentity()
        glLightfv(GL_LIGHT0, GL_POSITION, (0.5, 1.0, 1.0, 0.0))
        self._apply_camera_transform()
        self._gizmo_projection = None
        if self._controls != "mesh" and self._selection_center is not None:
            self._cache_gizmo_projection()

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
        self._prepare_scene_view()
        scene_matrix = self._active_scene_matrix()
        if self._pending_scene_pick is not None:
            kind, pos = self._pending_scene_pick
            self._pending_scene_pick = None
            key = self._draw_scene_pick(pos, scene_matrix, dpr)
            QTimer.singleShot(0, self, lambda kind=kind, key=key: self._finish_scene_pick(kind, key))
            glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
            self._prepare_scene_view()
        if scene_matrix is not None:
            glPushMatrix()
            glMultMatrixf(np.ascontiguousarray(scene_matrix.T, dtype=np.float32))
        self._draw_regular_scene()
        self._draw_triangles(self._solid_set, use_textures=False)
        if not self._draw_deferred_selection():
            self._draw_selection_glow()
        self._draw_hover_glow()
        if scene_matrix is not None:
            glPopMatrix()
        self._draw_light_probe_overlay()
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
        self.freecam.clear_input()
        super().hideEvent(event)

    def _change_fps_limit(self, value: int):
        self._fps_limit = int(value)
        self._save_view_setting("mesh_viewer_fps_limit", self._fps_limit)
        self._update_timer_state()

    def _set_camera_setting(self, attr: str, key: str, value: float):
        setattr(self, attr, float(value))
        if attr == "camera_speed":
            self.freecam.update_speed(self.camera_speed)
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
        self._invalidate_probe_shading()
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
