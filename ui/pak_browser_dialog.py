from __future__ import annotations
import os
from pathlib import Path
from typing import List
from contextlib import contextmanager

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, QPoint, QTimer, QSortFilterProxyModel, QRegularExpression, QStringListModel, QSize


from PySide6.QtGui import QStandardItemModel, QStandardItem, QColor

from PySide6.QtWidgets import (
	QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
	QFileDialog, QLineEdit, QCheckBox, QInputDialog,
	QMessageBox, QTreeView, QListView, QAbstractItemView, QMenu, QApplication,
	QSlider, QStackedWidget, QStyle, QToolButton
)

from settings import DEFAULT_SETTINGS, load_settings

from file_handlers.pak import scan_pak_files
from file_handlers.pak.reader import CachedPakReader
from ui.project_manager.constants import BASE_DIR
from ui.project_manager.pak_file_lists import (
	choose_pak_list_file, find_suggested_pak_list_paths_for_directory, read_pak_list_file,
)
from ui.pak_icon_view import PakIconEntry, PakIconModel, PakThumbnailProvider, thumbnail_cache_directory


DUMP_VALID_PATHS_TITLE = QT_TRANSLATE_NOOP("PakBrowserDialog", "Dump Valid Paths")


class PakBrowserDialog(QDialog):
	_ITEM_EXTRACT_PATH_ROLE = Qt.UserRole + 32
	_ITEM_IS_DIR_ROLE = Qt.UserRole + 33

	def __init__(self, parent=None):
		super().__init__(parent)
		self.setWindowTitle(self.tr("PAK Browser"))
		self.resize(900, 600)
		lay = QVBoxLayout(self)

		settings = getattr(parent, 'settings', None) or load_settings()
		highlight_color = settings.get(
			"tree_highlight_color", DEFAULT_SETTINGS["tree_highlight_color"]
		)
		self._highlight_color = QColor(highlight_color)

		top = QHBoxLayout()
		lay.addLayout(top)

		self.dir_edit = QLineEdit(self)
		self.dir_edit.setPlaceholderText(self.tr("Game directory (optional, for scan)"))
		top.addWidget(self.dir_edit, 1)
		top.addWidget(QPushButton(self.tr("Browse…"), clicked=self._choose_dir))
		self.ignore_mods_cb = QCheckBox(self.tr("Ignore mod PAKs (not 100% accurate)"), self)
		self.ignore_mods_cb.setChecked(True)
		self.ignore_mods_cb.toggled.connect(self._on_ignore_mods_toggled)
		top.addWidget(self.ignore_mods_cb)
		top.addWidget(QPushButton(self.tr("Scan"), clicked=self._scan_dir))

		row2 = QHBoxLayout()
		lay.addLayout(row2)
		row2.addWidget(QLabel(self.tr("PAK files (ordered):")))
		row2.addStretch(1)
		row2.addWidget(QPushButton(self.tr("Load .list…"), clicked=self._load_list_file))
		row2.addWidget(QPushButton(self.tr("Add PAK…"),   clicked=self._add_paks))
		row2.addWidget(QPushButton(self.tr("Remove"),     clicked=self._remove_paks))
		row2.addWidget(QPushButton(self.tr("Move Up"),    clicked=lambda: self._move_selected(-1)))
		row2.addWidget(QPushButton(self.tr("Move Down"),  clicked=lambda: self._move_selected(+1)))

		self.pak_list = QListWidget(self)
		lay.addWidget(self.pak_list, 1)


		mid = QHBoxLayout()
		lay.addLayout(mid)
		mid.addWidget(QLabel(self.tr("Filter:")))
		self.filter_edit = QLineEdit(self)
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

		self.icon_size_slider = QSlider(Qt.Horizontal, self)
		self.icon_size_slider.setRange(48, 160)
		self.icon_size_slider.setValue(88)
		self.icon_size_slider.setTracking(False)
		self.icon_size_slider.setFixedWidth(90)
		self.icon_size_slider.setToolTip(self.tr("Icon size"))
		self.icon_size_slider.valueChanged.connect(self._set_icon_size)
		self.icon_size_slider.hide()
		mid.addWidget(self.icon_size_slider)
		self.view_mode_btn = QToolButton(self)
		self.view_mode_btn.setCheckable(True)
		self.view_mode_btn.setAutoRaise(True)
		self.view_mode_btn.toggled.connect(self._set_view_mode)
		mid.addWidget(self.view_mode_btn)
		self._update_view_mode_button(False)

		self.tree = QTreeView(self)
		self.tree.setUniformRowHeights(True)
		self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
		self.tree.setHeaderHidden(True)
		self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
		self.tree.customContextMenuRequested.connect(self._show_tree_context_menu)
		self.tree.viewport().setContextMenuPolicy(Qt.CustomContextMenu)
		self.tree.viewport().customContextMenuRequested.connect(self._show_tree_context_menu)
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
		lay.addWidget(self.view_stack, 3)
		self._tree_model = None
		self._flat_model: QStringListModel | None = None
		self._flat_model_valid_only: QStringListModel | None = None
		self._filter_proxy = QSortFilterProxyModel(self)
		self._filter_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
		self._filter_proxy.setFilterKeyColumn(0)


		out = QHBoxLayout()
		lay.addLayout(out)
		out.addWidget(QLabel(self.tr("Output directory:")))
		self.out_edit = QLineEdit(self)
		out.addWidget(self.out_edit, 1)
		out.addWidget(QPushButton(self.tr("Choose…"), clicked=self._choose_out))
		out.addStretch(1)
		self.dump_valid_btn = QPushButton(self.tr(DUMP_VALID_PATHS_TITLE), clicked=self._dump_valid_files)
		self.dump_valid_btn.setVisible(False)
		self.dump_valid_btn.setStyleSheet(f"QPushButton {{ background-color: {self._highlight_color.name()}; color: white; }}")
		out.addWidget(self.dump_valid_btn)
		out.addWidget(QPushButton(self.tr("Extract Selected"), clicked=self._extract_selected))
		out.addWidget(QPushButton(self.tr("Extract All"), clicked=self._extract_all))

		self._all_manifest_paths: List[str] = []
		self._base_paths: List[str] = []
		self._cached_reader: CachedPakReader | None = None
		self._valid_paths: set[str] = set()
		self._cached_tree_model = None
		self._cached_show_valid = False
		self._cached_show_unknown = False
		self._cache_outdated = False
		self._scanned_root = ""
		self._loading_depth = 0
		self.loading_label = QLabel(self)
		self.loading_label.setStyleSheet(f"color: {self._highlight_color.name()}; font-weight: 600;")
		self.loading_label.setVisible(False)
		lay.addWidget(self.loading_label)

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
			paks = scan_pak_files(root, ignore_mod_paks=self.ignore_mods_cb.isChecked())
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

	def _on_ignore_mods_toggled(self, checked: bool):
		root = self.dir_edit.text().strip()
		if root and os.path.isdir(root):
			self._scan_dir()
			return
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
				QMessageBox.critical(self, self.tr("Index failed"), str(e))
				return
			self._recompute_display()

	def _on_show_only_valid_toggled(self, _checked: bool):
		with self._loading(self.tr("Updating file list...")):
			try:
				if self.show_only_valid_cb.isChecked():
					self._ensure_cache(validate=True)
			except Exception as e:
				QMessageBox.critical(self, self.tr("Index failed"), str(e))
				return
			self._recompute_display()

	def _on_filter_text_changed(self, _=None):
		self._filter_timer.start()

	def _apply_filter(self):
		self._apply_filter_now()

	def _apply_filter_now(self):
		text = self.filter_edit.text().strip()
		
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
				if not p.startswith("__Unknown/") and p_lower not in self._valid_paths:
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
			if p.startswith("__Unknown/"):
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
		self.icon_size_slider.setVisible(icons)
		self._update_view_mode_button(icons)
		if icons:
			self._rebuild_icon_model()

	def _update_view_mode_button(self, icons: bool):
		style = QApplication.style()
		pixmap = QStyle.SP_FileDialogDetailedView if icons else QStyle.SP_FileDialogListView
		self.view_mode_btn.setIcon(style.standardIcon(pixmap))
		self.view_mode_btn.setToolTip(
			self.tr("Switch to tree view") if icons else self.tr("Switch to icons view")
		)

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
		self._thumbnail_provider.close()
		super().closeEvent(event)

	def _rebuild_icon_model(self):
		paths = list(self._iter_display_paths())
		text = self.filter_edit.text().strip()
		if text:
			pattern = QRegularExpression(text)
			if not pattern.isValid():
				pattern = QRegularExpression(QRegularExpression.escape(text))
			pattern.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
			entries = [
				PakIconEntry(p.rsplit("/", 1)[-1], p)
				for p in paths if pattern.match(p).hasMatch()
			]
		else:
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
			entries = sorted(directories.values(), key=lambda e: e.label.lower())
			entries += sorted(files, key=lambda e: e.label.lower())
			if self._icon_directory:
				entries.insert(0, PakIconEntry("..", self._icon_directory.rpartition("/")[0], True))
		self._thumbnail_provider.set_source(
			self._current_reader(), self._selected_paks(), self._base_paths
		)
		self._thumbnail_provider.cancel_pending()
		self._icon_model.set_entries(entries)
		self._schedule_visible_thumbnails()

	def _open_icon_entry(self, index):
		if not index.data(self._ITEM_IS_DIR_ROLE):
			return
		self._icon_directory = index.data(self._ITEM_EXTRACT_PATH_ROLE) or ""
		self._rebuild_icon_model()

	def _selected_paks(self) -> List[str]:
		return [self.pak_list.item(i).text() for i in range(self.pak_list.count())]


	def _auto_merge_manifest(self):

		paks = self._selected_paks()
		if not paks:
			return []
		return CachedPakReader.read_manifest(paks)

	def _update_from_cache(self):
		if not self._cached_reader:
			return
		self._recompute_display()

	def _current_reader(self) -> CachedPakReader | None:
		paks = self._selected_paks()
		if not paks:
			self._cached_reader = None
			self._cache_outdated = False
			return None

		r = self._cached_reader if isinstance(self._cached_reader, CachedPakReader) else None
		if r is None or r.pak_file_priority != paks or self._cache_outdated:
			r = CachedPakReader()
			r.pak_file_priority = paks
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
			had_cache = r._cache is not None
			if r._cache is None:
				r.reset_file_list()
				if known:
					r.add_files(*known)
			if r._cache is None or not getattr(r, "_cache_complete", True):
				r.cache_entries(assign_paths=bool(known))
			elif known and had_cache:
				r.assign_paths(known)
		elif validate:
			if not known:
				self._valid_paths = set()
				return r
			if r._cache is None:
				r.cache_entries_for_paths(known)
			elif getattr(r, "_cache_complete", True):
				r.assign_paths(known)

		if full or validate:
			self._valid_paths = {p.lower() for p in r.cached_paths(include_unknown=False)}
		return r

	def _refresh_index(self):
		paks = self._selected_paks()
		if not paks:
			self._cached_reader = None
			self._valid_paths = set()
			self._all_manifest_paths = []
			self._cache_outdated = False
			self._apply_filter()
			return

		try:
			if not self._base_paths and not self.ignore_mods_cb.isChecked():
				manifest_only = self._auto_merge_manifest()
				if manifest_only:
					self._base_paths = sorted(set(p.lower() for p in manifest_only))

			if self.show_unknown_cb.isChecked():
				self._ensure_cache(full=True)
			elif self.show_only_valid_cb.isChecked():
				self._ensure_cache(validate=True)
			else:
				self._valid_paths = set()
		except Exception as e:
			QMessageBox.critical(self, self.tr("Index failed"), str(e))
			return

		self._recompute_display()

	def _recompute_display(self):
		self._cached_tree_model = None
		
		if self._cached_reader and self.show_unknown_cb.isChecked():

			base_set = set(self._base_paths)
			cached = self._cached_reader.cached_paths(include_unknown=True)
			unknowns = []
			for p in cached:
				if p.startswith("__Unknown/") and p.lower() not in base_set:
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

	def _collect_selected_paths(self) -> List[str]:
		paths: List[str] = []
		if self.view_stack.currentWidget() is self.icon_view:
			for idx in self.icon_view.selectedIndexes():
				if not idx.data(self._ITEM_IS_DIR_ROLE):
					paths.append(idx.data(self._ITEM_EXTRACT_PATH_ROLE))
			return paths
		model = self.tree.model()
		if model is None:
			return paths
		if isinstance(model, (QSortFilterProxyModel, QStringListModel)):
			for idx in self.tree.selectedIndexes():
				val = idx.data(Qt.DisplayRole)
				if isinstance(val, str) and val:
					paths.append(val)
			return paths
		if isinstance(model, QStandardItemModel):
			for idx in self.tree.selectedIndexes():
				is_dir = bool(idx.data(self._ITEM_IS_DIR_ROLE))
				if is_dir:
					continue
				data = idx.data(self._ITEM_EXTRACT_PATH_ROLE)
				if isinstance(data, str) and data:
					paths.append(data)
		return paths

	def _iter_display_paths(self):
		for p in self._all_manifest_paths:
			if not self.show_only_valid_cb.isChecked():
				yield p
				continue
			if p.startswith("__Unknown/") or p.lower() in self._valid_paths:
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
		valid_paths = [p for p in self._flat_model_valid_only.stringList() if not p.startswith("__Unknown/")]
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
		from ui.extraction_progress_dialog import ExtractionProgressDialog
		from threading import Thread

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

		if any(t.startswith("__Unknown/") for t in targets):

			try:
				rc = self._ensure_cache(full=True)
			except Exception as e:
				QMessageBox.critical(self, self.tr("Index failed"), str(e))
				return
			if not rc:
				QMessageBox.critical(self, self.tr("Index failed"), self.tr("Could not build PAK index."))
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
			QMessageBox.critical(self, self.tr("Index failed"), str(e))
			return
		if not r:
			QMessageBox.critical(self, self.tr("Index failed"), self.tr("Could not build PAK index."))
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
