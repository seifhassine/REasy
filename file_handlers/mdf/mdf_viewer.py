import os
from pathlib import Path

from PySide6.QtWidgets import (
	QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
	QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QComboBox,
	QGroupBox, QGridLayout, QCheckBox, QSpinBox, QSplitter, QTabWidget, QSizePolicy,
	QMessageBox, QStackedWidget, QFrame
)
lazy from PySide6.QtGui import QPixmap
from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, Signal
lazy from file_handlers.mesh.material_resolver import MeshMaterialResolver
lazy from file_handlers.mmtr.material_context import MaterialInstanceContext
lazy from file_handlers.mmtr.material_family import bind_drawn_master, parsed_master
lazy from file_handlers.mmtr.mmtr_viewer import ObjectScope, open_master_for_object
lazy from file_handlers.tex.qt_image_utils import decode_tex_bytes_to_qpixmap
lazy from file_handlers.tex.texture_quality import DEFAULT_TEXTURE_QUALITY, choose_texture_mip
lazy from utils.hash_util import murmur3_hash_utf16le
lazy from utils.number_format import format_display_value
lazy from utils.resource_file_utils import resource_context_for_handler, resource_path_key, resource_version_from_path
lazy from .mdf_clipboard import MdfClipboard
lazy from .mdf_file import (MATERIAL_FLAG_NAMES, GpbfHeader, MatData, ParamHeader, TexHeader,
    transparent_priority_bias)
lazy from .mdf_template_manager import MdfTemplateManager
lazy from ui.mdf_template_export_dialog import MdfTemplateExportDialog
lazy from ui.mdf_template_manager_dialog import MdfTemplateManagerDialog
lazy from ui.widgets_utils import get_color_preview_brush
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role
lazy from ui.editor_widgets import editor_strip, search_field, styled_label, tool_button
lazy from .material_graph import MdfMaterialGraph, is_color_parameter, layer_color_channel, pick_parameter_color, unit_value


COPY_MATERIALS_TITLE = QT_TRANSLATE_NOOP("MdfViewer", "Copy Materials")
EXPORT_TEMPLATE_TITLE = QT_TRANSLATE_NOOP("MdfViewer", "Export Template")


