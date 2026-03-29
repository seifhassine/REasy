from __future__ import annotations

from dataclasses import dataclass
import time

import numpy as np
from OpenGL.arrays import vbo
from OpenGL.GL import (
    GL_AMBIENT,
    GL_BACK,
    GL_CCW,
    GL_COLOR_ARRAY,
    GL_COLOR_BUFFER_BIT,
    GL_COLOR_MATERIAL,
    GL_CULL_FACE,
    GL_DEPTH_BUFFER_BIT,
    GL_DEPTH_TEST,
    GL_DIFFUSE,
    GL_ELEMENT_ARRAY_BUFFER,
    GL_FLOAT,
    GL_FRONT_AND_BACK,
    GL_LIGHT0,
    GL_LIGHTING,
    GL_LINE,
    GL_FILL,
    GL_MODELVIEW,
    GL_NORMAL_ARRAY,
    GL_NORMALIZE,
    GL_POSITION,
    GL_PROJECTION,
    GL_SMOOTH,
    GL_TRIANGLES,
    GL_UNSIGNED_INT,
    GL_VERTEX_ARRAY,
    glClear,
    glClearColor,
    glColor4f,
    glColorPointer,
    glCullFace,
    glDisableClientState,
    glDrawElements,
    glEnable,
    glEnableClientState,
    glFrontFace,
    glLightfv,
    glLineWidth,
    glLoadIdentity,
    glMatrixMode,
    glNormalPointer,
    glPolygonMode,
    glRotatef,
    glScalef,
    glShadeModel,
    glTranslatef,
    glVertexPointer,
    glViewport,
)
from OpenGL.GLU import gluPerspective
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QVBoxLayout


@dataclass(slots=True)
class SceneDrawMesh:
    key: str
    vertices: np.ndarray
    indices: np.ndarray
    color: tuple[float, float, float]
    force_solid: bool = False
    ignore_highlight_filter: bool = False


