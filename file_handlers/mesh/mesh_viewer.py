from __future__ import annotations

import time
from collections import deque
from contextlib import suppress

import numpy as np
from ui.scene.scene_preview import ScenePreviewWidget
from OpenGL.GL import (
    GL_BLEND,
    GL_CULL_FACE,
    GL_DEPTH_TEST,
    GL_FILL,
    GL_FLOAT,
    GL_FRAGMENT_SHADER,
    GL_FRONT_AND_BACK,
    GL_LIGHTING,
    GL_ONE_MINUS_SRC_ALPHA,
    GL_QUADS,
    GL_SRC_ALPHA,
    GL_TEXTURE_2D,
    GL_TEXTURE_COORD_ARRAY,
    GL_VERTEX_ARRAY,
    GL_VERTEX_SHADER,
    glBindTexture,
    glBlendFunc,
    glColor4f,
    glDeleteProgram,
    glDeleteTextures,
    glDisable,
    glDisableClientState,
    glDisableVertexAttribArray,
    glDrawArrays,
    glEnable,
    glEnableClientState,
    glEnableVertexAttribArray,
    glGenTextures,
    glGetAttribLocation,
    glGetUniformLocation,
    glPolygonMode,
    glTexCoordPointer,
    glUniform1i,
    glUniform2f,
    glUseProgram,
    glVertexAttribPointer,
    glVertexPointer,
)
from OpenGL.GL.shaders import compileProgram, compileShader
from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QFontMetrics, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from file_handlers.tex.qt_image_utils import TexPreviewUpload, build_tex_preview_upload, decode_parsed_tex_to_qimage_with_buffer, parse_tex_bytes
from file_handlers.tex.texture_quality import (
    DEFAULT_TEXTURE_QUALITY,
    choose_texture_mip,
    texture_quality_profile,
)
from settings import save_settings
from ui.scene.mesh_scene import build_mesh_scene
from .material_resolver import MeshMaterialBinding, MeshMaterialResolver

BONE_LABEL_VERTEX_SHADER = """
#version 120
attribute vec2 labelOffset;
uniform vec2 viewport;
varying vec2 labelTexCoord;

void main()
{
    vec4 clip = gl_ModelViewProjectionMatrix * gl_Vertex;
    labelTexCoord = gl_MultiTexCoord0.st;
    if (clip.w <= 0.0 || clip.z < -clip.w || clip.z > clip.w) {
        gl_Position = vec4(2.0, 2.0, 2.0, 1.0);
        return;
    }
    vec2 ndcOffset = vec2(labelOffset.x * 2.0 / viewport.x, -labelOffset.y * 2.0 / viewport.y) * clip.w;
    clip.xy += ndcOffset;
    gl_Position = clip;
}
"""

BONE_LABEL_FRAGMENT_SHADER = """
#version 120
uniform sampler2D labelTexture;
varying vec2 labelTexCoord;

void main()
{
    gl_FragColor = texture2D(labelTexture, labelTexCoord);
}
"""


