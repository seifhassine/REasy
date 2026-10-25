from __future__ import annotations

lazy import numpy as np
from PySide6.QtCore import Qt, Signal
lazy from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
    QSlider, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

lazy from file_handlers.mesh.scene_textures import plain_preview
lazy from ui.background_jobs import run_job
lazy from ui.scene.mesh_scene import build_mesh_scene
lazy from ui.scene.scene_model import SceneDrawMesh
lazy from ui.scene.scene_preview import ScenePreviewWidget
lazy from utils.resource_file_utils import resource_context_for_handler
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role

lazy from .geometry import (BakeCancelled, Grid, bake_levels, covering_grid,
                       geometry_bounds_at_native_center, mesh_bounds, surface_mesh)
lazy from .mesh_resource import reference_path
lazy from .sdftex_file import SdfTexFile, StorageFormat, supported_formats


def _signed_rgb(samples, scale: float):
    t = np.clip(np.abs(samples)/scale, 0, 1)[..., None]
    color = np.where((samples < 0)[..., None], np.array((.12, .45, 1)), np.array((1, .42, .08)))
    return np.ascontiguousarray(np.rint((1+(color-1)*t)*255), dtype=np.uint8)


def _legend_ramp(width: int = 120, height: int = 10) -> QPixmap:
    rgb = np.repeat(_signed_rgb(np.linspace(-1.0, 1.0, width), 1.0)[None], height, axis=0)
    return QPixmap.fromImage(QImage(rgb.data, width, height, rgb.strides[0], QImage.Format_RGB888).copy())


class _SliceImage(QLabel):
    sample_selected = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(220, 220)
        colors = current_colors()
        self.setStyleSheet(f"QLabel {{ background: {colors['canvas_bg']}; border: 1px solid {colors['border_subtle']};"
                           " border-radius: 3px; }")
        self._source = QPixmap()
        self.setMouseTracking(True)

    def set_image(self, image):
        self._source = QPixmap.fromImage(image)
        self._scale()

    def _scale(self):
        if not self._source.isNull():
            self.setPixmap(self._source.scaled(self.size(), Qt.KeepAspectRatio, Qt.FastTransformation))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._scale()

    def mouseMoveEvent(self, event):
        pixmap = self.pixmap()
        if pixmap is not None and not pixmap.isNull():
            x = event.position().x()-(self.width()-pixmap.width())/2
            y = event.position().y()-(self.height()-pixmap.height())/2
            if 0 <= x < pixmap.width() and 0 <= y < pixmap.height():
                self.sample_selected.emit(int(x*self._source.width()/pixmap.width()),
                                          int(y*self._source.height()/pixmap.height()))


