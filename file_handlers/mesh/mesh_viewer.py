from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QSizePolicy,
    QLabel,
    QSpinBox,
    QFrame,
    QHBoxLayout,
    QComboBox,
    QDoubleSpinBox,
)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
import numpy as np
from OpenGL.arrays import vbo
from OpenGL.GL import (
    glClearColor,
    glClear,
    GL_COLOR_BUFFER_BIT,
    GL_DEPTH_BUFFER_BIT,
    glEnable,
    glDisable,
    GL_DEPTH_TEST,
    glRotatef,
    glTranslatef,
    glScalef,
    glLoadIdentity,
    glMatrixMode,
    GL_PROJECTION,
    GL_MODELVIEW,
    glViewport,
    glLightfv,
    GL_LIGHTING,
    GL_LIGHT0,
    GL_POSITION,
    GL_COLOR_MATERIAL,
    GL_CULL_FACE,
    glCullFace,
    glFrontFace,
    GL_BACK,
    GL_CCW,
    GL_NORMALIZE,
    glEnableClientState,
    glDisableClientState,
    glVertexPointer,
    glNormalPointer,
    glColorPointer,
    glColor4f,
    glDrawElements,
    GL_TRIANGLES,
    GL_LINES,
    GL_FLOAT,
    GL_UNSIGNED_INT,
    GL_VERTEX_ARRAY,
    GL_NORMAL_ARRAY,
    GL_COLOR_ARRAY,
    GL_ELEMENT_ARRAY_BUFFER,
    GL_BLEND,
    glPolygonMode,
    GL_FRONT_AND_BACK,
    GL_LINE,
    GL_FILL,
    glLineWidth,
    glShadeModel,
    GL_SMOOTH,
    glDepthMask,
    GL_AMBIENT,
    GL_DIFFUSE,
)
from OpenGL.GLU import gluPerspective


class MeshViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._layout = QVBoxLayout(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.gl_widget = None
        if self.handler and getattr(self.handler, "mesh", None):
            self.gl_widget = _MeshGLWidget(self.handler.mesh)
            self._layout.addWidget(self.gl_widget, 1)


class _MeshGLWidget(QOpenGLWidget):
    def __init__(self, mesh):
        fmt = QSurfaceFormat()
        fmt.setDepthBufferSize(24)
        fmt.setSwapInterval(0)
        fmt.setVersion(2, 1)
        fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        QSurfaceFormat.setDefaultFormat(fmt)
        super().__init__()
        self.setFormat(fmt)

        self.mesh = mesh
        self.rot_x = 0.0
        self.rot_y = 0.0
        self.distance = 3.0
        self.last_pos = None
        self.fps = 0.0
        self._frame_count = 0
        self._last_time = time.time()
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self.update)
        self._fps_limit = 60
        self._timer.start(int(1000 / self._fps_limit))
        self._wireframe = False
        self._lighting = True
        self.wireframe_mode = "off"
        self.lighting_mode = "fixed"
        self.line_width = 1.5
        self.cull_enabled = True
        self.depth_enabled = True
        self.color_source = "vertex"
        self.ambient = 0.35
        self.diffuse = 0.65
        self._last_lighting_mode = "fixed"
        self._last_wf_mode = "lines_overlay"

        self.overlay = QFrame(self)
        self.overlay.setStyleSheet(
            "background-color: rgba(0, 0, 0, 160); color: #39ff14;"
        )
        olayout = QVBoxLayout(self.overlay)
        olayout.setContentsMargins(4, 4, 4, 4)
        self.fps_label = QLabel("0 FPS", self.overlay)
        olayout.addWidget(self.fps_label)

        limit_layout = QHBoxLayout()
        limit_layout.addWidget(QLabel("Limit", self.overlay))
        self.fps_spin = QSpinBox(self.overlay)
        self.fps_spin.setRange(0, 240)
        self.fps_spin.setFixedWidth(50)
        self.fps_spin.valueChanged.connect(self._change_fps_limit)
        limit_layout.addWidget(self.fps_spin)
        self.fps_spin.setValue(self._fps_limit)
        olayout.addLayout(limit_layout)

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
        olayout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Light", self.overlay))
        self.light_combo = QComboBox(self.overlay)
        self.light_combo.addItems(["off", "fixed", "software"])
        self.light_combo.setCurrentText(self.lighting_mode)
        self.light_combo.currentTextChanged.connect(self._set_lighting_mode)
        row2.addWidget(self.light_combo)
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
        olayout.addLayout(row2)

        self.overlay.adjustSize()
        self.overlay.move(10, 10)

        mb = mesh.mesh_buffer
        self.vertices = np.array(mb.positions, dtype=np.float32).reshape(-1, 3)
        self.normals = (
            np.array(mb.normals, dtype=np.float32).reshape(-1, 3)
            if mb.normals
            else None
        )
        if self.normals is not None:
            lengths = np.linalg.norm(self.normals, axis=1)
            safe_normals = np.zeros_like(self.normals, dtype=np.float32)
            np.divide(self.normals, lengths[:, np.newaxis], out=safe_normals, where=lengths[:, np.newaxis] > 0)
            invalid_mask = ~np.isfinite(safe_normals).all(axis=1)
            if np.any(invalid_mask):
                safe_normals[invalid_mask] = np.array([0.0, 0.0, 1.0], dtype=np.float32)
            self.normals = safe_normals
        self.colors = (
            np.array(mb.colors, dtype=np.uint8).reshape(-1, 4).astype(np.float32) / 255.0
            if mb.colors
            else None
        )
        if self.colors is None or len(self.colors) != len(self.vertices):
            self.base_colors = np.ones((len(self.vertices), 4), dtype=np.float32)
        else:
            self.base_colors = self.colors.copy()

        idx_list: list[int] = []
        if mesh.meshes:
            for m in mesh.meshes:
                if not m.lods:
                    continue
                lod0 = m.lods[0]
                for mg in lod0.mesh_groups:
                    for sm in mg.submeshes:
                        start = sm.faces_index_offset
                        end = start + sm.indices_count
                        base = sm.verts_index_offset
                        idx_list.extend(base + idx for idx in mb.faces[start:end])
        if not idx_list:
            idx_list = list(mb.faces)
        self.indices = np.array(idx_list, dtype=np.uint32)

        if len(self.indices) % 3 == 0:
            tris_edges = np.concatenate([
                self.indices.reshape(-1, 3)[:, [0, 1]],
                self.indices.reshape(-1, 3)[:, [1, 2]],
                self.indices.reshape(-1, 3)[:, [2, 0]],
            ], axis=0)
            self.indices_lines = tris_edges.astype(np.uint32).reshape(-1)
        else:
            self.indices_lines = self.indices.copy()

        mins = self.vertices.min(axis=0)
        maxs = self.vertices.max(axis=0)
        self.center = (mins + maxs) / 2.0
        extent = float(np.max(maxs - mins))
        self.scale = 1.0 / extent if extent else 1.0

        self.vbo_vertices = None
        self.vbo_normals = None
        self.vbo_colors = None
        self.vbo_indices = None
        self.vbo_indices_lines = None
        self._normals_generated = False
        self._colors_dirty = True

    def _compute_lit_colors(self) -> np.ndarray:
        if self.normals is None or not np.isfinite(self.normals).all():
            normals_used = np.tile(np.array([0.0, 0.0, 1.0], dtype=np.float32), (len(self.vertices), 1))
        else:
            normals_used = self.normals
        light_dir = np.array([0.4, 0.8, 0.4], dtype=np.float32)
        light_dir /= np.linalg.norm(light_dir) if np.linalg.norm(light_dir) != 0 else 1.0
        ambient = 0.45
        diffuse_scale = 0.55
        intensity = np.clip((normals_used @ light_dir) * diffuse_scale + ambient, 0.0, 1.0).astype(np.float32)
        lit = self.base_colors.copy()
        lit[:, :3] *= intensity[:, np.newaxis]
        lit[:, 3] = 1.0
        return lit

    def _ensure_color_vbo_for_current_mode(self):
        base = self.base_colors if self.color_source == "vertex" else np.ones((len(self.vertices), 4), dtype=np.float32)
        apply_lighting = (self.lighting_mode == "software" and self._lighting) or (not self._lighting)
        if apply_lighting:
            if self.normals is None or not np.isfinite(self.normals).all():
                normals_used = np.tile(np.array([0.0, 0.0, 1.0], dtype=np.float32), (len(self.vertices), 1))
            else:
                normals_used = self.normals
            light_dir = np.array([0.4, 0.8, 0.4], dtype=np.float32)
            n = np.linalg.norm(light_dir)
            light_dir = light_dir / (n if n != 0 else 1.0)
            ambient = float(self.ambient)
            diffuse_scale = float(self.diffuse)
            intensity = np.clip((normals_used @ light_dir) * diffuse_scale + ambient, 0.0, 1.0).astype(np.float32)
            colors_now = base.copy()
            colors_now[:, :3] *= intensity[:, np.newaxis]
            colors_now[:, 3] = 1.0
        else:
            colors_now = base
        self.vbo_colors = vbo.VBO(colors_now)
        self._colors_dirty = False

    def _cleanup_gl(self):
        self.makeCurrent()
        if self.vbo_indices_lines is not None:
            self.vbo_indices_lines.delete()
            self.vbo_indices_lines = None
        if self.vbo_indices is not None:
            self.vbo_indices.delete()
            self.vbo_indices = None
        if self.vbo_colors is not None:
            self.vbo_colors.delete()
            self.vbo_colors = None
        if self.vbo_normals is not None:
            self.vbo_normals.delete()
            self.vbo_normals = None
        if self.vbo_vertices is not None:
            self.vbo_vertices.delete()
            self.vbo_vertices = None
        self.doneCurrent()

    def _ensure_normals_vbo_for_lighting(self):
        if self.vbo_normals is not None:
            return
        if self.normals is not None and np.isfinite(self.normals).all():
            self.vbo_normals = vbo.VBO(self.normals)
            return
        if len(self.indices) % 3 == 0 and len(self.vertices) > 0:
            tris = self.indices.reshape(-1, 3)
            v0 = self.vertices[tris[:, 0]]
            v1 = self.vertices[tris[:, 1]]
            v2 = self.vertices[tris[:, 2]]
            edge1 = v1 - v0
            edge2 = v2 - v0
            face_normals = np.cross(edge1, edge2)
            vertex_normals = np.zeros_like(self.vertices, dtype=np.float32)
            np.add.at(vertex_normals, tris[:, 0], face_normals)
            np.add.at(vertex_normals, tris[:, 1], face_normals)
            np.add.at(vertex_normals, tris[:, 2], face_normals)
            lengths_v = np.linalg.norm(vertex_normals, axis=1)
            safe_vertex_normals = np.zeros_like(vertex_normals, dtype=np.float32)
            np.divide(
                vertex_normals,
                lengths_v[:, np.newaxis],
                out=safe_vertex_normals,
                where=lengths_v[:, np.newaxis] > 0,
            )
            invalid_v = ~np.isfinite(safe_vertex_normals).all(axis=1)
            if np.any(invalid_v):
                safe_vertex_normals[invalid_v] = np.array([0.0, 0.0, 1.0], dtype=np.float32)
            self.normals = safe_vertex_normals.astype(np.float32)
            self.vbo_normals = vbo.VBO(self.normals)
            self._normals_generated = True

    def initializeGL(self):
        glClearColor(0.1, 0.1, 0.1, 1.0)
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_LIGHT0)
        glEnable(GL_COLOR_MATERIAL)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glFrontFace(GL_CCW)
        glEnable(GL_NORMALIZE)
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (1.0, 1.0, 1.0, 1.0))
        glLightfv(GL_LIGHT0, GL_AMBIENT, (0.3, 0.3, 0.3, 1.0))

        ctx = self.context()
        ctx.aboutToBeDestroyed.connect(self._cleanup_gl)

        self.vbo_vertices = vbo.VBO(self.vertices)
        self.vbo_indices = vbo.VBO(self.indices, target=GL_ELEMENT_ARRAY_BUFFER)
        if self.indices_lines is not None and len(self.indices_lines) > 0:
            self.vbo_indices_lines = vbo.VBO(self.indices_lines, target=GL_ELEMENT_ARRAY_BUFFER)
        if self.normals is not None:
            self.vbo_normals = vbo.VBO(self.normals)
        self._ensure_color_vbo_for_current_mode()

    def resizeGL(self, w: int, h: int):
        glViewport(0, 0, w, max(h, 1))
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        aspect = w / h if h else 1.0
        gluPerspective(45.0, aspect, 0.1, 100.0)
        glMatrixMode(GL_MODELVIEW)
        self.overlay.move(10, 10)

    def _apply_render_state(self):
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)

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

        if self.wireframe_mode == "polygon":
            glPolygonMode(GL_FRONT_AND_BACK, GL_LINE)
            glLineWidth(self.line_width)
        else:
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
            glLineWidth(1.0)

    def paintGL(self):
        glDisable(GL_BLEND)
        self._apply_render_state()
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        glLightfv(GL_LIGHT0, GL_POSITION, (0.5, 1.0, 1.0, 0.0))
        glTranslatef(0.0, 0.0, -self.distance)
        glRotatef(self.rot_x, 1.0, 0.0, 0.0)
        glRotatef(self.rot_y, 0.0, 1.0, 0.0)
        glScalef(self.scale, self.scale, self.scale)
        glTranslatef(-self.center[0], -self.center[1], -self.center[2])
        glColor4f(1.0, 1.0, 1.0, 1.0)

        if self.vbo_vertices is not None:
            self.vbo_vertices.bind()
            glEnableClientState(GL_VERTEX_ARRAY)
            glVertexPointer(3, GL_FLOAT, 0, None)

            glDisableClientState(GL_NORMAL_ARRAY)
            glDisableClientState(GL_COLOR_ARRAY)

            bound_normals = False
            used_color_array = False
            if self.lighting_mode == "fixed":
                self._ensure_normals_vbo_for_lighting()
                if self.vbo_normals is not None:
                    self.vbo_normals.bind()
                    glEnableClientState(GL_NORMAL_ARRAY)
                    glNormalPointer(GL_FLOAT, 0, None)
                    bound_normals = True
            else:
                if self._colors_dirty or self.vbo_colors is None:
                    self._ensure_color_vbo_for_current_mode()
                if self.vbo_colors is not None:
                    self.vbo_colors.bind()
                    glEnableClientState(GL_COLOR_ARRAY)
                    glColorPointer(4, GL_FLOAT, 0, None)
                    used_color_array = True

            self.vbo_indices.bind()
            glDrawElements(GL_TRIANGLES, len(self.indices), GL_UNSIGNED_INT, None)
            self.vbo_indices.unbind()

            if self.vbo_indices_lines is not None and self.wireframe_mode in ("lines_depth", "lines_overlay"):
                if used_color_array:
                    glDisableClientState(GL_COLOR_ARRAY)
                    self.vbo_colors.unbind()
                    used_color_array = False
                if bound_normals:
                    glDisableClientState(GL_NORMAL_ARRAY)
                    self.vbo_normals.unbind()
                    bound_normals = False
                was_lighting = (self.lighting_mode == "fixed")
                if was_lighting:
                    glDisable(GL_LIGHTING)
                glDisable(GL_CULL_FACE)
                glLineWidth(self.line_width)
                glColor4f(0.2, 1.0, 0.2, 1.0)
                if self.wireframe_mode == "lines_overlay":
                    glDisable(GL_DEPTH_TEST)
                    glDepthMask(False)
                self.vbo_indices_lines.bind()
                glDrawElements(GL_LINES, len(self.indices_lines), GL_UNSIGNED_INT, None)
                self.vbo_indices_lines.unbind()
                if self.wireframe_mode == "lines_overlay":
                    glDepthMask(True)
                    glEnable(GL_DEPTH_TEST)
                glEnable(GL_CULL_FACE)
                if was_lighting:
                    glEnable(GL_LIGHTING)

            if used_color_array:
                glDisableClientState(GL_COLOR_ARRAY)
                self.vbo_colors.unbind()
            if bound_normals:
                glDisableClientState(GL_NORMAL_ARRAY)
                self.vbo_normals.unbind()
            glDisableClientState(GL_VERTEX_ARRAY)
            self.vbo_vertices.unbind()

        now = time.time()
        self._frame_count += 1
        elapsed = now - self._last_time
        if elapsed >= 1.0:
            self.fps = self._frame_count / elapsed
            self._frame_count = 0
            self._last_time = now
            self.fps_label.setText(f"{self.fps:.1f} FPS")

    def mousePressEvent(self, event):
        self.last_pos = event.position()

    def mouseMoveEvent(self, event):
        if self.last_pos is None:
            return
        dx = event.position().x() - self.last_pos.x()
        dy = event.position().y() - self.last_pos.y()
        if event.buttons() & Qt.LeftButton:
            self.rot_x += dy * 0.5
            self.rot_y += dx * 0.5
            if self._fps_limit == 0:
                self.update()
        self.last_pos = event.position()

    def wheelEvent(self, event):
        delta = event.angleDelta().y() / 120.0
        self.distance *= 0.9 ** delta
        if self._fps_limit == 0:
            self.update()

    def _change_fps_limit(self, value: int):
        self._fps_limit = value
        interval = 0 if value == 0 else int(1000 / value)
        self._timer.setInterval(interval)

    def _set_wireframe_mode(self, mode: str):
        self.wireframe_mode = mode
        self.makeCurrent()
        self._apply_render_state()
        self.doneCurrent()
        self.update()

    def _set_lighting_mode(self, mode: str):
        self.lighting_mode = mode
        self._colors_dirty = True
        self.makeCurrent()
        self._apply_render_state()
        self.doneCurrent()
        self.update()

    def _set_line_width(self, value: float):
        self.line_width = float(value)
        self.update()

    def _set_ambient(self, value: float):
        self.ambient = float(value)
        self._colors_dirty = True
        self.update()

    def _set_diffuse(self, value: float):
        self.diffuse = float(value)
        self._colors_dirty = True
        self.update()