class MeshViewer(QWidget):
    modified_changed = Signal(bool)
    VERTEX_COLORS_SETTINGS_KEY = "mesh_viewer_use_vertex_colors"

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._resolved_mdf = None
        self._material_bindings: list[MeshMaterialBinding] = []
        self._texture_cache: dict[str, TexPreviewUpload | None] = {}
        self._texture_preview_cache: dict[str, QImage | None] = {}
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
                self.gl_widget = _MeshGLWidget(
                    mesh,
                    self._settings_store(),
                    use_vertex_colors=self._setting_bool(self.VERTEX_COLORS_SETTINGS_KEY),
                )
                self.gl_widget.texture_quality_changed.connect(self._on_texture_quality_changed)
                self.preview_splitter.insertWidget(0, self.gl_widget)
                self.preview_splitter.setStretchFactor(0, 4)
                self.preview_splitter.setStretchFactor(1, 2)
            except Exception as e:
                self.preview_splitter.insertWidget(
                    0, QLabel(self.tr("Failed to create viewer: {}").format(e))
                )
        else:
            self.preview_splitter.insertWidget(
                0, QLabel(self.tr("No mesh buffer data available to display"))
            )
        self._reload_materials()

    def _build_ui(self):
        top = QHBoxLayout()
        self.mdf_label = QLabel(self.tr("MDF: unresolved"))
        self.mdf_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        top.addWidget(self.mdf_label, 1)
        self.vertex_colors_check = QCheckBox(self.tr("Vertex colors"))
        self.vertex_colors_check.setToolTip(
            self.tr("Multiply textured preview by mesh vertex colors")
        )
        self.vertex_colors_check.setChecked(self._setting_bool(self.VERTEX_COLORS_SETTINGS_KEY))
        self.vertex_colors_check.toggled.connect(self._on_vertex_colors_toggled)
        top.addWidget(self.vertex_colors_check)
        self.panel_toggle_btn = QPushButton(self.tr("Show texture panel"))
        self.panel_toggle_btn.clicked.connect(self._toggle_material_panel)
        top.addWidget(self.panel_toggle_btn)
        self._layout.addLayout(top)

        self.preview_splitter = QSplitter(Qt.Horizontal, self)
        self._layout.addWidget(self.preview_splitter, 1)

        self.material_panel = QWidget(self)
        side_layout = QVBoxLayout(self.material_panel)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.addWidget(QLabel(self.tr("Resolved material textures")))
        self.material_table = QTableWidget(0, 5, self.material_panel)
        self.material_table.setHorizontalHeaderLabels(
            [
                self.tr("Mesh"),
                "MDF",
                self.tr("Type"),
                self.tr("Texture"),
                self.tr("Status"),
            ]
        )
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

        self.texture_preview = QLabel(
            self.tr("Select a material to preview its texture.")
        )
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

    def _setting_bool(self, key: str) -> bool:
        settings = self._settings_store()
        return bool(settings.get(key, False)) if settings is not None else False

    def _save_bool_setting(self, key: str, value: bool):
        settings = self._settings_store()
        if settings is not None:
            settings[key] = bool(value)
            save_settings(settings)

    def _on_texture_quality_changed(self, _quality: str):
        self._texture_warmup_timer.stop()
        self._texture_warmup_queue.clear()
        self._texture_cache.clear()
        self._texture_preview_cache.clear()
        self._texture_buffer_refs.clear()
        if self.gl_widget:
            self.gl_widget.set_material_images({})
        self._reload_materials()

    def _on_vertex_colors_toggled(self, checked: bool):
        self._save_bool_setting(self.VERTEX_COLORS_SETTINGS_KEY, checked)
        if self.gl_widget:
            self.gl_widget.set_vertex_colors_enabled(checked)

    def _reload_materials(self):
        self._resolved_mdf, self._material_bindings = MeshMaterialResolver.resolve_for_handler(
            self.handler,
            prefer_streaming=self._quality_profile().prefer_streaming,
            resolve_textures=False,
            parse_in_subprocess=True,
            resource_cache=self._resolved_texture_cache,
        )
        if self._resolved_mdf:
            self.mdf_label.setText(
                self.tr("MDF: {path}").format(path=self._resolved_mdf.path)
            )
        else:
            self.mdf_label.setText(self.tr("MDF: not found"))
        self._material_table_populated = False
        if self._material_panel_visible:
            self._populate_material_table()
        self._apply_materials_to_gl_widget()
        self._schedule_texture_warmup()

    def _toggle_material_panel(self):
        self._material_panel_visible = not self._material_panel_visible
        if self._material_panel_visible:
            self.material_panel.show()
            self.panel_toggle_btn.setText(self.tr("Hide texture panel"))
            if not self._material_table_populated:
                self._populate_material_table()
        else:
            self.material_panel.hide()
            self.panel_toggle_btn.setText(self.tr("Show texture panel"))

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
            self.texture_preview.setText(self.tr("No material mappings found."))
        self._material_table_populated = True

    def _update_texture_preview(self):
        row = self.material_table.currentRow()
        if row < 0 or row >= len(self._material_bindings):
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText(
                self.tr("Select a material to preview its texture.")
            )
            return

        binding = self._material_bindings[row]
        if not binding.resolved_texture_path and not binding.texture_path:
            self.texture_preview.setPixmap(QPixmap())
            self.texture_preview.setText(binding.status)
            return

        image = self._load_texture_preview_image(binding)
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
        self.gl_widget.set_material_profiles({b.mesh_material_name: b.surface for b in self._material_bindings if b.surface is not None})
        images: dict[str, tuple[str, TexPreviewUpload]] = {
            b.mesh_material_name: (b.resolved_texture_path, texture)
            for b in self._material_bindings
            if b.resolved_texture_path and (texture := self._texture_cache.get(b.resolved_texture_path)) is not None
        }
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
        deadline = time.perf_counter() + 0.02
        loaded = {}
        processed = 0
        while self._texture_warmup_queue and (processed == 0 or time.perf_counter() < deadline):
            processed += 1
            binding = self._texture_warmup_queue.popleft()
            texture = self._load_texture_image(binding)
            if texture is not None:
                loaded[binding.mesh_material_name] = (binding.resolved_texture_path, texture)
        if loaded and self.gl_widget:
            self.gl_widget.update_material_images(loaded)
        if self._texture_warmup_queue:
            self._texture_warmup_timer.start(0)

    def _load_texture_image(self, binding: MeshMaterialBinding) -> TexPreviewUpload | None:
        if not binding.resolved_texture_path:
            if not binding.texture_path:
                return None
            resolved = MeshMaterialResolver.resolve_texture_path(
                self.handler,
                binding.texture_path,
                prefer_streaming=self._quality_profile().prefer_streaming,
                resource_cache=self._resolved_texture_cache,
            )
            if resolved is None:
                binding.status = "Texture not found"
                print(
                    f"Texture resolution failed: material={binding.mesh_material_name!r}, "
                    f"path={binding.texture_path!r}, quality={self._quality_profile().label}"
                )
                return None
            binding.resolved_texture_path, binding.resolved_texture_data = resolved
            binding.status = "Resolved"
        if not binding.resolved_texture_path:
            return None
        cached = self._texture_cache.get(binding.resolved_texture_path)
        if binding.resolved_texture_path in self._texture_cache:
            return cached

        try:
            tex_bytes = binding.resolved_texture_data
            tex = (
                self._parse_texture(binding.resolved_texture_path, tex_bytes, raise_errors=True)
                if tex_bytes
                else None
            )
            upload = build_tex_preview_upload(tex, mip_selector=self._choose_preview_mip)
        except Exception as exc:
            print(f"Texture preparation failed: path={binding.resolved_texture_path!r}: {exc}")
            upload = None
        self._texture_cache[binding.resolved_texture_path] = upload
        return self._texture_cache[binding.resolved_texture_path]

    def _load_texture_preview_image(self, binding: MeshMaterialBinding) -> QImage | None:
        self._load_texture_image(binding)
        path = binding.resolved_texture_path
        if not path:
            return None
        if path in self._texture_preview_cache:
            return self._texture_preview_cache[path]
        image = self._decode_texture_image(path, binding.resolved_texture_data) if binding.resolved_texture_data else None
        self._texture_preview_cache[path] = image
        return image

    def _quality_profile(self):
        quality = getattr(self.gl_widget, "texture_quality", DEFAULT_TEXTURE_QUALITY)
        return texture_quality_profile(quality)

    def _choose_preview_mip(self, tex) -> int:
        return choose_texture_mip(tex, self._quality_profile())

    def _decode_texture_image(self, resolved_texture_path: str, tex_bytes: bytes) -> QImage | None:
        parsed_tex = self._parse_texture(resolved_texture_path, tex_bytes)
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

    def _parse_texture(self, resolved_texture_path: str, tex_bytes: bytes, *, raise_errors: bool = False):
        if resolved_texture_path not in self._parsed_tex_cache:
            self._parsed_tex_cache[resolved_texture_path] = parse_tex_bytes(tex_bytes, raise_errors=raise_errors)
        return self._parsed_tex_cache[resolved_texture_path]


