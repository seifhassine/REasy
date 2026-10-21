from __future__ import annotations
import os
from pathlib import Path
from typing import List
from contextlib import contextmanager

from PySide6.QtCore import (
	QT_TRANSLATE_NOOP, QEvent, Qt, QPoint, QTimer, QSortFilterProxyModel, QRegularExpression, QStringListModel, QSize,
)


from PySide6.QtGui import QStandardItemModel, QStandardItem

from PySide6.QtWidgets import (
	QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
	QFileDialog, QLineEdit, QCheckBox, QComboBox, QInputDialog,
	QMessageBox, QTreeView, QListView, QAbstractItemView, QMenu, QApplication,
	QSlider, QStackedWidget, QToolButton, QWidget, QFrame, QSplitter, QButtonGroup
)

lazy from settings import load_settings
lazy from ui.breadcrumbs import breadcrumb_separator
lazy from ui.editor_widgets import editor_strip, search_field, styled_label, tool_button, toolbar_separator
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors
lazy from app_config import GAMES

lazy from file_handlers.pak import scan_pak_files
lazy from file_handlers.pak.reader import CachedPakReader
lazy from ui.project_manager.constants import BASE_DIR
lazy from ui.project_manager.pak_file_lists import (
	choose_pak_list_file, find_suggested_pak_list_paths_for_directory,
	game_for_pak_list_path, read_pak_list_file,
)
lazy from ui.pak_icon_view import PakIconEntry, PakIconModel, PakThumbnailProvider, thumbnail_cache_directory
lazy from utils.resource_file_utils import resource_context_for_app
lazy from file_handlers.pak.utils import any_pak_modded
lazy from ui.extraction_progress_dialog import ExtractionProgressDialog
lazy from threading import Thread


DUMP_VALID_PATHS_TITLE = QT_TRANSLATE_NOOP("PakBrowserDialog", "Dump Valid Paths")
INDEX_FAILED_TITLE = QT_TRANSLATE_NOOP("PakBrowserDialog", "Index failed")
UNKNOWN_PATH_PREFIX = "__Unknown/"


