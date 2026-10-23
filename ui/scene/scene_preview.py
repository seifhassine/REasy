from __future__ import annotations

import time
from contextlib import suppress
from dataclasses import dataclass, fields, is_dataclass, replace

import numpy as np
from PySide6.QtCore import QElapsedTimer, QEvent, QSize, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QCursor, QPainter
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

lazy from file_handlers.mesh.lit_preview import LitOverlays, engine_camera, free_camera
lazy from file_handlers.mesh.scene_pick import ScenePicker
lazy from file_handlers.mmtr.engine_buffers import Camera
lazy from file_handlers.mmtr.overlay import GRID, project_points
lazy from file_handlers.mmtr.overlay_scene import ALL_EDGES, NO_EDGES, VISIBLE_EDGES, SceneShown
lazy from file_handlers.mmtr.textures_view import ProbeLight, VertexLight, ViewLight, ViewProbes
from file_handlers.tex.texture_quality import (
    DEFAULT_TEXTURE_QUALITY,
    TEXTURE_QUALITY_PROFILES,
    normalize_texture_quality,
)
from settings import save_settings
lazy from ui.editor_widgets import EmbeddedPopupComboBox, tool_button
lazy from ui.icons import line_icon
from ui.orbit_camera import OrbitCameraMixin
lazy from ui.qt_timers import call_later
lazy from .device_frames import LIMIT_MOST, FrameWindow
lazy from .freecam_controller import FreecamController
lazy from .scene_buffers import (
    SOFTWARE_LIGHT_DIRECTION,
    VIEW_LIGHT_DIRECTION,
    computed_normals,
    mesh_bounds_points,
    point_bounds,
    scene_bounds,
    transform_points,
)
lazy from .lightprobe_preview import SceneLightProbeInstance
lazy from .lightprobe_shading import ProbeShadeLayer, layer_order, snapshot_probe_boxes
lazy from .scene_marks import (
    GIZMO_AXES, GIZMO_PLANES, bounds_marks, gizmo_circle_points, gizmo_marks, gizmo_plane_corners, probe_marks, unorm,
)
lazy from .scene_model import SceneDrawBatch, SceneDrawMesh, SceneSkinningBinding
lazy from .scene_skinning import SceneSkinning
lazy from .scene_view import SceneGeometry, ScenePlacement, ScenePose, SceneShape, SceneView
lazy from .viewport_overlay import ViewportOverlayManager, add_overlay_header, overlay_panel_stylesheet



def view_light(mode: str, ambient: float, diffuse: float) -> ViewLight | VertexLight | None:
    if mode == "fixed":
        return ViewLight(VIEW_LIGHT_DIRECTION, float(ambient), float(diffuse))
    if mode == "software":
        return VertexLight(SOFTWARE_LIGHT_DIRECTION, float(ambient), float(diffuse))
    return None


def _same(told, again) -> bool:
    if isinstance(told, np.ndarray) or isinstance(again, np.ndarray):
        return (isinstance(told, np.ndarray) and isinstance(again, np.ndarray) and told.shape == again.shape
                and np.array_equal(told, again))
    if is_dataclass(told):
        return type(told) is type(again) and all(_same(getattr(told, item.name), getattr(again, item.name))
                                                 for item in fields(told))
    if isinstance(told, tuple):
        return isinstance(again, tuple) and len(told) == len(again) and all(map(_same, told, again))
    return told == again


@dataclass(slots=True)
class _FullscreenRestore:
    window: QWidget
    window_state: Qt.WindowState
    hidden_widgets: tuple[QWidget, ...]


IDENTITY4 = np.identity(4, dtype=np.float32)
HOVER_DETECT_KEY = Qt.Key_H