class _MeshGLWidget(ScenePreviewWidget):
    def __init__(self, mesh, settings: dict | None = None, *, use_vertex_colors: bool = False):
        self.mesh = mesh
        self._bone_label_texture_id = None
        self._bone_label_centers_vbo = None
        self._bone_label_offsets_vbo = None
        self._bone_label_texcoords_vbo = None
        self._bone_label_shader = None
        self._bone_label_offset_attr = -1
        self._bone_label_viewport_uniform = -1
        self._bone_label_texture_uniform = -1
        super().__init__(
            controls="mesh",
            settings=settings,
            initial_rotation=(0.0, 0.0),
            initial_distance=3.0,
            background=(0.1, 0.1, 0.1, 1.0),
        )
        self.set_vertex_colors_enabled(use_vertex_colors, refresh=False)
        self.set_scene(build_mesh_scene(mesh, key="mesh"))
        self._bone_labels, self._bone_points = self._build_bone_label_points()
        self._bone_label_atlas, bone_label_rects = self._build_bone_label_atlas()
        (
            self._bone_label_centers,
            self._bone_label_offsets,
            self._bone_label_texcoords,
        ) = self._build_bone_label_quad_arrays(bone_label_rects)
        self._bone_label_vertex_count = len(self._bone_label_centers)

    def set_vertex_colors_enabled(self, enabled: bool, *, refresh: bool = True):
        color_source = "vertex" if enabled else ""
        if self.color_source == color_source:
            return
        self.color_source = color_source
        self._colors_dirty = True
        if refresh:
            self.update()

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

    def _build_bone_label_atlas(self) -> tuple[QImage | None, list[tuple[int, int, int, int]]]:
        if not self._bone_labels:
            return None, []
        metrics = QFontMetrics(self.font())
        max_width = 2048
        label_sizes = [
            (max(1, metrics.horizontalAdvance(label) + 2), max(1, metrics.height() + 2))
            for label in self._bone_labels
        ]
        atlas_width = min(max_width, max(width for width, _ in label_sizes))
        atlas_width = max(atlas_width, min(max_width, 512))
        x = y = row_height = 0
        rects: list[tuple[int, int, int, int]] = []
        for width, height in label_sizes:
            if x and x + width > atlas_width:
                x = 0
                y += row_height
                row_height = 0
            rects.append((x, y, width, height))
            x += width
            row_height = max(row_height, height)

        atlas = QImage(atlas_width, max(1, y + row_height), QImage.Format.Format_RGBA8888)
        atlas.fill(Qt.transparent)
        painter = QPainter(atlas)
        painter.setRenderHint(QPainter.TextAntialiasing, True)
        painter.setPen(Qt.yellow)
        for label, (x, y, _width, _height) in zip(self._bone_labels, rects):
            painter.drawText(x + 1, y + metrics.ascent() + 1, label)
        painter.end()
        return atlas, rects

    def _build_bone_label_quad_arrays(
        self,
        rects: list[tuple[int, int, int, int]],
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        if self._bone_label_atlas is None or not rects:
            empty2 = np.zeros((0, 2), dtype=np.float32)
            return np.zeros((0, 3), dtype=np.float32), empty2, empty2

        atlas_w = max(1, self._bone_label_atlas.width())
        atlas_h = max(1, self._bone_label_atlas.height())
        centers = np.repeat(self._bone_points[:len(rects)], 4, axis=0).astype(np.float32, copy=False)
        offsets = np.empty((len(rects) * 4, 2), dtype=np.float32)
        texcoords = np.empty_like(offsets)
        for i, (rx, ry, rw, rh) in enumerate(rects):
            base = i * 4
            offsets[base:base + 4] = (
                (4.0, -rh - 4.0),
                (rw + 4.0, -rh - 4.0),
                (rw + 4.0, -4.0),
                (4.0, -4.0),
            )
            u0, v0 = rx / atlas_w, ry / atlas_h
            u1, v1 = (rx + rw) / atlas_w, (ry + rh) / atlas_h
            texcoords[base:base + 4] = ((u0, v0), (u1, v0), (u1, v1), (u0, v1))
        return centers, offsets, texcoords

    def _after_gl_initialized(self):
        try:
            self._sync_bone_label_gl_resources()
        except Exception as exc:
            print(f"Bone label GL setup failed: {exc}")

    def _after_scene_draw(self):
        self._draw_bone_labels_gl()

    def _cleanup_extra_gl(self):
        if self._bone_label_texture_id is not None:
            with suppress(Exception):
                glDeleteTextures([self._bone_label_texture_id])
            self._bone_label_texture_id = None
        for name in ("_bone_label_centers_vbo", "_bone_label_offsets_vbo", "_bone_label_texcoords_vbo"):
            self._dispose_vbo(getattr(self, name))
            setattr(self, name, None)
        if self._bone_label_shader is not None:
            with suppress(Exception):
                glDeleteProgram(self._bone_label_shader)
            self._bone_label_shader = None

    def _sync_bone_label_gl_resources(self):
        if self._bone_label_texture_id is None and self._bone_label_atlas is not None:
            self._bone_label_texture_id = glGenTextures(1)
            self._upload_qimage_texture(self._bone_label_texture_id, self._bone_label_atlas)
        if self._bone_label_shader is None and self._bone_label_vertex_count > 0:
            self._bone_label_shader = compileProgram(
                compileShader(BONE_LABEL_VERTEX_SHADER, GL_VERTEX_SHADER),
                compileShader(BONE_LABEL_FRAGMENT_SHADER, GL_FRAGMENT_SHADER),
            )
            self._bone_label_offset_attr = glGetAttribLocation(self._bone_label_shader, "labelOffset")
            self._bone_label_viewport_uniform = glGetUniformLocation(self._bone_label_shader, "viewport")
            self._bone_label_texture_uniform = glGetUniformLocation(self._bone_label_shader, "labelTexture")
        if self._bone_label_centers_vbo is None and self._bone_label_vertex_count > 0:
            self._bone_label_centers_vbo = self._array_vbo(self._bone_label_centers)
            self._bone_label_offsets_vbo = self._array_vbo(self._bone_label_offsets)
            self._bone_label_texcoords_vbo = self._array_vbo(self._bone_label_texcoords)

    def _draw_bone_labels_gl(self):
        if (
            not self.show_bone_labels
            or self._bone_label_texture_id is None
            or self._bone_label_shader is None
            or self._bone_label_offset_attr < 0
            or self._bone_label_vertex_count <= 0
            or self._bone_label_centers_vbo is None
            or self._bone_label_offsets_vbo is None
            or self._bone_label_texcoords_vbo is None
        ):
            return
        w = max(1, self.width())
        h = max(1, self.height())

        glDisable(GL_DEPTH_TEST)
        glDisable(GL_LIGHTING)
        glDisable(GL_CULL_FACE)
        glEnable(GL_BLEND)
        glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        glEnable(GL_TEXTURE_2D)
        glBindTexture(GL_TEXTURE_2D, self._bone_label_texture_id)
        glPolygonMode(GL_FRONT_AND_BACK, GL_FILL)
        glColor4f(1.0, 1.0, 1.0, 1.0)
        glUseProgram(self._bone_label_shader)
        glUniform2f(self._bone_label_viewport_uniform, float(w), float(h))
        glUniform1i(self._bone_label_texture_uniform, 0)

        glEnableClientState(GL_VERTEX_ARRAY)
        glEnableClientState(GL_TEXTURE_COORD_ARRAY)
        glEnableVertexAttribArray(self._bone_label_offset_attr)
        self._bone_label_centers_vbo.bind()
        glVertexPointer(3, GL_FLOAT, 0, None)
        self._bone_label_texcoords_vbo.bind()
        glTexCoordPointer(2, GL_FLOAT, 0, None)
        self._bone_label_offsets_vbo.bind()
        glVertexAttribPointer(self._bone_label_offset_attr, 2, GL_FLOAT, False, 0, None)
        glDrawArrays(GL_QUADS, 0, self._bone_label_vertex_count)
        self._bone_label_offsets_vbo.unbind()
        self._bone_label_texcoords_vbo.unbind()
        self._bone_label_centers_vbo.unbind()
        glDisableVertexAttribArray(self._bone_label_offset_attr)
        glDisableClientState(GL_TEXTURE_COORD_ARRAY)
        glDisableClientState(GL_VERTEX_ARRAY)

        glUseProgram(0)
        glBindTexture(GL_TEXTURE_2D, 0)
        glDisable(GL_TEXTURE_2D)
        glDisable(GL_BLEND)
        glEnable(GL_DEPTH_TEST)
        glEnable(GL_CULL_FACE)
        if self.lighting_mode == "fixed":
            glEnable(GL_LIGHTING)