class ScenePreviewWidget(QOpenGLWidget):
    def __init__(self, parent=None):
        fmt = QSurfaceFormat()
        fmt.setDepthBufferSize(24)
        fmt.setSwapInterval(0)
        fmt.setVersion(2, 1)
        fmt.setProfile(QSurfaceFormat.CompatibilityProfile)
        fmt.setRenderableType(QSurfaceFormat.OpenGL)
        QSurfaceFormat.setDefaultFormat(fmt)
        super().__init__(parent)
        self.setFormat(fmt)

        self.rot_x = 20.0
        self.rot_y = -30.0
        self.distance = 8.0
        self.last_pos = None
        self.scale = 1.0
        self.center = np.zeros(3, dtype=np.float32)

        self._meshes: list[SceneDrawMesh] = []
        self._highlighted_keys: set[str] = set()
        self._vbo_vertices = None
        self._vbo_normals = None
        self._vbo_indices = None
        self._vbo_vertices_solid_only = None
        self._vbo_normals_solid_only = None
        self._vbo_indices_solid_only = None

        self.render_mode = "wire"
        self.show_only_highlighted = False
        self.fps = 0.0
        self._frame_count = 0
        self._last_time = time.time()
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.setInterval(16)

        self._build_overlay()
        self._timer.start()

    def _build_overlay(self):
        self.overlay = QFrame(self)
        self.overlay.setStyleSheet("background-color: rgba(0, 0, 0, 160); color: #39ff14;")
        lay = QVBoxLayout(self.overlay)
        lay.setContentsMargins(4, 4, 4, 4)

        self.fps_label = QLabel("0 FPS", self.overlay)
        lay.addWidget(self.fps_label)

        row = QHBoxLayout()
        row.addWidget(QLabel("Mode", self.overlay))
        mode_combo = QComboBox(self.overlay)
        mode_combo.addItem("Wireframe", "wire")
        mode_combo.addItem("Solid + Wire", "hybrid")
        mode_combo.addItem("Solid", "solid")
        mode_combo.currentIndexChanged.connect(lambda _: self._set_render_mode(mode_combo.currentData()))
        row.addWidget(mode_combo)
        lay.addLayout(row)

        self.highlight_only_check = QCheckBox("View only highlighted", self.overlay)
        self.highlight_only_check.toggled.connect(self._set_show_only_highlighted)
        lay.addWidget(self.highlight_only_check)

        self.overlay.adjustSize()
        self.overlay.move(10, 10)

    def _set_render_mode(self, mode: str):
        self.render_mode = str(mode or "wire")
        self.update()

    def _set_show_only_highlighted(self, enabled: bool):
        self.show_only_highlighted = bool(enabled)
        self._upload_buffers()
        self.update()

    def set_scene(self, meshes: list[SceneDrawMesh], highlighted_keys: set[str] | None = None):
        self._meshes = meshes
        self._highlighted_keys = set(highlighted_keys or set())
        self._recompute_bounds()
        self._upload_buffers()
        self.update()

    def _recompute_bounds(self):
        if not self._meshes:
            self.center = np.zeros(3, dtype=np.float32)
            self.scale = 1.0
            return
        all_vertices = np.concatenate([mesh.vertices for mesh in self._meshes if len(mesh.vertices)], axis=0)
        if not len(all_vertices):
            self.center = np.zeros(3, dtype=np.float32)
            self.scale = 1.0
            return
        mins = all_vertices.min(axis=0)
        maxs = all_vertices.max(axis=0)
        self.center = (mins + maxs) / 2.0
        extent = float(np.max(maxs - mins))
        self.scale = 1.0 / extent if extent > 1e-6 else 1.0

    def _delete_buffers(self):
        for attr in (
            "_vbo_vertices",
            "_vbo_normals",
            "_vbo_indices",
            "_vbo_vertices_solid_only",
            "_vbo_normals_solid_only",
            "_vbo_indices_solid_only",
        ):
            handle = getattr(self, attr)
            if handle is not None:
                handle.delete()
                setattr(self, attr, None)

    def _cleanup_gl(self):
        if self.context() is None:
            return
        self.makeCurrent()
        self._delete_buffers()
        self.doneCurrent()

    def _build_buffer_set(self, meshes: list[SceneDrawMesh]) -> tuple[vbo.VBO, vbo.VBO, vbo.VBO] | None:
        vertex_chunks = []
        normal_chunks = []
        index_chunks = []
        base = 0

        for mesh in meshes:
            if self.show_only_highlighted and mesh.key not in self._highlighted_keys and not mesh.ignore_highlight_filter:
                continue
            if not len(mesh.vertices) or not len(mesh.indices):
                continue
            verts = mesh.vertices.astype(np.float32)
            tris = mesh.indices.reshape(-1, 3)
            face_normals = np.cross(verts[tris[:, 1]] - verts[tris[:, 0]], verts[tris[:, 2]] - verts[tris[:, 0]])
            vertex_normals = np.zeros_like(verts, dtype=np.float32)
            np.add.at(vertex_normals, tris[:, 0], face_normals)
            np.add.at(vertex_normals, tris[:, 1], face_normals)
            np.add.at(vertex_normals, tris[:, 2], face_normals)
            lengths = np.linalg.norm(vertex_normals, axis=1)
            safe_normals = np.zeros_like(vertex_normals, dtype=np.float32)
            np.divide(vertex_normals, lengths[:, None], out=safe_normals, where=lengths[:, None] > 0)

            color = np.array([1.0, 1.0, 0.25], dtype=np.float32) if mesh.key in self._highlighted_keys else np.array(mesh.color, dtype=np.float32)
            packed = np.concatenate([verts, np.tile(color, (len(verts), 1))], axis=1)
            vertex_chunks.append(packed)
            normal_chunks.append(safe_normals)
            index_chunks.append(mesh.indices.astype(np.uint32) + np.uint32(base))
            base += len(verts)

        if not vertex_chunks:
            return None
        return (
            vbo.VBO(np.concatenate(vertex_chunks, axis=0)),
            vbo.VBO(np.concatenate(normal_chunks, axis=0)),
            vbo.VBO(np.concatenate(index_chunks, axis=0), target=GL_ELEMENT_ARRAY_BUFFER),
        )

    def _upload_buffers(self):
        if self.context() is None:
            return
        self.makeCurrent()
        self._delete_buffers()

        regular_meshes = [mesh for mesh in self._meshes if not mesh.force_solid]
        solid_only_meshes = [mesh for mesh in self._meshes if mesh.force_solid]
        regular_set = self._build_buffer_set(regular_meshes)
        solid_only_set = self._build_buffer_set(solid_only_meshes)
        if regular_set:
            self._vbo_vertices, self._vbo_normals, self._vbo_indices = regular_set
        if solid_only_set:
            self._vbo_vertices_solid_only, self._vbo_normals_solid_only, self._vbo_indices_solid_only = solid_only_set

        self.doneCurrent()

    def initializeGL(self):
        glClearColor(0.08, 0.08, 0.08, 1.0)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_LIGHT0)
        glEnable(GL_COLOR_MATERIAL)
        glEnable(GL_CULL_FACE)
        glCullFace(GL_BACK)
        glFrontFace(GL_CCW)
        glEnable(GL_NORMALIZE)
        glShadeModel(GL_SMOOTH)
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (0.8, 0.8, 0.8, 1.0))
        glLightfv(GL_LIGHT0, GL_AMBIENT, (0.3, 0.3, 0.3, 1.0))
        self.context().aboutToBeDestroyed.connect(self._cleanup_gl)
        self._upload_buffers()

    def resizeGL(self, w: int, h: int):
        self._apply_projection(w, h)
        self.overlay.move(10, 10)

    def _apply_projection(self, w: int, h: int):
        glViewport(0, 0, max(int(w), 1), max(int(h), 1))
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        safe_h = max(float(h), 1.0)
        gluPerspective(45.0, float(w) / safe_h, 0.1, 300.0)
        glMatrixMode(GL_MODELVIEW)

    def _draw_scene(self, vertices_vbo, normals_vbo, indices_vbo):
        if vertices_vbo is None or indices_vbo is None:
            return
        vertices_vbo.bind()
        glEnableClientState(GL_VERTEX_ARRAY)
        glVertexPointer(3, GL_FLOAT, 24, None)
        glEnableClientState(GL_COLOR_ARRAY)
        glColorPointer(3, GL_FLOAT, 24, vertices_vbo + 12)

        glEnableClientState(GL_NORMAL_ARRAY)
        if normals_vbo is not None:
            normals_vbo.bind()
            glNormalPointer(GL_FLOAT, 0, None)

        indices_vbo.bind()
        glDrawElements(GL_TRIANGLES, len(indices_vbo.data), GL_UNSIGNED_INT, None)
        indices_vbo.unbind()

        glDisableClientState(GL_NORMAL_ARRAY)
        if normals_vbo is not None:
            normals_vbo.unbind()
        glDisableClientState(GL_COLOR_ARRAY)
        glDisableClientState(GL_VERTEX_ARRAY)
        vertices_vbo.unbind()

    def paintGL(self):
        dpr = float(self.devicePixelRatioF())
        self._apply_projection(int(self.width() * dpr), int(self.height() * dpr))
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        glLightfv(GL_LIGHT0, GL_POSITION, (0.5, 1.0, 1.0, 0.0))
        glTranslatef(0.0, 0.0, -self.distance)
        glRotatef(self.rot_x, 1.0, 0.0, 0.0)
        glRotatef(self.rot_y, 0.0, 1.0, 0.0)
        glScalef(self.scale, self.scale, self.scale)
        glTranslatef(-self.center[0], -self.center[1], -self.center[2])

        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        if self.render_mode in {"solid", "hybrid"}:
            glEnable(GL_LIGHTING)
            glLightfv(GL_LIGHT0, GL_DIFFUSE, (0.65, 0.65, 0.65, 1.0))
            glLightfv(GL_LIGHT0, GL_AMBIENT, (0.25, 0.25, 0.25, 1.0))
            self._draw_scene(self._vbo_vertices, self._vbo_normals, self._vbo_indices)

        if self.render_mode in {"wire", "hybrid"}:
            glDisableClientState(GL_NORMAL_ARRAY)
            glPolygonMode(GL_FRONT_AND_BACK, GL_LINE)
            glLineWidth(1.4)
            glDisableClientState(GL_COLOR_ARRAY)
            glColor4f(0.2, 1.0, 0.3, 1.0)
            self._draw_scene(self._vbo_vertices, self._vbo_normals, self._vbo_indices)
            glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)

        # Meshes marked force_solid are always rendered in solid mode,
        # regardless of current wireframe/hybrid selection.
        if self._vbo_vertices_solid_only is not None and self._vbo_indices_solid_only is not None:
            glEnable(GL_LIGHTING)
            glLightfv(GL_LIGHT0, GL_DIFFUSE, (0.55, 0.55, 0.58, 1.0))
            glLightfv(GL_LIGHT0, GL_AMBIENT, (0.22, 0.22, 0.24, 1.0))
            self._draw_scene(self._vbo_vertices_solid_only, self._vbo_normals_solid_only, self._vbo_indices_solid_only)

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
            self.update()
        self.last_pos = event.position()

    def wheelEvent(self, event):
        self.distance *= 0.9 ** (event.angleDelta().y() / 120.0)
        self.update()
