import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QRunnable, QSignalBlocker, Qt, QThreadPool, Signal, Slot
lazy from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QScrollArea, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit,
    QToolButton, QVBoxLayout, QWidget,
)

lazy from services.file_io import write_files_atomically
lazy from ui.background_jobs import BackgroundJobs
lazy from ui.editor_widgets import DisclosureButton, styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role
from .conversion import conversion_targets
lazy from .conversion_geometry import retains_meshlets
lazy from .conversion_preview import MeshConversionPreview, part_color
lazy from .conversion_skin import (
    adapt_source_skinning, skin_weight_capacity, source_has_material_skinning, source_skinning_contract,
    source_weight_options, target_weight_options,
)
from .mesh_file import MeshFile, MPLY_MAGIC
from .profiles import BlendShapeLayout, MaterialSkinningLayout
lazy from .preview_controls import make_searchable
lazy from .blender import export_blend
lazy from .blender_ui import require_blender


BLENDER_TARGET = ("blender", None)


@dataclass
class _BlenderExport:
    path: Path
    objects: int
    changes: tuple = ()
    losses: tuple = ()
    parts: tuple = (None,)


class _ConversionSignals(QObject):
    stage = Signal(str)
    finished = Signal(object, object, str)


class _ConversionTask(QRunnable):
    def __init__(self, handler, profile, options):
        super().__init__()
        self.handler, self.profile, self.options = handler, profile, options
        self.signals = _ConversionSignals()
        self.source_contract = None

    def run(self):
        try:
            self.signals.stage.emit("Resolving source skinning…")
            if "source_skinning_contract" not in self.options:
                self.options["source_skinning_contract"] = self.handler.conversion_source_skinning_contract(
                    explicit_mdf_path=self.options.pop("source_mdf_path", ""))
            self.source_contract = self.options["source_skinning_contract"]
            self.signals.stage.emit("Converting and validating…")
            result = self.handler.convert_to(self.profile, **self.options)
            self.signals.stage.emit("Preparing comparison…")
            mesh = []
            for part in result.parts:
                decoded = MeshFile()
                decoded.read(part.output.main, file_version=result.file_version,
                             streaming_data=part.output.streaming)
                adapt_source_skinning(decoded, result.target_skinning_contract)
                mesh.append(decoded)
        except Exception as exc:
            self.signals.finished.emit(None, None, str(exc))
        else:
            self.signals.finished.emit(result, mesh, "")


def _source_mode_label(count, late):
    translate = QCoreApplication.translate
    if count == 1:
        return translate("MeshConversionDialog", "Rigid · one indexed bone")
    if count == 4:
        return (translate("MeshConversionDialog", "3 influences · 4-weight shader") if late
                else translate("MeshConversionDialog", "4 weights · normalized"))
    return translate("MeshConversionDialog", "{count} weights · implicit final weight").format(count=count)