class ScenePreviewWidget(OrbitCameraMixin, QWidget):
    object_clicked = Signal(str)
    gizmo_transform_committed = Signal(object)
    texture_quality_changed = Signal(str)
    # (SceneView, overlays), or (refusal string, None).
    device_view_changed = Signal(object, object)
    device_window_changed = Signal(int)
    fps_limit_changed = Signal(int)
    strands_stepped = Signal(object)

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
        "renderer_antialiasing": True,
        "renderer_shading": "game",
        "scene_render_mode": "off",
        "scene_gizmo_mode": "position",
        "scene_show_only_highlighted": False,
        "scene_camera_speed": 1.0,
        "scene_camera_look": 0.18,
        "scene_camera_wheel": 0.08,
        "scene_camera_boost": 3.0,
        "scene_camera_slow": 0.25,
    }
    # Edge modes: none, visible edges alone, visible edges over meshes, all edges over meshes.
    WIREFRAME_MODES = ("off", "wire", "hybrid", "xray")
    _WIREFRAME_RENAMED = {"polygon": "wire", "lines_depth": "hybrid", "lines_overlay": "xray", "solid": "off"}
    LIGHTING_MODES = ("off", "fixed", "software", "probes", "game")
    PROBE_VIZ_MODES = ("off", "volumes", "points", "all")

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
        super().__init__(parent)
        self._frame_due = QTimer(self)
        self._frame_due.setSingleShot(True)
        self._frame_due.timeout.connect(self._frame)
        self._device_frames = False
        self._device_told = None
        self._device_frame = None
        self._device_window = None
        self._device_content = 0
        self._device_motion = 0
        self._device_scene: tuple[tuple[SceneGeometry, ...], list[SceneDrawMesh]] | None = None
        # The device scene's meshes and each one's number by key.
        self._device_numbers: tuple[list[SceneDrawMesh], dict[str, int]] | None = None
        self._device_before: tuple | None = None
        self._device_placed: tuple[int, tuple[ScenePlacement, ...], list[SceneDrawMesh]] | None = None
        # Keys of the meshes placed or posed otherwise since the placements were made; None: any.
        self._device_moving: set[str] | None = None
        self._device_shapes: dict[int, SceneShape] = {}
        self._device_overlays_made: tuple = (None, None)
        self._device_probes_made: tuple = ((), None)
        self.strands_only = False
        self.strands_boxes: tuple = ()
        self.strands_source = None
        self.wrinkle_source = None
        self.edit_tool = None
        # Picks the mesh at a texel of the frame shown, from what it drew.
        self._picker = ScenePicker()
        self._mesh_poses: dict[str, ScenePose] = {}

        self._settings = settings if isinstance(settings, dict) else None
        self._controls = controls
        self.orthographic = False
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
        self._fullscreen_content: QWidget | None = None
        self._fullscreen_transition = False

        self._meshes: list[SceneDrawMesh] = []
        self._highlighted_keys: set[str] = set()
        self._selection_keys: set[str] = set()
        self._selection_center: np.ndarray | None = None
        self._selection_extent = 1.0
        self._gizmo_hover_axis: int | None = None
        self._gizmo_drag = None
        self._gizmo_projection = None
        self._hover_key = self._hover_block_key = ""
        self._hover_detect_down = False
        self._material_parameters: dict[str, dict[str, float]] = {}
        self._hidden_keys: set[str] = set()
        self._hidden_parts: dict[str, frozenset[int]] = {}
        self._skinning = SceneSkinning()
        self._mesh_draw_matrices: dict[str, np.ndarray] = {}

        self._wireframe_key = "mesh_viewer_wireframe_mode" if self._controls == "mesh" else "scene_render_mode"
        wireframe = str(self._setting_value(self._wireframe_key))
        wireframe = self._WIREFRAME_RENAMED.get(wireframe, wireframe)
        self.wireframe_mode = wireframe if wireframe in self.WIREFRAME_MODES else "off"
        self._gizmo_mode = self._setting_choice("scene_gizmo_mode", ("position", "rotation", "scale"))
        self.show_only_highlighted = self._setting_bool("scene_show_only_highlighted")
        self._fps_limit = self._setting_int("mesh_viewer_fps_limit", 0, LIMIT_MOST)
        self.texture_quality = self._setting_choice("renderer_texture_quality", tuple(TEXTURE_QUALITY_PROFILES))
        self.antialiasing = self._setting_bool("renderer_antialiasing")
        lighting_modes = tuple(mode for mode in self.LIGHTING_MODES
                               if mode != "game" and (controls != "mesh" or mode != "probes"))
        self.lighting_mode = self.texture_light = self._setting_choice("mesh_viewer_lighting_mode", lighting_modes)
        self.shading = self._setting_choice("renderer_shading", ("game", "gbuffer", "textures"))
        self._game_offered = False
        self._game_refused = ""
        self._shading_widgets: dict[str, list[QWidget]] = {"textures": [], "game": []}
        self._shading_shown = "textures"
        self._light_probe_instances: dict[str, SceneLightProbeInstance] = {}
        self._light_probe_status = ""
        self._preferred_selection_keys: set[str] = set()
        self._probe_viz_candidate_indices_cache: dict[tuple, np.ndarray] = {}
        self._probe_viz_point_cache_key = None
        self._probe_viz_point_cache: tuple[np.ndarray, np.ndarray] | None = None
        self.line_width = self._setting_float("mesh_viewer_line_width", 0.5, 8.0)
        self._color_source = "vertex"
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

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self._request_frame)
        self._presented = 0
        self._presented_clock = QElapsedTimer()
        self._fps_timer = QTimer(self)
        self._fps_timer.timeout.connect(self._show_device_rate)
        self._frame_clock = QElapsedTimer()
        self._told_clock = QElapsedTimer()
        self._frames_asked = 0
        self._frame_callback = None
        self.stepped_time = None
        self._seconds_clock = QElapsedTimer()
        self._stepped_seen: float | None = None
        self._strands_stepped = None
        self._overlays = ViewportOverlayManager(self, HOVER_DETECT_KEY)
        self._hud_labels: list[QLabel] = []

        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self._build_overlay()
        self._update_timer_state()

    def _build_overlay(self):
        self.overlay = QFrame(self)
        self.overlay.setObjectName("viewportHud")
        self.overlay.setStyleSheet(overlay_panel_stylesheet("viewportHud", dense_fields=True))
        layout = QVBoxLayout(self.overlay)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = add_overlay_header(self.overlay, "viewportHud", left_margin=8)
        self.fps_label = QLabel(self.tr("0 FPS"), self.overlay)
        self.fps_label.setObjectName("viewportHudTitle")
        self.overlay_fold_button = self._overlay_button("chevron_down", self.tr("Fold panel"))
        self.fullscreen_button = self._overlay_button(
            "fullscreen", self.tr("Fullscreen viewport"), self._toggle_view_fullscreen
        )
        header.addWidget(self.fps_label, 1)
        help_text = {
            "mesh": self.tr("Left drag: orbit · Right or middle drag: pan · Wheel: zoom"),
            "scene": "\n".join((self.tr("Hold H to hover/select viewport objects."),
                                self.tr("Main REasy shortcuts are disabled in Scene tabs."))),
        }.get(self._controls)
        if help_text:
            header.addWidget(self._overlay_button("question", help_text, self._show_help_tip))
        header.addWidget(self.fullscreen_button)
        header.addWidget(self.overlay_fold_button)
        self.overlay_body = QWidget(self.overlay)
        self.overlay_body.setObjectName("viewportHudBody")
        self.overlay_body.setAttribute(Qt.WA_StyledBackground, True)
        body_layout = QVBoxLayout(self.overlay_body)
        body_layout.setContentsMargins(8, 6, 8, 2)
        body_layout.setSpacing(4)
        layout.addWidget(self.overlay_body)

        builders = {
            "mesh": self._build_mesh_controls,
            "rcol": self._build_rcol_controls,
            "scene": self._build_scene_controls,
        }
        builders.get(self._controls, self._build_scene_controls)(body_layout)
        self._align_hud_labels()

        self.setup_viewport_overlay(self.overlay, self.overlay_body, self.overlay_fold_button)
        self.overlay.adjustSize()
        self.place_viewport_overlays()

    def _overlay_button(self, icon: str, tip: str, slot=None) -> QToolButton:
        button = tool_button(icon, tip, slot, parent=self.overlay)
        button.setIconSize(QSize(14, 14))
        button.setFixedSize(20, 20)
        button.setFocusPolicy(Qt.NoFocus)
        return button

    def _show_help_tip(self) -> None:
        button = self.sender()
        QToolTip.showText(button.mapToGlobal(button.rect().bottomLeft()), button.toolTip(), button)

    def show_fullscreen_state(self, fullscreen: bool) -> None:
        self.fullscreen_button.setIcon(line_icon("fullscreen_exit" if fullscreen else "fullscreen"))
        self.fullscreen_button.setToolTip(
            self.tr("Leave fullscreen (F11 or Esc)") if fullscreen else self.tr("Fullscreen viewport"))

    def setup_viewport_overlay(
        self,
        widget: QWidget,
        body: QWidget | None = None,
        fold_button: QToolButton | None = None,
    ) -> None:
        self._overlays.setup(widget, body, fold_button)

    def set_viewport_overlay_folded(self, widget: QWidget, folded: bool) -> None:
        self._overlays.set_folded(widget, folded)

    def set_fullscreen_content(self, widget: QWidget | None) -> None:
        """Use a larger same-window container for structural fullscreen."""
        self._fullscreen_content = widget

    def _toggle_view_fullscreen(self):
        owner = getattr(self, "_external_fullscreen_owner", None)
        if owner is not None:
            owner.leave_view_fullscreen() if owner.is_view_fullscreen() else owner.enter_view_fullscreen()
        else:
            self.leave_view_fullscreen() if self._fullscreen_restore else self._enter_view_fullscreen()

    def _enter_view_fullscreen(self):
        if self._fullscreen_restore is not None:
            return
        self._fullscreen_transition = True
        content = self._fullscreen_content or self
        window = content.window()
        self._fullscreen_restore = _FullscreenRestore(
            window,
            window.windowState(),
            self._visible_siblings_to_window(content, window),
        )
        for widget in self._fullscreen_restore.hidden_widgets:
            widget.hide()
        self.show_fullscreen_state(True)
        window.showFullScreen()
        call_later(self, 0, self._after_fullscreen_change)

    @staticmethod
    def _visible_siblings_to_window(
        content: QWidget,
        window: QWidget,
    ) -> tuple[QWidget, ...]:
        """Return visible chrome outside content's branch, without reparenting it."""
        hidden: list[QWidget] = []
        branch = content
        while branch is not window:
            parent = branch.parentWidget()
            if parent is None:
                break
            hidden.extend(
                child
                for child in parent.children()
                if (
                    isinstance(child, QWidget)
                    and child is not branch
                    and not child.isWindow()
                    and child.isVisible()
                )
            )
            branch = parent
        return tuple(hidden)

    def leave_view_fullscreen(self):
        if self._fullscreen_restore is None:
            return
        self._fullscreen_transition = True
        restore = self._fullscreen_restore
        self._fullscreen_restore = None
        self.show_fullscreen_state(False)
        for widget in restore.hidden_widgets:
            with suppress(RuntimeError):
                widget.show()
        if restore.window_state & Qt.WindowFullScreen:
            restore.window.showFullScreen()
        elif restore.window_state & Qt.WindowMaximized:
            restore.window.showMaximized()
        else:
            restore.window.showNormal()
        call_later(self, 0, self._after_fullscreen_change)

    def _after_fullscreen_change(self):
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_Hover, True)
        self.setFocus(Qt.OtherFocusReason)
        self._gizmo_projection = None
        self.place_viewport_overlays()
        self.update()
        self._fullscreen_transition = False
        self._overlays.place_later(50)

    def is_fullscreen_transitioning(self) -> bool:
        return self._fullscreen_transition

    def eventFilter(self, obj, event):
        handled = self._overlays.event_filter(obj, event)
        return super().eventFilter(obj, event) if handled is None else handled

    def _build_scene_controls(self, layout: QVBoxLayout):
        self._add_scene_mode_control(layout)
        self._add_texture_quality_control(layout)
        self._add_antialiasing_control(layout)
        self._add_fps_limit_control(layout)
        self._add_separator(layout)
        self._add_control_row(
            layout,
            self.tr("Speed"), self._camera_spin("camera_speed", 0.01, 50.0, 0.1, self.tr("Fly speed of the camera.")),
            self.tr("Look"), self._camera_spin("camera_look", 0.01, 2.0, 0.01, self.tr("Mouse look sensitivity.")),
        )
        self._add_control_row(
            layout,
            self.tr("Wheel"), self._camera_spin("camera_wheel", 0.001, 2.0, 0.01,
                                                self.tr("How far a wheel step moves the camera, times its speed.")),
            self.tr("Fast"), self._camera_spin("camera_boost", 1.0, 20.0, 0.25,
                                               self.tr("Speed multiplier while Shift is held.")),
            self.tr("Slow"), self._camera_spin("camera_slow", 0.01, 1.0, 0.05,
                                               self.tr("Speed multiplier while Ctrl is held.")),
        )
        self._add_separator(layout)
        self._add_light_control(layout)
        self.probe_exposure_spin = self._float_spin(0.01, 2.0, 0.01, self.probe_exposure, self._set_probe_exposure)
        self.probe_exposure_spin.setToolTip(self.tr("Exposure of the light probes' lighting and of the probe points."))
        self._shown_under("textures", self._add_control_row(layout, self.tr("Probe Exp"), self.probe_exposure_spin))
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
        self.probe_viz_combo.setToolTip(self.tr("Light probes drawn in the viewport: their volumes, points, or both."))
        self.probe_viz_points_spin = QSpinBox(self.overlay)
        self.probe_viz_points_spin.setRange(100, 50000)
        self.probe_viz_points_spin.setSingleStep(500)
        self.probe_viz_points_spin.setProperty("scrub", True)
        self.probe_viz_points_spin.setToolTip(self.tr("Most probe points drawn."))
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
        self.probe_status_label.setObjectName("overlayStatus")
        self.probe_status_label.setWordWrap(True)
        layout.addWidget(self.probe_status_label)
        self._refresh_probe_status_label()
        self._add_separator(layout)
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

    def _add_light_control(self, layout: QVBoxLayout):
        self.scene_shading_combo = self._data_combo(((self.tr("Textures"), "textures"),), self.set_shading)
        self.scene_shading_combo.setToolTip(self.tr(
            "Game lighting draws meshes with their materials' shaders and the game's light passes, where REasy has "
            "them for the build; Textures draws their textures, lit by the chosen light."))
        self._add_control_row(layout, self.tr("Shading"), self.scene_shading_combo)
        self.scene_light_combo = self._data_combo(
            (
                (self.tr("Fixed"), "fixed"),
                (self.tr("Software"), "software"),
                (self.tr("Probes"), "probes"),
                (self.tr("Off"), "off"),
            ),
            self._set_lighting_mode,
            self.texture_light,
        )
        self._shown_under("textures", self._add_control_row(layout, self.tr("Light"), self.scene_light_combo))

    def _build_rcol_controls(self, layout: QVBoxLayout):
        self._add_scene_mode_control(layout)
        self._add_fps_limit_control(layout)
        self._add_highlight_filter_control(layout)

    def _wireframe_combo(self) -> EmbeddedPopupComboBox:
        return self._data_combo(((self.tr("Off"), "off"), (self.tr("Wire"), "wire"), (self.tr("Hybrid"), "hybrid"),
                                 (self.tr("X-ray"), "xray")), self._set_wireframe_mode, self.wireframe_mode)

    def _add_scene_mode_control(self, layout: QVBoxLayout):
        self.scene_mode_combo = self._wireframe_combo()
        self._add_control_row(layout, self.tr("Wireframe"), self.scene_mode_combo)

    def _add_highlight_filter_control(self, layout: QVBoxLayout):
        self.highlight_only_check = QCheckBox(
            self.tr("View only highlighted"), self.overlay
        )
        self.highlight_only_check.setChecked(self.show_only_highlighted)
        self.highlight_only_check.toggled.connect(self._set_show_only_highlighted)
        layout.addWidget(self.highlight_only_check)

    def _add_fps_limit_control(self, layout: QVBoxLayout):
        fps_spin = QSpinBox(self.overlay)
        fps_spin.setRange(0, LIMIT_MOST)
        fps_spin.setProperty("scrub", True)
        fps_spin.setValue(self._fps_limit)
        fps_spin.setToolTip(
            self.tr("Frame rate of the viewport, animation poses and the game's strand chain; 0 is uncapped (strands "
                    "then step at most {most} times a second).").format(most=LIMIT_MOST)
        )
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

    def _add_antialiasing_control(self, layout: QVBoxLayout):
        check = QCheckBox(self.tr("Anti-aliasing (REasy custom)"), self.overlay)
        check.setChecked(self.antialiasing)
        check.setToolTip(self.tr(
            "Game lighting: smooths edges, brows and dithered detail by averaging frames drawn a fraction of a pixel "
            "apart while the view stays still. Strands are left out; their renderer smooths them."))
        check.toggled.connect(self._set_antialiasing)
        self._shown_under("game", self._add_control_row(layout, check))

    def _set_antialiasing(self, on: bool):
        self.antialiasing = bool(on)
        self._save_view_setting("renderer_antialiasing", self.antialiasing)
        self.update()

    def _shown_under(self, shading: str, row: QHBoxLayout) -> None:
        widgets = [row.itemAt(index).widget() for index in range(row.count())]
        for widget in filter(None, widgets):
            self._shading_widgets[shading].append(widget)
            widget.setVisible(self._shading_shown == shading)

    def set_shading_controls(self, shading: str) -> None:
        if shading == self._shading_shown:
            return
        self._shading_shown = shading
        for kind, widgets in self._shading_widgets.items():
            for widget in widgets:
                widget.setVisible(kind == shading)
        overlay = getattr(self, "overlay", None)
        if overlay is not None:
            overlay.adjustSize()
            self.place_viewport_overlays()

    def _add_control_row(self, layout: QVBoxLayout, *items):
        row = QHBoxLayout()
        row.setSpacing(6)
        for index, item in enumerate(items):
            if isinstance(item, str):
                label = QLabel(item, self.overlay)
                label.setObjectName("overlayFieldLabel")
                label.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
                if index == 0 and len(items) > 1:
                    self._hud_labels.append(label)
                item = label
            row.addWidget(item)
        layout.addLayout(row)
        return row

    def _align_hud_labels(self) -> None:
        width = max((label.sizeHint().width() for label in self._hud_labels), default=0)
        for label in self._hud_labels:
            label.setFixedWidth(width)

    def _add_separator(self, layout: QVBoxLayout) -> None:
        line = QFrame(self.overlay)
        line.setFrameShape(QFrame.HLine)
        layout.addWidget(line)

    def _data_combo(self, items, slot, current=None) -> EmbeddedPopupComboBox:
        combo = EmbeddedPopupComboBox(self.overlay)
        # Items added later, such as Game lighting, widen it and the panel.
        combo.setSizeAdjustPolicy(EmbeddedPopupComboBox.AdjustToContents)
        for label, data in items:
            combo.addItem(label, data)
        if current is not None and (index := combo.findData(current)) >= 0:
            combo.setCurrentIndex(index)
        combo.currentIndexChanged.connect(lambda _: slot(combo.currentData()))
        return combo

    def _float_spin(self, minimum: float, maximum: float, step: float, value: float, slot) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self.overlay)
        spin.setRange(minimum, maximum)
        spin.setDecimals(3 if step < 0.01 else 2)
        spin.setSingleStep(step)
        spin.setProperty("scrub", True)
        spin.setValue(value)
        spin.valueChanged.connect(slot)
        return spin

    def _camera_spin(self, attr: str, minimum: float, maximum: float, step: float, tip: str) -> QDoubleSpinBox:
        spin = self._float_spin(minimum, maximum, step, getattr(self, attr), lambda value: self._set_camera_setting(attr, f"scene_{attr}", value))
        spin.setToolTip(tip)
        return spin

    def _build_mesh_controls(self, layout: QVBoxLayout):
        self._add_texture_quality_control(layout)
        self._add_antialiasing_control(layout)
        self._add_fps_limit_control(layout)
        self._add_separator(layout)

        self.wf_combo = self._wireframe_combo()
        self.line_spin = self._float_spin(0.5, 8.0, 0.1, self.line_width, self._set_line_width)
        self.line_spin.setToolTip(self.tr("Width of the wireframe's lines, in pixels."))
        self._add_control_row(
            layout,
            self.tr("Wireframe"),
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
        self.amb_spin = self._float_spin(0.0, 1.0, 0.05, self.ambient, self._set_ambient)
        self.diff_spin = self._float_spin(0.0, 1.0, 0.05, self.diffuse, self._set_diffuse)
        self.amb_spin.setToolTip(self.tr("Ambient light: the brightness of faces turned away from the light."))
        self.diff_spin.setToolTip(self.tr("Diffuse light: the brightness the light adds to faces turned to it."))
        self._shown_under("textures", self._add_control_row(
            layout, self.tr("Light"), self.light_combo, self.tr("Amb"), self.amb_spin, self.tr("Diff"),
            self.diff_spin))
        mesh = getattr(self, "mesh", None)
        if getattr(mesh, "streaming_buffer_count", 0):
            stream_status = (
                self.tr("Loaded")
                if getattr(mesh, "streaming_data_loaded", False)
                else self.tr("Missing")
            )
            self._add_control_row(layout, self.tr("Stream {status}").format(status=stream_status))

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
        self._skinning.clear()
        self._mesh_poses.clear()
        self._mesh_draw_matrices.clear()
        self._meshes = meshes
        self._device_changed()
        self._highlighted_keys = set(self._selection_keys if highlighted_keys is None else highlighted_keys)
        self._hover_key = self._hover_block_key = ""
        self._recompute_bounds()
        self._refresh_selection_bounds()
        if reset_camera:
            self.freecam.reset(self.center, self.extent, self.camera_speed)
        self.update()

    def set_mesh_skinning(
        self,
        key: str,
        binding: SceneSkinningBinding,
    ) -> None:
        matches = [mesh for mesh in self._meshes if mesh.key == key]
        if len(matches) != 1:
            raise ValueError(
                f"skinned mesh key {key!r} matched {len(matches)} scene meshes"
            )
        if len(matches[0].vertices) != len(binding.positions):
            raise ValueError(
                f"skinned mesh {key!r} has {len(binding.positions)} bind "
                f"vertices; scene geometry has {len(matches[0].vertices)}"
            )
        self._skinning.set_binding(key, binding)
        self._device_rebound({key})
        self.update()

    def update_mesh_skinning(
        self,
        key: str,
        matrices: np.ndarray,
    ) -> None:
        self._skinning.update_palette(key, matrices)
        self._device_moved({key})
        self.update()

    def update_mesh_skinning_source(
        self,
        key: str,
        positions: np.ndarray,
        normals: np.ndarray | None,
    ) -> None:
        self._skinning.update_source(key, positions, normals)
        for mesh in self._meshes:
            if mesh.key == key:
                self._device_reshaped(mesh)
        self.update()

    def offer_game_lighting(self) -> None:
        combo = getattr(self, "scene_shading_combo", None)
        if combo is None:
            self._add_light_control(self.overlay_body.layout())
            self._align_hud_labels()
            self.overlay.adjustSize()
            self.place_viewport_overlays()
            combo = self.scene_shading_combo
        if combo.findData("game") < 0:
            combo.blockSignals(True)
            combo.insertItem(0, self.tr("Game lighting"), "game")
            combo.blockSignals(False)
        self._game_offered = True
        self._apply_shading()
        # The panel is sized again once the combo box's new item has reached the layouts.
        self._overlays.place_later()

    def set_shading(self, shading: str) -> None:
        if shading not in ("game", "textures"):
            return
        self.shading = shading
        self._save_view_setting("renderer_shading", shading)
        self._apply_shading()

    def refuse_game_lighting(self, reason: str) -> None:
        if reason != self._game_refused:
            self._game_refused = reason
            self._apply_shading()

    @property
    def game_lighting_refusal(self) -> str:
        return self._game_refused if self._game_chosen else ""

    @property
    def _game_chosen(self) -> bool:
        return self.shading != "textures" and self._game_offered

    def _apply_shading(self) -> None:
        game = self._game_chosen and not self._game_refused
        lighting = "game" if game else self.texture_light
        shading = getattr(self, "scene_shading_combo", None)
        if shading is not None and (index := shading.findData("game")) >= 0:
            shading.model().item(index).setEnabled(not self._game_refused)
            shading.setItemData(index, self._game_refused or None, Qt.ItemDataRole.ToolTipRole)
        for combo, value in ((shading, "game" if game else "textures"),
                             (getattr(self, "scene_light_combo", None), self.texture_light)):
            if combo is not None and combo.findData(value) >= 0:
                combo.blockSignals(True)
                combo.setCurrentIndex(combo.findData(value))
                combo.blockSignals(False)
        self.set_shading_controls("game" if game else "textures")
        if lighting != self.lighting_mode:
            self.lighting_mode = lighting
            self._refresh_probe_status_label()
            self.update()

    def set_mesh_pose(self, key: str, pose: ScenePose | None) -> None:
        if pose is None:
            self._mesh_poses.pop(key, None)
        else:
            self._mesh_poses[key] = pose
        self._device_moved({key})
        self.update()

    def clear_mesh_skinning(self, keys: set[str] | None = None) -> None:
        removed = self._skinning.remove(keys)
        if removed:
            self._device_rebound(removed)
        self.update()

    def set_light_probe_instances(self, instances: list[SceneLightProbeInstance]) -> None:
        self._light_probe_instances = {instance.key: instance for instance in instances}
        loaded = sum(instance.probe_set is not None for instance in instances)
        self._light_probe_status = (
            self.tr("LightProbes: {loaded}/{total} loaded").format(loaded=loaded, total=len(instances))
            if instances
            else ""
        )
        self._invalidate_probe_viz_points()
        if loaded and self._controls != "mesh" and any(len(mesh.vertices) for mesh in self._meshes):
            self.texture_light = "probes"
            self._apply_shading()
        self._refresh_probe_status_label()
        self.update()

    @property
    def light_probe_status(self) -> str:
        return self._light_probe_status

    def _refresh_probe_status_label(self) -> None:
        label = getattr(self, "probe_status_label", None)
        if label is None:
            return
        visible = self._visible_light_probe_instances()
        loaded = [instance for instance in visible if instance.probe_set is not None]
        if not loaded:
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
        probe_count = sum(len(self._probe_viz_candidate_indices(instance)) for instance in loaded)
        label.setText(
            self.tr("Probe: {mode} | sets {sets} | OBB {boxes} | probes {probes}").format(
                mode=mode, sets=len(loaded), boxes=len(boxes), probes=probe_count
            )
        )

    def _light_probe_boxes(self) -> list[object]:
        return [box for _key, box in self._light_probe_box_entries()]

    def _light_probe_box_entries(self) -> list[tuple[str, object]]:
        return [
            (instance.key, box)
            for instance in self._visible_light_probe_instances()
            for box in self._instance_light_probe_boxes(instance)
        ]

    @staticmethod
    def _instance_light_probe_boxes(instance: SceneLightProbeInstance) -> list[object]:
        return [
            box
            for box in instance.obbs
            if not getattr(box, "is_default_unit_box", lambda: False)()
        ]

    def _visible_light_probe_instances(self) -> list[SceneLightProbeInstance]:
        return [
            instance
            for key, instance in self._light_probe_instances.items()
            if key not in self._hidden_keys
        ]

    def _set_probe_exposure(self, value: float) -> None:
        self.probe_exposure = float(value)
        self._save_view_setting("scene_probe_exposure", self.probe_exposure)
        self._invalidate_probe_viz_points()
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
        self._probe_viz_candidate_indices_cache.clear()
        self._probe_viz_point_cache_key = None
        self._probe_viz_point_cache = None

    def set_selected_keys(
        self,
        keys: set[str],
        *,
        preferred_keys: set[str] | None = None,
        focus: bool = False,
    ) -> None:
        keys = set(keys)
        preferred = set(preferred_keys or ())
        if keys == self._selection_keys and preferred == self._preferred_selection_keys and not focus:
            return
        self._finish_gizmo_drag(commit=False)
        self._selection_keys = keys
        self._preferred_selection_keys = preferred
        self._highlighted_keys = set(keys)
        self._gizmo_hover_axis = None
        self._refresh_selection_bounds()
        if focus:
            self.focus_selection()
        self.update()

    def update_mesh_transforms(
        self,
        matrices: dict[str, np.ndarray],
        *,
        recompute_bounds: bool = True,
    ) -> None:
        if not matrices:
            return
        matches = [mesh for mesh in self._meshes if mesh.key in matrices]
        for mesh in matches:
            mesh.transform_matrix = np.asarray(matrices[mesh.key], dtype=np.float32).reshape(4, 4)
        if matches:
            self._device_moved({mesh.key for mesh in matches})
        if recompute_bounds:
            self._recompute_bounds()
            self._refresh_selection_bounds()
        self.update()

    def set_mesh_draw_transforms(
        self,
        matrices: dict[str, np.ndarray],
    ) -> None:
        """Set transient world transforms without rewriting vertex buffers."""
        by_key = {mesh.key: mesh for mesh in self._meshes}
        draw_matrices: dict[str, np.ndarray] = {}
        for key, value in matrices.items():
            mesh = by_key.get(str(key))
            if mesh is None:
                continue
            world = np.asarray(value, dtype=np.float32).reshape(4, 4)
            authored = np.asarray(
                mesh.transform_matrix
                if mesh.transform_matrix is not None
                else IDENTITY4,
                dtype=np.float32,
            )
            if not np.isfinite(world).all():
                raise ValueError(f"mesh {key!r} has a nonfinite draw transform")
            delta = world @ np.linalg.inv(authored)
            draw_matrices[str(key)] = delta.astype(np.float32, copy=False)
        self._device_moved(self._mesh_draw_matrices.keys() | draw_matrices.keys())
        self._mesh_draw_matrices = draw_matrices
        self.update()

    def clear_mesh_draw_transforms(self) -> None:
        self.set_mesh_draw_transforms({})

    def update_mesh_geometry(
        self,
        key: str,
        vertices: np.ndarray,
        normals: np.ndarray | None = None,
        *,
        recompute_bounds: bool = True,
        colors: np.ndarray | None = None,
    ) -> None:
        """Replace one keyed mesh's vertex data without rebuilding its scene."""
        vertices = np.asarray(vertices, dtype=np.float32).reshape(-1, 3)
        normals = (
            np.asarray(normals, dtype=np.float32).reshape(-1, 3)
            if normals is not None
            else None
        )
        if normals is not None and len(normals) != len(vertices):
            raise ValueError("dynamic mesh normals must match its vertex count")
        if colors is not None:
            colors = np.asarray(colors, dtype=np.float32).reshape(-1, 4)
            if len(colors) != len(vertices):
                raise ValueError("dynamic mesh colors must match its vertex count")
        matches = [mesh for mesh in self._meshes if mesh.key == key]
        if len(matches) != 1:
            raise ValueError(f"dynamic mesh key {key!r} matched {len(matches)} meshes")
        mesh = matches[0]
        if len(mesh.vertices) != len(vertices):
            raise ValueError(
                f"dynamic mesh {key!r} has {len(vertices)} vertices; expected {len(mesh.vertices)}"
            )
        if mesh.normals is None and normals is not None or mesh.colors is None and colors is not None:
            self._device_changed()
        self._device_reshaped(mesh)
        mesh.vertices = vertices
        if normals is not None:
            mesh.normals = normals
        if colors is not None:
            mesh.colors = colors
        if recompute_bounds:
            self._recompute_bounds()
            self._refresh_selection_bounds()
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
        probe_visibility_changed = bool(
            (keys ^ self._hidden_keys) & self._light_probe_instances.keys()
        )
        self._hidden_keys = keys
        if self._hover_key in self._hidden_keys:
            self._hover_key = ""
        self._refresh_selection_bounds()
        if probe_visibility_changed:
            self._invalidate_probe_viz_points()
            self._refresh_probe_status_label()
        if refresh:
            self.update()

    def set_hidden_parts(
        self,
        parts: dict[str, set[int] | frozenset[int]],
        *,
        refresh: bool = True,
    ) -> None:
        normalized = {
            str(key): frozenset(int(index) for index in indices if index >= 0)
            for key, indices in parts.items()
            if indices
        }
        if normalized == self._hidden_parts:
            return
        self._hidden_parts = normalized
        if refresh:
            self.update()

    def set_material_parameters(self, values: dict[str, dict[str, float]]) -> None:
        values = {str(material): {str(name): float(value) for name, value in parameters.items()}
                  for material, parameters in values.items() if parameters}
        if values == self._material_parameters:
            return
        if values.keys() != self._material_parameters.keys():
            self._device_rebound(())
        else:
            self._device_moved(())
        self._material_parameters = values
        self.update()

    def device_materials(self) -> dict[str, dict[str, float]]:
        return self._material_parameters

    def materials_changed(self):
        self._device_changed()
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
            if self.edit_tool is not None and self.edit_tool.press(event, self._screen_pos(event)):
                event.accept()
                return
            if self._hover_detect_down:
                self._scene_pick("click", self._screen_pos(event))
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
        if (self.edit_tool is not None and self._cursor_lock_pos is None
                and self.edit_tool.move(event, self._screen_pos(event))):
            event.accept()
            return
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
        if self.edit_tool is not None and self.edit_tool.release(event, self._screen_pos(event)):
            event.accept()
            return
        if not (event.buttons() & Qt.RightButton):
            self._unlock_scene_cursor()
        event.accept()

    def wheelEvent(self, event):
        if self._controls == "mesh":
            super().wheelEvent(event)
            return
        if self.edit_tool is not None and self.edit_tool.wheel(event):
            event.accept()
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
        if self._controls != "mesh" and self.edit_tool is not None and self.edit_tool.key_press(event):
            event.accept()
            return
        if self._controls != "mesh" and event.key() == HOVER_DETECT_KEY:
            if not event.isAutoRepeat():
                self._hover_detect_down = True
                pos = self.mapFromGlobal(QCursor.pos())
                if self.rect().contains(pos):
                    self._update_scene_hover(np.array((pos.x(), pos.y()), dtype=np.float32))
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
                self._hover_detect_down = False
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
        self._hover_detect_down = False
        self.freecam.clear_input()
        super().focusOutEvent(event)

    def leaveEvent(self, event):
        self._set_hover_key("")
        if self.edit_tool is not None:
            self.edit_tool.leave()
        super().leaveEvent(event)

    def event(self, event):
        if (event.type() == QEvent.ShortcutOverride and self.edit_tool is not None
                and self.edit_tool.takes_key(event)):
            # Accepting ShortcutOverride gives the tool its keys before the window's shortcuts.
            event.accept()
            return True
        if event.type() == QEvent.HoverMove and getattr(self, "_hover_detect_down", False) and getattr(self, "_controls", "mesh") != "mesh" and getattr(self, "_gizmo_drag", None) is None and getattr(self, "_cursor_lock_pos", None) is None:
            self._update_scene_hover(self._screen_pos(event))
            return True
        return super().event(event)

    def _set_gizmo_hover(self, axis: int | None) -> None:
        if axis == self._gizmo_hover_axis:
            return
        self._gizmo_hover_axis = axis
        self.update()

    def _update_scene_hover(self, pos: np.ndarray) -> None:
        axis = self._pick_gizmo_axis(pos)
        self._set_gizmo_hover(axis)
        if axis is None:
            self._scene_pick("hover", pos)
        else:
            self._set_hover_key("")

    def _scene_pick(self, kind: str, pos: np.ndarray) -> None:
        """At once, on the CPU, from what the frame shown drew."""
        frame = self._device_frame
        pickable = getattr(frame, "pickable", None)
        key = ""
        if pickable is not None and frame.view.content == self._device_content and self._device_window is not None \
                and not self._device_window.isHidden():
            camera = frame.view.camera()
            width, height = camera.size
            ratio = width / max(self.width(), 1)
            texel = min(max(int(pos[0] * ratio), 0), width - 1), min(max(int(pos[1] * ratio), 0), height - 1)
            geometries, meshes = self._device_content_shown()
            number = self._picker.pick(camera, texel, geometries, self.device_motion(), pickable, frame.view.hidden,
                                       frame.view.hidden_parts)
            key = "" if number is None or number >= len(meshes) else meshes[number].key
        self._finish_scene_pick(kind, key)
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
        self.update()

    def _recompute_bounds(self):
        self.center, self.extent = scene_bounds(self._meshes)
        self.scale = 1.0 / self.extent if self.extent > 1e-6 else 1.0

    def _refresh_selection_bounds(self) -> None:
        held = self._tool_gizmo()
        if held is not None:
            self._selection_center, self._selection_extent = held
            return
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
        return [box for _key, box in self._selected_light_probe_box_entries()]

    def _selected_light_probe_box_entries(self) -> list[tuple[str, object]]:
        selected = self._preferred_selection_keys & self._light_probe_instances.keys()
        if not selected:
            selected = self._selection_keys & self._light_probe_instances.keys()
        return [
            (key, box)
            for key in selected
            if key not in self._hidden_keys
            for box in self._instance_light_probe_boxes(self._light_probe_instances[key])
        ]

    def _selection_is_whole_scene(self) -> bool:
        visible = {mesh.key for mesh in self._meshes if mesh.key not in self._hidden_keys}
        return bool(visible) and visible <= self._selection_keys

    def _gizmo_size(self) -> float:
        if self._controls != "mesh" and self._selection_center is not None:
            distance = max(float(np.linalg.norm(self._selection_center - self.freecam.pos)), 1.0)
            return max(2.0 * distance * np.tan(np.deg2rad(22.5)) * 120.0 / max(self.height(), 1), self.extent * 0.004, 0.1)
        return max(float(self._selection_extent) * 0.45, max(float(self.extent) * 0.025, 0.25))

    def _view_pixels(self) -> tuple[int, int]:
        if self._device_window is not None and not self._device_window.isHidden():
            return self._device_window.pixels()
        dpr = float(self.devicePixelRatioF())
        return max(1, int(self.width() * dpr)), max(1, int(self.height() * dpr))

    def _device_camera(self, size: tuple[int, int]) -> Camera:
        if self._controls == "mesh":
            return engine_camera(float(self.rot_x), float(self.rot_y), float(self.distance),
                                 tuple(map(float, self.center)), float(self.scale), size, bool(self.orthographic))
        camera = self.freecam
        return free_camera(tuple(map(float, camera.pos)), float(camera.pitch), float(camera.yaw), 0.01,
                           max(float(self.extent) * 6.0, 1000.0), size)

    def _cache_gizmo_projection(self) -> None:
        self._gizmo_projection = self.view_camera()

    def _project_gizmo_point(self, point) -> np.ndarray | None:
        """In widget units."""
        if self._gizmo_projection is None:
            return None
        camera, ratio = self._gizmo_projection
        drawn, grid, _depths = project_points(np.asarray(point, np.float64).reshape(1, 3), camera)
        return (grid[0] / (GRID * ratio)).astype(np.float32) if len(drawn) else None

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
                    screen = [self._project_gizmo_point(point) for point in gizmo_plane_corners(center, size, a, b)]
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
            for axis_index, points in enumerate(gizmo_circle_points(center, size)):
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
        key = key if key and key not in self._hidden_keys else ""
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
        tool = self.edit_tool if self._tool_gizmo() is not None else None
        self._gizmo_drag = {
            "tool": tool,
            "mode": self._gizmo_mode,
            "axis": handle,
            "start": self._screen_pos(event),
            "center": self._selection_center.copy(),
            "size": self._gizmo_size(),
            "extent": max(float(self._selection_extent), 1e-3),
            "screen_axis": None if screen_axis is None else screen_axis / np.linalg.norm(screen_axis),
            "matrices": {} if tool is not None else {mesh.key: np.asarray(mesh.transform_matrix if mesh.transform_matrix is not None else np.identity(4), dtype=np.float32) for mesh in self._meshes if mesh.key in self._selection_keys},
            "probe_matrices": [] if tool is not None else [
                (key, box, box.matrix())
                for key, box in self._selected_light_probe_box_entries()
                if hasattr(box, "matrix")
            ],
        }
        if isinstance(handle, tuple):
            self._gizmo_drag["plane_axes"] = handle
            self._gizmo_drag["screen_basis_inv"] = np.linalg.inv(basis)
        if tool is not None:
            tool.gizmo_begin()
        elif not self._gizmo_drag["matrices"] and not self._gizmo_drag["probe_matrices"]:
            self._gizmo_drag = None
            return False
        self._set_hover_key("")
        self._gizmo_drag["whole_scene"] = self._selection_is_whole_scene()
        self._gizmo_hover_axis = handle
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
        if drag["tool"] is not None:
            drag["tool"].gizmo_drag(matrix)
        self._apply_light_probe_drag_matrix(matrix)
        self._device_moved(drag["matrices"])
        self.update()

    def _finish_gizmo_drag(self, *, commit: bool) -> None:
        drag = self._gizmo_drag
        if drag is None:
            return
        matrix = drag.get("matrix")
        if drag.get("tool") is not None:
            drag["tool"].gizmo_end(commit and matrix is not None)
        committed = {}
        if commit and matrix is not None:
            for mesh in self._meshes:
                start = drag["matrices"].get(mesh.key)
                if start is not None:
                    mesh.transform_matrix = (matrix @ start).astype(np.float32, copy=False)
                    committed[mesh.key] = mesh.transform_matrix.copy()
            if drag.get("probe_matrices"):
                self._apply_light_probe_drag_matrix(matrix)
                for key, box, _start in drag["probe_matrices"]:
                    committed.setdefault(key, np.asarray(box.matrix(), dtype=np.float32).copy())
        if not commit and drag.get("probe_matrices"):
            self._restore_light_probe_drag_matrices()
        self._gizmo_drag = None
        self._device_moved(drag["matrices"])
        if committed:
            self.gizmo_transform_committed.emit((committed, bool(drag.get("whole_scene"))))
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
        for _key, box, start in drag.get("probe_matrices", ()):
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
        for _key, box, start in drag.get("probe_matrices", ()):
            try:
                box.set_from_matrix(start)
                changed = True
            except Exception:
                continue
        if changed:
            self._invalidate_probe_viz_points()

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

    def _gizmo_active_axis(self) -> int | None:
        return self._gizmo_drag["axis"] if self._gizmo_drag else self._gizmo_hover_axis

    def _gizmo_marks(self) -> tuple:
        if self._controls == "mesh" or self._selection_center is None:
            return ()
        return gizmo_marks(self._gizmo_mode, self._selection_center, self._gizmo_size(), self._gizmo_active_axis())

    def _probe_viz_candidate_indices(self, instance: SceneLightProbeInstance) -> np.ndarray:
        probe_set = instance.probe_set
        if probe_set is None or not len(probe_set.probe_positions):
            return np.zeros((0,), dtype=np.int64)
        boxes = self._instance_light_probe_boxes(instance)
        key = (instance.key, id(probe_set), tuple(id(box) for box in boxes))
        cached = self._probe_viz_candidate_indices_cache.get(key)
        if cached is not None:
            return cached
        if not boxes:
            indices = np.arange(probe_set.probe_count, dtype=np.int64)
        else:
            points = np.asarray(probe_set.probe_positions, dtype=np.float32).reshape(-1, 3)
            inside = np.zeros(len(points), dtype=bool)
            for box in boxes:
                inside |= self._points_inside_light_probe_box(points, box)
            indices = np.flatnonzero(inside).astype(np.int64, copy=False)
        self._probe_viz_candidate_indices_cache[key] = indices
        return indices

    def _probe_viz_points(self) -> tuple[np.ndarray, np.ndarray]:
        entries = [
            (instance, self._probe_viz_candidate_indices(instance))
            for instance in self._visible_light_probe_instances()
            if instance.probe_set is not None
        ]
        entries = [(instance, indices) for instance, indices in entries if len(indices)]
        if not entries:
            return np.zeros((0, 3), dtype=np.float32), np.zeros((0, 4), dtype=np.float32)
        key = (
            tuple((instance.key, id(instance.probe_set), len(indices)) for instance, indices in entries),
            int(self.probe_viz_points),
            round(float(self.probe_exposure), 4),
        )
        if self._probe_viz_point_cache_key != key or self._probe_viz_point_cache is None:
            counts = np.asarray([len(indices) for _instance, indices in entries], dtype=np.int64)
            limit = max(1, int(self.probe_viz_points))
            if int(counts.sum()) <= limit:
                allocations = counts
            else:
                shares = counts.astype(np.float64) * (limit / float(counts.sum()))
                allocations = np.floor(shares).astype(np.int64)
                remainder = limit - int(allocations.sum())
                if remainder:
                    order = np.argsort(-(shares - allocations), kind="stable")
                    allocations[order[:remainder]] += 1
            clouds = [
                instance.probe_set.probe_point_cloud(
                    max_points=int(allocation),
                    normal=(0.0, 1.0, 0.0),
                    exposure=self.probe_exposure * max(0.0, float(instance.intensity)),
                    candidate_indices=indices,
                    normalize_display=True,
                )
                for (instance, indices), allocation in zip(entries, allocations)
                if allocation > 0 and instance.probe_set is not None
            ]
            self._probe_viz_point_cache = (
                np.concatenate([points for points, _colors in clouds], axis=0),
                np.concatenate([colors for _points, colors in clouds], axis=0),
            ) if clouds else (
                np.zeros((0, 3), dtype=np.float32),
                np.zeros((0, 4), dtype=np.float32),
            )
            self._probe_viz_point_cache_key = key
        return self._probe_viz_point_cache

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

    def _probe_layers(self) -> tuple[ProbeShadeLayer, ...]:
        return tuple(
            ProbeShadeLayer(
                key=instance.key,
                probe_set=instance.probe_set,
                boxes=snapshot_probe_boxes(self._instance_light_probe_boxes(instance)),
                priority=instance.priority,
                intensity=instance.intensity,
            )
            for instance in self._visible_light_probe_instances()
            if instance.probe_set is not None
        )

    def cleanup(self):
        self._timer.stop()
        self._frame_due.stop()
        self.leave_view_fullscreen()
        self._overlays.cleanup()
        self._clear_scene_memory()

    def _clear_scene_memory(self):
        with suppress(Exception):
            self._unlock_scene_cursor()
        self.freecam.clear_input()
        self._meshes.clear()
        self._skinning.clear()
        self._device_changed()
        self._mesh_draw_matrices.clear()
        self._highlighted_keys.clear()
        self._selection_keys.clear()
        self._hidden_keys.clear()
        self._hidden_parts.clear()
        self._material_parameters.clear()
        self._light_probe_instances.clear()
        self._light_probe_status = ""
        self._preferred_selection_keys.clear()
        self._invalidate_probe_viz_points()
        self._hover_key = self._hover_block_key = ""
        self._hover_detect_down = False
        self._selection_center = self._gizmo_drag = self._gizmo_projection = None

    def closeEvent(self, event):
        if self._fullscreen_restore is not None:
            self.leave_view_fullscreen()
            event.ignore()
            return
        self.cleanup()
        super().closeEvent(event)

    def place_viewport_overlays(self):
        self._overlays.place()

    def set_device_frames(self, enabled: bool) -> None:
        """Qt makes the native window's sibling widgets native too, so they lose translucency."""
        if enabled and self._device_window is None:
            # Made before frames are on: creating it repaints the viewport.
            window = FrameWindow(self)
            window.window_changed.connect(self.device_window_changed)
            window.setGeometry(self.rect())
            window.winId()
            window.lower()
            self._device_window = window
            self.device_window_changed.emit(int(window.winId()))
        self._device_frames, self._device_told, self._device_frame = bool(enabled), None, None
        self._sync_device_window()
        self.update()
        self._next_pass()

    def set_device_frame(self, frame) -> None:
        self._device_frame = frame
        if self._device_frames:
            self._presented += frame is not None
            self._next_pass()
        self._sync_device_window()
        self.update()

    def _sync_device_window(self) -> None:
        if self._device_window is None:
            return
        told, frame = self._device_told, self._device_frame
        shown = bool(self._device_frames and told is not None and frame is not None
                     and self._device_frame_shows(told[0], frame))
        if self._device_window.isHidden() == shown:
            self._device_window.setVisible(shown)

    def _device_view(self) -> tuple[object, object]:
        _geometries, meshes = self._device_content_shown()
        game = self.lighting_mode == "game"
        if self.lighting_mode == "probes":
            probes = self._device_probes()
            light = None if probes is None else ProbeLight(probes, float(self.probe_exposure))
        else:
            light = view_light(self.lighting_mode, self.ambient, self.diffuse)
        hidden = set(self._hidden_keys)
        if self.show_only_highlighted and self._highlighted_keys:
            hidden |= {mesh.key for mesh in meshes
                       if mesh.key not in self._highlighted_keys and not mesh.ignore_highlight_filter}
        hidden_parts = frozenset((key, part) for key, parts in self._hidden_parts.items() for part in parts)
        background = unorm(self._background)[:3]
        view = SceneView.through(self._device_camera(self._device_window.pixels()), light, background,
                                 self._device_content, self._device_motion, frozenset(hidden), hidden_parts,
                                 game=game, strands_only=game and self.strands_only,
                                 antialiased=game and self.antialiasing)
        return view, self._device_overlays(meshes, background)

    def _device_probes(self) -> ViewProbes | None:
        layers = sorted(self._probe_layers(), key=layer_order)
        made = tuple(layer.fingerprint() for layer in layers)
        if self._device_probes_made[0] != made:
            self._device_probes_made = made, ViewProbes.of(layers) if layers else None
        return self._device_probes_made[1]

    def _device_overlays(self, meshes: list[SceneDrawMesh], background: tuple[int, int, int]) -> LitOverlays:
        numbers = self._device_numbered(meshes)
        selected = frozenset(numbers[key] for key in self._selection_keys if key in numbers)
        pulse = round(255 * (0.22 + 0.10 * np.sin(time.perf_counter() * 3.5))) if selected else 56
        mode = self.wireframe_mode
        scene = SceneShown(
            NO_EDGES if mode == "off" else ALL_EDGES if mode == "xray" else VISIBLE_EDGES, 1.4,
            hovered=numbers.get(self._hover_key) if self._hover_key else None, selected=selected,
            selection_colour=(255, 219, 46, pulse), outline=max(1, round(2 * self._view_pixels()[0]
                                                                         / max(self.width(), 1))),
            filled=mode != "wire")
        probes = self._controls != "mesh"
        boxes = self._light_probe_box_entries() if probes and self.probe_viz_mode in {"volumes", "all"} else []
        points, colours = (self._probe_viz_points() if probes and self.probe_viz_mode in {"points", "all"}
                           else (np.zeros((0, 3)), np.zeros((0, 4))))
        keys = frozenset(self._selection_keys | self._preferred_selection_keys)
        # ``id(points)`` suffices: probe points are cached while unchanged.
        made = (background, scene, self._gizmo_mode, None if self._selection_center is None else (
            np.asarray(self._selection_center).tobytes(), self._gizmo_size(), self._gizmo_active_axis()),
            tuple((key, np.asarray(box.corners(), np.float64).tobytes()) for key, box in boxes),
            keys, id(points) if len(points) else None,
            tuple((minimum.tobytes(), maximum.tobytes(), colour) for minimum, maximum, colour in self.strands_boxes),
            edited := (() if self.edit_tool is None else self.edit_tool.marks()))
        if self._device_overlays_made[0] != made:
            self._device_overlays_made = made, LitOverlays(
                background, (*probe_marks(boxes, keys, points, colours), *bounds_marks(self.strands_boxes),
                             *edited, *self._gizmo_marks()),
                scene=scene)
        return self._device_overlays_made[1]

    def _show_device_rate(self) -> None:
        if self._device_frames and self._presented_clock.isValid():
            self._show_fps(self._presented * 1000.0 / max(self._presented_clock.elapsed(), 1))
        self._presented = 0
        self._presented_clock.start()

    def _device_frame_shows(self, view, frame) -> bool:
        """Parts hidden are not compared: a frame of the previous ones still matches."""
        drawn = frame.view
        return (isinstance(view, SceneView)
                and (drawn.content, drawn.size, drawn.hidden, drawn.game)
                == (view.content, view.size, view.hidden, view.game))

    def _device_changed(self) -> None:
        self._device_content += 1
        self._device_scene = self._device_placed = self._device_before = None
        self._device_shapes.clear()

    def _device_rebound(self, keys) -> None:
        """The device gets new content only if it differs, so an identical rebind (switching animations) keeps it."""
        if self._device_scene is not None and self._device_before is None:
            self._device_before = self._device_scene[0], frozenset(self._material_parameters)
        self._device_scene = None
        for mesh in self._meshes:
            if mesh.key in keys:
                self._device_shapes.pop(id(mesh), None)
        self._device_moved()

    def _device_reshaped(self, mesh: SceneDrawMesh) -> None:
        geometries, meshes = self._device_scene or (None, [])
        number = next((number for number, shown in enumerate(meshes) if shown is mesh), None)
        if number is not None and sum(geometry.vertices == geometries[number].vertices
                                      for geometry in geometries) > 1:
            self._device_changed()
            return
        self._device_shapes.pop(id(mesh), None)
        self._device_moved({mesh.key})

    def _device_numbered(self, meshes: list[SceneDrawMesh]) -> dict[str, int]:
        if self._device_numbers is None or self._device_numbers[0] is not meshes:
            self._device_numbers = meshes, {mesh.key: number for number, mesh in enumerate(meshes)}
        return self._device_numbers[1]

    def _device_moved(self, keys=None) -> None:
        """The meshes ``keys`` (None: any) are placed or posed otherwise."""
        self._device_motion += 1
        if keys is None or self._device_moving is None:
            self._device_moving = None
        else:
            self._device_moving.update(keys)

    def _device_content_shown(self) -> tuple[tuple[SceneGeometry, ...], list[SceneDrawMesh]]:
        if self._device_scene is None:
            self._device_scene = self._device_geometries()
            before, self._device_before = self._device_before, None
            if before is not None and not _same(before, (self._device_scene[0], frozenset(self._material_parameters))):
                self._device_content += 1
                self._device_placed = None
                self._device_shapes.clear()
        return self._device_scene

    def _device_geometries(self) -> tuple[tuple[SceneGeometry, ...], list[SceneDrawMesh]]:
        geometries, meshes = [], []
        triangles_of: dict[tuple[int, int], np.ndarray] = {}
        uvs_of: dict[int, np.ndarray] = {}
        first: dict[tuple[int, ...], int] = {}
        for mesh in self._meshes:
            count = len(np.asarray(mesh.vertices).reshape(-1, 3))
            batches = []
            for batch in mesh.batches or [SceneDrawBatch(indices=mesh.indices, material_name=mesh.material_name)]:
                shared = id(batch.indices), count
                if shared not in triangles_of:
                    indices = np.asarray(batch.indices, np.uint32)
                    if indices.ndim != 1:
                        indices = indices.reshape(-1)
                    triangles = indices[:len(indices) // 3 * 3].reshape(-1, 3)
                    inside = (triangles < count).all(axis=1)
                    # A batch whose triangles are all whole and of the mesh's vertices is shared, not copied.
                    triangles_of[shared] = indices if inside.all() and len(indices) % 3 == 0 \
                        else triangles[inside].reshape(-1)
                if len(triangles_of[shared]):
                    batches.append(batch if triangles_of[shared] is batch.indices
                                   else replace(batch, indices=triangles_of[shared]))
            if not batches:
                continue
            skin = self._skinning.influences(mesh.key)
            flat = np.ones(4)
            if self._vertex_colours(mesh, count) is None:
                given = np.asarray(mesh.color, np.float64).reshape(-1)[:4]
                flat[:len(given)] = given
            sets = []
            if mesh.uvs is not None and np.size(mesh.uvs) == count * 2:
                for given in (mesh.uvs, mesh.uv1, mesh.uv2):
                    if given is not None and np.size(given) == count * 2:
                        if id(given) not in uvs_of:
                            uvs_of[id(given)] = np.asarray(given, np.float32).reshape(-1, 2)
                        sets.append(uvs_of[id(given)])
            uvs = sets[0] if sets else None
            number = len(geometries)
            if skin is None:
                number = first.setdefault((id(mesh.vertices), id(mesh.normals), id(mesh.colors), id(mesh.uvs)), number)
            geometries.append(SceneGeometry(mesh.key, number, uvs, tuple(map(float, flat)),
                                            *((None, None) if skin is None else skin), tuple(batches),
                                            solid=bool(mesh.force_solid), edge_excluded=mesh.edge_excluded,
                                            decal_receive_mode=mesh.decal_receive_mode,
                                            transparent_bias=mesh.transparent_bias,
                                            speedtree_placement=mesh.speedtree_placement,
                                            more_uvs=tuple(sets[1:])))
            meshes.append(mesh)
        return tuple(geometries), meshes

    @property
    def color_source(self) -> str:
        """"vertex" to show vertex colours, "" for each mesh's own colour."""
        return self._color_source

    @color_source.setter
    def color_source(self, source: str) -> None:
        if source != self._color_source:
            self._color_source = source
            self._device_changed()
            self.update()

    def _vertex_colours(self, mesh: SceneDrawMesh, count: int) -> np.ndarray | None:
        if (self.color_source != "vertex" or mesh.colors is None
                or np.size(mesh.colors) not in (count * 3, count * 4)):
            return None
        colours = np.ones((count, 4), np.float32)
        given = np.asarray(mesh.colors, np.float32).reshape(count, -1)
        colours[:, :given.shape[1]] = given
        return colours

    def device_scene(self) -> tuple[int, tuple[SceneGeometry, ...]]:
        geometries = self._device_content_shown()[0]
        return self._device_content, geometries

    def device_strands(self) -> tuple[str, object, tuple[str, str, str], dict] | None:
        return None if self.strands_source is None else self.strands_source()

    def device_wrinkles(self) -> tuple:
        return () if self.wrinkle_source is None else tuple(self.wrinkle_source())

    def set_edit_tool(self, tool) -> None:
        """``tool``'s handlers return True when they take the event; it gives ``marks()`` and, while ``gizmo()`` returns
        (centre, extent), owns the gizmo."""
        if self._gizmo_drag is not None:
            self._finish_gizmo_drag(commit=False)
        self.edit_tool = tool
        self.edit_tool_changed()

    def edit_tool_changed(self) -> None:
        if self._gizmo_drag is None:
            self._refresh_selection_bounds()
        self.update()

    def _tool_gizmo(self):
        return None if self.edit_tool is None else self.edit_tool.gizmo()

    def view_camera(self):
        pixels = self._view_pixels()
        return self._device_camera(pixels), pixels[0] / max(self.width(), 1)

    def fit_points(self, points) -> None:
        if len(points):
            minimum, maximum = points.min(axis=0), points.max(axis=0)
            self.center = (minimum + maximum) * 0.5
            self.extent = max(float(np.max(maximum - minimum)), 1e-6)
            self.scale = 1 / self.extent
            self.freecam.focus(self.center, self.extent)
            self.update()

    def device_motion(self) -> tuple[ScenePlacement, ...]:
        """Placed again: only the meshes moved since; the others' placements are the same objects."""
        placed = self._device_placed
        if placed is not None and placed[0] == self._device_motion:
            return placed[1]
        geometries, meshes = self._device_content_shown()
        moving, self._device_moving = self._device_moving, set()
        if placed is None or placed[2] is not meshes or moving is None:
            rows, placements = range(len(meshes)), [None] * len(meshes)
        else:
            numbers = self._device_numbered(meshes)
            rows = sorted({numbers[key] for key in moving if key in numbers})
            if not rows:
                self._device_placed = self._device_motion, placed[1], meshes
                return placed[1]
            placements = list(placed[1])
        drag = self._gizmo_drag or {}
        dragged = drag["matrices"] if drag.get("matrix") is not None else {}
        for row in rows:
            geometry, mesh = geometries[row], meshes[row]
            skinned = self._skinning.influences(mesh.key) is not None
            positions, normals, bones = (self._skinning.posed(mesh.key) if skinned
                                         else (mesh.vertices, mesh.normals, None))
            of = meshes[geometry.vertices]
            shape = self._device_shapes.get(id(of))
            if shape is None:
                # A mesh's own arrays are replaced, never written, so its shape shares them; posed ones are copied.
                held = np.array if skinned else np.asarray
                positions = held(positions, np.float32).reshape(-1, 3)
                normals = (held(normals, np.float32).reshape(-1, 3)
                           if normals is not None and np.size(normals) == positions.size else computed_normals(
                               positions, np.concatenate([batch.indices for batch in geometry.batches])))
                shape = SceneShape(positions, normals, self._vertex_colours(mesh, len(positions)))
                self._device_shapes[id(of)] = shape
            world = IDENTITY4 if mesh.transform_matrix is None else np.asarray(mesh.transform_matrix, np.float32)
            moved = self._mesh_draw_matrices.get(mesh.key)
            if moved is not None:
                world = moved @ world
            if mesh.key in dragged:
                world = drag["matrix"] @ world
            placements[row] = ScenePlacement(shape, np.array(world[:3], np.float64), bones,
                                             self._mesh_poses.get(mesh.key))
        self._device_placed = self._device_motion, tuple(placements), meshes
        return self._device_placed[1]

    def _device_draws(self) -> bool:
        """Sends the view every frame, changed or not, as a game does."""
        if not self._device_frames:
            return False
        if self._fps_limit and self._told_clock.isValid():
            wait = 1000.0 / self._fps_limit - self._told_clock.elapsed()
            if wait > 0:
                if not self._timer.isActive() or self._timer.remainingTime() > wait:
                    self._timer.start(int(wait) + 1)
                return False
        told = self._device_view()
        changed = told != self._device_told
        self._device_told = told
        self._told_clock.start()
        self.device_view_changed.emit(*told)
        if changed:
            self._sync_device_window()
        return not isinstance(told[0], str)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._device_window is not None:
            self._device_window.setGeometry(self.rect())
        self.place_viewport_overlays()

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

    def update(self, *area) -> None:
        super().update(*area)
        self._frame_due.start(0)

    def paintEvent(self, event):
        QPainter(self).fillRect(event.rect(), QColor.fromRgbF(*self._background))

    def _frame(self) -> None:
        if not self.isVisible():
            return
        if self._controls != "mesh":
            self.freecam.step(self.camera_boost, self.camera_slow)
        self._gizmo_projection = None
        if self._controls != "mesh" and self._selection_center is not None:
            self._cache_gizmo_projection()
        told = self._device_draws()
        if not self._device_frames:
            self._record_frame()
        self._after_pass(told)

    def _after_pass(self, told: bool) -> None:
        if not self._device_frames:
            if self._fps_limit == 0:
                self._timer.start(0)
        elif not told and (self.freecam.keys or self._frame_callback is not None):
            self._next_pass()

    def _update_timer_state(self):
        self._timer.stop()
        if not self.isVisible():
            return
        self._frame_clock.start()
        self._frames_asked = 0
        self._timer.start(self._frame_interval())
        self._fps_timer.start(1000)

    def _next_pass(self) -> None:
        if self._timer.isActive() or not self.isVisible():
            return
        if self._fps_limit:
            self._ask_next_frame()
        else:
            self._timer.start(0)

    def _request_frame(self) -> None:
        if self._fps_limit and not self._device_frames:
            self._ask_next_frame()
        if self._frame_callback is not None:
            self._frame_callback(self._frame_seconds())
        self.update()

    def _frame_seconds(self) -> float:
        if self._seconds_clock.isValid():
            wall = max(0, self._seconds_clock.restart()) / 1000.0
        else:
            wall = 0.0
            self._seconds_clock.start()
        stepped = None if self.stepped_time is None else self.stepped_time()
        before, self._stepped_seen = self._stepped_seen, stepped
        return wall if stepped is None or before is None else stepped - before

    def _ask_next_frame(self) -> None:
        period = 1000.0 / self._fps_limit
        if not self._frame_clock.isValid():
            self._frame_clock.start()
            self._frames_asked = 0
        self._frames_asked += 1
        due = self._frames_asked * period - self._frame_clock.elapsed()
        if due < -period:
            self._frame_clock.start()
            self._frames_asked, due = 0, period
        self._timer.start(max(0, round(due)))

    def _frame_interval(self) -> int:
        return 0 if self._fps_limit == 0 else max(1, round(1000 / self._fps_limit))

    def set_frame_callback(self, callback) -> None:
        """``callback(seconds)`` gets the time since its last call."""
        if callback is not None and not callable(callback):
            raise TypeError("frame callback must be callable")
        self._frame_callback = callback
        if callback is not None:
            self._seconds_clock.start()
            self._stepped_seen = None if self.stepped_time is None else self.stepped_time()
            self._next_pass()

    @property
    def fps_limit(self) -> int:
        """0 for uncapped."""
        return self._fps_limit

    def set_strands_stepped(self, stepped) -> None:
        if stepped != self._strands_stepped:
            self._strands_stepped = stepped
            self.strands_stepped.emit(stepped)

    def _update_after_camera_change(self) -> None:
        if self._fps_limit == 0:
            self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._overlays.place_later()
        self._update_timer_state()

    def hideEvent(self, event):
        self._unlock_scene_cursor()
        self._timer.stop()
        self._fps_timer.stop()
        self.freecam.clear_input()
        super().hideEvent(event)

    def _change_fps_limit(self, value: int):
        self._fps_limit = int(value)
        self._save_view_setting("mesh_viewer_fps_limit", self._fps_limit)
        self._update_timer_state()
        self.fps_limit_changed.emit(self._fps_limit)

    def _set_camera_setting(self, attr: str, key: str, value: float):
        setattr(self, attr, float(value))
        if attr == "camera_speed":
            self.freecam.update_speed(self.camera_speed)
        self._save_view_setting(key, float(value))

    def _set_wireframe_mode(self, mode: str):
        if mode not in self.WIREFRAME_MODES:
            return
        self.wireframe_mode = mode
        self._save_view_setting(self._wireframe_key, mode)
        self.update()

    def _set_lighting_mode(self, mode: str):
        """Game lighting is a shading (``set_shading``), not a light."""
        if mode == "game" or mode not in self.LIGHTING_MODES:
            return
        self.texture_light = mode
        self._save_view_setting("mesh_viewer_lighting_mode", mode)
        if self.lighting_mode != "game":
            self.lighting_mode = mode
        self._refresh_probe_status_label()
        self.update()

    def _set_line_width(self, value: float):
        self.line_width = float(value)
        self._save_view_setting("mesh_viewer_line_width", self.line_width)
        self.update()

    def _set_ambient(self, value: float):
        self.ambient = float(value)
        self._save_view_setting("mesh_viewer_ambient", self.ambient)
        self.update()

    def _set_diffuse(self, value: float):
        self.diffuse = float(value)
        self._save_view_setting("mesh_viewer_diffuse", self.diffuse)
        self.update()

    def _set_show_bone_labels(self, checked: bool):
        self.show_bone_labels = bool(checked)
        self._save_view_setting("mesh_viewer_show_bones", self.show_bone_labels)
        self.update()
