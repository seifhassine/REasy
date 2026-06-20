from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
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
    glTexImage2D,
    glTexParameteri,
    glTranslatef,
    glVertexPointer,
    glViewport,
)
from OpenGL.GLU import gluPerspective
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QImage, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QHBoxLayout, QLabel, QSpinBox, QVBoxLayout

from settings import save_settings
from ui.opengl_camera import OrbitCameraMixin
from .scene_model import SceneDrawBatch, SceneDrawMesh


@dataclass(slots=True)
class _SceneBufferSet:
    vertices: np.ndarray
    normals: np.ndarray | None
    base_colors: np.ndarray
    uvs: np.ndarray | None
    indices: np.ndarray
    batches: list[tuple[str, np.ndarray]]
    line_indices: np.ndarray
    vertices_vbo: object | None = None
    normals_vbo: object | None = None
    colors_vbo: object | None = None
    uvs_vbo: object | None = None
    indices_vbo: object | None = None
    line_indices_vbo: object | None = None
    batch_vbos: list[tuple[str, object, int]] = field(default_factory=list)


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
        self.center = np.zeros(3, dtype=np.float32)

        self._meshes: list[SceneDrawMesh] = []
        self._highlighted_keys: set[str] = set()
        self._regular_set: _SceneBufferSet | None = None
        self._solid_set: _SceneBufferSet | None = None
        self._texture_ids: dict[str, int] = {}
        self._texture_sources: dict[str, str] = {}
        self._pending_material_images: dict[str, tuple[str, QImage]] = {}

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

        self._build_overlay()
        self._update_timer_state()

    def _build_overlay(self):
        self.overlay = QFrame(self)
        self.overlay.setStyleSheet("background-color: rgba(0, 0, 0, 160); color: #39ff14;")
        layout = QVBoxLayout(self.overlay)
        layout.setContentsMargins(4, 4, 4, 4)
        self.fps_label = QLabel("0 FPS", self.overlay)
        layout.addWidget(self.fps_label)

        if self._controls == "mesh":
            self._build_mesh_controls(layout)
        else:
            self._build_scene_controls(layout)

        self.overlay.adjustSize()
        self.overlay.move(10, 10)

    def _build_scene_controls(self, layout: QVBoxLayout):
        row = QHBoxLayout()
        row.addWidget(QLabel("Mode", self.overlay))
        mode_combo = QComboBox(self.overlay)
        mode_combo.addItem("Wireframe", "wire")
        mode_combo.addItem("Solid + Wire", "hybrid")
        mode_combo.addItem("Solid", "solid")
        mode_combo.currentIndexChanged.connect(lambda _: self._set_render_mode(mode_combo.currentData()))
        row.addWidget(mode_combo)
        layout.addLayout(row)

        self.highlight_only_check = QCheckBox("View only highlighted", self.overlay)
        self.highlight_only_check.toggled.connect(self._set_show_only_highlighted)
        layout.addWidget(self.highlight_only_check)

    def _build_mesh_controls(self, layout: QVBoxLayout):
        limit_layout = QHBoxLayout()
        limit_layout.addWidget(QLabel("Limit", self.overlay))
        self.fps_spin = QSpinBox(self.overlay)
        self.fps_spin.setRange(0, 240)
        self.fps_spin.setFixedWidth(50)
        self.fps_spin.setValue(self._fps_limit)
        self.fps_spin.valueChanged.connect(self._change_fps_limit)
        limit_layout.addWidget(self.fps_spin)
        layout.addLayout(limit_layout)

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

    def set_scene(self, meshes: list[SceneDrawMesh], highlighted_keys: set[str] | None = None):
        self._meshes = meshes
        self._highlighted_keys = set(highlighted_keys or set())
        self._recompute_bounds()
        self._upload_buffers()
        self.update()

    def set_material_images(self, images: dict[str, tuple[str, QImage]]):
        self._pending_material_images = dict(images)
        if self.context() is None:
            self.update()
            return
        self.makeCurrent()
        self._sync_gl_textures()
        self.doneCurrent()
        self.update()

    def _set_render_mode(self, mode: str):
        self.render_mode = str(mode or "wire")
        self.update()

    def _set_show_only_highlighted(self, enabled: bool):
        self.show_only_highlighted = bool(enabled)
        self._upload_buffers()
        self.update()

    def _recompute_bounds(self):
        vertex_chunks = [np.asarray(mesh.vertices, dtype=np.float32).reshape(-1, 3) for mesh in self._meshes if len(mesh.vertices)]
        if not vertex_chunks:
            self.center = np.zeros(3, dtype=np.float32)
            self.scale = 1.0
            return
        vertices = np.concatenate(vertex_chunks, axis=0)
        mins = vertices.min(axis=0)
        maxs = vertices.max(axis=0)
        self.center = (mins + maxs) / 2.0
        extent = float(np.max(maxs - mins))
        self.scale = 1.0 / extent if extent > 1e-6 else 1.0

    @staticmethod
    def _color_array(color, count: int) -> np.ndarray:
        rgba = np.ones(4, dtype=np.float32)
        raw = np.asarray(color, dtype=np.float32).reshape(-1)
        rgba[:min(len(raw), 4)] = raw[:4]
        return np.tile(rgba, (count, 1))

    @staticmethod
    def _normalized_normals(normals: np.ndarray) -> np.ndarray:
        lengths = np.linalg.norm(normals, axis=1)
        safe = np.zeros_like(normals, dtype=np.float32)
        np.divide(normals, lengths[:, np.newaxis], out=safe, where=lengths[:, np.newaxis] > 0)
        invalid = ~np.isfinite(safe).all(axis=1)
        if np.any(invalid):
            safe[invalid] = np.array([0.0, 0.0, 1.0], dtype=np.float32)
        return safe

    @classmethod
    def _computed_normals(cls, vertices: np.ndarray, indices: np.ndarray) -> np.ndarray:
        if len(vertices) == 0 or len(indices) < 3:
            return np.zeros((len(vertices), 3), dtype=np.float32)
        usable = (len(indices) // 3) * 3
        tris = indices[:usable].reshape(-1, 3)
        valid = (tris < len(vertices)).all(axis=1)
        tris = tris[valid]
        normals = np.zeros_like(vertices, dtype=np.float32)
        if len(tris):
            v0 = vertices[tris[:, 0]]
            v1 = vertices[tris[:, 1]]
            v2 = vertices[tris[:, 2]]
            face_normals = np.cross(v1 - v0, v2 - v0)
            np.add.at(normals, tris[:, 0], face_normals)
            np.add.at(normals, tris[:, 1], face_normals)
            np.add.at(normals, tris[:, 2], face_normals)
        return cls._normalized_normals(normals)

    @staticmethod
    def _line_indices(indices: np.ndarray) -> np.ndarray:
        usable = (len(indices) // 3) * 3
        if usable == 0:
            return np.zeros((0,), dtype=np.uint32)
        tris = indices[:usable].reshape(-1, 3)
        return np.concatenate([tris[:, [0, 1]], tris[:, [1, 2]], tris[:, [2, 0]]], axis=0).astype(np.uint32).reshape(-1)

    def _mesh_batches(self, mesh: SceneDrawMesh) -> list[SceneDrawBatch]:
        return list(mesh.batches) if mesh.batches else [SceneDrawBatch(indices=mesh.indices, material_name=mesh.material_name)]

    def _build_buffer_set(self, *, force_solid: bool) -> _SceneBufferSet | None:
        vertex_chunks: list[np.ndarray] = []
        normal_chunks: list[np.ndarray] = []
        color_chunks: list[np.ndarray] = []
        uv_chunks: list[np.ndarray] = []
        index_chunks: list[np.ndarray] = []
        draw_batches: list[tuple[str, np.ndarray]] = []
        any_uvs = False
        base = 0

        for mesh in self._meshes:
            if bool(mesh.force_solid) != force_solid:
                continue
            if self.show_only_highlighted and mesh.key not in self._highlighted_keys and not mesh.ignore_highlight_filter:
                continue
            if not len(mesh.vertices):
                continue
            vertices = np.asarray(mesh.vertices, dtype=np.float32).reshape(-1, 3)
            batches = []
            for batch in self._mesh_batches(mesh):
                indices = np.asarray(batch.indices, dtype=np.uint32).reshape(-1)
                usable = (len(indices) // 3) * 3
                if usable >= 3:
                    triangles = indices[:usable].reshape(-1, 3)
                    valid = (triangles < len(vertices)).all(axis=1)
                    if np.any(valid):
                        batches.append((batch.material_name, triangles[valid].reshape(-1)))
            if not batches:
                continue

            all_indices = np.concatenate([indices for _, indices in batches])
            normals = None
            if mesh.normals is not None:
                raw_normals = np.asarray(mesh.normals, dtype=np.float32).reshape(-1)
                if raw_normals.size == len(vertices) * 3:
                    raw_normals = raw_normals.reshape(-1, 3)
                    normals = self._normalized_normals(raw_normals)
            if normals is None:
                normals = self._computed_normals(vertices, all_indices)

            if mesh.colors is not None and mesh.key not in self._highlighted_keys:
                raw_colors = np.asarray(mesh.colors, dtype=np.float32).reshape(-1)
                if raw_colors.size == len(vertices) * 3:
                    colors = np.concatenate(
                        [raw_colors.reshape(-1, 3), np.ones((len(vertices), 1), dtype=np.float32)],
                        axis=1,
                    )
                elif raw_colors.size == len(vertices) * 4:
                    colors = raw_colors.reshape(-1, 4)
                else:
                    colors = self._color_array(mesh.color, len(vertices))
            else:
                highlight = (1.0, 1.0, 0.25, 1.0) if mesh.key in self._highlighted_keys else mesh.color
                colors = self._color_array(highlight, len(vertices))

            uvs = None
            if mesh.uvs is not None:
                raw_uvs = np.asarray(mesh.uvs, dtype=np.float32).reshape(-1)
                if raw_uvs.size == len(vertices) * 2:
                    raw_uvs = raw_uvs.reshape(-1, 2)
                    uvs = raw_uvs
                    any_uvs = True

            vertex_chunks.append(vertices)
            normal_chunks.append(normals)
            color_chunks.append(colors)
            uv_chunks.append(uvs if uvs is not None else np.zeros((len(vertices), 2), dtype=np.float32))
            for material_name, indices in batches:
                shifted = indices + np.uint32(base)
                index_chunks.append(shifted)
                draw_batches.append((material_name, shifted))
            base += len(vertices)

        if not vertex_chunks or not index_chunks:
            return None

        indices = np.concatenate(index_chunks).astype(np.uint32, copy=False)
        return _SceneBufferSet(
            vertices=np.concatenate(vertex_chunks, axis=0).astype(np.float32, copy=False),
            normals=np.concatenate(normal_chunks, axis=0).astype(np.float32, copy=False),
            base_colors=np.concatenate(color_chunks, axis=0).astype(np.float32, copy=False),
            uvs=np.concatenate(uv_chunks, axis=0).astype(np.float32, copy=False) if any_uvs else None,
            indices=indices,
            batches=draw_batches,
            line_indices=self._line_indices(indices),
        )

    def _delete_buffer_set(self, buffer_set: _SceneBufferSet | None):
        if buffer_set is None:
            return
        for handle in (
            buffer_set.vertices_vbo,
            buffer_set.normals_vbo,
            buffer_set.colors_vbo,
            buffer_set.uvs_vbo,
            buffer_set.indices_vbo,
            buffer_set.line_indices_vbo,
        ):
            if handle is not None:
                handle.delete()
        for _, batch_vbo, _ in buffer_set.batch_vbos:
            batch_vbo.delete()
        buffer_set.batch_vbos.clear()

    def _display_colors(self, buffer_set: _SceneBufferSet) -> np.ndarray:
        if self.color_source == "vertex":
            base = buffer_set.base_colors
        else:
            base = np.ones((len(buffer_set.vertices), 4), dtype=np.float32)
        if self.lighting_mode != "software":
            return base
        normals = buffer_set.normals
        if normals is None or not np.isfinite(normals).all():
            normals = np.tile(np.array([0.0, 0.0, 1.0], dtype=np.float32), (len(buffer_set.vertices), 1))
        light_dir = np.array([0.4, 0.8, 0.4], dtype=np.float32)
        norm = np.linalg.norm(light_dir)
        light_dir = light_dir / (norm if norm else 1.0)
        intensity = np.clip((normals @ light_dir) * float(self.diffuse) + float(self.ambient), 0.0, 1.0).astype(np.float32)
        colors = base.copy()
        colors[:, :3] *= intensity[:, np.newaxis]
        colors[:, 3] = 1.0
        return colors

    def _upload_buffer_set(self, buffer_set: _SceneBufferSet | None):
        if buffer_set is None:
            return
        buffer_set.vertices_vbo = self._array_vbo(buffer_set.vertices)
        buffer_set.indices_vbo = self._element_vbo(buffer_set.indices)
        buffer_set.normals_vbo = self._array_vbo(buffer_set.normals) if buffer_set.normals is not None else None
        buffer_set.colors_vbo = self._array_vbo(self._display_colors(buffer_set))
        buffer_set.uvs_vbo = self._array_vbo(buffer_set.uvs) if buffer_set.uvs is not None else None
        buffer_set.line_indices_vbo = (
            self._element_vbo(buffer_set.line_indices)
            if len(buffer_set.line_indices)
            else None
        )
        buffer_set.batch_vbos = [
            (material_name, self._element_vbo(indices), len(indices))
            for material_name, indices in buffer_set.batches
            if len(indices)
        ]

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
                buffer_set.colors_vbo.delete()
            buffer_set.colors_vbo = self._array_vbo(self._display_colors(buffer_set))
        self._colors_dirty = False

    def _upload_buffers(self):
        if self.context() is None:
            return
        self.makeCurrent()
        self._delete_buffer_set(self._regular_set)
        self._delete_buffer_set(self._solid_set)
        self._regular_set = self._build_buffer_set(force_solid=False)
        self._solid_set = self._build_buffer_set(force_solid=True)
        self._upload_buffer_set(self._regular_set)
        self._upload_buffer_set(self._solid_set)
        self._colors_dirty = False
        self.doneCurrent()

    def _clear_gl_textures(self):
        if self._texture_ids:
            glDeleteTextures(list(self._texture_ids.values()))
            self._texture_ids.clear()
        self._texture_sources.clear()

    def _sync_gl_textures(self):
        stale = set(self._texture_ids) - set(self._pending_material_images)
        for name in stale:
            glDeleteTextures([self._texture_ids.pop(name)])
            self._texture_sources.pop(name, None)
        for name, (source_path, image) in self._pending_material_images.items():
            if image.isNull():
                continue
            texture_id = self._texture_ids.get(name)
            if texture_id is not None and self._texture_sources.get(name) == source_path:
                continue
            if texture_id is None:
                texture_id = glGenTextures(1)
                self._texture_ids[name] = texture_id
            self._upload_texture(texture_id, image)
            self._texture_sources[name] = source_path

    @staticmethod
    def _upload_texture(texture_id: int, image: QImage):
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
        if self.context() is None:
            return
        self.makeCurrent()
        self._clear_gl_textures()
        self._delete_buffer_set(self._regular_set)
        self._delete_buffer_set(self._solid_set)
        self._regular_set = None
        self._solid_set = None
        self._cleanup_extra_gl()
        self.doneCurrent()

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
        self.context().aboutToBeDestroyed.connect(self._cleanup_gl)
        self._regular_set = self._build_buffer_set(force_solid=False)
        self._solid_set = self._build_buffer_set(force_solid=True)
        self._upload_buffer_set(self._regular_set)
        self._upload_buffer_set(self._solid_set)
        self._sync_gl_textures()
        self._after_gl_initialized()

    def resizeGL(self, w: int, h: int):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(w * dpr), int(h * dpr))
        self.overlay.move(10, 10)
        self.overlay.raise_()

    def _apply_projection(self, w: int, h: int):
        glViewport(0, 0, max(int(w), 1), max(int(h), 1))
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        safe_h = max(float(h), 1.0)
        far_plane = 300.0 if self._controls != "mesh" else 100.0
        gluPerspective(45.0, float(w) / safe_h, 0.1, far_plane)
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

    def _bind_arrays(self, buffer_set: _SceneBufferSet, *, use_textures: bool):
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

    def _unbind_arrays(self, buffer_set: _SceneBufferSet, *, textured: bool):
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

    def _draw_triangles(self, buffer_set: _SceneBufferSet | None, *, use_textures: bool = True):
        if buffer_set is None or buffer_set.vertices_vbo is None or buffer_set.indices_vbo is None:
            return
        textured = self._bind_arrays(buffer_set, use_textures=use_textures)
        glColor4f(1.0, 1.0, 1.0, 1.0)

        if buffer_set.batch_vbos:
            for material_name, batch_vbo, count in buffer_set.batch_vbos:
                tex_id = self._texture_ids.get(material_name) if textured else None
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
            glDrawElements(GL_TRIANGLES, len(buffer_set.indices), GL_UNSIGNED_INT, None)
            buffer_set.indices_vbo.unbind()

        glBindTexture(GL_TEXTURE_2D, 0)
        glDisable(GL_TEXTURE_2D)
        self._unbind_arrays(buffer_set, textured=textured)

    def _draw_lines(self, buffer_set: _SceneBufferSet | None, *, overlay: bool):
        if buffer_set is None or buffer_set.vertices_vbo is None or buffer_set.line_indices_vbo is None:
            return
        if self._colors_dirty:
            self._refresh_color_vbos()
        use_vertex_colors = self._controls != "mesh" and buffer_set.colors_vbo is not None
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
        glDrawElements(GL_LINES, len(buffer_set.line_indices), GL_UNSIGNED_INT, None)
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
            self._draw_triangles(self._regular_set, use_textures=False)
        if self.render_mode in {"wire", "hybrid"}:
            self._draw_lines(self._regular_set, overlay=False)

    def paintGL(self):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(self.width() * dpr), int(self.height() * dpr))
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        self._apply_render_state()
        glLoadIdentity()
        glLightfv(GL_LIGHT0, GL_POSITION, (0.5, 1.0, 1.0, 0.0))
        glTranslatef(0.0, 0.0, -self.distance)
        glRotatef(self.rot_x, 1.0, 0.0, 0.0)
        glRotatef(self.rot_y, 0.0, 1.0, 0.0)
        glScalef(self.scale, self.scale, self.scale)
        glTranslatef(-self.center[0], -self.center[1], -self.center[2])

        self._draw_regular_scene()
        self._draw_triangles(self._solid_set, use_textures=False)
        self._after_scene_draw()
        self._record_frame()

    def _update_timer_state(self):
        if not self.isVisible():
            self._timer.stop()
            return
        if self._controls == "mesh":
            interval = 0 if self._fps_limit == 0 else max(1, round(1000 / self._fps_limit))
        else:
            interval = 16
        self._timer.start(interval)

    def _update_after_camera_change(self) -> None:
        if self._controls != "mesh" or self._fps_limit == 0:
            self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._update_timer_state()

    def hideEvent(self, event):
        self._timer.stop()
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