class _MaterialSkinningDialog(QDialog):
    def __init__(self, mesh, counts, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Source skinning by material"))
        self.resize(640, 420)
        layout = QVBoxLayout(self)
        hint = QLabel(self.tr("Choose how the source shader reads each material's weights. "
                              "These choices apply to all LODs and replace MDF lookup."))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        names = list(dict.fromkeys(mesh.material_names))
        table = QTableWidget(len(names), 2)
        table.setHorizontalHeaderLabels([self.tr("Source material"), self.tr("Skinning mode")])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.verticalHeader().hide()
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        layout.addWidget(table)
        self.choices = {}
        late = mesh.profile.material_skinning_layout is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT
        for row, name in enumerate(names):
            table.setItem(row, 0, QTableWidgetItem(name))
            combo = QComboBox()
            combo.addItem(self.tr("Choose a mode…"), None)
            for count in source_weight_options(mesh):
                combo.addItem(_source_mode_label(count, late), count)
            combo.setCurrentIndex(max(0, combo.findData(counts.get(name))))
            combo.currentIndexChanged.connect(self._update_accept)
            table.setCellWidget(row, 1, combo)
            self.choices[name] = combo
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.accept_button = buttons.button(QDialogButtonBox.Ok)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_accept()

    def _update_accept(self):
        self.accept_button.setEnabled(bool(self.choices) and all(
            combo.currentData() is not None for combo in self.choices.values()))

    def material_counts(self):
        return {name: combo.currentData() for name, combo in self.choices.items()}


def _mesh_metrics(mesh):
    buffer = mesh.mesh_buffer
    if mesh.header.magic == MPLY_MAGIC:
        vertices = sum(c.vert_count for c in buffer.mply_chunks)
        triangles = sum(len(c.faces) // 3 for c in buffer.mply_chunks)
        lods = len({c.lod_index for c in buffer.mply_chunks})
    else:
        vertices = sum(p.vertex_count for p in buffer.buffer_payloads.values())
        lods = max((len(data.lods) for data in mesh.meshes), default=0)
        draws = [s for data in mesh.meshes for lod in data.lods
                 for group in lod.mesh_groups for s in group.submeshes]
        triangles = sum(s.indices_count // 4 * 2 if s.is_quad else s.indices_count // 3 for s in draws)
    blend = mesh.blend_shape_data
    channels = {c.name_slot for body in blend.bodies for c in body.channels} if blend else set()
    return ("MPLY" if mesh.header.magic == MPLY_MAGIC else "MESH", vertices, triangles,
            lods, len(channels), len(mesh.bones), mesh.streaming_buffer_count)


class MeshConversionDialog(QDialog):
    def __init__(self, handler, parent=None):
        super().__init__(parent)
        self.handler = handler
        self.result = None
        self.source_material_counts = {}
        self._task = None
        self._blender_jobs = BackgroundJobs(self)
        self._blending = False
        self._blend_directory = None
        self._closed = False
        self.finished.connect(self._closed_dialog)
        self.setWindowTitle(self.tr("Cross-game mesh converter"))
        self.resize(1240, 820)
        self.setMinimumSize(960, 680)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        header = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title = styled_label(self.tr("Cross-game mesh converter"), heading="title")
        title.setObjectName("conversionTitle")
        titles.addWidget(title)
        subtitle = styled_label(self.tr("Choose a build, explore the result, then export.") + "  ·  " +
                                Path(handler.filepath or "Untitled mesh").name, tone="muted")
        subtitle.setTextFormat(Qt.PlainText)
        titles.addWidget(subtitle)
        header.addLayout(titles, 1)
        self.status = QLabel()
        self.status.setObjectName("conversionStatus")
        self._set_status(self.tr("Ready to compare"))
        header.addWidget(self.status, 0, Qt.AlignTop)
        layout.addLayout(header)
        split = QSplitter(Qt.Horizontal)
        layout.addWidget(split, 1)
        split.addWidget(self._build_options())
        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        self.preview = MeshConversionPreview(handler.mesh, self)
        self.finished.connect(self.preview.cleanup)
        review_split = QSplitter(Qt.Vertical, review)
        review_split.setChildrenCollapsible(False)
        review_split.addWidget(self.preview)
        review_layout.addWidget(review_split)
        self.details = QTabWidget()
        self.details.setMinimumHeight(140)
        self.summary = QTableWidget(7, 3)
        self.summary.setHorizontalHeaderLabels([self.tr("All main LODs"), self.tr("Source"), self.tr("Converted")])
        self.summary.verticalHeader().hide()
        self.summary.verticalHeader().setDefaultSectionSize(23)
        self.summary.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.summary.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.summary.setSelectionMode(QAbstractItemView.NoSelection)
        self.summary.setShowGrid(False)
        for row, label in enumerate((self.tr("Geometry"), self.tr("Vertices"), self.tr("Triangles"),
                                    self.tr("LODs"), self.tr("Morph channels"), self.tr("Bones"),
                                    self.tr("Streaming buffers"))):
            self.summary.setItem(row, 0, QTableWidgetItem(label))
        self._update_summary()
        self.details.addTab(self.summary, self.tr("Before / after"))
        self.report = QTextEdit()
        self.report.setReadOnly(True)
        self.report.setPlainText(self.tr("Choose a target and preview conversion to review compatibility."))
        self.details.addTab(self.report, self.tr("Conversion report"))
        self.files = QTableWidget(0, 3)
        self.files.setHorizontalHeaderLabels([self.tr("Output file"), self.tr("Bone palette"), self.tr("Size")])
        self.files.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.files.verticalHeader().hide()
        self.files.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.files.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.files.cellClicked.connect(self._show_file_part)
        self.details.addTab(self.files, self.tr("Export files"))
        review_split.addWidget(self.details)
        review_split.setStretchFactor(0, 1)
        review_split.setSizes([470, 190])
        split.addWidget(review)
        split.setStretchFactor(1, 1)
        split.setSizes([310, 890])
        footer = QHBoxLayout()
        self.allow_loss = QCheckBox(self.tr("Allow the reported data loss"))
        self.allow_loss.setEnabled(False)
        self.allow_loss.toggled.connect(self._update_save)
        footer.addWidget(self.allow_loss, 1)
        self.copy_report_button = QPushButton(self.tr("Copy report"))
        self.copy_report_button.setAutoDefault(False)
        self.copy_report_button.setToolTip(self.tr("Copy the conversion report for notes or troubleshooting."))
        self.copy_report_button.clicked.connect(self._copy_report)
        footer.addWidget(self.copy_report_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        self.save_button = buttons.button(QDialogButtonBox.Save)
        self.save_button.setText(self.tr("Export…"))
        self.save_button.setEnabled(False)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        footer.addWidget(buttons)
        layout.addLayout(footer)
        for combo in (self.target_combo, self.kind_combo, self.palette_combo, self.source_skin_combo, self.target_skin_combo, *self.axis_combos):
            combo.currentIndexChanged.connect(self._invalidate)
        self.source_mdf.textChanged.connect(self._invalidate)
        self._update_target_info()

    def _set_status(self, text: str, tone: str = "neutral") -> None:
        self.status.setText(text)
        set_style_role(self.status, badge=tone)

    def _show_file_part(self, row):
        if self.result and row < len(self.result.parts):
            self.preview.part_combo.setCurrentIndex(row + 1)

    def _copy_report(self):
        QApplication.clipboard().setText(self.report.toPlainText())

    def _build_options(self):
        options = QFrame()
        options.setObjectName("conversionOptions")
        set_style_role(options, surface="card")
        options.setMinimumWidth(270)
        options.setMaximumWidth(360)
        layout = QVBoxLayout(options)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        self.target_combo = QComboBox()
        mesh = self.handler.mesh
        self.target_combo.addItem(self.tr("Blender scene · .blend (lossless edit round trip)"), BLENDER_TARGET)
        self.target_combo.setItemData(0, self.tr(
            "Edit in Blender, then use Import .blend… on the mesh toolbar. Untouched data stays bit-exact."),
            Qt.ToolTipRole)
        for profile, pair in conversion_targets():
            self.target_combo.addItem(f"{profile.display_name} · {pair[1]}", (profile, pair))
            self.target_combo.setItemData(self.target_combo.count() - 1,
                f".mesh.{pair[1]} · internal version {pair[0]}", Qt.ToolTipRole)
            if profile is mesh.profile and pair[0] == mesh.header.version:
                self.target_combo.setCurrentIndex(self.target_combo.count() - 1)
        make_searchable(self.target_combo, self.tr("Search games or versions…"))
        form.addRow(self.tr("Target game / build"), self.target_combo)
        self.options_form = form
        self.kind_combo = QComboBox()
        self.kind_combo.addItem(self.tr("Automatic · best supported geometry"), "auto")
        self.kind_combo.addItem(self.tr("Ordinary MESH · expand meshlets"), "mesh")
        form.addRow(self.tr("Geometry"), self.kind_combo)
        self.palette_combo = QComboBox()
        self.palette_combo.addItem(self.tr("Preserve weights · split when needed"), "split")
        self.palette_combo.addItem(self.tr("Single file · stop at target limits"), "single")
        self.palette_combo.setToolTip(self.tr("Split parts preserve geometry within the target's bone and draw limits. "
                                             "They keep the full skeleton and must be used together."))
        form.addRow(self.tr("Target limits"), self.palette_combo)
        self.target_skin_combo = QComboBox()
        self.target_skin_combo.setToolTip(self.tr(
            "Automatic preserves as many influences as the target allows. A lower mode keeps the strongest "
            "influences and reports any reduction. Target materials must use the shader listed in the report."))
        self.target_skin_label = QLabel(self.tr("Target weights"))
        form.addRow(self.target_skin_label, self.target_skin_combo)
        layout.addLayout(form)
        self.merge_seams = QCheckBox(self.tr("Weld UV seams for editing"))
        self.merge_seams.setChecked(True)
        self.merge_seams.setToolTip(self.tr(
            "Vertices split only by UVs or normals become one Blender vertex with per-corner data, so edits "
            "keep seams closed. Both choices import back bit-exactly."))
        self.merge_seams.toggled.connect(self._invalidate)
        self.merge_seams.hide()
        layout.addWidget(self.merge_seams)
        self.target_info = QLabel()
        self.target_info.setWordWrap(True)
        layout.addWidget(self.target_info)
        self.source_skin_combo = QComboBox()
        self.source_skin_combo.addItem(self.tr("Automatic · mesh first"), None)
        self.source_mdf = QLineEdit(options)
        self.source_mdf.setClearButtonEnabled(True)
        self.source_mdf.setPlaceholderText(self.tr("Optional source MDF"))
        self.source_mdf.setToolTip(self.tr(
            "Optional MDF file or game resource path, including a custom MDF. An explicit choice is always "
            "used; leave blank to consult materials only when the mesh is ambiguous."))
        self.source_mdf_button = QToolButton(options)
        self.source_mdf_button.setIcon(line_icon("folder_open"))
        self.source_mdf_button.setToolTip(self.tr("Choose source MDF"))
        self.source_mdf_button.clicked.connect(self._choose_source_mdf)
        self.source_mdf_panel = QWidget(options)
        mdf_row = QHBoxLayout(self.source_mdf_panel)
        mdf_row.setContentsMargins(0, 0, 0, 0)
        mdf_row.addWidget(self.source_mdf)
        mdf_row.addWidget(self.source_mdf_button)
        self.source_material_button = QPushButton(self.tr("Choose material modes…"), options)
        self.source_material_button.clicked.connect(self._choose_source_material_modes)
        self.source_skin_help = QLabel(options)
        self.source_skin_help.setWordWrap(True)
        self.source_material_button.hide()
        has_weights = any(p.skin_weights is not None for p in mesh.mesh_buffer.buffer_payloads.values())
        if source_has_material_skinning(mesh.profile) and mesh.header.magic != MPLY_MAGIC and has_weights:
            late = mesh.profile.material_skinning_layout is MaterialSkinningLayout.LATE_DXIL_PACKED_10BIT
            try:
                modes = source_weight_options(mesh)
            except ValueError:
                modes = ()
            for count in modes:
                self.source_skin_combo.addItem(self.tr("Manual · {mode}").format(mode=_source_mode_label(count, late)), count)
            if modes:
                self.source_skin_combo.addItem(self.tr("Manual · per material…"), "materials")
            source_label = QLabel(self.tr("Source skinning"))
            self.source_skin_label = source_label
            self.source_skin_available = True
            layout.addWidget(source_label)
            layout.addWidget(self.source_skin_combo)
            layout.addWidget(self.source_mdf_panel)
            layout.addWidget(self.source_material_button)
            layout.addWidget(self.source_skin_help)
            self.source_skin_combo.setToolTip(self.tr(
                "Automatic uses verified mesh rules first. Manual modes bypass material lookup; "
                "choose the shader used to author the source, independently of the target weight limit."))
        else:
            self.source_skin_label = None
            self.source_skin_available = False
            self.source_skin_combo.hide()
            self.source_mdf_panel.hide()
            self.source_skin_help.hide()
        self._update_source_controls()
        axes_toggle = DisclosureButton(self.tr("Advanced · override coordinates"))
        self.axes_toggle = axes_toggle
        coordinate_info = QLabel(self.tr("Native game coordinates are handled automatically."))
        self.coordinate_info = coordinate_info
        coordinate_info.setWordWrap(True)
        layout.addWidget(coordinate_info)
        layout.addWidget(axes_toggle)
        axes_panel = QWidget()
        self.axes_panel = axes_panel
        axis_layout = QVBoxLayout(axes_panel)
        axis_layout.setContentsMargins(0, 0, 0, 0)
        hint = QLabel(self.tr("XYZ keeps the automatic native conversion. Override only for an asset authored "
                              "in a different basis; its intended orientation cannot be inferred reliably."))
        hint.setWordWrap(True)
        axis_layout.addWidget(hint)
        axis_row = QHBoxLayout()
        self.axis_combos = []
        for index, axis in enumerate("XYZ"):
            axis_row.addWidget(QLabel(axis))
            combo = QComboBox()
            for value, text in ((1, "X"), (2, "Y"), (3, "Z"), (-1, "−X"), (-2, "−Y"), (-3, "−Z")):
                combo.addItem(text, value)
            combo.setCurrentIndex(index)
            combo.setAccessibleName(self.tr("Output {} axis").format(axis))
            combo.setToolTip(self.tr("Map this output axis to a signed input axis."))
            self.axis_combos.append(combo)
            axis_row.addWidget(combo)
        axis_layout.addLayout(axis_row)
        layout.addWidget(axes_panel)
        axes_panel.hide()
        axes_toggle.toggled.connect(axes_panel.setVisible)
        self.check_button = QPushButton(self.tr("Preview conversion"))
        self.check_button.setObjectName("conversionCheck")
        set_style_role(self.check_button, variant="primary")
        self.check_button.setShortcut("Ctrl+Return")
        self.check_button.setToolTip(self.tr("Build and validate the comparison (Ctrl+Enter)."))
        self.check_button.clicked.connect(self._check)
        layout.addWidget(self.check_button)
        self.reset_button = QPushButton(self.tr("Reset options"))
        self.reset_button.setAutoDefault(False)
        self.reset_button.setToolTip(self.tr("Keep the target build; reset geometry, weights, source interpretation and axes."))
        self.reset_button.clicked.connect(self._reset_options)
        layout.addWidget(self.reset_button)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(4)
        self.progress.hide()
        layout.addWidget(self.progress)
        layout.addStretch(1)
        note = styled_label(self.tr("Your source stays intact. The preview shows decoded geometry; target materials, "
                                    "textures and animation retargeting are separate resources."), tone="subtle")
        note.setWordWrap(True)
        layout.addWidget(note)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setMinimumWidth(290)
        scroll.setMaximumWidth(380)
        scroll.setWidget(options)
        return scroll

    def _reset_options(self):
        controls = (self.kind_combo, self.palette_combo, self.source_skin_combo,
                    self.target_skin_combo, self.source_mdf, *self.axis_combos)
        blockers = [QSignalBlocker(control) for control in controls]
        for combo in controls[:4]:
            combo.setCurrentIndex(0)
        self.source_mdf.clear()
        self.source_material_counts.clear()
        for index, combo in enumerate(self.axis_combos):
            combo.setCurrentIndex(index)
        del blockers
        self._invalidate()

    def _choose_source_mdf(self):
        path, _filter = QFileDialog.getOpenFileName(
            self, self.tr("Choose source MDF"), str(Path(self.handler.filepath).parent), "MDF files (*.mdf2.*)")
        if path:
            self.source_skin_combo.setCurrentIndex(0)
            self.source_mdf.setText(path)

    def _choose_source_material_modes(self):
        dialog = _MaterialSkinningDialog(self.handler.mesh, self.source_material_counts, self)
        if dialog.exec() == QDialog.Accepted:
            self.source_material_counts = dialog.material_counts()
            self._invalidate()

    def _update_source_controls(self):
        selector = self.source_skin_combo.currentData()
        visible = not self.source_skin_combo.isHidden() and not self._is_blender()
        self.source_mdf_panel.setVisible(visible and selector is None)
        self.source_material_button.setVisible(visible and selector == "materials")
        if selector is None:
            message = self.tr("Uses the mesh first. Materials are needed only when shader choices change deformation.")
        elif selector == "materials":
            count = len(self.source_material_counts)
            message = self.tr("{} material modes selected. No MDF lookup; choices apply to all LODs.").format(count)
        else:
            message = self.tr("No MDF lookup. Applies this source shader to all materials and LODs.")
        self.source_skin_help.setText(message)

    def _is_blender(self):
        return self.target_combo.currentData() == BLENDER_TARGET

    def _show_conversion_options(self, visible):
        for field in (self.kind_combo, self.palette_combo, self.target_skin_combo):
            self.options_form.setRowVisible(field, visible)
        self.merge_seams.setVisible(not visible)
        self.axes_toggle.setVisible(visible)
        self.coordinate_info.setVisible(visible)
        if not visible:
            self.axes_panel.hide()
            self.axes_toggle.setChecked(False)

    def _update_target_info(self):
        blender = self._is_blender()
        self._show_conversion_options(not blender)
        if blender:
            self.source_skin_combo.hide()
            self.source_mdf_panel.hide()
            self.source_material_button.hide()
            self.source_skin_help.hide()
            self.target_info.setText(self.tr(
                "Creates a .blend with every LOD, shadow and occluder layer, the skeleton, weights, UV maps, "
                "colors, exact custom normals and shape keys.") + "\n\n" + self.tr(
                "The mesh is embedded in the scene, so data Blender cannot show (normal recalculation, "
                "meshlets, streaming, flags) is kept. Preview checks that the scene imports back bit-exactly."))
            return
        if self.source_skin_label is not None:
            self.source_skin_label.setVisible(self.source_skin_available)
            for widget in (self.source_skin_combo, self.source_skin_help):
                widget.setVisible(self.source_skin_available)
            self._update_source_controls()
        profile, _pair = self.target_combo.currentData()
        source = self.handler.mesh
        meshlets = retains_meshlets(source, profile, self.kind_combo.currentData())
        choices = target_weight_options(profile, meshlets=meshlets)
        selected = self.target_skin_combo.currentData()
        with QSignalBlocker(self.target_skin_combo):
            self.target_skin_combo.clear()
            self.target_skin_combo.addItem(self.tr("Automatic · preserve influences"), None)
            for count in choices:
                label = (self.tr("3 influences · 4-weight shader") if count == 3
                         else self.tr("{} influences").format(count))
                self.target_skin_combo.addItem(label, count)
            self.target_skin_combo.setCurrentIndex(max(0, self.target_skin_combo.findData(selected)))
        streaming = self.tr("Streaming LODs supported") if profile.supports_streaming_buffers else self.tr("All LODs become resident")
        morphs = self.tr("Morphs on LOD0") if profile.blend_shape_layout is BlendShapeLayout.LEGACY else self.tr("Morphs across LODs")
        self.target_info.setText(self.tr("Up to {} stored influences per vertex").format(
            skin_weight_capacity(profile, meshlets=meshlets)) + "\n" + streaming + "\n" + morphs +
            ("\n" + self.tr("Meshlets become ordinary MESH") if source.header.magic == MPLY_MAGIC and not meshlets else "") +
            "\n\n" + self.tr("The conversion report lists any required material shader."))

    def _update_summary(self, target=None):
        for column, mesh in ((1, self.handler.mesh), (2, target)):
            if isinstance(mesh, list):
                values = [_mesh_metrics(part) for part in mesh]
                metrics = (values[0][0] if len(mesh) == 1 else f"{len(mesh)} MESH files", sum(v[1] for v in values), sum(v[2] for v in values),
                           max(v[3] for v in values), max(v[4] for v in values), max(v[5] for v in values),
                           sum(v[6] for v in values))
            else:
                metrics = _mesh_metrics(mesh) if mesh is not None else ("—",) * self.summary.rowCount()
            for row, value in enumerate(metrics):
                self.summary.setItem(row, column, QTableWidgetItem(f"{value:,}" if isinstance(value, int) else value))

    def _invalidate(self):
        self.result = None
        self.files.setRowCount(0)
        self.allow_loss.setChecked(False)
        self.allow_loss.setEnabled(False)
        self.preview.clear_target(self.tr("Settings changed · preview again to compare"))
        self._update_summary()
        self._update_target_info()
        self._update_source_controls()
        self._set_status(self.tr("Preview needs updating"))
        self.report.setPlainText(self.tr("Settings changed; preview conversion again."))
        self._update_save()

    def _update_save(self):
        ready = self.result is not None and (not self.result.losses or self.allow_loss.isChecked())
        self.save_button.setEnabled(ready)
        count = len(self.result.parts) if self.result else 1
        self.save_button.setText(self.tr("Export {} parts…").format(count) if count > 1 else self.tr("Export…"))
        self.save_button.setToolTip(self.tr("Export the reviewed conversion.") if ready else
            self.tr("Preview conversion first.") if self.result is None else
            self.tr("Review the report and allow the reported data loss to export."))

    def _set_busy(self, busy):
        for control in (self.check_button, self.reset_button, self.target_combo, self.kind_combo, self.palette_combo, self.source_skin_combo, self.merge_seams,
                        self.source_mdf_panel, self.source_material_button, self.target_skin_combo, *self.axis_combos):
            control.setEnabled(not busy)
        self.preview.set_busy(busy)
        self.progress.setVisible(busy)

    def _check(self):
        if self._task is not None or self._blending or self._closed:
            return
        self._invalidate()
        if self._is_blender():
            self._check_blender()
            return
        profile, pair = self.target_combo.currentData()
        options = dict(version_pair=pair, allow_lossy=True,
                       axes=tuple(combo.currentData() for combo in self.axis_combos),
                       output_kind=self.kind_combo.currentData(),
                       palette_policy=self.palette_combo.currentData(),
                       target_influence_count=self.target_skin_combo.currentData())
        selector = self.source_skin_combo.currentData()
        if selector is None:
            options["source_mdf_path"] = self.source_mdf.text().strip()
        else:
            try:
                if selector == "materials" and set(self.handler.mesh.material_names) - self.source_material_counts.keys():
                    raise ValueError(self.tr("Choose a source skinning mode for every material using Choose material modes…"))
                options["source_skinning_contract"] = (
                    source_skinning_contract(self.handler.mesh, material_counts=self.source_material_counts)
                    if selector == "materials" else source_skinning_contract(self.handler.mesh, selector))
            except ValueError as exc:
                self._checked(None, None, str(exc))
                return
        self._task = _ConversionTask(self.handler, profile, options)
        self._task.signals.finished.connect(self._checked)
        self._task.signals.stage.connect(self._conversion_stage)
        self._set_busy(True)
        self._set_status(self.tr("Building your preview…"), "info")
        self.report.setPlainText(self.tr("Adapting geometry and validating the target format…"))
        QThreadPool.globalInstance().start(self._task)

    @Slot(str)
    def _conversion_stage(self, stage):
        if not self._closed and self._task is not None:
            self._set_status(self.tr(stage), "info")

    @Slot(object, object, str)
    def _checked(self, result, mesh, error):
        source_contract = self._task.source_contract if self._task is not None else None
        self._task = None
        if self._closed:
            return
        self.result = result
        self._set_busy(False)
        self._update_source_controls()
        if error:
            self._set_status(self.tr("Conversion needs attention"), "danger")
            self.preview.clear_target(self.tr("Conversion unavailable · see the report"))
            self.report.setPlainText(error)
            self.details.setCurrentWidget(self.report)
        else:
            self.preview.set_source_skinning(source_contract)
            self.preview.set_target(mesh, result.profile.display_name)
            paths = result.part_paths(result.suggested_path(self.handler.filepath or "converted.mesh"))
            self.files.setRowCount(len(paths) + int(len(paths) > 1))
            for row, (path, part) in enumerate(zip(paths, result.parts)):
                item = QTableWidgetItem(path.name)
                if len(paths) > 1:
                    item.setData(Qt.DecorationRole, QColor.fromRgbF(*part_color(row)))
                self.files.setItem(row, 0, item)
                self.files.setItem(row, 1, QTableWidgetItem(f"{len(part.palette)} / {result.profile.max_deform_bones}"))
                size = len(part.output.main) + len(part.output.streaming or b"")
                self.files.setItem(row, 2, QTableWidgetItem(f"{size / 1024:,.0f} KiB"))
            if len(paths) > 1:
                self.files.setItem(len(paths), 0, QTableWidgetItem(paths[0].name.rsplit(".mesh", 1)[0] + ".assembly.json"))
                self.files.setItem(len(paths), 1, QTableWidgetItem(self.tr("Assembly instructions")))
            self._update_summary(mesh)
            self.allow_loss.setEnabled(bool(result.losses))
            message = self.tr("Target format validation passed.")
            if self.source_skin_combo.currentData() is not None:
                message += "\n" + self.tr("Source skinning: manual selection; no material lookup.")
            elif source_contract is None:
                message += "\n" + self.tr("Source skinning: resolved from the mesh; no material lookup needed.")
            else:
                message += "\n" + self.tr("Source skinning: resolved using source materials.")
            if len(result.parts) > 1:
                message += "\n" + self.tr("{} mesh files + an assembly manifest. All parts keep the complete skeleton.").format(len(result.parts))
                message += "\n" + "\n".join(self.tr("Part {:02}: {} / {} palette entries").format(
                    i + 1, len(part.palette), result.profile.max_deform_bones) for i, part in enumerate(result.parts))
            if any(part.output.streaming is not None for part in result.parts):
                message += "\n" + self.tr("Includes a streaming companion; export below natives/<platform>.")
            if result.requirements:
                message += "\n\n" + self.tr("Target setup:") + "\n"
                message += "\n".join(f"• {item}" for item in result.requirements)
            if result.losses:
                self._set_status(self.tr("Review this change") if len(result.losses) == 1
                                 else self.tr("Review {} changes").format(len(result.losses)), "warning")
                message += "\n\n" + self.tr("Changes requiring approval:") + "\n"
                message += "\n".join(f"• {item}" for item in result.losses)
                self.details.setCurrentWidget(self.report)
            else:
                self._set_status(self.tr("Review target setup") if result.requirements else self.tr("Ready to export"),
                                 "warning" if result.requirements else "success")
                message += "\n" + self.tr("No representational data loss detected.")
                self.details.setCurrentWidget(self.report if result.requirements else self.summary)
            self.report.setPlainText(message)
        self._update_save()

    def _closed_dialog(self):
        self._closed = True
        self._blender_jobs.close()
        self.result = None
        if self._blend_directory is not None:
            self._blend_directory.cleanup()
            self._blend_directory = None

    def _check_blender(self):
        blender = require_blender(self, getattr(self.handler, "app", None))
        if blender is None:
            self.report.setPlainText(self.tr("Blender is required for .blend export."))
            return
        if self._blend_directory is None:
            self._blend_directory = tempfile.TemporaryDirectory(prefix="reasy_blend_preview_")
        title = Path(self.handler.filepath or "mesh").name.split(".")[0]
        target = Path(self._blend_directory.name) / f"{title}.blend"
        mesh, merge = self.handler.mesh, self.merge_seams.isChecked()
        version = self.handler._file_version_from_path(self.handler.filepath) if self.handler.filepath else None
        self._blending = True
        self._set_busy(True)
        self._set_status(self.tr("Building the Blender scene…"), "info")
        self.report.setPlainText(self.tr("Exporting to Blender and importing it back to verify the round trip…"))
        self._blender_jobs.start(
            lambda: export_blend(mesh, target, blender=blender, title=title, file_version=version or None,
                                 merge_seams=merge),
            lambda exported: self._blender_checked(exported, ""), lambda error: self._blender_checked(None, error))

    def _blender_checked(self, exported, error):
        self._blending = False
        if self._closed:
            return
        self._set_busy(False)
        self._update_source_controls()
        if error:
            self.result = None
            self._set_status(self.tr("Blender export needs attention"), "danger")
            self.preview.clear_target(self.tr("Blender export unavailable · see the report"))
            self.report.setPlainText(error)
            self.details.setCurrentWidget(self.report)
            self._update_save()
            return
        self.result = _BlenderExport(exported.path, exported.objects)
        self.preview.set_target([exported.round_trip.mesh], self.tr("Imported back from Blender"))
        self._update_summary([exported.round_trip.mesh])
        self.files.setRowCount(1)
        self.files.setItem(0, 0, QTableWidgetItem(exported.path.name))
        self.files.setItem(0, 1, QTableWidgetItem(self.tr("{} objects").format(exported.objects)))
        self.files.setItem(0, 2, QTableWidgetItem(f"{exported.path.stat().st_size / 1024:,.0f} KiB"))
        self._set_status(self.tr("Ready to export"), "success")
        self.report.setPlainText(self.tr(
            "Round trip verified: importing this scene unchanged reproduces the mesh bit for bit.") + "\n\n" +
            self.tr("Edit it in Blender, save, then use Import .blend… on the mesh toolbar. Edits are applied "
                    "on top of the embedded mesh; the import report lists every change and any data loss."))
        self.details.setCurrentWidget(self.report)
        self._update_save()

    def _save_blender(self):
        suggested = str(Path(self.handler.filepath or "mesh").with_name(self.result.path.name))
        filename, _filter = QFileDialog.getSaveFileName(self, self.tr("Export Blender scene"), suggested,
                                                        self.tr("Blender scenes (*.blend)"))
        if not filename:
            return
        try:
            shutil.copyfile(self.result.path, filename)
        except OSError as exc:
            QMessageBox.warning(self, self.tr("Blender export failed"), str(exc))
            return
        self._set_status(self.tr("Export complete"), "success")
        self.report.setPlainText(self.tr("Exported:\n") + filename)
        self.details.setCurrentWidget(self.report)

    def _save(self):
        if self.result is None or (self.result.losses and not self.allow_loss.isChecked()):
            return
        if isinstance(self.result, _BlenderExport):
            self._save_blender()
            return
        suggested = str(self.result.suggested_path(self.handler.filepath or "converted.mesh"))
        filename, _filter = QFileDialog.getSaveFileName(
            self, self.tr("Export converted mesh"), suggested,
            f"MESH files (*.mesh.{self.result.file_version})")
        if not filename:
            return
        try:
            outputs = self.handler.conversion_output_targets(self.result, filename)
            companions = [path for path in outputs if Path(path) != Path(filename) and Path(path).exists()]
            if companions and QMessageBox.question(
                    self, self.tr("Replace companion?"),
                    self.tr("Also replace these existing companion files?\n") + "\n".join(companions)
            ) != QMessageBox.Yes:
                return
            write_files_atomically(outputs)
        except (ValueError, OSError) as exc:
            QMessageBox.warning(self, self.tr("Mesh export failed"), str(exc))
            return
        self._set_status(self.tr("Export complete"), "success")
        self.report.setPlainText(self.tr("Exported:\n") + "\n".join(outputs))
        self.details.setCurrentWidget(self.report)
