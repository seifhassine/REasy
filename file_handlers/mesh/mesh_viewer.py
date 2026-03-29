from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap, QSurfaceFormat, QPainter
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
    QCheckBox,
    QHeaderView,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
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
    glTexCoordPointer,
    glColor4f,
    glDrawElements,
    GL_TRIANGLES,
    GL_LINES,
    GL_FLOAT,
    GL_UNSIGNED_INT,
    GL_VERTEX_ARRAY,
    GL_NORMAL_ARRAY,
    GL_COLOR_ARRAY,
    GL_TEXTURE_COORD_ARRAY,
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
    glBindTexture,
    glGenTextures,
    glDeleteTextures,
    glTexParameteri,
    glTexImage2D,
    glPixelStorei,
    glTexEnvi,
    GL_TEXTURE_2D,
    GL_TEXTURE_MIN_FILTER,
    GL_TEXTURE_MAG_FILTER,
    GL_LINEAR,
    GL_RGBA,
    GL_UNSIGNED_BYTE,
    GL_UNPACK_ALIGNMENT,
    GL_TEXTURE_ENV,
    GL_TEXTURE_ENV_MODE,
    GL_MODULATE,
)
from OpenGL.GLU import gluPerspective

from file_handlers.tex.qt_image_utils import decode_parsed_tex_to_qimage_with_buffer, parse_tex_bytes
from settings import save_settings
from .material_resolver import MeshMaterialBinding, MeshMaterialResolver

MATERIAL_TEXTURE_MAX_DIMENSION = 1024