class PakBrowserDialog(QDialog):
	_ITEM_EXTRACT_PATH_ROLE = Qt.UserRole + 32
	_ITEM_IS_DIR_ROLE = Qt.UserRole + 33

	def __init__(self, parent=None):
		super().__init__(parent)
		self.setWindowTitle(self.tr("PAK Browser"))
		self.resize(960, 660)
		lay = QVBoxLayout(self)
		lay.setContentsMargins(0, 0, 0, 0)
		lay.setSpacing(0)

		settings = getattr(parent, 'settings', None) or load_settings()

		toolbar, top = editor_strip("toolbar", (8, 6, 8, 6), 6)
		lay.addWidget(toolbar)

		self.dir_edit = QLineEdit(self)
		self.dir_edit.setPlaceholderText(self.tr("Game directory (optional, for scan)"))
		top.addWidget(self.dir_edit, 1)
		top.addWidget(tool_button("folder_open", self.tr("Browse…"), self._choose_dir, parent=self))
		top.addWidget(QPushButton(line_icon("refresh"), self.tr("Scan"), clicked=self._scan_dir))
		top.addWidget(toolbar_separator(18))
		top.addWidget(QLabel(self.tr("Game:")))
		self.game_combo = QComboBox(self)
		self.game_combo.addItems(GAMES)
		initial_game = str(
			getattr(parent, "current_game", "")
			or settings.get("game_version", "")
			or GAMES[0]
		)
		if initial_game in GAMES:
			self.game_combo.setCurrentText(initial_game)
		top.addWidget(self.game_combo)

		body = QVBoxLayout()
		body.setContentsMargins(8, 8, 8, 8)
		body.setSpacing(6)
		lay.addLayout(body, 1)

		self.mod_warning = QFrame(self)
		self.mod_warning.setProperty("banner", "danger")
		warning_row = QHBoxLayout(self.mod_warning)
		warning_row.setContentsMargins(0, 0, 0, 0)
		warning_row.setSpacing(8)
		warning_icon = QLabel()
		warning_icon.setPixmap(line_icon("warning", current_colors()["danger"]).pixmap(16, 16))
		warning_row.addWidget(warning_icon, 0, Qt.AlignTop)
		warning_text = QLabel(self.tr(
			"Modded PAKs detected in the game folder (mod payloads or "
			"invalidated entries). Please disable your mods and rescan."))
		warning_text.setWordWrap(True)
		warning_row.addWidget(warning_text, 1)
		self.mod_warning.hide()
		body.addWidget(self.mod_warning)

		splitter = QSplitter(Qt.Vertical, self)
		splitter.setChildrenCollapsible(False)
		body.addWidget(splitter, 1)

		pak_panel = QWidget()
		pak_layout = QVBoxLayout(pak_panel)
		pak_layout.setContentsMargins(0, 0, 0, 4)
		pak_layout.setSpacing(4)
		row2 = QHBoxLayout()
		row2.setSpacing(2)
		pak_title = styled_label(self.tr("PAK files:"), heading="panel")
		row2.addWidget(pak_title)
		row2.addStretch(1)
		row2.addWidget(tool_button("list", self.tr("Load .list…"), self._load_list_file, text=self.tr("Load .list…"), parent=self))
		row2.addWidget(tool_button("add", self.tr("Add PAK…"), self._add_paks, text=self.tr("Add PAK…"), parent=self))
		row2.addWidget(toolbar_separator(18))
		self._remove_pak_btn = tool_button("remove", self.tr("Remove"), self._remove_paks, parent=self)
		self._move_up_btn = tool_button("arrow_up", self.tr("Move Up"), self._move_selected_up, parent=self)
		self._move_down_btn = tool_button("arrow_down", self.tr("Move Down"), self._move_selected_down, parent=self)
		for button in (self._remove_pak_btn, self._move_up_btn, self._move_down_btn):
			button.setEnabled(False)
			row2.addWidget(button)
		pak_layout.addLayout(row2)

		self.pak_list = QListWidget(self)
		self.pak_list.itemSelectionChanged.connect(self._update_pak_buttons)
		pak_layout.addWidget(self.pak_list, 1)
		splitter.addWidget(pak_panel)

		browser_panel = QWidget()
		browser_layout = QVBoxLayout(browser_panel)
		browser_layout.setContentsMargins(0, 4, 0, 0)
		browser_layout.setSpacing(4)
		mid = QHBoxLayout()
		mid.setSpacing(8)
		browser_layout.addLayout(mid)
		self.filter_edit = search_field(QLineEdit(self))
		self.filter_edit.setPlaceholderText(self.tr("Search (supports regex)"))
		self._filter_timer = QTimer(self)
		self._filter_timer.setSingleShot(True)
		self._filter_timer.setInterval(180)
		self._filter_timer.timeout.connect(self._apply_filter_now)
		self.filter_edit.textChanged.connect(self._on_filter_text_changed)
		mid.addWidget(self.filter_edit, 1)
		self.show_unknown_cb = QCheckBox(self.tr("Include unknown entries"))
		self.show_unknown_cb.setChecked(False)
		self.show_unknown_cb.toggled.connect(self._on_show_unknown_toggled)
		mid.addWidget(self.show_unknown_cb)
		
		self.show_only_valid_cb = QCheckBox(self.tr("Show only valid files"))
		self.show_only_valid_cb.setChecked(False)
		self.show_only_valid_cb.toggled.connect(self._on_show_only_valid_toggled)
		mid.addWidget(self.show_only_valid_cb)

		self.include_stubs_cb = QCheckBox(self.tr("Include DLC stubs"))
		self.include_stubs_cb.setToolTip(self.tr(
			"Also treat redirect stubs as valid files: base PAK entries the game redirects "
			"to a DLC PAK that is not installed. Opening or extracting one reads the stub's own data."
		))
		self.include_stubs_cb.setChecked(False)
		mid.addWidget(self.include_stubs_cb)

		self.icon_size_slider = QSlider(Qt.Horizontal, self)
		self.icon_size_slider.setRange(48, 160)
		self.icon_size_slider.setValue(88)
		self.icon_size_slider.setTracking(False)
		self.icon_size_slider.setFixedWidth(96)
		self.icon_size_slider.setToolTip(self.tr("Icon size"))
		self.icon_size_slider.valueChanged.connect(self._set_icon_size)
		slider_policy = self.icon_size_slider.sizePolicy()
		slider_policy.setRetainSizeWhenHidden(True)
		self.icon_size_slider.setSizePolicy(slider_policy)
		self.icon_size_slider.hide()
		mid.addWidget(self.icon_size_slider)
		view_buttons = QHBoxLayout()
		view_buttons.setSpacing(0)
		tree_mode_btn = tool_button("list", self.tr("Switch to tree view"), parent=self)
		self.view_mode_btn = tool_button("grid", self.tr("Switch to icons view"), parent=self)
		self._view_mode_group = QButtonGroup(self)
		for button in (tree_mode_btn, self.view_mode_btn):
			button.setCheckable(True)
			self._view_mode_group.addButton(button)
			view_buttons.addWidget(button)
		tree_mode_btn.setChecked(True)
		self.view_mode_btn.toggled.connect(self._set_view_mode)
		mid.addLayout(view_buttons)

		self.tree = QTreeView(self)
		self.tree.setUniformRowHeights(True)
		self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
		self.tree.setHeaderHidden(True)
		self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
		self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
		self.tree.viewport().setContextMenuPolicy(Qt.CustomContextMenu)
		self.tree.viewport().customContextMenuRequested.connect(self._show_tree_context_menu)
		self.tree.doubleClicked.connect(self._open_tree_entry)
		self.icon_view = QListView(self)
		self.icon_view.setViewMode(QListView.IconMode)
		self.icon_view.setResizeMode(QListView.Adjust)
		self.icon_view.setMovement(QListView.Static)
		self.icon_view.setSelectionMode(QAbstractItemView.ExtendedSelection)
		self._set_icon_size(self.icon_size_slider.value())
		self.icon_view.setWordWrap(True)
		self.icon_view.viewport().setContextMenuPolicy(Qt.CustomContextMenu)
		self.icon_view.viewport().customContextMenuRequested.connect(self._show_icon_context_menu)
		self.icon_view.doubleClicked.connect(self._open_icon_entry)
		self._thumbnail_provider = PakThumbnailProvider(thumbnail_cache_directory(), settings, self)
		self._icon_model = PakIconModel(self._thumbnail_provider, self)
		self.icon_view.setModel(self._icon_model)
		self._thumbnail_timer = QTimer(self)
		self._thumbnail_timer.setSingleShot(True)
		self._thumbnail_timer.timeout.connect(self._request_visible_thumbnails)
		self.icon_view.verticalScrollBar().valueChanged.connect(self._schedule_visible_thumbnails)
		self.icon_view.horizontalScrollBar().valueChanged.connect(self._schedule_visible_thumbnails)
		self._icon_directory = ""
		self.view_stack = QStackedWidget(self)
		self.view_stack.addWidget(self.tree)
		self.view_stack.addWidget(self.icon_view)
		self._empty_hint = styled_label(self.tr(
			"Scan a game folder or add PAK files, then load a .list file to browse their contents."
		), self.view_stack, emptyState=True)
		self._empty_hint.setWordWrap(True)
		self._empty_hint.setAttribute(Qt.WA_TransparentForMouseEvents)
		self.view_stack.installEventFilter(self)
		self._crumbs = QFrame(self)
		self._crumbs.setObjectName("editorBreadcrumbs")
		self._crumbs.setFixedHeight(26)
		self._crumbs_layout = QHBoxLayout(self._crumbs)
		self._crumbs_layout.setContentsMargins(2, 0, 2, 0)
		self._crumbs_layout.setSpacing(0)
		self._crumbs.hide()
		browser_layout.addWidget(self._crumbs)
		browser_layout.addWidget(self.view_stack, 1)
		splitter.addWidget(browser_panel)
		splitter.setStretchFactor(0, 0)
		splitter.setStretchFactor(1, 1)
		splitter.setSizes([130, 420])
		self._tree_model = None
		self._flat_model: QStringListModel | None = None
		self._flat_model_valid_only: QStringListModel | None = None
		self._filter_proxy = QSortFilterProxyModel(self)
		self._filter_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
		self._filter_proxy.setFilterKeyColumn(0)


		out = QHBoxLayout()
		out.setSpacing(6)
		body.addLayout(out)
		out.addWidget(QLabel(self.tr("Output directory:")))
		self.out_edit = QLineEdit(self)
		out.addWidget(self.out_edit, 1)
		out.addWidget(tool_button("folder_open", self.tr("Choose…"), self._choose_out, parent=self))

		footer, actions = editor_strip("footer", (8, 6, 8, 6), 6)
		lay.addWidget(footer)
		self.loading_label = styled_label(parent=self, tone="muted")
		self.loading_label.setVisible(False)
		actions.addWidget(self.loading_label)
		actions.addStretch(1)
		self.dump_valid_btn = QPushButton(self.tr(DUMP_VALID_PATHS_TITLE), clicked=self._dump_valid_files)
		self.dump_valid_btn.setVisible(False)
		actions.addWidget(self.dump_valid_btn)
		actions.addWidget(QPushButton(self.tr("Extract All"), clicked=self._extract_all))
		extract_selected_btn = QPushButton(self.tr("Extract Selected"), clicked=self._extract_selected)
		extract_selected_btn.setProperty("variant", "primary")
		actions.addWidget(extract_selected_btn)

		self._all_manifest_paths: List[str] = []
		self._base_paths: List[str] = []
		self._cached_reader: CachedPakReader | None = None
		self._valid_paths: set[str] = set()
		self._cached_tree_model = None
		self._cached_show_valid = False
		self._cached_show_unknown = False
		self._cache_outdated = False
		self.game_combo.currentTextChanged.connect(self._on_resolution_changed)
		self.include_stubs_cb.toggled.connect(self._on_resolution_changed)
		self._scanned_root = ""
		self._loading_depth = 0

	def eventFilter(self, watched, event):
		if watched is self.view_stack and event.type() == QEvent.Resize:
			self._empty_hint.setGeometry(self.view_stack.rect())
		return super().eventFilter(watched, event)

	def _update_pak_buttons(self):
		selected = bool(self.pak_list.selectedItems())
		for button in (self._remove_pak_btn, self._move_up_btn, self._move_down_btn):
			button.setEnabled(selected)

	def _move_selected_up(self):
		self._move_selected(-1)

	def _move_selected_down(self):
		self._move_selected(+1)

	@contextmanager
	def _loading(self, message: str):
		self._loading_depth += 1
		if self._loading_depth == 1:
			self.loading_label.setText(message)
			self.loading_label.setVisible(True)
			QApplication.setOverrideCursor(Qt.WaitCursor)
			QApplication.processEvents()
		try:
			yield
		finally:
			self._loading_depth = max(0, self._loading_depth - 1)
			if self._loading_depth == 0:
				self.loading_label.setVisible(False)
				QApplication.restoreOverrideCursor()
				QApplication.processEvents()

	def _choose_dir(self):
		d = QFileDialog.getExistingDirectory(self, self.tr("Select Game Directory"))
		if d:
			self.dir_edit.setText(d)
			self._scan_dir()
			self._prompt_auto_list_from_directory(d)

	def _scan_dir(self):
		root = self.dir_edit.text().strip()
		if not root:
			QMessageBox.information(self, self.tr("Scan"), self.tr("Select a directory to scan."))
			return
		with self._loading(self.tr("Scanning PAK files...")):
			paks = scan_pak_files(root)

		if not paks:
			QMessageBox.information(self, self.tr("Scan"), self.tr("No .pak files found."))
			return

		normalized_root = os.path.normcase(os.path.abspath(root))
		if self._scanned_root and normalized_root != self._scanned_root:
			# File lists and their resolved cache belong to a specific game. Keeping
			# them across a folder switch makes the new reader resolve old-game paths.
			self._thumbnail_provider.cancel_pending()
			self._base_paths = []
			self._all_manifest_paths = []
			self._valid_paths = set()
			self._cached_reader = None
			self._icon_directory = ""
		self._scanned_root = normalized_root

		self._cache_outdated = True
		old_paks = [self.pak_list.item(i).text() for i in range(self.pak_list.count())]
		if paks == old_paks and self._cached_reader:

			self._refresh_index()
			return
		
		self.pak_list.clear()
		for p in paks:
			self.pak_list.addItem(p)
		self._refresh_index()

	def _on_resolution_changed(self, _value=None):
		self._thumbnail_provider.cancel_pending()
		self._cached_reader = None
		self._valid_paths = set()
		self._cache_outdated = True
		if self._selected_paks():
			with self._loading(self.tr("Applying game PAK rules...")):
				self._refresh_index()

	def _add_paks(self):
		files, _ = QFileDialog.getOpenFileNames(self, self.tr("Add PAK files"), filter=self.tr("PAK files (*.pak)"))
		for f in files:
			if not any(self.pak_list.item(i).text() == f for i in range(self.pak_list.count())):
				self.pak_list.addItem(f)
		if files:
			self._refresh_index()

	def _remove_paks(self):
		for it in self.pak_list.selectedItems():
			row = self.pak_list.row(it)
			self.pak_list.takeItem(row)
		self._refresh_index()

	def _move_selected(self, delta: int):
		idxs = sorted((self.pak_list.row(i) for i in self.pak_list.selectedItems()))
		if not idxs:
			return
		if delta < 0 and idxs[0] == 0:
			return
		if delta > 0 and idxs[-1] == self.pak_list.count() - 1:
			return
		for idx in (idxs if delta < 0 else reversed(idxs)):
			it = self.pak_list.takeItem(idx)
			self.pak_list.insertItem(idx + delta, it)
			it.setSelected(True)
		if self._cached_reader:
			self._cache_outdated = True

	def _choose_out(self):
		d = QFileDialog.getExistingDirectory(self, self.tr("Select Output Directory"))
		if d:
			self.out_edit.setText(d)
	
	def _on_show_unknown_toggled(self, _checked: bool):
		with self._loading(self.tr("Updating file list...")):
			try:
				if self.show_unknown_cb.isChecked():
					self._ensure_cache(full=True)
			except Exception as e:
				QMessageBox.critical(self, self.tr(INDEX_FAILED_TITLE), str(e))
				return
			self._recompute_display()

	def _on_show_only_valid_toggled(self, _checked: bool):
		with self._loading(self.tr("Updating file list...")):
			try:
				if self.show_only_valid_cb.isChecked():
					self._ensure_cache(validate=True)
			except Exception as e:
				QMessageBox.critical(self, self.tr(INDEX_FAILED_TITLE), str(e))
				return
			self._recompute_display()

	def _on_filter_text_changed(self, _=None):
		self._filter_timer.start()

	def _apply_filter(self):
		self._apply_filter_now()

	def _apply_filter_now(self):
		text = self.filter_edit.text().strip()
		self._empty_hint.setVisible(not self._all_manifest_paths)

		if not self._all_manifest_paths:
			model = QStandardItemModel()
			model.setHorizontalHeaderLabels([self.tr("Paths")])
			self.tree.setModel(model)
			self._tree_model = model
			self._rebuild_icon_model()
			return
		
		if text:
			source = self._flat_model_valid_only if self.show_only_valid_cb.isChecked() else self._flat_model
			if source is None:
				model = QStandardItemModel()
				model.setHorizontalHeaderLabels([self.tr("Search Results")])
				self.tree.setModel(model)
				self._tree_model = model
				return
			self._filter_proxy.setSourceModel(source)
			pat = QRegularExpression(text)
			if not pat.isValid():
				pat = QRegularExpression(QRegularExpression.escape(text))
			pat.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
			self._filter_proxy.setFilterRegularExpression(pat)
			self.tree.setModel(self._filter_proxy)
			self._tree_model = None
			self._rebuild_icon_model()
			return
		
		if hasattr(self, '_cached_tree_model') and self._cached_tree_model:
			if (self._cached_show_valid == self.show_only_valid_cb.isChecked() and 
				self._cached_show_unknown == self.show_unknown_cb.isChecked()):
				self.tree.setModel(self._cached_tree_model)
				self._tree_model = self._cached_tree_model
				self._rebuild_icon_model()
				return
		
		model = QStandardItemModel()
		model.setHorizontalHeaderLabels([self.tr("Paths")])
		
		root = {}
		for p in self._all_manifest_paths:
			p_lower = p.lower()
			if self.show_only_valid_cb.isChecked():
				if not p.startswith(UNKNOWN_PATH_PREFIX) and p_lower not in self._valid_paths:
					continue
			parts = p.split('/')
			node = root
			for part in parts:
				node = node.setdefault(part, {})
		

		count_cache = {}
		def compute_counts(node):
			if id(node) in count_cache:
				return count_cache[id(node)]
			if not node:
				count = 1
			else:
				count = sum(compute_counts(child) for child in node.values())
			count_cache[id(node)] = count
			return count
		

		compute_counts(root)
		
		def build(parent, node, prefix="", is_root=False):
			for name, child in node.items():
				display_name = name
				if child:
					file_count = count_cache.get(id(child), 0)
					if is_root or (parent == model.invisibleRootItem()):
						display_name = f"{name} ({file_count:,})"
				
				item = QStandardItem(display_name)
				item.setEditable(False)
				full = prefix + name
				item.setData(full, self._ITEM_EXTRACT_PATH_ROLE)
				item.setData(bool(child), self._ITEM_IS_DIR_ROLE)
				parent.appendRow(item)
				if child:
					build(item, child, full + "/", False)
		
		build(model.invisibleRootItem(), root, "", True)
		# The recursive closure holds self and the model in a cycle; empty its cell.
		del build
		self.tree.setModel(model)
		self._tree_model = model
		
		self._cached_tree_model = model
		self._cached_show_valid = self.show_only_valid_cb.isChecked()
		self._cached_show_unknown = self.show_unknown_cb.isChecked()
		self._rebuild_icon_model()


	def _build_flat_models(self):
		if self._all_manifest_paths is None:
			return
		if self._flat_model is None:
			self._flat_model = QStringListModel(self)
		self._flat_model.setStringList(list(self._all_manifest_paths))
		valid_only = []
		for p in self._all_manifest_paths:
			if p.startswith(UNKNOWN_PATH_PREFIX):
				valid_only.append(p)
				continue
			if p.lower() in self._valid_paths:
				valid_only.append(p)
		if self._flat_model_valid_only is None:
			self._flat_model_valid_only = QStringListModel(self)
		self._flat_model_valid_only.setStringList(valid_only)
		self._update_dump_button_visibility()

	def _set_view_mode(self, icons: bool):
		self.view_stack.setCurrentIndex(int(icons))
		self._empty_hint.raise_()
		self.icon_size_slider.setVisible(icons)
		if icons:
			self._rebuild_icon_model()
		else:
			self._update_breadcrumbs()

	def _update_breadcrumbs(self):
		show = (
			self.view_stack.currentWidget() is self.icon_view
			and bool(self._all_manifest_paths)
			and not self.filter_edit.text().strip()
		)
		self._crumbs.setVisible(show)
		while self._crumbs_layout.count():
			if widget := self._crumbs_layout.takeAt(0).widget():
				widget.deleteLater()
		if not show:
			return
		root = self._crumb_button("", "")
		root.setIcon(line_icon("home"))
		root.setToolTip(self.tr("Root folder"))
		parts = [part for part in self._icon_directory.split("/") if part]
		for index, part in enumerate(parts):
			self._crumbs_layout.addWidget(breadcrumb_separator(self._crumbs))
			self._crumb_button(part, "/".join(parts[:index + 1]))
		self._crumbs_layout.addStretch(1)

	def _crumb_button(self, text: str, path: str) -> QToolButton:
		button = QToolButton(self._crumbs)
		button.setObjectName("breadcrumbButton")
		button.setAutoRaise(True)
		button.setText(text)
		button.setProperty("path", path)
		button.clicked.connect(self._open_breadcrumb)
		self._crumbs_layout.addWidget(button)
		return button

	def _open_breadcrumb(self):
		self._icon_directory = self.sender().property("path")
		self._rebuild_icon_model()

	def _set_icon_size(self, size: int):
		if not hasattr(self, "icon_view"):
			return
		self.icon_view.setIconSize(QSize(size, size))
		self.icon_view.setGridSize(QSize(size + 32, size + 48))
		self._schedule_visible_thumbnails()

	def _schedule_visible_thumbnails(self):
		if hasattr(self, "_thumbnail_timer") and self.view_stack.currentWidget() is self.icon_view:
			self._thumbnail_timer.start(50)

	def _request_visible_thumbnails(self):
		viewport = self.icon_view.viewport()
		step_x = max(24, self.icon_view.gridSize().width() // 2)
		step_y = max(24, self.icon_view.gridSize().height() // 2)
		indexes = {
			self.icon_view.indexAt(QPoint(x, y))
			for y in range(0, viewport.height() + step_y, step_y)
			for x in range(0, viewport.width() + step_x, step_x)
		}
		for index in indexes:
			if index.isValid() and not index.data(self._ITEM_IS_DIR_ROLE):
				self._thumbnail_provider.request(index.data(self._ITEM_EXTRACT_PATH_ROLE))

	def resizeEvent(self, event):
		super().resizeEvent(event)
		self._schedule_visible_thumbnails()

	def closeEvent(self, event):
		self._thumbnail_provider.cancel_pending()
		super().closeEvent(event)

	def _filtered_icon_entries(self, paths, text):
		pattern = QRegularExpression(text)
		if not pattern.isValid():
			pattern = QRegularExpression(QRegularExpression.escape(text))
		pattern.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
		return [
			PakIconEntry(path.rsplit("/", 1)[-1], path)
			for path in paths if pattern.match(path).hasMatch()
		]

	def _directory_icon_entries(self, paths):
		prefix = f"{self._icon_directory}/" if self._icon_directory else ""
		directories: dict[str, PakIconEntry] = {}
		files = []
		for path in paths:
			if prefix and not path.startswith(prefix):
				continue
			remainder = path[len(prefix):]
			if "/" in remainder:
				name = remainder.split("/", 1)[0]
				folder = prefix + name
				directories.setdefault(folder, PakIconEntry(name, folder, True))
			elif remainder:
				files.append(PakIconEntry(remainder, path))
		entries = sorted(directories.values(), key=lambda entry: entry.label.lower())
		entries += sorted(files, key=lambda entry: entry.label.lower())
		if self._icon_directory:
			parent = self._icon_directory.rpartition("/")[0]
			entries.insert(0, PakIconEntry("..", parent, True))
		return entries

	def _rebuild_icon_model(self):
		paths = list(self._iter_display_paths())
		text = self.filter_edit.text().strip()
		if text:
			entries = self._filtered_icon_entries(paths, text)
		else:
			entries = self._directory_icon_entries(paths)
		self._thumbnail_provider.set_source(
			self._current_reader(),
			self._selected_paks(),
			self._base_paths,
			resource_context_for_app(self.parent()),
		)
		self._thumbnail_provider.cancel_pending()
		self._icon_model.set_entries(entries)
		self._schedule_visible_thumbnails()
		self._update_breadcrumbs()

	def _open_icon_entry(self, index):
		if not index.isValid():
			return
		path = index.data(self._ITEM_EXTRACT_PATH_ROLE) or ""
		if index.data(self._ITEM_IS_DIR_ROLE):
			self._icon_directory = path
			self._rebuild_icon_model()
		elif path:
			self._open_file_entry(path)

	def _open_tree_entry(self, index):
		if not index.isValid() or index.data(self._ITEM_IS_DIR_ROLE):
			return
		model = self.tree.model()
		if isinstance(model, (QSortFilterProxyModel, QStringListModel)):
			path = index.data(Qt.DisplayRole)
		else:
			path = index.data(self._ITEM_EXTRACT_PATH_ROLE)
		if isinstance(path, str) and path:
			self._open_file_entry(path)

	def _open_file_entry(self, path: str) -> bool:
		try:
			reader = self._current_reader()
			if reader is None:
				return False
			context = resource_context_for_app(self.parent())
			if context is not None:
				context = context.with_pak_reader(reader)
			entry = (
				int(path.split("/", 1)[1], 16)
				if path.startswith(UNKNOWN_PATH_PREFIX)
				else path
			)
			stream = reader.get_file(entry)
			if stream:
				self.parent().add_tab(
					path,
					stream.read(),
					pak_source_path=path,
					resource_context=context,
				)
			return bool(stream)
		except Exception as e:
			QMessageBox.critical(self, self.tr("Open failed"), str(e))
			return False

	def _selected_paks(self) -> List[str]:
		return [self.pak_list.item(i).text() for i in range(self.pak_list.count())]


	def _auto_merge_manifest(self):

		paks = self._selected_paks()
		if not paks:
			return []
		return CachedPakReader.read_manifest(paks)


	def _current_reader(self) -> CachedPakReader | None:
		paks = self._selected_paks()
		if not paks:
			self._cached_reader = None
			self._cache_outdated = False
			return None

		game = self.game_combo.currentText().strip()
		include_stubs = self.include_stubs_cb.isChecked()
		r = self._cached_reader if isinstance(self._cached_reader, CachedPakReader) else None
		if (
			r is None
			or not r.matches_source(paks, game=game, include_stubs=include_stubs)
			or self._cache_outdated
		):
			r = CachedPakReader.from_paks(paks, game=game, include_stubs=include_stubs)
			if self._base_paths:
				r.add_files(*self._base_paths)
			self._cached_reader = r
			self._cache_outdated = False
		return r

	def _ensure_cache(self, *, full: bool = False, validate: bool = False) -> CachedPakReader | None:
		r = self._current_reader()
		if r is None:
			self._valid_paths = set()
			return None

		known = sorted(set(self._base_paths))
		if full:
			self._ensure_full_cache(r, known)
		elif validate:
			if not known:
				self._valid_paths = set()
				return r
			self._ensure_validation_cache(r, known)

		if full or validate:
			self._valid_paths = {p.lower() for p in r.cached_paths(include_unknown=False)}
		return r

	@staticmethod
	def _ensure_full_cache(reader, known):
		reader.prepare(known, full=True)

	@staticmethod
	def _ensure_validation_cache(reader, known):
		reader.prepare(known, full=False)

	def _refresh_index(self):
		paks = self._selected_paks()
		self.mod_warning.setVisible(any_pak_modded(paks))
		if not paks:
			self._cached_reader = None
			self._valid_paths = set()
			self._all_manifest_paths = []
			self._cache_outdated = False
			self._apply_filter()
			return

		try:
			if self.show_unknown_cb.isChecked():
				self._ensure_cache(full=True)
			elif self.show_only_valid_cb.isChecked():
				self._ensure_cache(validate=True)
			else:
				self._valid_paths = set()
		except Exception as e:
			QMessageBox.critical(self, self.tr(INDEX_FAILED_TITLE), str(e))
			return

		self._recompute_display()

	def _recompute_display(self):
		self._cached_tree_model = None
		
		if self._cached_reader and self.show_unknown_cb.isChecked():

			base_set = set(self._base_paths)
			cached = self._cached_reader.cached_paths(include_unknown=True)
			unknowns = []
			for p in cached:
				if p.startswith(UNKNOWN_PATH_PREFIX) and p.lower() not in base_set:
					unknowns.append(p)

			self._all_manifest_paths = sorted(base_set) + unknowns
		else:

			self._all_manifest_paths = sorted(set(self._base_paths))
		self._build_flat_models()
		self._apply_filter_now()

	def _load_list_file(self):
		path = choose_pak_list_file(self)
		if not path:
			return
		self._load_list_file_from_path(path)

	def _load_list_file_from_path(self, path: str):
		with self._loading(self.tr("Loading list file...")):
			try:
				items = read_pak_list_file(path)
			except Exception as e:
				QMessageBox.critical(self, self.tr("Read failed"), str(e))
				return

		list_game = game_for_pak_list_path(path)
		if list_game and list_game != self.game_combo.currentText():
			was_blocked = self.game_combo.blockSignals(True)
			try:
				self.game_combo.setCurrentText(list_game)
			finally:
				self.game_combo.blockSignals(was_blocked)

		with self._loading(self.tr("Resolving list entries...")):
			manifest_paths = self._auto_merge_manifest()
			self._base_paths = sorted(set(items) | {p.lower() for p in manifest_paths})
			self._cached_reader = None
			self._valid_paths = set()
			self._cache_outdated = False
			self._refresh_index()

	def _prompt_auto_list_from_directory(self, directory_path: str):
		suggestions = find_suggested_pak_list_paths_for_directory(directory_path, BASE_DIR)
		if not suggestions:
			return
		choices = [str(p) for p in suggestions]
		selected, ok = QInputDialog.getItem(
			self,
			self.tr("Suggested List File"),
			self.tr("Detected game folder name. Choose a list file to load:"),
			choices,
			0,
			False,
		)
		if ok and selected:
			self._load_list_file_from_path(selected)

	def _extract_selected(self):
		targets = self._collect_selected_paths()
		self._extract(targets)

	def _extract_all(self):
		targets = list(self._iter_display_paths())
		self._extract(targets)

	def _selected_icon_paths(self) -> List[str]:
		return [
			index.data(self._ITEM_EXTRACT_PATH_ROLE)
			for index in self.icon_view.selectedIndexes()
			if not index.data(self._ITEM_IS_DIR_ROLE)
		]

	def _selected_flat_paths(self) -> List[str]:
		paths = []
		for index in self.tree.selectedIndexes():
			value = index.data(Qt.DisplayRole)
			if isinstance(value, str) and value:
				paths.append(value)
		return paths

	def _selected_tree_paths(self) -> List[str]:
		paths = []
		for index in self.tree.selectedIndexes():
			if index.data(self._ITEM_IS_DIR_ROLE):
				continue
			value = index.data(self._ITEM_EXTRACT_PATH_ROLE)
			if isinstance(value, str) and value:
				paths.append(value)
		return paths

	def _collect_selected_paths(self) -> List[str]:
		if self.view_stack.currentWidget() is self.icon_view:
			return self._selected_icon_paths()
		model = self.tree.model()
		if model is None:
			return []
		if isinstance(model, (QSortFilterProxyModel, QStringListModel)):
			return self._selected_flat_paths()
		if isinstance(model, QStandardItemModel):
			return self._selected_tree_paths()
		return []

	def _iter_display_paths(self):
		for p in self._all_manifest_paths:
			if not self.show_only_valid_cb.isChecked():
				yield p
				continue
			if p.startswith(UNKNOWN_PATH_PREFIX) or p.lower() in self._valid_paths:
				yield p

	def _show_tree_context_menu(self, pos):
		sender = self.sender()
		if sender is self.tree:
			vpos = self.tree.viewport().mapFrom(self.tree, pos)
		else:
			vpos = pos
		index = self.tree.indexAt(vpos)
		if not index.isValid():
			return
		is_dir = bool(index.data(self._ITEM_IS_DIR_ROLE))
		if not is_dir:
			return
		folder_path = index.data(self._ITEM_EXTRACT_PATH_ROLE)
		if not isinstance(folder_path, str) or not folder_path:
			return
		menu = QMenu(self)
		action = menu.addAction(self.tr("Extract Folder"))
		chosen = menu.exec(self.tree.viewport().mapToGlobal(vpos))
		if chosen != action:
			return
		prefix = folder_path + "/"
		targets = [p for p in self._iter_display_paths() if p.startswith(prefix)]
		self._extract(targets)

	def _show_icon_context_menu(self, pos):
		index = self.icon_view.indexAt(pos)
		if not index.isValid():
			if self._icon_directory and not self.filter_edit.text().strip():
				menu = QMenu(self)
				up = menu.addAction(self.tr("Up"))
				if menu.exec(self.icon_view.viewport().mapToGlobal(pos)) == up:
					self._icon_directory = self._icon_directory.rpartition("/")[0]
					self._rebuild_icon_model()
			return
		if not index.data(self._ITEM_IS_DIR_ROLE):
			return
		folder = index.data(self._ITEM_EXTRACT_PATH_ROLE)
		menu = QMenu(self)
		open_action = menu.addAction(self.tr("Open Folder"))
		extract_action = None if index.data(Qt.DisplayRole) == ".." else menu.addAction(self.tr("Extract Folder"))
		chosen = menu.exec(self.icon_view.viewport().mapToGlobal(pos))
		if chosen == open_action:
			self._icon_directory = folder
			self._rebuild_icon_model()
		elif extract_action is not None and chosen == extract_action:
			prefix = folder + "/"
			self._extract([p for p in self._iter_display_paths() if p.startswith(prefix)])

	def _update_dump_button_visibility(self):
		show = (self.show_only_valid_cb.isChecked() and 
				self._flat_model_valid_only is not None and 
				self._flat_model_valid_only.rowCount() > 0)
		self.dump_valid_btn.setVisible(bool(show))
	
	def _dump_valid_files(self):
		if not self._flat_model_valid_only or self._flat_model_valid_only.rowCount() == 0:
			QMessageBox.information(self, self.tr(DUMP_VALID_PATHS_TITLE), self.tr("No valid paths to dump."))
			return
		valid_paths = [p for p in self._flat_model_valid_only.stringList() if not p.startswith(UNKNOWN_PATH_PREFIX)]
		if not valid_paths:
			QMessageBox.information(self, self.tr(DUMP_VALID_PATHS_TITLE), self.tr("No valid paths to dump."))
			return
		path, _ = QFileDialog.getSaveFileName(self, self.tr("Save valid paths list"), "valid_paths.list", self.tr("List files (*.list *.txt);;All files (*)"))
		if not path:
			return
		try:
			with open(path, "w", encoding="utf-8") as f:
				for p in valid_paths:
					f.write(p + "\n")
			QMessageBox.information(self, self.tr("Success"), self.tr("Dumped {count} valid path(s) to:\n{path}").format(count=len(valid_paths), path=path))
		except Exception as e:
			QMessageBox.critical(self, self.tr("Write failed"), str(e))

	def _run_extraction_with_progress(self, reader_getter, outdir_getter, targets: List[str], missing: List[str]):
		progress_dialog = ExtractionProgressDialog(len(targets), self)
		extraction_error = [None]
		extraction_count = [0]

		def do_extraction():
			try:
				reader = reader_getter()
				extraction_count[0] = reader.extract_files_to(
					outdir_getter(),
					targets,
					missing_files=missing,
					progress_dialog=progress_dialog
				)
				progress_dialog.signals.extraction_complete.emit()
			except Exception as e:
				extraction_error[0] = e
				progress_dialog.signals.extraction_error.emit(str(e))

		extraction_thread = Thread(target=do_extraction)
		extraction_thread.start()

		progress_dialog.exec()
		extraction_thread.join(timeout=2.0)

		if extraction_error[0]:
			QMessageBox.critical(self, self.tr("Extract failed"), str(extraction_error[0]))
			return None

		if progress_dialog.cancelled and progress_dialog.completed_files < progress_dialog.total_files:
			QMessageBox.information(self, self.tr("Cancelled"), self.tr("Extraction was cancelled"))
			return None

		return extraction_count[0]

	def _append_missing_paths_message(self, msg: str, missing: List[str]) -> str:
		if missing:
			msg += f"\n\n{self.tr('Missing paths (not found in PAKs):')}\n" + "\n".join(missing[:50])
			if len(missing) > 50:
				msg += "\n… " + self.tr("and {count} more").format(
					count=len(missing) - 50
				)
		return msg
	
	def _extract(self, targets: List[str]):
		if not targets:
			QMessageBox.information(self, self.tr("Extract"), self.tr("No files selected."))
			return
		paks = self._selected_paks()
		if not paks:
			QMessageBox.information(self, self.tr("Extract"), self.tr("Add one or more PAK files first."))
			return
		outdir = self.out_edit.text().strip()
		if not outdir:
			QMessageBox.information(self, self.tr("Extract"), self.tr("Choose an output directory."))
			return
		Path(outdir).mkdir(parents=True, exist_ok=True)

		if any(t.startswith(UNKNOWN_PATH_PREFIX) for t in targets):

			try:
				rc = self._ensure_cache(full=True)
			except Exception as e:
				QMessageBox.critical(self, self.tr(INDEX_FAILED_TITLE), str(e))
				return
			if not rc:
				QMessageBox.critical(self, self.tr(INDEX_FAILED_TITLE), self.tr("Could not build PAK index."))
				return
			missing: List[str] = []
			count = self._run_extraction_with_progress(
				lambda: rc,
				lambda: self.out_edit.text().strip(),
				targets,
				missing
			)
			if count is None:
				return

			msg = self.tr("Extracted {count} file(s) to:\n{dest}").format(count=count, dest=self.out_edit.text().strip())
			QMessageBox.information(self, self.tr("Done"), self._append_missing_paths_message(msg, missing))
			return

		try:
			r = self._ensure_cache(validate=True)
		except Exception as e:
			QMessageBox.critical(self, self.tr(INDEX_FAILED_TITLE), str(e))
			return
		if not r:
			QMessageBox.critical(self, self.tr(INDEX_FAILED_TITLE), self.tr("Could not build PAK index."))
			return
		missing: List[str] = []

		count = self._run_extraction_with_progress(
			lambda: r,
			lambda: outdir,
			targets,
			missing
		)
		if count is None:
			return

		msg = self.tr("Extracted {count} file(s) to:\n{dest}").format(
			count=count, dest=outdir
		)
		QMessageBox.information(self, self.tr("Done"), self._append_missing_paths_message(msg, missing))