class DistanceFieldViewer(QWidget):
    def __init__(self, document: SdfTexFile | None, *, center=(0, 0, 0), scene=None, parent=None):
        super().__init__(parent)
        self.document = None
        self.center = tuple(map(float, center))
        self.scene = scene or []
        self.values = None
        self._surface = None
        self._gl = self._device = None
        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        row = QHBoxLayout()
        row.addWidget(QLabel(self.tr("Quality level:")))
        self.quality = QComboBox()
        self.quality.currentIndexChanged.connect(self._level_changed)
        row.addWidget(self.quality)
        row.addStretch()
        layout.addLayout(row)
        self.info = QLabel()
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_style_role(self.info, tone="muted")
        layout.addWidget(self.info)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        slice_tab = QWidget()
        slices = QVBoxLayout(slice_tab)
        row = QHBoxLayout()
        self.axis = QComboBox()
        self.axis.addItems(['X', 'Y', 'Z'])
        self.axis.setCurrentIndex(2)
        self.axis.currentIndexChanged.connect(self._axis_changed)
        row.addWidget(QLabel(self.tr("Slice axis:")))
        row.addWidget(self.axis)
        self.slice_slider = QSlider(Qt.Horizontal)
        self.slice_slider.valueChanged.connect(self._slice_changed)
        row.addWidget(self.slice_slider, 1)
        self.slice_label = QLabel()
        row.addWidget(self.slice_label)
        slices.addLayout(row)
        self.image = _SliceImage()
        self.image.sample_selected.connect(self._sample_selected)
        slices.addWidget(self.image, 1)
        self.sample_label = QLabel(self.tr("Move over the slice to inspect exact decoded samples."))
        self.sample_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        slices.addWidget(self.sample_label)
        legend = QHBoxLayout()
        legend.setSpacing(8)
        ramp = QLabel()
        ramp.setPixmap(_legend_ramp())
        legend.addWidget(ramp)
        legend_text = QLabel(self.tr("Blue: inside · Orange: outside · White: zero · Distances in mesh-local units"))
        set_style_role(legend_text, tone="muted")
        legend.addWidget(legend_text, 1)
        slices.addLayout(legend)
        self.tabs.addTab(slice_tab, self.tr("Distance slices"))
        surface_tab = QWidget()
        self.surface_layout = QVBoxLayout(surface_tab)
        row = QHBoxLayout()
        self.show_mesh = QCheckBox(self.tr("Show source mesh"))
        self.show_mesh.setChecked(False)
        self.show_mesh.setEnabled(bool(self.scene))
        self.show_mesh.toggled.connect(self._show_surface)
        row.addWidget(self.show_mesh)
        row.addWidget(QLabel(self.tr("Preview stride:")))
        self.stride = QSpinBox()
        self.stride.setRange(1, 16)
        self.stride.setToolTip(self.tr("1 uses every sample. Larger values reduce preview cost, not the saved volume."))
        self.stride.valueChanged.connect(self._invalidate_surface)
        row.addWidget(self.stride)
        self.surface_button = QPushButton(self.tr("Build zero surface"))
        self.surface_button.setIcon(line_icon("cube"))
        self.surface_button.clicked.connect(self._build_surface)
        row.addWidget(self.surface_button)
        row.addStretch()
        self.surface_layout.addLayout(row)
        self.surface_status = QLabel(self.tr("This previews the distance field, not engine distance-field shadows/GI."))
        self.surface_status.setWordWrap(True)
        set_style_role(self.surface_status, tone="muted")
        self.surface_layout.addWidget(self.surface_status)
        self.tabs.addTab(surface_tab, self.tr("3D zero surface"))
        self.set_document(document)

    def set_document(self, document):
        self.document = document
        self.quality.blockSignals(True)
        self.quality.clear()
        if document:
            self.quality.addItems([str(i) for i in range(len(document.levels))])
        self.quality.blockSignals(False)
        self._level_changed()

    def _level(self):
        index = self.quality.currentIndex()
        return self.document.levels[index] if self.document and index >= 0 else None

    def _level_changed(self, *_):
        level = self._level()
        self.values = level.distances() if level else None
        self.tabs.setEnabled(level is not None)
        self._invalidate_surface()
        if level:
            self.info.setText(self.tr(
                "Mesh XYZ: {} · Texture XYZ: {} · {} · Rotation {}\n"
                "Voxel: {:.7g} · Decoded range: {:.7g} to {:.7g} · Grid center: {}"
            ).format(level.logical_dimensions, level.dimensions, level.format.name,
                     ('XYZ', 'XZY', 'YZX')[level.rotation], level.voxel_size,
                     float(self.values.min()), float(self.values.max()),
                     '(' + ', '.join(f'{n:.7g}' for n in self.center) + ')'))
        else:
            self.info.setText(self.tr("No distance-field levels. This is a native no-op resource."))
            self.image.clear()
        self._axis_changed()

    def _invalidate_surface(self, *_):
        self._surface = None
        if self._gl:
            self._gl.set_scene([])
        self.surface_status.setText(self.tr("Build the zero surface for this quality level and preview stride."))

    def _axis_changed(self, *_):
        if self.values is None:
            return
        length = self.values.shape[2-self.axis.currentIndex()]
        self.slice_slider.setRange(0, length-1)
        self.slice_slider.setValue(length//2)
        self._slice_changed()

    def _slice(self):
        return np.take(self.values, self.slice_slider.value(), axis=2-self.axis.currentIndex())

    def _slice_changed(self, *_):
        if self.values is None:
            return
        samples = self._slice()
        scale = max(abs(float(self.values.min())), abs(float(self.values.max())), 1e-20)
        rgb = _signed_rgb(samples, scale)
        image = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888).copy()
        self.image.set_image(image)
        self.slice_label.setText(f'{self.slice_slider.value()+1}/{self.slice_slider.maximum()+1}')

    def _sample_selected(self, x, y):
        if self.values is None:
            return
        index = [y, x]
        index.insert(2-self.axis.currentIndex(), self.slice_slider.value())
        z, yy, xx = index
        grid = Grid.for_level(self._level(), self.center)
        position = grid.first_sample+np.array((xx, yy, z))*grid.voxel_size
        self.sample_label.setText(self.tr('Texel XYZ ({x}, {y}, {z}) · Position {position} · Distance {distance}').format(
            x=xx, y=yy, z=z, position=tuple(round(float(n), 6) for n in position),
            distance=f'{float(self.values[z, yy, xx]):.9g}'))

    def _build_surface(self):
        level = self._level()
        if level is None:
            return
        stride = self.stride.value()
        try:
            self._surface = run_job(self, self.tr("Extract distance-field surface"),
                lambda progress, cancel: surface_mesh(level, self.center, stride=stride, cancelled=cancel),
                cancel_error=BakeCancelled)
            self._show_surface(reset_camera=True)
        except BakeCancelled:
            pass
        except Exception as error:
            QMessageBox.warning(self, self.tr("Distance field"), str(error))

    def _show_surface(self, *_args, reset_camera=False):
        if self._surface is None:
            return
        vertices, indices = self._surface
        if self._gl is None:
            self._gl = ScenePreviewWidget(controls='mesh', initial_distance=2.5)
            self._device = plain_preview(self._gl, self)
            self._gl.bone_labels_check.hide()
            self.surface_layout.addWidget(self._gl, 1)
            reset_camera = True
        scene = list(self.scene) if self.show_mesh.isChecked() else []
        if len(indices):
            scene.append(SceneDrawMesh('sdftex', vertices, indices, (.1, .85, .72, 1), force_solid=True))
        self._gl.set_scene(scene, reset_camera=reset_camera)
        self.surface_status.setText(self.tr('{:,} triangles · Preview stride {} · Not a shadow/GI preview').format(
            len(indices), self.stride.value()) if len(indices) else
            self.tr('No zero crossing at this preview stride. Inspect slices or reduce the stride.'))

    def closeEvent(self, event):
        if self._gl:
            self._device.set_active(False)
            self._gl.close()
        super().closeEvent(event)