class MeshViewer(QWidget):
    modified_changed = Signal(bool)
    STREAMING_SETTINGS_KEY = "mesh_viewer_prefer_streaming_tex"

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._resolved_mdf = None
        self._material_bindings: list[MeshMaterialBinding] = []
        self._texture_cache: dict[str, QImage | None] = {}
        self._texture_buffer_refs: dict[str, bytes] = {}
        self._parsed_tex_cache: dict[str, object | None] = {}
        self._resolved_texture_cache: dict[tuple[bool, str], tuple[str, bytes] | None] = {}
        self._material_panel_visible = False
        self._material_table_populated = False
        self._texture_warmup_timer = QTimer(self)
        self._texture_warmup_timer.setSingleShot(True)
        self._texture_warmup_timer.timeout.connect(self._warm_material_textures_step)
        self._texture_warmup_queue: deque[MeshMaterialBinding] = deque()
        self._layout = QVBoxLayout(self)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.gl_widget = None
        self._build_ui()
        mesh = getattr(self.handler, "mesh", None)
        if mesh and getattr(mesh, "mesh_buffer", None) and mesh.mesh_buffer.positions:
            try:
                self.gl_widget = _MeshGLWidget(mesh)
                self.preview_splitter.insertWidget(0, self.gl_widget)
                self.preview_splitter.setStretchFactor(0, 4)
                self.preview_splitter.setStretchFactor(1, 2)
            except Exception as e:
                self.preview_splitter.insertWidget(0, QLabel(f"Failed to create viewer: {e}"))
        else:
            self.preview_splitter.insertWidget(0, QLabel("No mesh buffer data available to display"))
        self._reload_materials()

    def _build_ui(self):
        top = QHBoxLayout()
        self.mdf_label = QLabel("MDF: unresolved")
        self.mdf_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.mdf_label, 1)
        self.streaming_check = QCheckBox("Prefer streaming TEX")
        self.streaming_check.setChecked(self._streaming_preference())
        self.streaming_check.toggled.connect(self._on_streaming_toggled)
        top.addWidget(self.streaming_check)
        self.panel_toggle_btn = QPushButton("Show texture panel")
        self.panel_toggle_btn.clicked.connect(self._toggle_material_panel)
        top.addWidget(self.panel_toggle_btn)
        self._layout.addLayout(top)

        self.preview_splitter = QSplitter(Qt.Horizontal, self)
        self._layout.addWidget(self.preview_splitter, 1)

        self.material_panel = QWidget(self)
        side_layout = QVBoxLayout(self.material_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(QLabel("Resolved material textures"))
        self.material_table = QTableWidget(0, 5, self.material_panel)
        self.material_table.setHorizontalHeaderLabels(["Mesh", "MDF", "Type", "Texture", "Status"])
        self.material_table.setSelectionBehavior(QTableWidget.SelectRows)
        self.material_table.setSelectionMode(QTableWidget.SingleSelection)
        self.material_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.material_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.material_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.material_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.material_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.material_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeToContents)
        self.material_table.itemSelectionChanged.connect(self._update_texture_preview)
        side_layout.addWidget(self.material_table, 1)

        self.texture_preview = QLabel("Select a material to preview its texture.")
        self.texture_preview.setAlignment(Qt.AlignCenter)
        self.texture_preview.setMinimumHeight(220)
        self.texture_preview.setWordWrap(True)
        side_layout.addWidget(self.texture_preview)

        self.preview_splitter.addWidget(self.material_panel)
        self.material_panel.hide()

    def _settings_store(self) -> dict | None:
        app = getattr(self.handler, "app", None)
        settings = getattr(app, "settings", None) if app is not None else None
        return settings if isinstance(settings, dict) else None

    def _streaming_preference(self) -> bool:
        settings = self._settings_store()
        return bool(settings.get(self.STREAMING_SETTINGS_KEY, False)) if settings is not None else False

    def _on_streaming_toggled(self, checked: bool):
        settings = self._settings_store()
        if settings is not None:
            settings[self.STREAMING_SETTINGS_KEY] = bool(checked)
            save_settings(settings)
        self._reload_materials()

    def _reload_materials(self):
        self._resolved_mdf, self._material_bindings = MeshMaterialResolver.resolve_for_handler(
            self.handler,
            prefer_streaming=self.streaming_check.isChecked(),
            resolve_textures=False,
            parse_in_subprocess=True,
            resource_cache=self._resolved_texture_cache,
        )
        active_paths = {binding.resolved_texture_path for binding in self._material_bindings if binding.resolved_texture_path}
        self._texture_cache = {
            path: image
            for path, image in self._texture_cache.items()
            if path in active_paths
        }
        self._texture_buffer_refs = {
            path: buffer_ref
            for path, buffer_ref in self._texture_buffer_refs.items()
            if path in active_paths
        }
        self._parsed_tex_cache = {
            path: tex
            for path, tex in self._parsed_tex_cache.items()
            if path in active_paths
        }
        if self._resolved_mdf:
            self.mdf_label.setText(f"MDF: {self._resolved_mdf.path}")
        else:
            self.mdf_label.setText("MDF: not found")
        self._material_table_populated = False
        if self._material_panel_visible:
            self._populate_material_table()
        self._apply_materials_to_gl_widget()
        self._schedule_texture_warmup()

    def _toggle_material_panel(self):
        self._material_panel_visible = not self._material_panel_visible
        if self._material_panel_visible:
            self.material_panel.show()
            self.panel_toggle_btn.setText("Hide texture panel")
            if not self._material_table_populated:
                self._populate_material_table()
        else:
            self.material_panel.hide()
            self.panel_toggle_btn.setText("Show texture panel")

    def _populate_material_table(self):
        table = self.material_table
        table.setRowCount(len(self._material_bindings))
        for row, binding in enumerate(self._material_bindings):
            values = [
                binding.mesh_material_name,
                binding.mdf_material_name,
                binding.texture_type,
                binding.texture_path or binding.resolved_texture_path,
                binding.status,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                table.setItem(row, col, item)
        if table.rowCount():
            table.selectRow(0)
        else:
            self.texture_preview.setText("No material mappings found.")
        self._material_table_populated = True

    def _update_texture_preview(self):
        row = self.material_table.currentRow()
        if row < 0 or row >= len(self._material_bindings):
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText("Select a material to preview its texture.")
            return

        binding = self._material_bindings[row]
        if not binding.resolved_texture_path and not binding.texture_path:
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText(binding.status)
            return

        image = self._load_texture_image(binding)
        if not image or image.isNull():
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText(f"{binding.status}\n{binding.resolved_texture_path}")
            return

        scaled = QPixmap.fromImage(image).scaled(
            320,
            320,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.texture_preview.setText("")
        self.texture_preview.setPixmap(scaled)
        self.texture_preview.setToolTip(binding.resolved_texture_path)

    def _apply_materials_to_gl_widget(self):
        if not self.gl_widget:
            return
        images: dict[str, tuple[str, QImage]] = {}
        for binding in self._material_bindings:
            if not binding.resolved_texture_path:
                continue
            image = self._texture_cache.get(binding.resolved_texture_path)
            if image is not None and not image.isNull():
                images[binding.mesh_material_name] = (binding.resolved_texture_path, image)
        self.gl_widget.set_material_images(images)

    def _schedule_texture_warmup(self):
        self._texture_warmup_timer.stop()
        self._texture_warmup_queue = deque(
            b
            for b in self._material_bindings
            if b.texture_path and (not b.resolved_texture_path or b.resolved_texture_path not in self._texture_cache)
        )
        if self._texture_warmup_queue:
            self._texture_warmup_timer.start(0)

    def _warm_material_textures_step(self):
        if not self._texture_warmup_queue:
            return
        did_load = False
        for _ in range(min(2, len(self._texture_warmup_queue))):
            binding = self._texture_warmup_queue.popleft()
            image = self._load_texture_image(binding)
            if image is not None and not image.isNull():
                did_load = True
        if did_load:
            self._apply_materials_to_gl_widget()
        if self._texture_warmup_queue:
            self._texture_warmup_timer.start(0)

    def _load_texture_image(self, binding: MeshMaterialBinding) -> QImage | None:
        if not binding.resolved_texture_path:
            if not binding.texture_path:
                return None
            resolved = MeshMaterialResolver.resolve_texture_path(
                self.handler,
                binding.texture_path,
                prefer_streaming=self.streaming_check.isChecked(),
                resource_cache=self._resolved_texture_cache,
            )
            if resolved is None:
                binding.status = "Texture not found"
                return None
            binding.resolved_texture_path, binding.resolved_texture_data = resolved
            binding.status = "Resolved"
        if not binding.resolved_texture_path:
            return None
        cached = self._texture_cache.get(binding.resolved_texture_path)
        if binding.resolved_texture_path in self._texture_cache:
            return cached

        tex_bytes = binding.resolved_texture_data
        image = self._decode_texture_image(binding.resolved_texture_path, tex_bytes) if tex_bytes else None
        self._texture_cache[binding.resolved_texture_path] = image
        return image

    @staticmethod
    def _choose_preview_mip(tex) -> int:
        mip_count = max(1, getattr(tex.header, "mip_count", 1))
        width = max(1, getattr(tex.header, "width", 1))
        height = max(1, getattr(tex.header, "height", 1))
        mip_index = 0
        while mip_index + 1 < mip_count and max(width, height) > MATERIAL_TEXTURE_MAX_DIMENSION:
            width = max(1, width // 2)
            height = max(1, height // 2)
            mip_index += 1
        return mip_index

    def _decode_texture_image(self, resolved_texture_path: str, tex_bytes: bytes) -> QImage | None:
        parsed_tex = self._parsed_tex_cache.get(resolved_texture_path)
        if resolved_texture_path not in self._parsed_tex_cache:
            parsed_tex = parse_tex_bytes(tex_bytes)
            self._parsed_tex_cache[resolved_texture_path] = parsed_tex
        if parsed_tex is None:
            return None
        decoded = decode_parsed_tex_to_qimage_with_buffer(
            parsed_tex,
            mip_selector=self._choose_preview_mip,
        )
        if decoded is None:
            return None
        image, backing_buffer = decoded
        self._texture_buffer_refs[resolved_texture_path] = backing_buffer
        return image


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
        self._update_timer_state()
        self.wireframe_mode = "off"
        self.lighting_mode = "fixed"
        self.line_width = 1.5
        self.color_source = "vertex"
        self.ambient = 0.35
        self.diffuse = 0.65        
        self.show_bone_labels = False

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
        olayout.addLayout(row2)
        
        row3 = QHBoxLayout()
        self.bone_labels_check = QCheckBox("Bones", self.overlay)
        self.bone_labels_check.setChecked(self.show_bone_labels)
        self.bone_labels_check.toggled.connect(self._set_show_bone_labels)
        row3.addWidget(self.bone_labels_check)
        olayout.addLayout(row3)

        self.overlay.adjustSize()
        self.overlay.move(10, 10)

        mb = mesh.mesh_buffer
        payloads = getattr(mb, "buffer_payloads", {}) or {0: mb}
        vertex_chunks: list[np.ndarray] = []
        normal_chunks: list[np.ndarray] = []
        color_chunks: list[np.ndarray] = []
        uv_chunks: list[np.ndarray] = []
        payload_base: dict[int, int] = {}
        running_base = 0
        for buffer_index in sorted(payloads.keys()):
            payload = payloads[buffer_index]
            if not getattr(payload, "positions", None):
                continue
            verts = np.array(payload.positions, dtype=np.float32).reshape(-1, 3)
            payload_base[buffer_index] = running_base
            running_base += len(verts)
            vertex_chunks.append(verts)
            if getattr(payload, "normals", None):
                normal_chunks.append(np.array(payload.normals, dtype=np.float32).reshape(-1, 3))
            else:
                normal_chunks.append(np.zeros((len(verts), 3), dtype=np.float32))
            if getattr(payload, "colors", None):
                color_chunks.append(np.array(payload.colors, dtype=np.uint8).reshape(-1, 4).astype(np.float32) / 255.0)
            else:
                color_chunks.append(np.ones((len(verts), 4), dtype=np.float32))
            if getattr(payload, "uv0", None):
                uvs = np.array(payload.uv0, dtype=np.float32).reshape(-1, 2)
                if len(uvs) == len(verts):
                    uv_chunks.append(1.0 - uvs)
                else:
                    uv_chunks.append(np.zeros((len(verts), 2), dtype=np.float32))
            else:
                uv_chunks.append(np.zeros((len(verts), 2), dtype=np.float32))
        self.vertices = np.concatenate(vertex_chunks, axis=0) if vertex_chunks else np.zeros((0, 3), dtype=np.float32)
        self.normals = np.concatenate(normal_chunks, axis=0) if normal_chunks else None
        if self.normals is not None and len(self.normals):
            lengths = np.linalg.norm(self.normals, axis=1)
            safe_normals = np.zeros_like(self.normals, dtype=np.float32)
            np.divide(self.normals, lengths[:, np.newaxis], out=safe_normals, where=lengths[:, np.newaxis] > 0)
            invalid_mask = ~np.isfinite(safe_normals).all(axis=1)
            if np.any(invalid_mask):
                safe_normals[invalid_mask] = np.array([0.0, 0.0, 1.0], dtype=np.float32)
            self.normals = safe_normals
        self.colors = np.concatenate(color_chunks, axis=0) if color_chunks else None
        self.uvs = np.concatenate(uv_chunks, axis=0) if uv_chunks else None
        self.base_colors = self.colors.copy() if self.colors is not None and len(self.colors) == len(self.vertices) else np.ones((len(self.vertices), 4), dtype=np.float32)

        index_chunks: list[np.ndarray] = []
        self._draw_batches_data: list[tuple[str, np.ndarray]] = []
        material_names = list(getattr(mesh, "material_names", []) or [])
        if mesh.meshes:
            for m in mesh.meshes:
                if not m.lods:
                    continue
                lod0 = m.lods[0]
                for mg in lod0.parts:
                    for sm in mg.submeshes:
                        payload = payloads.get(getattr(sm, "buffer_index", 0), payloads.get(0))
                        if payload is None:
                            continue
                        face_array = payload.integer_faces if getattr(payload, "integer_faces", None) is not None else payload.faces
                        start = sm.faces_index_offset
                        end = start + sm.indices_count
                        base = payload_base.get(getattr(sm, "buffer_index", 0), 0) + sm.verts_index_offset
                        batch_indices = np.asarray(face_array[start:end], dtype=np.uint32) + np.uint32(base)
                        index_chunks.append(batch_indices)
                        material_name = ""
                        material_idx = getattr(sm, "material_index", -1)
                        if 0 <= material_idx < len(material_names):
                            material_name = material_names[material_idx]
                        self._draw_batches_data.append((material_name, batch_indices))
        if not index_chunks:
            payload0 = payloads.get(0)
            if payload0 is not None:
                face_array = payload0.integer_faces if getattr(payload0, "integer_faces", None) is not None else payload0.faces
                fallback_indices = np.asarray(face_array, dtype=np.uint32)
                index_chunks.append(fallback_indices)
                self._draw_batches_data.append(("", fallback_indices))
        self.indices = np.concatenate(index_chunks) if index_chunks else np.zeros((0,), dtype=np.uint32)

        self.indices_lines = None

        mins = self.vertices.min(axis=0)
        maxs = self.vertices.max(axis=0)
        self.center = (mins + maxs) / 2.0
        extent = float(np.max(maxs - mins))
        self.scale = 1.0 / extent if extent else 1.0

        self.vbo_vertices = None
        self.vbo_normals = None
        self.vbo_uvs = None
        self.vbo_colors = None
        self.vbo_indices = None
        self.vbo_indices_lines = None
        self._draw_batches: list[tuple[str, vbo.VBO, int]] = []
        self._texture_ids: dict[str, int] = {}
        self._pending_material_images: dict[str, tuple[str, QImage]] = {}
        self._colors_dirty = True
        self._texture_sources: dict[str, str] = {}        
        self._bone_labels, self._bone_points = self._build_bone_label_points()

    def _build_bone_label_points(self) -> tuple[list[str], np.ndarray]:
        joint_count = int(getattr(self.mesh, "joint_count", 0) or 0)
        if joint_count <= 0:
            return [], np.zeros((0, 3), dtype=np.float32)
        matrices = list(getattr(self.mesh, "world_matrices", None) or getattr(self.mesh, "local_matrices", None) or [])
        if not matrices:
            return [], np.zeros((0, 3), dtype=np.float32)
        names = list(getattr(self.mesh, "names", []) or [])
        bone_indices = list(getattr(self.mesh, "bone_indices", []) or [])

        labels: list[str] = []
        points = np.zeros((joint_count, 3), dtype=np.float32)
        for i in range(joint_count):
            matrix = matrices[i]
            points[i] = (matrix[12], matrix[13], matrix[14])
            if i < len(bone_indices) and 0 <= bone_indices[i] < len(names):
                labels.append(names[bone_indices[i]])
            else:
                labels.append(f"bone_{i}")
        return labels, points

    def _project_points(self, points: np.ndarray) -> np.ndarray:
        w = max(1, self.width())
        h = max(1, self.height())
        aspect = w / h
        f = 1.0 / np.tan(np.radians(45.0) / 2.0)
        cx, sx = np.cos(np.radians(self.rot_x)), np.sin(np.radians(self.rot_x))
        cy, sy = np.cos(np.radians(self.rot_y)), np.sin(np.radians(self.rot_y))
        center = self.center.astype(np.float64)

        t0 = np.array([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, -self.distance], [0, 0, 0, 1]], dtype=np.float64)
        rx = np.array([[1, 0, 0, 0], [0, cx, -sx, 0], [0, sx, cx, 0], [0, 0, 0, 1]], dtype=np.float64)
        ry = np.array([[cy, 0, sy, 0], [0, 1, 0, 0], [-sy, 0, cy, 0], [0, 0, 0, 1]], dtype=np.float64)
        s = np.array([[self.scale, 0, 0, 0], [0, self.scale, 0, 0], [0, 0, self.scale, 0], [0, 0, 0, 1]], dtype=np.float64)
        tc = np.array([[1, 0, 0, -center[0]], [0, 1, 0, -center[1]], [0, 0, 1, -center[2]], [0, 0, 0, 1]], dtype=np.float64)
        model = t0 @ rx @ ry @ s @ tc
        proj = np.array([
            [f / aspect, 0.0, 0.0, 0.0],
            [0.0, f, 0.0, 0.0],
            [0.0, 0.0, -1.002002002002002, -0.20020020020020018],
            [0.0, 0.0, -1.0, 0.0],
        ], dtype=np.float64)

        pts = np.c_[points.astype(np.float64), np.ones((len(points), 1), dtype=np.float64)]
        clip = (proj @ model @ pts.T).T
        ndc = clip[:, :3] / clip[:, 3:4]
        w_non_zero = ~np.isclose(clip[:, 3], 0.0, rtol=0.0, atol=1e-12)
        visible = w_non_zero & (ndc[:, 2] >= -1.0) & (ndc[:, 2] <= 1.0)
        screen = np.column_stack(((ndc[:, 0] * 0.5 + 0.5) * w, (1.0 - (ndc[:, 1] * 0.5 + 0.5)) * h))
        return np.column_stack((screen, visible.astype(np.float64)))

    def _draw_bone_labels(self):
        if not self.show_bone_labels or len(self._bone_points) == 0:
            return
        projected = self._project_points(self._bone_points)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setPen(Qt.yellow)
        for i, label in enumerate(self._bone_labels):
            x, y, visible = projected[i]
            if not visible:
                continue
            if 0 <= x <= self.width() and 0 <= y <= self.height():
                painter.drawText(int(x) + 4, int(y) - 4, label)
        painter.end()

    def _ensure_color_vbo_for_current_mode(self):
        base = self.base_colors if self.color_source == "vertex" else np.ones((len(self.vertices), 4), dtype=np.float32)
        apply_lighting = self.lighting_mode == "software"
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

    def _ensure_line_indices(self):
        if self.indices_lines is not None:
            return
        if len(self.indices) % 3 == 0:
            tris_edges = np.concatenate([
                self.indices.reshape(-1, 3)[:, [0, 1]],
                self.indices.reshape(-1, 3)[:, [1, 2]],
                self.indices.reshape(-1, 3)[:, [2, 0]],
            ], axis=0)
            self.indices_lines = tris_edges.astype(np.uint32).reshape(-1)
        else:
            self.indices_lines = self.indices.copy()

    def _cleanup_gl(self):
        self.makeCurrent()
        self._clear_gl_textures()
        for _, batch_vbo, _ in self._draw_batches:
            batch_vbo.delete()
        self._draw_batches.clear()
        if self.vbo_indices_lines is not None:
            self.vbo_indices_lines.delete()
            self.vbo_indices_lines = None
        if self.vbo_indices is not None:
            self.vbo_indices.delete()
            self.vbo_indices = None
        if self.vbo_uvs is not None:
            self.vbo_uvs.delete()
            self.vbo_uvs = None
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

    def set_material_images(self, images: dict[str, tuple[str, QImage]]):
        self._pending_material_images = dict(images)
        if self.context() is None:
            self.update()
            return
        self.makeCurrent()
        self._sync_gl_textures()
        self.doneCurrent()
        self.update()

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

    def _clear_gl_textures(self):
        if self._texture_ids:
            glDeleteTextures(list(self._texture_ids.values()))
            self._texture_ids.clear()
        self._texture_sources.clear()

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
        glTexEnvi(GL_TEXTURE_ENV, GL_TEXTURE_ENV_MODE, GL_MODULATE)
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glLightfv(GL_LIGHT0, GL_DIFFUSE, (1.0, 1.0, 1.0, 1.0))
        glLightfv(GL_LIGHT0, GL_AMBIENT, (0.3, 0.3, 0.3, 1.0))

        ctx = self.context()
        ctx.aboutToBeDestroyed.connect(self._cleanup_gl)

        self.vbo_vertices = vbo.VBO(self.vertices)
        self.vbo_indices = vbo.VBO(self.indices, target=GL_ELEMENT_ARRAY_BUFFER)
        if self.uvs is not None and len(self.uvs) == len(self.vertices):
            self.vbo_uvs = vbo.VBO(self.uvs.astype(np.float32))
        if self.normals is not None:
            self.vbo_normals = vbo.VBO(self.normals)
        self._draw_batches = [
            (material_name, vbo.VBO(indices, target=GL_ELEMENT_ARRAY_BUFFER), len(indices))
            for material_name, indices in self._draw_batches_data
            if len(indices) > 0
        ]
        self._ensure_color_vbo_for_current_mode()
        self._sync_gl_textures()

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
            textured = self.vbo_uvs is not None and bool(self._texture_ids)
            if textured:
                self.vbo_uvs.bind()
                glEnableClientState(GL_TEXTURE_COORD_ARRAY)
                glTexCoordPointer(2, GL_FLOAT, 0, None)
            else:
                glDisableClientState(GL_TEXTURE_COORD_ARRAY)

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

            if self._draw_batches:
                for material_name, batch_vbo, count in self._draw_batches:
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
                glBindTexture(GL_TEXTURE_2D, 0)
                glDisable(GL_TEXTURE_2D)
            else:
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
            if textured:
                glDisableClientState(GL_TEXTURE_COORD_ARRAY)
                self.vbo_uvs.unbind()
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
        self._draw_bone_labels()

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

    def showEvent(self, event):
        super().showEvent(event)
        self._update_timer_state()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def _update_timer_state(self):
        if not self.isVisible():
            self._timer.stop()
            return
        interval = 0 if self._fps_limit == 0 else max(1, round(1000 / self._fps_limit))
        self._timer.start(interval)

    def _change_fps_limit(self, value: int):
        self._fps_limit = value
        self._update_timer_state()

    def _set_wireframe_mode(self, mode: str):
        self.wireframe_mode = mode
        if mode in ("lines_depth", "lines_overlay") and self.vbo_indices_lines is None:
            self._ensure_line_indices()
            if self.indices_lines is not None and len(self.indices_lines) > 0:
                self.makeCurrent()
                self.vbo_indices_lines = vbo.VBO(self.indices_lines, target=GL_ELEMENT_ARRAY_BUFFER)
                self.doneCurrent()
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
        
    def _set_show_bone_labels(self, checked: bool):
        self.show_bone_labels = bool(checked)
        self.update()