class MdfViewer(QWidget):
	modified_changed = Signal(bool)

	def __init__(self, handler):
		super().__init__()
		self.setObjectName("mdfViewer")
		self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
		self.handler = handler
		self._modified = False
		self._current_index = -1
		self._mmtr_document_cache = {}
		self._mmtr_document_errors = {}
		self._texture_preview_cache = {}
		self._setup_ui()
		self._populate()

	def _get_existing_material_names(self, exclude_index: int = -1) -> set:
		m = self.handler.mdf
		if not m:
			return set()
		names = set()
		for i, mat in enumerate(m.materials):
			if i != exclude_index:
				names.add(mat.header.mat_name or "")
		return names

	def _generate_unique_material_name(self, base_name: str, existing_names: set) -> str:
		if base_name not in existing_names:
			return base_name
		counter = 1
		while True:
			new_name = f"{base_name} ({counter})"
			if new_name not in existing_names:
				return new_name
			counter += 1

	def _check_duplicate_material_name(self, name: str, exclude_index: int = -1) -> bool:
		existing_names = self._get_existing_material_names(exclude_index)
		return name in existing_names

	@property
	def modified(self):
		return self._modified

	@modified.setter
	def modified(self, value: bool):
		if self._modified != value:
			self._modified = value
			self.modified_changed.emit(value)

	def _setup_ui(self):
		layout = QVBoxLayout(self)
		layout.setContentsMargins(0, 0, 0, 0)
		layout.setSpacing(0)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

		header, head = editor_strip()
		version_label = styled_label("MDF Version:", tone="muted")
		head.addWidget(version_label)
		self.version_edit = QLineEdit()
		self.version_edit.setReadOnly(False)
		self.version_edit.setMaximumWidth(80)
		set_style_role(self.version_edit, mono=True)
		self.version_edit.textChanged.connect(self._on_version_changed)
		head.addWidget(self.version_edit)
		head.addSpacing(8)
		self.meshlet_check = QCheckBox(self.tr("Meshlet Material"))
		self.meshlet_check.setToolTip(
			self.tr("Sets bit 0 of the MDF header options field.")
		)
		self.meshlet_check.stateChanged.connect(self._on_meshlet_changed)
		head.addWidget(self.meshlet_check)
		head.addStretch(1)
		layout.addWidget(header)

		splitter = QSplitter()
		splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
		body = QVBoxLayout()
		body.setContentsMargins(6, 6, 6, 6)
		body.addWidget(splitter)
		layout.addLayout(body, 1)

		left_panel = QWidget()
		left_v = QVBoxLayout(left_panel)
		left_v.setContentsMargins(0, 0, 0, 0)
		left_v.setSpacing(0)
		toolbar_strip, toolbar = editor_strip("header", margins=(4, 3, 4, 3), spacing=2)
		self.filter_edit = search_field(QLineEdit())
		self.filter_edit.setPlaceholderText(self.tr("Filter materials..."))
		self.filter_edit.textChanged.connect(self._on_filter_changed)
		self.add_btn = tool_button("add", self.tr("Add a new material"), self._on_add_material)
		self.add_btn.setText(self.tr("Add"))
		self.del_btn = tool_button("delete", self.tr("Delete selected material(s)"), self._on_delete_material)
		self.del_btn.setText(self.tr("Delete"))
		self.copy_btn = tool_button("copy", self.tr("Copy selected material(s) to clipboard"), self._on_copy_materials)
		self.copy_btn.setText(self.tr("Copy"))
		self.paste_btn = tool_button("paste", self.tr("Paste material(s) from clipboard"), self._on_paste_materials)
		self.paste_btn.setText(self.tr("Paste"))
		self.export_btn = tool_button("export", self.tr("Export selected material as template"), self._on_export_material)
		self.export_btn.setText(self.tr("Export"))
		self.template_btn = tool_button("template", self.tr("Open the MDF template manager"), self._open_template_manager)
		self.template_btn.setText(self.tr("Templates"))
		for btn in (
			self.add_btn,
			self.del_btn,
			self.copy_btn,
			self.paste_btn,
			self.export_btn,
			self.template_btn,
		):
			toolbar.addWidget(btn)
		toolbar.addStretch(1)
		left_v.addWidget(toolbar_strip)
		filter_row = QHBoxLayout()
		filter_row.setContentsMargins(0, 4, 0, 4)
		filter_row.addWidget(self.filter_edit)
		left_v.addLayout(filter_row)
		self.materials_table = QTableWidget(0, 1)
		self.materials_table.setHorizontalHeaderLabels([self.tr("Materials")])
		self.materials_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.materials_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.materials_table.setSelectionBehavior(QAbstractItemView.SelectRows)
		self.materials_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
		self.materials_table.itemChanged.connect(self._on_material_changed)
		left_v.addWidget(self.materials_table)
		splitter.addWidget(left_panel)

		self.tabs = QTabWidget()
		splitter.addWidget(self.tabs)
		splitter.setStretchFactor(0, 1)
		splitter.setStretchFactor(1, 3)

		self.material_graph = MdfMaterialGraph()
		self.material_graph.material_changed.connect(self._on_graph_material_changed)
		self.material_graph_tab_idx = self.tabs.addTab(
			self.material_graph, self.tr("Material Graph")
		)

		overview = QWidget()
		ov = QGridLayout(overview)
		ov.setContentsMargins(8, 8, 8, 8)
		ov.setHorizontalSpacing(8)
		ov.setVerticalSpacing(6)
		ov.addWidget(styled_label("Material Name", tone="muted"), 0, 0)
		self.matname_edit = QLineEdit()
		self.matname_edit.textChanged.connect(self._on_matname_changed)
		ov.addWidget(self.matname_edit, 0, 1)
		ov.addWidget(styled_label("Name Hash", tone="muted"), 0, 2)
		self.matname_hash_label = QLabel("0x00000000")
		self.matname_hash_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
		set_style_role(self.matname_hash_label, mono=True)
		ov.addWidget(self.matname_hash_label, 0, 3)
		ov.addWidget(styled_label("mmtrPath", tone="muted"), 1, 0)
		self.mmtr_edit = QLineEdit()
		self.mmtr_edit.textChanged.connect(self._on_mmtr_changed)
		ov.addWidget(self.mmtr_edit, 1, 1, 1, 2)
		self.edit_shader_button = QPushButton(self.tr("Edit Shader…"))
		self.edit_shader_button.setIcon(line_icon("edit"))
		self.edit_shader_button.setToolTip(self.tr(
			"Open this material's master material in the material editor, for this object only or for every object "
			"using it."))
		self.edit_shader_button.clicked.connect(self._edit_shader)
		ov.addWidget(self.edit_shader_button, 1, 3)
		ov.addWidget(styled_label("ShaderType", tone="muted"), 2, 0)
		self.shader_combo = QComboBox()
		self._shader_names = [
			"Standard","Decal","DecalWithMetallic","DecalNRMR","Transparent","Distortion",
			"PrimitiveMesh","PrimitiveSolidMesh","Water","SpeedTree","GUI","GUIMesh",
			"GUIMeshTransparent","ExpensiveTransparent","Forward","RenderTarget","PostProcess",
			"PrimitiveMaterial","PrimitiveSolidMaterial","SpineMaterial","ReflectiveTransparent"
		]
		self.shader_combo.addItems(self._shader_names)
		self.shader_combo.currentIndexChanged.connect(self._on_shader_changed)
		ov.addWidget(self.shader_combo, 2, 1)
		self.bake_texture_label = styled_label("BakeTextureArraySize", tone="muted")
		ov.addWidget(self.bake_texture_label, 3, 0)
		self.bake_texture_spin = QSpinBox()
		self.bake_texture_spin.setRange(0, 2147483647)
		self.bake_texture_spin.valueChanged.connect(self._on_bake_texture_changed)
		ov.addWidget(self.bake_texture_spin, 3, 1)
		self.ukn_label = styled_label("Unknown 64", tone="muted")
		ov.addWidget(self.ukn_label, 3, 2)
		self.ukn_edit = QLineEdit()
		self.ukn_edit.setPlaceholderText("0 (or 0x...)" )
		self.ukn_edit.textChanged.connect(self._on_ukn_changed)
		ov.addWidget(self.ukn_edit, 3, 3)
		self.flags_group = QGroupBox("Material Flags")
		fg = QGridLayout(self.flags_group)
		fg.setHorizontalSpacing(12)
		fg.setVerticalSpacing(6)
		self._flags1_names = list(MATERIAL_FLAG_NAMES)
		self.flags1_checks = [QCheckBox(n) for n in self._flags1_names]
		for i, cb in enumerate(self.flags1_checks):
			cb.stateChanged.connect(self._on_flags_changed)
			fg.addWidget(cb, 0 + (i // 5), i % 5)
		self.transparent_zpostpass_check = QCheckBox("TransparentZPostPassEnable")
		self.transparent_zpostpass_check.stateChanged.connect(self._on_flags_changed)
		fg.addWidget(self.transparent_zpostpass_check, 2, 0, 1, 2)
		fg.addWidget(styled_label("Tessellation", tone="muted"), 3, 0)
		self.tess_spin = QSpinBox()
		self.tess_spin.setRange(0, 63)
		self.tess_spin.valueChanged.connect(self._on_flags_changed)
		fg.addWidget(self.tess_spin, 3, 1)
		fg.addWidget(styled_label("Phong", tone="muted"), 3, 2)
		self.phong_spin = QSpinBox()
		self.phong_spin.setRange(0, 255)
		self.phong_spin.valueChanged.connect(self._on_flags_changed)
		fg.addWidget(self.phong_spin, 3, 3)
		self._flags2_names = [
			"RoughTransparentEnable","ForcedAlphaTestEnable","AlphaTestEnable","SSSProfileUsed",
			"EnableStencilPriority","RequireDualQuaternion","PixelDepthOffsetUsed","NoRayTracing"
		]
		self.flags2_checks = [QCheckBox(n) for n in self._flags2_names]
		for i, cb in enumerate(self.flags2_checks):
			cb.stateChanged.connect(self._on_flags_changed)
			fg.addWidget(cb, 5 + (i // 4), i % 4)
		fg.addWidget(self._separator(), 4, 0, 1, 5)
		self._flags3_separator = self._separator()
		fg.addWidget(self._flags3_separator, 7, 0, 1, 5)
		
		self._flags3_names = [
			"TransparentDistortionEnable","AlphaUsed","BakeTextureUseSecondaryUV","ForwardPrepassEnabled",
			"ForcedAlphaTestEnableShadow","TessellationZPrepassDisable","DitheredLodTransitionEnable","reserved0"
		]
		self.flags3_checks = [QCheckBox(n) for n in self._flags3_names]
		for i, cb in enumerate(self.flags3_checks):
			cb.stateChanged.connect(self._on_flags_changed)
			fg.addWidget(cb, 8 + (i // 4), i % 4)
		
		self.transparent_priority_bias_label = styled_label("TransparentPriorityBias", tone="muted")
		fg.addWidget(self.transparent_priority_bias_label, 10, 0)
		self.transparent_priority_bias_spin = QSpinBox()
		self.transparent_priority_bias_spin.setRange(-128, 127)
		self.transparent_priority_bias_spin.valueChanged.connect(self._on_flags_changed)
		fg.addWidget(self.transparent_priority_bias_spin, 10, 1)
		
		ov.addWidget(self.flags_group, 4, 0, 1, 4)
		ov.setRowStretch(5, 1)
		self.overview_tab_idx = self.tabs.addTab(overview, self.tr("Overview"))

		textures_tab = QWidget()
		tg = QVBoxLayout(textures_tab)
		tg.setContentsMargins(6, 6, 6, 6)
		tg.setSpacing(4)
		self.textures_table = QTableWidget(0, 3)
		self.textures_table.setHorizontalHeaderLabels([
			self.tr("Type"), self.tr("Path"), self.tr("Locked")
		])
		self.textures_table.horizontalHeaderItem(2).setIcon(line_icon("lock"))
		self._set_header_tooltip(self.textures_table, 2, self.tr(
			"Checked: the game can't change this texture at runtime; setTexture() is skipped and the file's value stays."))
		self.textures_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
		self.textures_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
		self.textures_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
		self.textures_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.textures_table.itemChanged.connect(self._on_texture_changed)
		self.tex_add_btn = QPushButton(self.tr("Add"))
		self.tex_del_btn = QPushButton(self.tr("Delete"))
		self.tex_add_btn.clicked.connect(self._on_add_texture)
		self.tex_del_btn.clicked.connect(self._on_delete_texture)
		tg.addLayout(self._table_actions((self.tex_add_btn, "add"), (self.tex_del_btn, "delete")))
		tg.addWidget(self.textures_table, 1)
		self.textures_tab_idx = self.tabs.addTab(textures_tab, self.tr("Textures"))

		params_tab = QWidget()
		pg = QVBoxLayout(params_tab)
		pg.setContentsMargins(6, 6, 6, 6)
		pg.setSpacing(4)
		self.params_info_label = QLabel("")
		self.params_info_label.setWordWrap(True)
		set_style_role(self.params_info_label, banner="danger")
		self.params_info_label.hide()

		self.params_stack = QStackedWidget()

		self.par_add_btn = QPushButton(self.tr("Add"))
		self.par_add_above_btn = QPushButton(self.tr("Add Above"))
		self.par_del_btn = QPushButton(self.tr("Delete"))
		self.par_add_btn.clicked.connect(self._on_add_param)
		self.par_add_above_btn.clicked.connect(self._on_add_param_above)
		self.par_del_btn.clicked.connect(self._on_delete_param)
		pg.addLayout(self._table_actions(
			(self.par_add_btn, "add"), (self.par_add_above_btn, "arrow_up"), (self.par_del_btn, "delete")))
		pg.addWidget(self.params_info_label)
		pg.addWidget(self.params_stack, 1)
		self.params_tab_idx = self.tabs.addTab(params_tab, self.tr("Parameters"))

		gpbf_tab = QWidget()
		gg = QVBoxLayout(gpbf_tab)
		gg.setContentsMargins(6, 6, 6, 6)
		gg.setSpacing(4)
		self.gpbf_table = QTableWidget(0, 2)
		self.gpbf_table.setHorizontalHeaderLabels([self.tr("Name"), self.tr("Data")])
		self.gpbf_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.gpbf_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
		self.gpbf_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.gpbf_table.itemChanged.connect(self._on_gpbf_changed)
		self.gpbf_add_btn = QPushButton(self.tr("Add"))
		self.gpbf_del_btn = QPushButton(self.tr("Delete"))
		self.gpbf_add_btn.clicked.connect(self._on_add_gpbf)
		self.gpbf_del_btn.clicked.connect(self._on_delete_gpbf)
		gg.addLayout(self._table_actions((self.gpbf_add_btn, "add"), (self.gpbf_del_btn, "delete")))
		gg.addWidget(self.gpbf_table, 1)
		self.gpbf_tab_idx = self.tabs.addTab(gpbf_tab, self.tr("GPU Buffers"))

		self.shaderLODRedirects_tab = QWidget()
		tx = QGridLayout(self.shaderLODRedirects_tab)
		tx.setContentsMargins(6, 6, 6, 6)
		tx.setSpacing(6)
		tx.addWidget(styled_label("ShaderLOD Count", tone="muted"), 0, 0)
		self.shaderLOD_count_spin = QSpinBox()
		self.shaderLOD_count_spin.setRange(0, 2048)
		self.shaderLOD_count_spin.valueChanged.connect(self._on_shaderLOD_count_changed)
		tx.addWidget(self.shaderLOD_count_spin, 0, 1)
		self.shaderLODRedirects_table = QTableWidget(0, 2)
		self.shaderLODRedirects_table.setHorizontalHeaderLabels(["Texture Table", "Byte Buffer Table"])
		self.shaderLODRedirects_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.shaderLODRedirects_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
		self.shaderLODRedirects_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.shaderLODRedirects_table.itemChanged.connect(self._on_shaderLODRedirects_changed)
		tx.addWidget(self.shaderLODRedirects_table, 1, 0, 1, 3)
		self.shaderLODRedirects_tab_idx = self.tabs.addTab(
			self.shaderLODRedirects_tab, self.tr("Shader LOD Redirects")
		)

		self.tabs.currentChanged.connect(self._on_tab_changed)
		self.materials_table.itemSelectionChanged.connect(self._on_select_material)

	def _set_header_tooltip(self, table: QTableWidget, column: int, tooltip: str):
		model = table.model()
		if model:
			model.setHeaderData(column, Qt.Horizontal, tooltip, Qt.ToolTipRole)

	@staticmethod
	def _separator() -> QFrame:
		line = QFrame()
		line.setFrameShape(QFrame.Shape.HLine)
		return line

	@staticmethod
	def _table_actions(*buttons) -> QHBoxLayout:
		row = QHBoxLayout()
		row.setSpacing(4)
		for button, icon in buttons:
			destructive = icon == "delete"
			button.setIcon(line_icon(icon, current_colors()["danger"] if destructive else None))
			set_style_role(button, compact=True, variant="danger" if destructive else "")
			row.addWidget(button)
		row.addStretch(1)
		return row

	def _populate(self):
		m = self.handler.mdf
		if m is None:
			raise RuntimeError("MdfViewer: handler.mdf is None")
		self.version_edit.blockSignals(True)
		self.version_edit.setText(str(self._current_file_version()))
		self.version_edit.blockSignals(False)
		self.meshlet_check.blockSignals(True)
		self.meshlet_check.setChecked(bool(m.header.meshlet_material))
		self.meshlet_check.blockSignals(False)
		self._update_version_dependent_tabs()
		self._refresh_materials_list()
		if self.materials_table.rowCount() > 0:
			self.materials_table.selectRow(0)
			self._current_index = 0
			self._refresh_details_for_current_material()
		else:
			self._current_index = -1
			self._refresh_details_for_current_material()

	def _refresh_materials_list(self):
		m = self.handler.mdf
		if m is None:
			raise RuntimeError("MdfViewer: handler.mdf is None")
		flt = (self.filter_edit.text() or "").lower()
		self.materials_table.blockSignals(True)
		self.materials_table.setRowCount(0)
		for md in m.materials:
			name = md.header.mat_name or ""
			if flt and flt not in name.lower():
				continue
			r = self.materials_table.rowCount()
			self.materials_table.insertRow(r)
			self.materials_table.setItem(r, 0, QTableWidgetItem(name))
		self.materials_table.blockSignals(False)

	def _on_filter_changed(self, _):
		self._refresh_materials_list()

	def _on_version_changed(self, text: str):
		m = self.handler.mdf
		if not m:
			return
		try:
			ver = int(text)
		except ValueError:
			return
		m.header.version = ver
		m.file_version = ver
		self._update_version_dependent_tabs()
		self.modified = True
		current_row = self.materials_table.currentRow()
		if current_row >= 0:
			self._refresh_material_row(current_row)
		self._refresh_details_for_current_material()

	def _on_meshlet_changed(self, state: int):
		m = self.handler.mdf
		if not m:
			return
		enabled = state == Qt.Checked.value
		if m.header.meshlet_material != enabled:
			m.header.meshlet_material = enabled
			self.modified = True

	def _refresh_material_row(self, r: int):
		m = self.handler.mdf
		if not m or not (0 <= r < len(m.materials)):
			return
		name = m.materials[r].header.mat_name
		self.materials_table.blockSignals(True)
		self.materials_table.setItem(r, 0, QTableWidgetItem(name))
		self.materials_table.blockSignals(False)

	def _on_shader_changed(self, idx: int):
		rows = self.materials_table.selectionModel().selectedRows()
		m = self.handler.mdf
		if not m or not rows:
			return
		i = rows[0].row()
		m.materials[i].header.shader_type = idx
		self.modified = True

	def _on_bake_texture_changed(self, value: int):
		rows = self.materials_table.selectionModel().selectedRows()
		m = self.handler.mdf
		if not m or not rows:
			return
		i = rows[0].row()
		m.materials[i].header.BakeTextureArraySize = value
		self.modified = True

	def _on_ukn_changed(self, text: str):
		rows = self.materials_table.selectionModel().selectedRows()
		m = self.handler.mdf
		if not m or not rows or m.layout == "onimusha_wots":
			return
		i = rows[0].row()
		text = (text or "").strip()
		if not text:
			value = 0
		else:
			try:
				value = int(text, 0)
			except ValueError:
				return
		value = max(0, min(value, 0xFFFFFFFFFFFFFFFF))
		header = m.materials[i].header
		current_value = header.ukn_re7 if self._current_file_version() == 6 else header.ukn
		if current_value != value:
			if self._current_file_version() == 6:
				header.ukn_re7 = value
			else:
				header.ukn = value
			self.modified = True

	def _on_flags_changed(self, *_):
		rows = self.materials_table.selectionModel().selectedRows()
		m = self.handler.mdf
		if not m or not rows:
			return
		i = rows[0].row()
		h = m.materials[i].header
		version = self._current_file_version()
		
		alpha = self._checked_flag_bits(self.flags1_checks)
		
		if version >= 31:
			if self.transparent_zpostpass_check.isChecked():
				alpha |= (1 << 10)
			alpha |= (self.tess_spin.value() & 0x1F) << 11
		else:
			alpha |= (self.tess_spin.value() & 0x3F) << 10
		
		alpha |= (self.phong_spin.value() & 0xFF) << 16
		alpha |= self._checked_flag_bits(self.flags2_checks, start_bit=24)
		
		if version >= 31:
			alpha |= self._checked_flag_bits(self.flags3_checks, start_bit=32)
			bias = self.transparent_priority_bias_spin.value()
			if bias < 0:
				bias = (1 << 8) + bias
			alpha |= (bias & 0xFF) << 40
		
		h.material_flags = alpha
		self.modified = True

	@staticmethod
	def _checked_flag_bits(checks, start_bit: int = 0) -> int:
		return sum(
			1 << (start_bit + bit)
			for bit, checkbox in enumerate(checks)
			if checkbox.isChecked()
		)

	def _update_flags_ui(self, h):
		alpha = int(h.material_flags)
		version = self._current_file_version()
		
		for bit, cb in enumerate(self.flags1_checks):
			cb.blockSignals(True)
			cb.setChecked(bool((alpha >> bit) & 1))
			cb.blockSignals(False)
		
		self.transparent_zpostpass_check.blockSignals(True)
		if version >= 31:
			self.transparent_zpostpass_check.setChecked(bool((alpha >> 10) & 1))
			self.transparent_zpostpass_check.setVisible(True)
		else:
			self.transparent_zpostpass_check.setChecked(False)
			self.transparent_zpostpass_check.setVisible(False)
		self.transparent_zpostpass_check.blockSignals(False)
		
		self.tess_spin.blockSignals(True)
		if version >= 31:
			self.tess_spin.setRange(0, 31)
			self.tess_spin.setValue((alpha >> 11) & 0x1F)
		else:
			self.tess_spin.setRange(0, 63)
			self.tess_spin.setValue((alpha >> 10) & 0x3F)
		self.tess_spin.blockSignals(False)
		
		self.phong_spin.blockSignals(True)
		self.phong_spin.setValue((alpha >> 16) & 0xFF)
		self.phong_spin.blockSignals(False)
		
		for bit, cb in enumerate(self.flags2_checks):
			cb.blockSignals(True)
			cb.setChecked(bool((alpha >> (24 + bit)) & 1))
			cb.blockSignals(False)
		
		self._flags3_separator.setVisible(version >= 31)
		for bit, cb in enumerate(self.flags3_checks):
			cb.blockSignals(True)
			if version >= 31:
				cb.setChecked(bool((alpha >> (32 + bit)) & 1))
				cb.setVisible(True)
			else:
				cb.setChecked(False)
				cb.setVisible(False)
			cb.blockSignals(False)
		
		self.transparent_priority_bias_spin.blockSignals(True)
		if version >= 31:
			self.transparent_priority_bias_spin.setValue(transparent_priority_bias(alpha))
			self.transparent_priority_bias_label.setVisible(True)
			self.transparent_priority_bias_spin.setVisible(True)
		else:
			self.transparent_priority_bias_spin.setValue(0)
			self.transparent_priority_bias_label.setVisible(False)
			self.transparent_priority_bias_spin.setVisible(False)
		self.transparent_priority_bias_spin.blockSignals(False)

	def _on_mmtr_changed(self, text: str):
		m = self.handler.mdf
		idx = self._get_current_index()
		if 0 <= idx < len(m.materials):
			m.materials[idx].header.mmtr_path = text
			self._mmtr_document_cache.pop(resource_path_key(text), None)
			self.modified = True

	def _on_matname_changed(self, text: str):
		m = self.handler.mdf
		idx = self._get_current_index()
		if not m or not (0 <= idx < len(m.materials)):
			return
		
		if self._check_duplicate_material_name(text, exclude_index=idx):
			QMessageBox.warning(
				self,
				self.tr("Duplicate Material Name"),
				self.tr(
					"A material with the name '{name}' already exists.\n"
					"Please use a unique name for each material."
				).format(name=text),
			)
		
		h = m.materials[idx].header
		h.mat_name = text
		self.matname_hash_label.setText(f"0x{murmur3_hash_utf16le(text):08x}")
		self._refresh_material_row(idx)
		self.modified = True

	def _get_current_index(self) -> int:
		rows = self.materials_table.selectionModel().selectedRows()
		if rows:
			self._current_index = rows[0].row()
		return self._current_index

	def _current_file_version(self) -> int:
		m = self.handler.mdf
		if not m:
			return 0
		return int(m.file_version)

	def _current_file_name(self) -> str:
		path = self.handler.filepath
		return os.path.basename(path) if path else ''

	def _update_version_dependent_tabs(self):
		version = self._current_file_version()
		has_gpu = version >= 19
		self.tabs.setTabVisible(self.gpbf_tab_idx, has_gpu)
		if not has_gpu:
			self.gpbf_table.blockSignals(True)
			self.gpbf_table.setRowCount(0)
			self.gpbf_table.blockSignals(False)
		has_shader_lods = version >= 31
		self.tabs.setTabVisible(self.shaderLODRedirects_tab_idx, has_shader_lods)
		if not has_shader_lods:
			self.shaderLODRedirects_table.blockSignals(True)
			self.shaderLODRedirects_table.setRowCount(0)
			self.shaderLODRedirects_table.blockSignals(False)
			self.shaderLOD_count_spin.blockSignals(True)
			self.shaderLOD_count_spin.setValue(0)
			self.shaderLOD_count_spin.blockSignals(False)

	def _insert_materials(self, materials, insert_at=None):
		m = self.handler.mdf
		if not m or not materials:
			return 0
		if insert_at is None:
			selected = self.materials_table.selectionModel().selectedRows()
			if selected:
				insert_at = min(len(m.materials), max(idx.row() for idx in selected) + 1)
			else:
				insert_at = len(m.materials)
		insert_at = max(0, min(insert_at, len(m.materials)))
		for offset, mat in enumerate(materials):
			m.materials.insert(insert_at + offset, mat)

		self._clear_params_cache()
		self._refresh_materials_list()
		if self.materials_table.rowCount() > 0:
			target_row = min(insert_at, self.materials_table.rowCount() - 1)
			if target_row >= 0:
				self.materials_table.selectRow(target_row)
				self._current_index = target_row
				self._refresh_details_for_current_material()
		self.modified = True
		return len(materials)

	def get_material_export_context(self):
		m = self.handler.mdf
		idx = self._get_current_index()
		mat = None
		if m and 0 <= idx < len(m.materials):
			mat = m.materials[idx]
		return mat, self._current_file_version(), self._current_file_name()

	def get_target_version(self) -> int:
		return self._current_file_version()

	def _open_template_manager(self):
		dlg = MdfTemplateManagerDialog(self, viewer=self)
		dlg.template_imported.connect(self._on_template_imported)
		dlg.exec()

	def _on_template_imported(self, material, metadata):
		if material is None:
			return
		
		material_name = material.header.mat_name or ""
		if self._check_duplicate_material_name(material_name):
			QMessageBox.warning(
				self,
				self.tr("Import Template"),
				self.tr(
					"Cannot import template: A material with the name '{name}' already exists.\n"
					"Please rename the existing material first or use a different template."
				).format(name=material_name),
			)
			return
		
		inserted = self._insert_materials([material])
		if inserted:
			name = "Template"
			if isinstance(metadata, dict):
				name = metadata.get("name") or metadata.get("id") or name
			QMessageBox.information(
				self,
				self.tr("Import Template"),
				self.tr("Imported template '{name}'.").format(name=name),
			)

	def _clear_params_cache(self):
		while self.params_stack.count() > 0:
			widget = self.params_stack.widget(0)
			self.params_stack.removeWidget(widget)
			widget.deleteLater()

	def _get_or_create_params_table(self, mat_index):
		while self.params_stack.count() <= mat_index:
			table = QTableWidget(0, 8)
			table.setHorizontalHeaderLabels([
				self.tr("Name"), self.tr("Components"), self.tr("Locked"),
				"X", "Y", "Z", "W", self.tr("Color"),
			])
			table.horizontalHeaderItem(2).setIcon(line_icon("lock"))
			self._set_header_tooltip(table, 2, self.tr(
				"Checked: the game can't change this parameter at runtime; setFloat() is skipped and the file's value stays."))
			table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
			for column in range(1, 8):
				table.horizontalHeader().setSectionResizeMode(column, QHeaderView.ResizeToContents)
			table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
			table.setSelectionBehavior(QAbstractItemView.SelectRows)
			table.itemChanged.connect(self._on_param_changed)
			table.itemClicked.connect(self._on_param_clicked)
			self.params_stack.addWidget(table)

		return self.params_stack.widget(mat_index)

	def _refresh_params_table(self, md, mat_index: int):
		table = self._get_or_create_params_table(mat_index)
		table.blockSignals(True)
		self._update_table_rows(table, len(md.parameters))
		for row, parameter in enumerate(md.parameters):
			self._populate_param_row_in_table(table, row, parameter)
		table.blockSignals(False)
		self.params_stack.setCurrentIndex(mat_index)
		self._update_params_info(md)

	def _refresh_details_for_current_material(self):
		m = self.handler.mdf
		mat_index = self._current_index

		if not m or not (0 <= mat_index < len(m.materials)):
			self.mmtr_edit.setText("")
			self.matname_edit.setText("")
			self.matname_hash_label.setText("0x00000000")
			self.bake_texture_label.setVisible(False)
			self.bake_texture_spin.setVisible(False)
			self.ukn_label.setVisible(False)
			self.ukn_edit.setVisible(False)
			self.ukn_edit.setText("0")
			self._update_table_rows(self.textures_table, 0)
			self._update_table_rows(self.gpbf_table, 0)
			self._update_table_rows(self.shaderLODRedirects_table, 0)
			self.material_graph.set_material(None)
			return

		md = m.materials[mat_index]
		version = self._current_file_version()

		self.mmtr_edit.blockSignals(True)
		self.mmtr_edit.setText(md.header.mmtr_path)
		self.mmtr_edit.blockSignals(False)

		self.matname_edit.blockSignals(True)
		self.matname_edit.setText(md.header.mat_name)
		self.matname_edit.blockSignals(False)
		self.matname_hash_label.setText(f"0x{murmur3_hash_utf16le(md.header.mat_name):08x}")

		self.shader_combo.blockSignals(True)
		sh = int(md.header.shader_type)
		if 0 <= sh < self.shader_combo.count():
			self.shader_combo.setCurrentIndex(sh)
		self.shader_combo.blockSignals(False)

		self.bake_texture_spin.blockSignals(True)
		if version >= 31:
			self.bake_texture_spin.setValue(md.header.BakeTextureArraySize)
			self.bake_texture_label.setVisible(True)
			self.bake_texture_spin.setVisible(True)
		else:
			self.bake_texture_spin.setValue(0)
			self.bake_texture_label.setVisible(False)
			self.bake_texture_spin.setVisible(False)
		self.bake_texture_spin.blockSignals(False)

		self.ukn_edit.blockSignals(True)
		onimusha_layout = m.layout == "onimusha_wots"
		self.ukn_edit.setReadOnly(onimusha_layout)
		if version == 6 or version >= 51:
			if onimusha_layout:
				self.ukn_edit.setText(str(md.header.parameter_value_count))
				self.ukn_label.setText(self.tr("Parameter float slots"))
			elif version == 6:
				self.ukn_edit.setText(str(md.header.ukn_re7))
				self.ukn_label.setText(self.tr("Unknown RE7 64"))
			else:
				self.ukn_edit.setText(str(md.header.ukn))
				self.ukn_label.setText(self.tr("Unknown 64"))
			self.ukn_label.setVisible(True)
			self.ukn_edit.setVisible(True)
		else:
			self.ukn_edit.setText("0")
			self.ukn_label.setVisible(False)
			self.ukn_edit.setVisible(False)
		self.ukn_edit.blockSignals(False)

		self._update_flags_ui(md.header)
		self._refresh_params_table(md, mat_index)

		current_tab = self.tabs.currentIndex()
		if (
			current_tab == self.material_graph_tab_idx
			or self.material_graph._material is not md
		):
			self._show_material_graph(md)
		if current_tab == self.textures_tab_idx:
			self._refresh_textures_table(md)
		elif current_tab == self.gpbf_tab_idx and version >= 19:
			self._refresh_gpbf_table(md)
		elif current_tab == self.shaderLODRedirects_tab_idx and version >= 31:
			self._refresh_shader_lod_table(md)
	def _refresh_textures_table(self, md):
		self.textures_table.blockSignals(True)
		self._update_table_rows(self.textures_table, len(md.textures))
		for row, texture in enumerate(md.textures):
			self._update_texture_row(row, texture)
		self.textures_table.blockSignals(False)

	def _populate_param_row_in_table(self, table, row, parameter):
		table.setItem(row, 0, QTableWidgetItem(parameter.name))
		table.setItem(row, 1, QTableWidgetItem(str(parameter.component_count)))

		locked = QTableWidgetItem()
		locked.setFlags(locked.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
		locked.setCheckState(Qt.Checked if parameter.component_locked else Qt.Unchecked)
		locked.setFlags(locked.flags() & ~Qt.ItemIsEditable)
		table.setItem(row, 2, locked)

		values = list(parameter.parameter)
		for component in range(4):
			item = QTableWidgetItem("")
			if component < parameter.component_count:
				item.setText(format_display_value(values[component]))
				item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
			else:
				item.setFlags((item.flags() | Qt.ItemIsEnabled) & ~(Qt.ItemIsEditable | Qt.ItemIsSelectable))
			table.setItem(row, 3 + component, item)
		table.setItem(row, 7, self._parameter_color_item(parameter, values))

	def _parameter_color_item(self, parameter, values) -> QTableWidgetItem:
		item = QTableWidgetItem("")
		if not is_color_parameter(parameter):
			item.setFlags((item.flags() | Qt.ItemIsEnabled) & ~(Qt.ItemIsEditable | Qt.ItemIsSelectable))
			return item
		red, green, blue = (int(unit_value(value) * 255) for value in values[:3])
		alpha = int(unit_value(values[3] if parameter.component_count == 4 else 1.0) * 255)
		item.setBackground(get_color_preview_brush(red, green, blue, alpha))
		item.setToolTip(
			f"RGBA: {red},{green},{blue},{alpha}"
			if parameter.component_count == 4
			else f"RGB: {red},{green},{blue}"
		)
		item.setFlags((item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled) & ~Qt.ItemIsEditable)
		return item

	def _refresh_gpbf_table(self, md):
		self.gpbf_table.blockSignals(True)
		self._update_table_rows(self.gpbf_table, len(md.gpu_buffers))
		for r, (n, d) in enumerate(md.gpu_buffers):
			self._update_gpbf_row(r, n.name, d.name)
		self.gpbf_table.blockSignals(False)

	def _refresh_shader_lod_table(self, md):
		self.shaderLODRedirects_table.blockSignals(True)
		self.shaderLOD_count_spin.blockSignals(True)
		count = int(md.header.shaderLODNum)
		self.shaderLOD_count_spin.setValue(count)
		self._update_table_rows(self.shaderLODRedirects_table, count)
		for i in range(count):
			counts, elems = (md.shader_lod_redirects[i] if i < len(md.shader_lod_redirects) else ([], []))
			self._update_shader_lod_row(i, counts, elems)
		self.shaderLOD_count_spin.blockSignals(False)
		self.shaderLODRedirects_table.blockSignals(False)

	def _on_tab_changed(self, index):
		self._refresh_details_for_current_material()

	def _current_material(self):
		m = self.handler.mdf
		if m and 0 <= self._current_index < len(m.materials):
			return m.materials[self._current_index]
		return None

	def _material_edited(self, md, *, textures=False):
		self._refresh_material_row(self._current_index)
		if textures:
			self._refresh_textures_table(md)
		else:
			self._refresh_params_table(md, self._current_index)
		self._refresh_material_graph(md)
		self.modified = True

	def _on_add_texture(self):
		md = self._current_material()
		if md is None:
			return
		md.textures.append(TexHeader())
		self._material_edited(md, textures=True)

	def _delete_selected_rows(self, md, items: list, table, *, textures=False):
		selected = sorted({index.row() for index in table.selectedIndexes()}, reverse=True)
		for row in selected:
			if 0 <= row < len(items):
				del items[row]
		if selected:
			self._material_edited(md, textures=textures)

	def _on_delete_texture(self):
		md = self._current_material()
		if md is not None:
			self._delete_selected_rows(md, md.textures, self.textures_table, textures=True)

	@staticmethod
	def _new_parameter(name="Param"):
		return ParamHeader(name=name, component_count=1)

	def _on_add_param(self):
		md = self._current_material()
		if md is None:
			return
		table = self._get_or_create_params_table(self._current_index)
		name = "Param"
		row = table.currentRow()
		if 0 <= row < len(md.parameters):
			previous = md.parameters[row - 1].name if row > 0 else ""
			is_layer, color_index = layer_color_channel(previous)
			if is_layer and color_index is not None and color_index < 2:
				name = ("LayerColor_Red", "LayerColor_Green", "LayerColor_Blue")[color_index + 1]
		md.parameters.append(self._new_parameter(name))
		self._material_edited(md)

	def _on_add_param_above(self):
		md = self._current_material()
		if md is None:
			return
		table = self._get_or_create_params_table(self._current_index)
		insert_at = max(0, min(table.currentRow(), len(md.parameters)))
		md.parameters.insert(insert_at, self._new_parameter())
		self._material_edited(md)
		table.selectRow(insert_at)

	def _on_delete_param(self):
		md = self._current_material()
		if md is not None:
			self._delete_selected_rows(md, md.parameters, self._get_or_create_params_table(self._current_index))

	def _update_params_info(self, md):
		segments = []
		current = []
		for parameter in md.parameters:
			is_layer, color_index = layer_color_channel(parameter.name)
			if is_layer:
				current.append(color_index)
			elif current:
				segments.append(current)
				current = []
		if current:
			segments.append(current)

		messages = []
		seen = set()
		for segment in segments:
			for color_index in segment:
				if color_index in (0, 1, 2):
					if color_index in seen:
						messages.append(self.tr("LayerColor: Only one of each (Red, Green, Blue) is allowed."))
					seen.add(color_index)
			previous = None
			for color_index in segment:
				if color_index is None:
					continue
				if previous is not None and previous != 0 and color_index > previous + 1:
					messages.append(self.tr("LayerColor: Only Green may be between Red and Blue."))
				previous = color_index
		if len(segments) > 1:
			messages.append(self.tr("LayerColor: Only one RGB sequence is allowed."))
		if messages:
			self.params_info_label.setText("\n".join(sorted(set(messages))))
		else:
			self.params_info_label.setText("")
		self.params_info_label.setVisible(bool(messages))

	def _edit_shader(self):
		m = self.handler.mdf
		idx = self._get_current_index()
		app = getattr(self.handler, "app", None)
		if not (0 <= idx < len(m.materials)) or app is None:
			return
		material = m.materials[idx]
		resource_path = str(material.header.mmtr_path or "").replace("\\", "/").strip()
		resolved = MeshMaterialResolver._resolve_mmtr_resource(self.handler, resource_path) if resource_path else None
		if resolved is None:
			QMessageBox.warning(self, self.tr("Master material not found"), self.tr(
				"The master material {path} is not in the project, the unpacked files or the game PAKs.").format(
				path=resource_path or self.tr("(none)")))
			return
		path, data = resolved
		choice = QMessageBox(self)
		choice.setWindowTitle(self.tr("Edit Shader"))
		choice.setText(self.tr("Change the shader of {material} only, or of every object using its master material?")
				.format(material=material.header.mat_name or self.tr("this material")))
		choice.setInformativeText(self.tr(
			"This object only: REasy saves a copy of the master for this MDF material with Create Variant.\n"
			"Every object: saving changes the master itself."))
		only = choice.addButton(self.tr("This Object Only"), QMessageBox.ButtonRole.AcceptRole)
		every = choice.addButton(self.tr("Every Object Using It"), QMessageBox.ButtonRole.AcceptRole)
		choice.addButton(QMessageBox.StandardButton.Cancel)
		choice.exec()
		if choice.clickedButton() not in {only, every}:
			return
		scope = ObjectScope(material, self.handler if choice.clickedButton() is only else None, idx,
				lambda path: MeshMaterialResolver._resolve_resource(self.handler, path))
		context = resource_context_for_handler(self.handler)
		in_paks = not Path(path).is_file()
		open_master_for_object(app, path, data, scope,
				pak_project_dir=str(getattr(context, "project_dir", "") or "") if in_paks else "")

	def _resolve_mmtr_document(self, material):
		resource_path = str(material.header.mmtr_path or "").replace("\\", "/").strip()
		if not resource_path:
			return None
		key = resource_path_key(resource_path)
		found, parsed = self._mmtr_document_cache.setdefault(key, ({}, {}))

		def load(path):
			if found.get(path) is None:
				found[path] = MeshMaterialResolver._resolve_resource(self.handler, path)
			return found[path]

		def master(path, data):
			if path not in parsed:
				context = resource_context_for_handler(self.handler)
				game = str(getattr(context, "game", "") or getattr(self.handler, "game_version", "") or "")
				parsed[path] = parsed_master(data, game, resource_version_from_path(path, "mmtr"), path)
			return parsed[path]

		try:
			document = bind_drawn_master(MaterialInstanceContext.from_material(material), resource_path, load, master)
		except Exception as exc:
			self._mmtr_document_errors[key] = self.tr(
				"Could not resolve the compiled master material: {error}"
			).format(error=exc)
			return None
		self._mmtr_document_errors.pop(key, None)
		return document

	def _resolve_texture_preview(self, texture):
		resource_path = str(texture.tex_path or "").replace("\\", "/").strip()
		cache_key = resource_path.casefold()
		if not resource_path:
			return QPixmap()
		if cache_key in self._texture_preview_cache:
			return self._texture_preview_cache[cache_key]
		try:
			resolved = MeshMaterialResolver.resolve_texture_path(self.handler, resource_path, prefer_streaming=False)
			if resolved is None:
				preview = self.tr("The texture {path} was not found.").format(path=resource_path)
			else:
				preview = decode_tex_bytes_to_qpixmap(
					resolved[1], mip_selector=lambda tex: choose_texture_mip(tex, DEFAULT_TEXTURE_QUALITY))
				if preview is None:
					preview = self.tr("The texture {path} has no image this preview can show.").format(
						path=resource_path)
		except (OSError, ValueError) as error:
			preview = self.tr("The texture {path} cannot be read: {error}").format(path=resource_path, error=error)
		self._texture_preview_cache[cache_key] = preview
		return preview

	def _on_graph_material_changed(self, kind: str, item_index: int):
		material = self._current_material()
		if material is None:
			return
		if kind == "texture" and 0 <= item_index < len(material.textures):
			self._texture_preview_cache.clear()
			self._refresh_textures_table(material)
		elif kind == "parameter" and 0 <= item_index < len(material.parameters):
			self._refresh_params_table(material, self._current_index)
		elif kind == "header":
			self._update_flags_ui(material.header)
		self.modified = True

	def _show_material_graph(self, material):
		self.material_graph.set_material(material, self._resolve_mmtr_document(material),
				texture_preview_provider=self._resolve_texture_preview)
		error = self._mmtr_document_errors.get(resource_path_key(material.header.mmtr_path), "")
		if error:
			self.material_graph.status.setToolTip(error)

	def _refresh_material_graph(self, material):
		if self.material_graph._material is material:
			self._show_material_graph(material)


	def _on_add_gpbf(self):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		md.gpu_buffers.append((GpbfHeader("Name"), GpbfHeader("Data")))
		self._refresh_details_for_current_material()
		self.modified = True

	def _on_delete_gpbf(self):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		sel = sorted({r.row() for r in self.gpbf_table.selectedIndexes()}, reverse=True)
		for r in sel:
			if 0 <= r < len(md.gpu_buffers):
				del md.gpu_buffers[r]
		self._refresh_details_for_current_material()
		if sel:
			self.modified = True

	def _on_select_material(self):
		rows = self.materials_table.selectionModel().selectedRows()
		if not rows:
			return
		new_index = rows[0].row()
		if new_index == self._current_index:
			return
		self._current_index = new_index
		self._refresh_details_for_current_material()

	def _on_shaderLOD_count_changed(self, val: int):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		md.header.shaderLODNum = max(0, int(val))
		while len(md.shader_lod_redirects) < md.header.shaderLODNum:
			md.shader_lod_redirects.append(([], []))
		while len(md.shader_lod_redirects) > md.header.shaderLODNum:
			md.shader_lod_redirects.pop()
		self._refresh_details_for_current_material()
		self.modified = True

	def _on_shaderLODRedirects_changed(self, item):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		r = item.row()
		c = item.column()
		if not (0 <= r < len(md.shader_lod_redirects)):
			return
		counts, elems = md.shader_lod_redirects[r]
		text = item.text() or ""
		parts = [p.strip() for p in text.split(',') if p.strip()]
		vals = []
		for p in parts:
			try:
				vals.append(int(p))
			except ValueError:
				return
		if c == 0:
			md.shader_lod_redirects[r] = (vals, elems)
		elif c == 1:
			md.shader_lod_redirects[r] = (counts, vals)
		self.modified = True

	def _on_texture_changed(self, item):
		m = self.handler.mdf
		material_index = self._get_current_index()
		if not m or not (0 <= material_index < len(m.materials)):
			return
		row = item.row()
		material = m.materials[material_index]
		if not (0 <= row < len(material.textures)):
			return
		texture = material.textures[row]
		if item.column() == 0:
			texture.tex_type = item.text()
		elif item.column() == 1:
			texture.tex_path = item.text()
			self._texture_preview_cache.clear()
		elif item.column() == 2:
			texture.locked = 1 if item.checkState() == Qt.Checked else 0
		self._refresh_material_graph(material)
		self.modified = True

	def _param_for_item(self, item):
		table = item.tableWidget()
		material_index = self.params_stack.indexOf(table)
		m = self.handler.mdf
		if not m or not (0 <= material_index < len(m.materials)):
			return None
		material = m.materials[material_index]
		row = item.row()
		if not (0 <= row < len(material.parameters)):
			return None
		return table, material, row, material.parameters[row]

	def _on_param_changed(self, item):
		target = self._param_for_item(item)
		if target is None:
			return
		table, material, row, parameter = target
		column = item.column()
		try:
			if column == 0:
				parameter.name = item.text()
			elif column == 1:
				component_count = max(1, min(4, int(item.text())))
				parameter.component_count = component_count
				values = list(parameter.parameter)
				for component in range(component_count, 4):
					values[component] = 0.0
				parameter.parameter = tuple(values)
			elif column == 2:
				parameter.component_locked = 1 if item.checkState() == Qt.Checked else 0
			elif 3 <= column <= 6:
				component = column - 3
				if component >= parameter.component_count:
					return
				values = list(parameter.parameter)
				values[component] = float(item.text())
				parameter.parameter = tuple(values)
		except ValueError:
			table.blockSignals(True)
			self._populate_param_row_in_table(table, row, parameter)
			table.blockSignals(False)
			return
		table.blockSignals(True)
		self._populate_param_row_in_table(table, row, parameter)
		table.blockSignals(False)
		self._update_params_info(material)
		self._refresh_material_graph(material)
		self.modified = True


	def _update_table_rows(self, table: QTableWidget, target_count: int):
		current_count = table.rowCount()
		if current_count == target_count:
			return
		if current_count < target_count:
			for _ in range(target_count - current_count):
				table.insertRow(table.rowCount())
		else:
			for _ in range(current_count - target_count):
				table.removeRow(table.rowCount() - 1)

	def _update_texture_row(self, row: int, texture):
		for column, value in ((0, texture.tex_type), (1, texture.tex_path)):
			item = self.textures_table.item(row, column)
			if item is None:
				item = QTableWidgetItem(value)
				self.textures_table.setItem(row, column, item)
			else:
				item.setText(value)
		locked = self.textures_table.item(row, 2)
		if locked is None:
			locked = QTableWidgetItem()
			locked.setFlags((locked.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled) & ~Qt.ItemIsEditable)
			self.textures_table.setItem(row, 2, locked)
		locked.setCheckState(Qt.Checked if texture.locked else Qt.Unchecked)


	def _update_gpbf_row(self, row: int, name_str: str, data_str: str):
		item = self.gpbf_table.item(row, 0)
		if item is None:
			item = QTableWidgetItem(name_str)
			self.gpbf_table.setItem(row, 0, item)
		else:
			item.setText(name_str)
		
		item = self.gpbf_table.item(row, 1)
		if item is None:
			item = QTableWidgetItem(data_str)
			self.gpbf_table.setItem(row, 1, item)
		else:
			item.setText(data_str)

	def _update_shader_lod_row(self, row: int, counts, elems):
		counts_str = ",".join(str(v) for v in counts)
		item = self.shaderLODRedirects_table.item(row, 0)
		if item is None:
			item = QTableWidgetItem(counts_str)
			self.shaderLODRedirects_table.setItem(row, 0, item)
		else:
			item.setText(counts_str)
		
		elems_str = ",".join(str(v) for v in elems)
		item = self.shaderLODRedirects_table.item(row, 1)
		if item is None:
			item = QTableWidgetItem(elems_str)
			self.shaderLODRedirects_table.setItem(row, 1, item)
		else:
			item.setText(elems_str)

	def _on_param_clicked(self, item):
		if item.column() != 7:
			return
		target = self._param_for_item(item)
		if target is None:
			return
		table, material, row, _parameter = target
		if not pick_parameter_color(self, material, row):
			return
		self._refresh_params_table(material, self.params_stack.indexOf(table))
		self._refresh_material_graph(material)
		self.modified = True

	def _on_gpbf_changed(self, item):
		m = self.handler.mdf
		mi = self._get_current_index()
		if not (0 <= mi < len(m.materials)):
			return
		gi = item.row()
		if not (0 <= gi < len(m.materials[mi].gpu_buffers)):
			return
		n, d = m.materials[mi].gpu_buffers[gi]
		if item.column() == 0:
			n.name = item.text()
		else:
			d.name = item.text()
		self.modified = True

	def _on_material_changed(self, item):
		r = item.row()
		c = item.column()
		m = self.handler.mdf
		if not m or r >= len(m.materials):
			return
		h = m.materials[r].header
		val = item.text()
		try:
			if c == 0:
				if self._check_duplicate_material_name(val, exclude_index=r):
					QMessageBox.warning(
						self,
						self.tr("Duplicate Material Name"),
						self.tr(
							"A material with the name '{name}' already exists.\n"
							"Please use a unique name for each material."
						).format(name=val),
					)
				h.mat_name = val
			elif c == 1:
				h.shader_type = int(val)
			elif c == 2:
				h.tex_count = int(val)
			elif c == 3:
				h.param_count = int(val)
			elif c == 4:
				h.material_flags = int(val)
			self.modified = True
		except ValueError:
			pass

	def _on_add_material(self):
		m = self.handler.mdf
		if not m:
			return
		
		existing_names = self._get_existing_material_names()
		new_name = self._generate_unique_material_name("Material", existing_names)
		
		mat = MatData()
		mat.header.mat_name = new_name
		m.materials.append(mat)
		self._clear_params_cache()
		self.materials_table.blockSignals(True)
		r = self.materials_table.rowCount()
		self.materials_table.insertRow(r)
		self.materials_table.setItem(r, 0, QTableWidgetItem(new_name))
		self.materials_table.blockSignals(False)
		self.materials_table.selectRow(r)
		self.modified = True
	
	def _on_delete_material(self):
		rows = sorted({i.row() for i in self.materials_table.selectedIndexes()}, reverse=True)
		m = self.handler.mdf
		if not m:
			return
		for r in rows:
			if 0 <= r < len(m.materials):
				del m.materials[r]
				self.materials_table.removeRow(r)
		if rows:
			self._clear_params_cache()
			self._current_index = min(rows[-1], len(m.materials) - 1)
			if self._current_index >= 0:
				self.materials_table.selectRow(self._current_index)
			self._refresh_details_for_current_material()
			self.modified = True

	def _on_copy_materials(self):
		m = self.handler.mdf
		if not m:
			return
		selected = self.materials_table.selectionModel().selectedRows()
		if not selected:
			QMessageBox.information(
				self, self.tr(COPY_MATERIALS_TITLE), self.tr("Select at least one material to copy.")
			)
			return
		indices = sorted(idx.row() for idx in selected)
		materials = [m.materials[i] for i in indices if 0 <= i < len(m.materials)]
		if not materials:
			QMessageBox.warning(
				self,
				self.tr(COPY_MATERIALS_TITLE),
				self.tr("No valid materials selected for copying."),
			)
			return
		MdfClipboard.copy_materials(
			materials,
			self._current_file_version(),
			self._current_file_name(),
		)
		QMessageBox.information(
			self,
			self.tr(COPY_MATERIALS_TITLE),
			self.tr("Copied {count} material(s) to clipboard.").format(count=len(materials)),
		)

	def _on_export_material(self):
		material, file_version, source_name = self.get_material_export_context()
		if material is None:
			QMessageBox.information(
				self, self.tr(EXPORT_TEMPLATE_TITLE), self.tr("Select a material to export.")
			)
			return
		default_name = material.header.mat_name
		mmtr_path = material.header.mmtr_path
		dlg = MdfTemplateExportDialog(self, default_name=default_name, mmtr_path=mmtr_path)
		if not dlg.exec():
			return
		data = dlg.export_data()
		result = MdfTemplateManager.export_material(
			material,
			file_version,
			data["name"],
			data["description"],
			data["tags"],
			source_name,
		)
		if not result.get("success"):
			QMessageBox.warning(
				self,
				self.tr(EXPORT_TEMPLATE_TITLE),
				result.get("message") or self.tr("Failed to export template."),
			)
			return
		QMessageBox.information(
			self,
			self.tr(EXPORT_TEMPLATE_TITLE),
			self.tr("Template '{name}' exported successfully.").format(name=data["name"]),
		)

	def _on_paste_materials(self):
		m = self.handler.mdf
		if not m:
			return
		target_version = self._current_file_version()
		materials, metadata = MdfClipboard.load_materials(target_version)
		if not materials:
			QMessageBox.warning(
				self,
				self.tr("Paste Materials"),
				self.tr("Clipboard does not contain MDF material data."),
			)
			return
		
		existing_names = self._get_existing_material_names()
		renamed_count = 0
		for mat in materials:
			original_name = mat.header.mat_name or ""
			unique_name = self._generate_unique_material_name(original_name, existing_names)
			if unique_name != original_name:
				mat.header.mat_name = unique_name
				renamed_count += 1
			existing_names.add(unique_name)
		
		selected = self.materials_table.selectionModel().selectedRows()
		insert_at = len(m.materials)
		if selected:
			insert_at = min(len(m.materials), max(idx.row() for idx in selected) + 1)
		inserted = self._insert_materials(materials, insert_at)
		source_name = metadata.get("source_file_name") if isinstance(metadata, dict) else ""
		source_version = metadata.get("source_file_version") if isinstance(metadata, dict) else None
		if not source_name:
			source_name = self.tr("Unknown file")
		msg_version = str(source_version) if source_version else self.tr("unknown")
		
		msg = self.tr(
			"Pasted {count} material(s) from {source} (version {version})."
		).format(count=inserted, source=source_name, version=msg_version)
		if renamed_count > 0:
			msg += "\n\n" + self.tr(
				"{count} material(s) were renamed to avoid duplicate names."
			).format(count=renamed_count)
		
		QMessageBox.information(self, self.tr("Paste Materials"), msg)