class MeshDistanceFieldDialog(QDialog):
    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.binding = handler.sdftex
        self.setWindowTitle(self.tr('Mesh distance field'))
        self.resize(950, 850)
        layout = QVBoxLayout(self)
        self.path_label = QLabel()
        self.path_label.setWordWrap(True)
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        set_style_role(self.path_label, tone="muted")
        layout.addWidget(self.path_label)
        resource_context_for_handler(handler)
        self.bounds = mesh_bounds(handler.mesh)
        self.center = tuple((self.bounds[0]+self.bounds[1])/2)
        geometry_error = ''
        try:
            self.scene = build_mesh_scene(handler.mesh, normal_recalculation_enabled=False,
                                          force_solid=True, color=(.55, .55, .55, 1))
        except ValueError as error:
            self.scene = []
            geometry_error = str(error)
        self.complete_geometry = bool(self.scene) and not (
            handler.mesh.streaming_buffer_count and not handler.mesh.streaming_data_loaded)
        if not self.complete_geometry:
            geometry_error = geometry_error or self.tr('Complete LOD 0 geometry, including its streaming companion, is required to bake.')
        if self.scene:
            self.bounds = geometry_bounds_at_native_center(self.bounds,
                np.concatenate([mesh.vertices for mesh in self.scene]))
        document = None
        self._load_error = ''
        try:
            document = run_job(self, self.tr('Load SDFTEX'), lambda progress, cancel: self.binding.load(),
                               cancel_error=BakeCancelled)
        except BakeCancelled:
            pass
        except (OSError, ValueError) as error:
            self._load_error = str(error)
        self.viewer = DistanceFieldViewer(document, center=self.center, scene=self.scene)
        layout.addWidget(self.viewer, 1)
        self.reference = QLineEdit()
        try:
            self.reference.setText(self.binding.proposed_reference())
        except ValueError:
            self.reference.setPlaceholderText(self.tr('Relative resource path ending in .sdftex'))
        self.reference.setToolTip(self.tr('The unversioned path stored in MESH. Baking a shared path affects other meshes using it.'))
        form = QFormLayout()
        form.addRow(self.tr('Bake output reference:'), self.reference)
        row = QHBoxLayout()
        self.keep_grids = QCheckBox(self.tr('Rebake all existing quality grids'))
        self.keep_grids.setChecked(bool(document and document.levels))
        self.keep_grids.setEnabled(bool(document and document.levels))
        self.keep_grids.setToolTip(self.tr('Off: replace the resource with one new grid at the requested resolution.'))
        row.addWidget(self.keep_grids)
        self.resolution = QSpinBox()
        self.resolution.setRange(8, 256)
        self.resolution.setValue(64)
        self.resolution.setPrefix(self.tr('Longest mesh axis: '))
        self.resolution.setSuffix(self.tr(' voxels'))
        row.addWidget(self.resolution)
        self.storage = QComboBox()
        for fmt in supported_formats(self.binding.version):
            self.storage.addItem(fmt.name, fmt)
        default = self.storage.findData(StorageFormat.BC4_UNORM)
        if default >= 0:
            self.storage.setCurrentIndex(default)
        row.addWidget(self.storage)
        form.addRow(row)
        self.keep_grids.toggled.connect(self._grid_mode)
        self._grid_mode()
        row = QHBoxLayout()
        self.sign_mode = QComboBox()
        self.sign_mode.addItems([self.tr('Closed solid'), self.tr('Surface shell')])
        row.addWidget(self.sign_mode)
        self.thickness = QDoubleSpinBox()
        self.thickness.setDecimals(6)
        self.thickness.setRange(.000001, 100000)
        self.thickness.setValue(max(.000001, float(max(self.bounds[1]-self.bounds[0]))/64))
        self.thickness.setPrefix(self.tr('Shell thickness: '))
        self.thickness.setSuffix(self.tr(' local units'))
        self.thickness.setEnabled(False)
        self.sign_mode.currentIndexChanged.connect(self._shell_mode)
        row.addWidget(self.thickness)
        self.bake_button = QPushButton(self.tr('Bake from LOD 0…'))
        set_style_role(self.bake_button, variant="primary")
        self.bake_button.setEnabled(handler.supports_editing() and self.complete_geometry)
        self.bake_button.setToolTip(geometry_error)
        self.bake_button.clicked.connect(self._bake)
        row.addWidget(self.bake_button)
        form.addRow(row)
        layout.addLayout(form)
        note = QLabel(self.tr('Bake uses undeformed LOD 0 geometry. It is staged in memory; save the mesh to write '
                             'MESH + streaming + SDFTEX together. No files are changed by previewing.'))
        note.setWordWrap(True)
        set_style_role(note, tone="muted")
        layout.addWidget(note)
        self.result_label = QLabel()
        self.result_label.setWordWrap(True)
        self.result_label.setText(geometry_error)
        set_style_role(self.result_label, tone="warning")
        layout.addWidget(self.result_label)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self._update_path()

    def done(self, result):
        self.viewer.close()
        super().done(result)

    def _update_path(self):
        where = self.binding.resolved_path or (
            self.tr('Baked in memory — save the mesh to write this companion') if self.binding.authored
            else self._load_error)
        self.path_label.setText(f'SDFTEX: {self.binding.reference or self.tr("None")}\n{where}')

    def _grid_mode(self, *_):
        self.resolution.setEnabled(not self.keep_grids.isChecked())
        self.storage.setEnabled(not self.keep_grids.isChecked())

    def _shell_mode(self, *_):
        self.thickness.setEnabled(self.sign_mode.currentIndex() == 1)

    def _bake(self):
        try:
            if not self.complete_geometry:
                raise ValueError('Complete LOD 0 geometry is required to bake')
            reference = reference_path(self.reference.text())
            doc = self.viewer.document
            thickness = self.thickness.value() if self.sign_mode.currentIndex() else 0.0
            if self.keep_grids.isChecked() and doc and doc.levels:
                specs = [(Grid.for_level(level, self.center), level.format, level.rotation) for level in doc.levels]
            else:
                fmt = self.storage.currentData()
                voxel = float(max(self.bounds[1]-self.bounds[0]))/self.resolution.value()
                bounds = self.bounds[0]-thickness/2, self.bounds[1]+thickness/2
                specs = [(covering_grid(bounds, voxel, fmt), fmt, 0)]
            vertices, faces, offset = [], [], 0
            for mesh in self.scene:
                vertices.append(mesh.vertices)
                faces.append(np.asarray(mesh.indices).reshape(-1,3)+offset)
                offset += len(mesh.vertices)
            vertices, faces = np.concatenate(vertices), np.concatenate(faces)
            result, errors = run_job(self, self.tr('Bake distance field'), lambda progress, cancel:
                bake_levels(vertices, faces, specs, center=self.center, resource_version=self.binding.version,
                            shadow_offset_scale=doc.shadow_offset_scale if doc else 1.0,
                            shell_thickness=thickness, progress=progress, cancelled=cancel),
                describe=lambda current, total: self.tr("{:,} / {:,} samples").format(current, total),
                cancel_error=BakeCancelled)
            self.binding.stage(result, reference)
            self.viewer.set_document(result)
            self.keep_grids.setEnabled(True)
            self.keep_grids.setChecked(True)
            self._update_path()
            boundary_inside = any(any(np.any(face < 0) for face in
                (v[0], v[-1], v[:,0], v[:,-1], v[:,:,0], v[:,:,-1])) for v in
                (level.distances() for level in result.levels))
            self.result_label.setText(self.tr('Bake staged. Max encoding error per level: {} local units.{}').format(
                ', '.join(f'{error:.6g}' for error in errors),
                self.tr(' Warning: interior samples reach the grid edge; use a larger grid.') if boundary_inside else ''))
            set_style_role(self.result_label, tone="warning" if boundary_inside else "success")
        except BakeCancelled:
            self.result_label.setText(self.tr('Bake cancelled. The previous volume was kept.'))
            set_style_role(self.result_label, tone="muted")
        except Exception as error:
            QMessageBox.warning(self, self.tr('Could not bake distance field'), str(error))
