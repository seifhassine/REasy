from PySide6.QtWidgets import (
	QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
	QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView, QComboBox,
	QGroupBox, QGridLayout, QCheckBox, QSpinBox, QSplitter, QTabWidget, QSizePolicy, QColorDialog
)
from PySide6.QtGui import QColor
from PySide6.QtCore import Qt, Signal
from utils.hash_util import murmur3_hash_utf16le


class MdfViewer(QWidget):
	modified_changed = Signal(bool)

	def __init__(self, handler):
		super().__init__()
		self.handler = handler
		self._modified = False
		self._current_index = 0
		self._setup_ui()
		self._populate()

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
		layout.setContentsMargins(8, 8, 8, 8)
		self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

		head = QHBoxLayout()
		head.addWidget(QLabel("MDF Version:"))
		self.version_edit = QLineEdit()
		self.version_edit.setReadOnly(False)
		self.version_edit.textChanged.connect(self._on_version_changed)
		head.addWidget(self.version_edit)
		layout.addLayout(head)

		splitter = QSplitter()
		splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
		layout.addWidget(splitter)

		left_panel = QWidget()
		left_v = QVBoxLayout(left_panel)
		flt = QHBoxLayout()
		flt.addWidget(QLabel("Filter:"))
		self.filter_edit = QLineEdit()
		self.filter_edit.setPlaceholderText("Filter materials...")
		self.filter_edit.textChanged.connect(self._on_filter_changed)
		flt.addWidget(self.filter_edit)
		left_v.addLayout(flt)
		self.materials_table = QTableWidget(0, 1)
		self.materials_table.setHorizontalHeaderLabels(["Materials"])
		self.materials_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.materials_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.materials_table.itemChanged.connect(self._on_material_changed)
		left_v.addWidget(self.materials_table)
		mrow = QHBoxLayout()
		self.add_btn = QPushButton("Add")
		self.add_btn.clicked.connect(self._on_add_material)
		self.del_btn = QPushButton("Delete")
		self.del_btn.clicked.connect(self._on_delete_material)
		mrow.addWidget(self.add_btn)
		mrow.addWidget(self.del_btn)
		mrow.addStretch()
		left_v.addLayout(mrow)
		splitter.addWidget(left_panel)

		self.tabs = QTabWidget()
		splitter.addWidget(self.tabs)
		splitter.setStretchFactor(0, 1)
		splitter.setStretchFactor(1, 3)

		overview = QWidget()
		ov = QGridLayout(overview)
		ov.addWidget(QLabel("Material Name"), 0, 0)
		self.matname_edit = QLineEdit()
		self.matname_edit.textChanged.connect(self._on_matname_changed)
		ov.addWidget(self.matname_edit, 0, 1)
		ov.addWidget(QLabel("Name Hash"), 0, 2)
		self.matname_hash_label = QLabel("0x00000000")
		ov.addWidget(self.matname_hash_label, 0, 3)
		ov.addWidget(QLabel("mmtrPath"), 1, 0)
		self.mmtr_edit = QLineEdit()
		self.mmtr_edit.textChanged.connect(self._on_mmtr_changed)
		ov.addWidget(self.mmtr_edit, 1, 1, 1, 3)
		ov.addWidget(QLabel("ShaderType"), 2, 0)
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
		self.flags_group = QGroupBox("Alpha Flags")
		fg = QGridLayout(self.flags_group)
		self._flags1_names = [
			"BaseTwoSideEnable","BaseAlphaTestEnable","ShadowCastDisable","VertexShaderUsed",
			"EmissiveUsed","TessellationEnable","EnableIgnoreDepth","AlphaMaskUsed",
			"ForcedTwoSideEnable","TwoSideEnable"
		]
		self.flags1_checks = [QCheckBox(n) for n in self._flags1_names]
		for i, cb in enumerate(self.flags1_checks):
			cb.stateChanged.connect(self._on_flags_changed)
			fg.addWidget(cb, 0 + (i // 5), i % 5)
		fg.addWidget(QLabel("Tessellation"), 2, 0)
		self.tess_spin = QSpinBox()
		self.tess_spin.setRange(0, 63)
		self.tess_spin.valueChanged.connect(self._on_flags_changed)
		fg.addWidget(self.tess_spin, 2, 1)
		fg.addWidget(QLabel("Phong"), 2, 2)
		self.phong_spin = QSpinBox()
		self.phong_spin.setRange(0, 255)
		self.phong_spin.valueChanged.connect(self._on_flags_changed)
		fg.addWidget(self.phong_spin, 2, 3)
		self._flags2_names = [
			"RoughTransparentEnable","ForcedAlphaTestEnable","AlphaTestEnable","SSSProfileUsed",
			"EnableStencilPriority","RequireDualQuaternion","PixelDepthOffsetUsed","NoRayTracing"
		]
		self.flags2_checks = [QCheckBox(n) for n in self._flags2_names]
		for i, cb in enumerate(self.flags2_checks):
			cb.stateChanged.connect(self._on_flags_changed)
			fg.addWidget(cb, 3 + (i // 4), i % 4)
		ov.addWidget(self.flags_group, 3, 0, 1, 4)
		self.tabs.addTab(overview, "Overview")

		textures_tab = QWidget()
		tg = QGridLayout(textures_tab)
		self.textures_table = QTableWidget(0, 2)
		self.textures_table.setHorizontalHeaderLabels(["Type", "Path"])
		self.textures_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
		self.textures_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
		self.textures_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.textures_table.itemChanged.connect(self._on_texture_changed)
		tg.addWidget(self.textures_table, 0, 0, 1, 3)
		self.tex_add_btn = QPushButton("Add")
		self.tex_del_btn = QPushButton("Delete")
		self.tex_add_btn.clicked.connect(self._on_add_texture)
		self.tex_del_btn.clicked.connect(self._on_delete_texture)
		tg.addWidget(self.tex_add_btn, 1, 1)
		tg.addWidget(self.tex_del_btn, 1, 2)
		self.tabs.addTab(textures_tab, "Textures")

		params_tab = QWidget()
		pg = QGridLayout(params_tab)
		self.params_info_label = QLabel("")
		self.params_info_label.setWordWrap(True)
		pg.addWidget(self.params_info_label, 0, 0, 1, 3)
		self.params_table = QTableWidget(0, 7)
		self.params_table.setHorizontalHeaderLabels(["Name", "CompCount", "X", "Y", "Z", "W", "Color"])
		self.params_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
		self.params_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
		for c in range(2, 7): self.params_table.horizontalHeader().setSectionResizeMode(c, QHeaderView.ResizeToContents)
		self.params_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.params_table.itemChanged.connect(self._on_param_changed)
		self.params_table.itemClicked.connect(self._on_param_clicked)
		pg.addWidget(self.params_table, 1, 0, 1, 3)
		self.par_add_btn = QPushButton("Add")
		self.par_add_above_btn = QPushButton("Add Above")
		self.par_del_btn = QPushButton("Delete")
		self.par_add_btn.clicked.connect(self._on_add_param)
		self.par_add_above_btn.clicked.connect(self._on_add_param_above)
		self.par_del_btn.clicked.connect(self._on_delete_param)
		pg.addWidget(self.par_add_btn, 2, 0)
		pg.addWidget(self.par_add_above_btn, 2, 1)
		pg.addWidget(self.par_del_btn, 2, 2)
		self.tabs.addTab(params_tab, "Parameters")

		gpbf_tab = QWidget()
		gg = QGridLayout(gpbf_tab)
		self.gpbf_table = QTableWidget(0, 2)
		self.gpbf_table.setHorizontalHeaderLabels(["Name", "Data"])
		self.gpbf_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.gpbf_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
		self.gpbf_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.gpbf_table.itemChanged.connect(self._on_gpbf_changed)
		gg.addWidget(self.gpbf_table, 0, 0, 1, 3)
		self.gpbf_add_btn = QPushButton("Add")
		self.gpbf_del_btn = QPushButton("Delete")
		self.gpbf_add_btn.clicked.connect(self._on_add_gpbf)
		self.gpbf_del_btn.clicked.connect(self._on_delete_gpbf)
		gg.addWidget(self.gpbf_add_btn, 1, 1)
		gg.addWidget(self.gpbf_del_btn, 1, 2)
		self.tabs.addTab(gpbf_tab, "GPU Buffers")

		self.texids_tab = QWidget()
		tx = QGridLayout(self.texids_tab)
		tx.addWidget(QLabel("TexID Count"), 0, 0)
		self.texid_count_spin = QSpinBox()
		self.texid_count_spin.setRange(0, 2048)
		self.texid_count_spin.valueChanged.connect(self._on_texid_count_changed)
		tx.addWidget(self.texid_count_spin, 0, 1)
		self.texids_table = QTableWidget(0, 2)
		self.texids_table.setHorizontalHeaderLabels(["Texture IDs", "Uknown IDs (?)"])
		self.texids_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
		self.texids_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
		self.texids_table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
		self.texids_table.itemChanged.connect(self._on_texids_changed)
		tx.addWidget(self.texids_table, 1, 0, 1, 3)
		self.texids_tab_idx = self.tabs.addTab(self.texids_tab, "Tex IDs")

		self.materials_table.itemSelectionChanged.connect(self._on_select_material)

	def _populate(self):
		m = self.handler.mdf
		if m is None:
			raise RuntimeError("MdfViewer: handler.mdf is None")
		self.version_edit.blockSignals(True)
		self.version_edit.setText(str(m.header.version))
		self.version_edit.blockSignals(False)
		self._refresh_materials_list()
		self.materials_table.selectRow(0)
		self._current_index = 0
		self._populate_details(0)

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
		m.header.version = int(text)
		self.modified = True
		if self.materials_table.currentRow() >= 0:
			self._refresh_material_row(self.materials_table.currentRow())

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
		self._update_params_info(m.materials[i])
	def _on_flags_changed(self, *_):
		rows = self.materials_table.selectionModel().selectedRows()
		m = self.handler.mdf
		if not m or not rows:
			return
		i = rows[0].row()
		h = m.materials[i].header
		alpha = 0
		for bit, cb in enumerate(self.flags1_checks):
			if cb.isChecked():
				alpha |= (1 << bit)
		alpha |= (self.tess_spin.value() & 0x3F) << 10
		alpha |= (self.phong_spin.value() & 0xFF) << 16
		for bit, cb in enumerate(self.flags2_checks):
			if cb.isChecked():
				alpha |= (1 << (24 + bit))
		h.alpha_flags = alpha
		self.modified = True

	def _update_flags_ui(self, h):
		alpha = int(getattr(h, 'alpha_flags', 0))
		for bit, cb in enumerate(self.flags1_checks):
			cb.blockSignals(True)
			cb.setChecked(bool((alpha >> bit) & 1))
			cb.blockSignals(False)
		self.tess_spin.blockSignals(True)
		self.tess_spin.setValue((alpha >> 10) & 0x3F)
		self.tess_spin.blockSignals(False)
		self.phong_spin.blockSignals(True)
		self.phong_spin.setValue((alpha >> 16) & 0xFF)
		self.phong_spin.blockSignals(False)
		for bit, cb in enumerate(self.flags2_checks):
			cb.blockSignals(True)
			cb.setChecked(bool((alpha >> (24 + bit)) & 1))
			cb.blockSignals(False)

	def _on_mmtr_changed(self, text: str):
		m = self.handler.mdf
		idx = self._get_current_index()
		if 0 <= idx < len(m.materials):
			m.materials[idx].header.mmtr_path = text
			self.modified = True

	def _on_matname_changed(self, text: str):
		m = self.handler.mdf
		idx = self._get_current_index()
		if not m or not (0 <= idx < len(m.materials)):
			return
		h = m.materials[idx].header
		h.mat_name = text
		self.matname_hash_label.setText(f"0x{murmur3_hash_utf16le(text):08x}")
		self.modified = True

	def _get_current_index(self) -> int:
		rows = self.materials_table.selectionModel().selectedRows()
		if rows:
			self._current_index = rows[0].row()
		return self._current_index

	def _populate_details(self, mat_index: int):
		m = self.handler.mdf
		if not m or not (0 <= mat_index < len(m.materials)):
			self.mmtr_edit.setText("")
			self.textures_table.setRowCount(0)
			self.params_table.setRowCount(0)
			self.gpbf_table.setRowCount(0)
			return
		md = m.materials[mat_index]
		self.mmtr_edit.blockSignals(True)
		self.mmtr_edit.setText(md.header.mmtr_path)
		self.mmtr_edit.blockSignals(False)
		self.matname_edit.blockSignals(True)
		self.matname_edit.setText(md.header.mat_name)
		self.matname_edit.blockSignals(False)
		self.matname_hash_label.setText(f"0x{murmur3_hash_utf16le(md.header.mat_name):08x}")
		self.shader_combo.blockSignals(True)
		sh = int(getattr(md.header, 'shader_type', 0))
		if 0 <= sh < self.shader_combo.count():
			self.shader_combo.setCurrentIndex(sh)
		self.shader_combo.blockSignals(False)
		self._update_flags_ui(md.header)

		self.textures_table.blockSignals(True)
		self.textures_table.setRowCount(len(md.textures))
		for r, t in enumerate(md.textures):
			self.textures_table.setItem(r, 0, QTableWidgetItem(t.tex_type))
			self.textures_table.setItem(r, 1, QTableWidgetItem(t.tex_path))
		self.textures_table.blockSignals(False)

		self.params_table.blockSignals(True)
		self.params_table.setRowCount(len(md.parameters))
		for r, p in enumerate(md.parameters):
			self._refresh_param_row_internal(r, p)
		self.params_table.blockSignals(False)
		self._update_params_info(md)
		self._apply_layercolor_spans(md)

		self.gpbf_table.blockSignals(True)
		self.gpbf_table.setRowCount(len(md.gpu_buffers))
		for r, (n,d) in enumerate(md.gpu_buffers):
			self.gpbf_table.setItem(r, 0, QTableWidgetItem(n.name))
			self.gpbf_table.setItem(r, 1, QTableWidgetItem(d.name))
		self.gpbf_table.blockSignals(False)

		if int(getattr(self.handler.mdf, 'file_version', 0)) >= 31:
			self.tabs.setTabVisible(self.texids_tab_idx, True)
			self.texids_table.blockSignals(True)
			self.texid_count_spin.blockSignals(True)
			count = int(getattr(md.header, 'texID_count', 0))
			self.texid_count_spin.setValue(count)
			rows = count
			self.texids_table.setRowCount(rows)
			for i in range(rows):
				counts, elems = (md.tex_id_arrays[i] if i < len(md.tex_id_arrays) else ([], []))
				self.texids_table.setItem(i, 0, QTableWidgetItem(",".join(str(v) for v in counts)))
				self.texids_table.setItem(i, 1, QTableWidgetItem(",".join(str(v) for v in elems)))
			self.texid_count_spin.blockSignals(False)
			self.texids_table.blockSignals(False)
		else:
			self.tabs.setTabVisible(self.texids_tab_idx, False)

	def _on_add_texture(self):
		from .mdf_file import TexHeader
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		md.textures.append(TexHeader())
		self._populate_details(i)
		self._refresh_material_row(i)
		self.modified = True

	def _on_delete_texture(self):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		sel = sorted({r.row() for r in self.textures_table.selectedIndexes()}, reverse=True)
		for r in sel:
			if 0 <= r < len(md.textures):
				del md.textures[r]
		self._populate_details(i)
		self._refresh_material_row(i)
		if sel:
			self.modified = True

	def _on_add_param(self):
		from .mdf_file import ParamHeader
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		ph = ParamHeader()
		ph.name = "Param"
		ph.component_count = 1
		ph.parameter = (0.0, 0.0, 0.0, 0.0)
		ph.gap_size = 0
		row_idx = self.params_table.currentRow()
		if 0 <= row_idx < len(md.parameters):
			before = md.parameters[max(0, row_idx-1)].name if row_idx > 0 else ""
			is_layer, cidx = self._is_layercolor_name(before)
			if is_layer and cidx is not None and cidx < 2:
				ph.name = ["LayerColor_Red","LayerColor_Green","LayerColor_Blue"][cidx+1]
		md.parameters.append(ph)
		self._populate_details(i)
		self._refresh_material_row(i)
		self.modified = True

	def _on_add_param_above(self):
		from .mdf_file import ParamHeader
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		insert_at = 0
		if self.params_table.currentRow() >= 0:
			insert_at = max(0, min(self.params_table.currentRow(), len(md.parameters)))
		ph = ParamHeader()
		ph.name = "Param"
		ph.component_count = 1
		ph.parameter = (0.0, 0.0, 0.0, 0.0)
		ph.gap_size = 0
		md.parameters.insert(insert_at, ph)
		self._populate_details(i)
		self._refresh_material_row(i)
		self.modified = True

	def _update_params_info(self, md):
		names = [(p.name or "") for p in md.parameters]
		msg: list[str] = []
		segments = []
		cur_seg: list[int] = []
		for n in names:
			ln = n.lower()
			if ln.startswith("layercolor_"):
				idx = 0 if "red" in ln else 1 if "green" in ln else 2 if "blue" in ln else -1
				cur_seg.append(idx)
			else:
				if cur_seg:
					segments.append(cur_seg)
					cur_seg = []
		if cur_seg:
			segments.append(cur_seg)

		seen_colors = set()
		for seg in segments:
			for idx in seg:
				if idx in (0,1,2):
					key = idx
					if key in seen_colors:
						msg.append("LayerColor: Only one of each (Red, Green, Blue) is allowed.")
					else:
						seen_colors.add(key)
		if len(segments) > 1:
			msg.append("LayerColor: Only one RGB sequence is allowed.")
		for seg in segments:
			prev_idx = None
			for idx in seg:
				if idx < 0:
					continue
				if prev_idx is None:
					prev_idx = idx
					continue
				if prev_idx == 0 and idx == 2:
					prev_idx = idx
					continue
				if idx > prev_idx and (idx - prev_idx) > 1:
					msg.append("LayerColor: Only Green may be between Red and Blue.")
				prev_idx = idx

		if msg:
			self.params_info_label.setStyleSheet("color:#c00;font-weight:600;")
			self.params_info_label.setText("\n".join(sorted(set(msg))))
		else:
			self.params_info_label.setStyleSheet("")
			self.params_info_label.setText("")

	def _apply_layercolor_spans(self, md):
		rows = self.params_table.rowCount()
		if rows <= 0:
			return
		try:
			self.params_table.clearSpans()
		except Exception:
			pass
		start = None
		end = None
		for r in range(rows+1):
			if r < rows:
				name = md.parameters[r].name or ""
				is_layer = name.lower().startswith("layercolor_")
			else:
				is_layer = False
			if is_layer and start is None:
				start = r
			elif (not is_layer or r == rows) and start is not None:
				end = r - 1
				if end >= start:
					span_len = end - start + 1
					R, G, B = 1.0, 1.0, 1.0
					for rr in range(start, end + 1):
						nl = (md.parameters[rr].name or "").lower()
						val = float(md.parameters[rr].parameter[0]) if md.parameters[rr].component_count >= 1 else 0.0
						if nl.startswith("layercolor_"):
							if "red" in nl: R = val
							elif "green" in nl: G = val
							elif "blue" in nl: B = val
					qcol = QColor(int(max(0.0, min(1.0, R)) * 255), int(max(0.0, min(1.0, G)) * 255), int(max(0.0, min(1.0, B)) * 255))
					if span_len > 1:
						self.params_table.setSpan(start, 6, span_len, 1)
					item = self.params_table.item(start, 6)
					if item is None:
						item = QTableWidgetItem("")
						self.params_table.setItem(start, 6, item)
					item.setBackground(qcol)
					item.setToolTip(f"LayerColor RGB: {R:.3f},{G:.3f},{B:.3f} (click to change)")
					item.setFlags((item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled) & ~Qt.ItemIsEditable)
				start = None

	def _is_layercolor_name(self, name: str) -> tuple[bool, int | None]:
		ln = (name or "").lower()
		if not ln.startswith("layercolor_"):
			return False, None
		if "red" in ln: return True, 0
		if "green" in ln: return True, 1
		if "blue" in ln: return True, 2
		return True, None

	def _on_delete_param(self):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		sel = sorted({r.row() for r in self.params_table.selectedIndexes()}, reverse=True)
		for r in sel:
			if 0 <= r < len(md.parameters):
				del md.parameters[r]
		self._populate_details(i)
		self._refresh_material_row(i)
		if sel:
			self.modified = True

	def _on_add_gpbf(self):
		from .mdf_file import GpbfHeader
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		md.gpu_buffers.append((GpbfHeader("Name"), GpbfHeader("Data")))
		self._populate_details(i)
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
		self._populate_details(i)
		if sel:
			self.modified = True

	def _on_select_material(self):
		rows = self.materials_table.selectionModel().selectedRows()
		if not rows:
			return
		self._populate_details(rows[0].row())

	def _on_texid_count_changed(self, val: int):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		md.header.texID_count = max(0, int(val))
		while len(md.tex_id_arrays) < md.header.texID_count:
			md.tex_id_arrays.append(([], []))
		while len(md.tex_id_arrays) > md.header.texID_count:
			md.tex_id_arrays.pop()
		self._populate_details(i)
		self.modified = True

	def _on_texids_changed(self, item):
		m = self.handler.mdf
		rows = self.materials_table.selectionModel().selectedRows()
		if not m or not rows:
			return
		i = rows[0].row()
		md = m.materials[i]
		r = item.row()
		c = item.column()
		if not (0 <= r < len(md.tex_id_arrays)):
			return
		counts, elems = md.tex_id_arrays[r]
		text = item.text() or ""
		parts = [p.strip() for p in text.split(',') if p.strip()]
		vals = []
		for p in parts:
			try:
				vals.append(int(p))
			except ValueError:
				return
		if c == 0:
			md.tex_id_arrays[r] = (vals, elems)
		elif c == 1:
			md.tex_id_arrays[r] = (counts, vals)
		self.modified = True

	def _on_texture_changed(self, item):
		m = self.handler.mdf
		mi = self._get_current_index()
		if not (0 <= mi < len(m.materials)):
			return
		tmi = item.row()
		if not (0 <= tmi < len(m.materials[mi].textures)):
			return
		tex = m.materials[mi].textures[tmi]
		val = item.text()
		if item.column() == 0:
			tex.tex_type = val
		else:
			tex.tex_path = val
		self.modified = True

	def _on_param_changed(self, item):
		m = self.handler.mdf
		mi = self._get_current_index()
		if not (0 <= mi < len(m.materials)):
			return
		pi = item.row()
		if not (0 <= pi < len(m.materials[mi].parameters)):
			return
		p = m.materials[mi].parameters[pi]
		val = item.text()
		c = item.column()
		if c == 0:
				p.name = val
				self._update_params_info(m.materials[mi])
				self._apply_layercolor_spans(m.materials[mi])
		elif c == 1:
			new_cc = max(1, min(4, int(val)))
			old_cc = p.component_count
			if new_cc != old_cc:
				p.component_count = new_cc
				arr = list(p.parameter)
				for i in range(4):
					if i >= new_cc:
						arr[i] = 0.0
				p.parameter = tuple(arr)
				self._refresh_param_row(mi, pi)
				self._update_params_info(m.materials[mi])
				self._apply_layercolor_spans(m.materials[mi])
		elif c >= 2 and c <= 5:
			idx = c - 2
			if idx < 0 or idx >= p.component_count:
				return
			arr = list(p.parameter)
			arr[idx] = float(val)
			p.parameter = tuple(arr)
			name_lower = (p.name or "").lower()
			if (name_lower.endswith("color") or name_lower.endswith("color1") or name_lower.endswith("color2") 
				or name_lower.endswith("color3")) and p.component_count in (3, 4):
				self._refresh_param_row(mi, pi)
				self._apply_layercolor_spans(m.materials[mi])
			md_local = m.materials[mi]
			if (p.name or "").lower().startswith("layercolor_"):
				self._apply_layercolor_spans(md_local)
			self._update_params_info(md_local)
		elif c == 6:
			pass
		self.modified = True

	def _refresh_param_row(self, mat_index: int, row: int):
		m = self.handler.mdf
		p = m.materials[mat_index].parameters[row]
		self.params_table.blockSignals(True)
		self._refresh_param_row_internal(row, p)
		self.params_table.blockSignals(False)

	def _refresh_param_row_internal(self, row: int, p):
		self.params_table.setItem(row, 0, QTableWidgetItem(p.name))
		self.params_table.setItem(row, 1, QTableWidgetItem(str(p.component_count)))
		x, y, z, w = p.parameter
		values = [x, y, z, w]
		for i in range(4):
			item = QTableWidgetItem("")
			if 0 <= i < p.component_count:
				item.setText(str(values[i]))
				item.setFlags(item.flags() | Qt.ItemIsEditable | Qt.ItemIsSelectable | Qt.ItemIsEnabled)
			else:
				item.setText("")
				item.setFlags((item.flags() | Qt.ItemIsEnabled) & ~(Qt.ItemIsEditable | Qt.ItemIsSelectable))
			self.params_table.setItem(row, 2 + i, item)
		color_item = QTableWidgetItem("")
		name_lower = (p.name or "").lower()
		is_color = (name_lower.endswith("color") or name_lower.endswith("color1") or name_lower.endswith("color2") 
				  or name_lower.endswith("color3")) and p.component_count in (3, 4)
		if is_color:
			rgb = [values[0], values[1], values[2]]
			alpha = values[3] if p.component_count == 4 else 1.0
			def clamp01(v):
				try:
					return max(0.0, min(1.0, float(v)))
				except Exception:
					return 0.0
			R = int(clamp01(rgb[0]) * 255)
			G = int(clamp01(rgb[1]) * 255)
			B = int(clamp01(rgb[2]) * 255)
			A = int(clamp01(alpha) * 255)
			qcol = QColor(R, G, B, A)
			color_item.setBackground(qcol)
			if p.component_count == 4:
				color_item.setToolTip(f"RGBA: {R},{G},{B},{A}")
			else:
				color_item.setToolTip(f"RGB: {R},{G},{B}")
			color_item.setFlags((color_item.flags() | Qt.ItemIsSelectable | Qt.ItemIsEnabled) & ~Qt.ItemIsEditable)
		else:
			color_item.setFlags((color_item.flags() | Qt.ItemIsEnabled) & ~(Qt.ItemIsEditable | Qt.ItemIsSelectable))
			color_item.setText("")
		self.params_table.setItem(row, 6, color_item)

	def _on_param_clicked(self, item):
		if item.column() != 6:
			return
		m = self.handler.mdf
		mi = self._get_current_index()
		if not (0 <= mi < len(m.materials)):
			return
		pi = item.row()
		if not (0 <= pi < len(m.materials[mi].parameters)):
			return
		md = m.materials[mi]
		p = md.parameters[pi]
		name_lower = (p.name or "").lower()
		is_layer = name_lower.startswith("layercolor_")
		is_normal_color = (name_lower.endswith("color") or name_lower.endswith("color1") or name_lower.endswith("color2") or name_lower.endswith("color3")) and p.component_count in (3, 4)

		if is_normal_color and not is_layer:
			x, y, z, w = p.parameter
			col = QColor(int(max(0.0, min(1.0, x)) * 255), int(max(0.0, min(1.0, y)) * 255), int(max(0.0, min(1.0, z)) * 255), int(max(0.0, min(1.0, w if p.component_count == 4 else 1.0)) * 255))
			dlg = QColorDialog(self)
			dlg.setOption(QColorDialog.ShowAlphaChannel, p.component_count == 4)
			dlg.setCurrentColor(col)
			if dlg.exec():
				picked = dlg.currentColor()
				if p.component_count == 4:
					p.parameter = (picked.redF(), picked.greenF(), picked.blueF(), picked.alphaF())
				else:
					p.parameter = (picked.redF(), picked.greenF(), picked.blueF(), 0.0)
				self.params_table.blockSignals(True)
				vx, vy, vz, vw = p.parameter
				self.params_table.setItem(pi, 2, QTableWidgetItem(str(vx)))
				self.params_table.setItem(pi, 3, QTableWidgetItem(str(vy)))
				self.params_table.setItem(pi, 4, QTableWidgetItem(str(vz)))
				if p.component_count == 4:
					self.params_table.setItem(pi, 5, QTableWidgetItem(str(vw)))
				self.params_table.blockSignals(False)
				self._refresh_param_row(mi, pi)
				self.modified = True
			return

		if is_layer:
			rows = self.params_table.rowCount()
			start = pi
			while start > 0 and (md.parameters[start-1].name or "").lower().startswith("layercolor_"):
				start -= 1
			end = pi
			while end + 1 < rows and (md.parameters[end+1].name or "").lower().startswith("layercolor_"):
				end += 1
			R = G = B = 1.0
			for rr in range(start, end + 1):
				nl = (md.parameters[rr].name or "").lower()
				val = float(md.parameters[rr].parameter[0]) if md.parameters[rr].component_count >= 1 else 0.0
				if "red" in nl: R = val
				elif "green" in nl: G = val
				elif "blue" in nl: B = val
			col = QColor(int(max(0.0, min(1.0, R)) * 255), int(max(0.0, min(1.0, G)) * 255), int(max(0.0, min(1.0, B)) * 255))
			dlg = QColorDialog(self)
			dlg.setOption(QColorDialog.ShowAlphaChannel, False)
			dlg.setCurrentColor(col)
			if dlg.exec():
				picked = dlg.currentColor()
				for rr in range(start, end + 1):
					nl = (md.parameters[rr].name or "").lower()
					if "red" in nl:
						md.parameters[rr].parameter = (picked.redF(), 0.0, 0.0, 0.0)
					elif "green" in nl:
						md.parameters[rr].parameter = (picked.greenF(), 0.0, 0.0, 0.0)
					elif "blue" in nl:
						md.parameters[rr].parameter = (picked.blueF(), 0.0, 0.0, 0.0)
				for rr in range(start, end + 1):
					self._refresh_param_row(mi, rr)
				self._apply_layercolor_spans(md)
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
				h.mat_name = val
			elif c == 1:
				h.shader_type = int(val)
			elif c == 2:
				h.tex_count = int(val)
			elif c == 3:
				h.param_count = int(val)
			elif c == 4:
				h.alpha_flags = int(val)
			self.modified = True
		except ValueError:
			pass

	def _on_add_material(self):
		m = self.handler.mdf
		if not m:
			return
		from .mdf_file import MatData
		m.materials.append(MatData())
		self.materials_table.blockSignals(True)
		r = self.materials_table.rowCount()
		self.materials_table.insertRow(r)
		for c, txt in enumerate(["", "0", "0", "0", "0"]):
			self.materials_table.setItem(r, c, QTableWidgetItem(txt))
		self.materials_table.blockSignals(False)
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
			self.modified = True

