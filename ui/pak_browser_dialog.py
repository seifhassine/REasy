from __future__ import annotations
import os
from pathlib import Path
from typing import List
import time

from PySide6.QtCore import Qt, QTimer, QSortFilterProxyModel, QRegularExpression, QStringListModel


from PySide6.QtGui import QStandardItemModel, QStandardItem

from PySide6.QtWidgets import (
	QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QListWidget,
	QFileDialog, QLineEdit, QCheckBox,
	QMessageBox, QTreeView, QAbstractItemView
)

from file_handlers.pak import scan_pak_files
from file_handlers.pak.reader import CachedPakReader
from ui.widgets_utils import create_list_file_help_widget


class PakBrowserDialog(QDialog):
	def __init__(self, parent=None):
		super().__init__(parent)
		self.setWindowTitle(self.tr("PAK Browser"))
		self.resize(900, 600)
		lay = QVBoxLayout(self)


		top = QHBoxLayout()
		lay.addLayout(top)

		self.dir_edit = QLineEdit(self)
		self.dir_edit.setPlaceholderText(self.tr("Game directory (optional, for scan)"))
		top.addWidget(self.dir_edit, 1)
		top.addWidget(QPushButton(self.tr("Browse…"), clicked=self._choose_dir))
		self.ignore_mods_cb = QCheckBox(self.tr("Ignore mod PAKs"), self)
		self.ignore_mods_cb.setChecked(True)
		top.addWidget(self.ignore_mods_cb)
		top.addWidget(QPushButton(self.tr("Scan"), clicked=self._scan_dir))

		row2 = QHBoxLayout()
		lay.addLayout(row2)
		row2.addWidget(QLabel(self.tr("PAK files (ordered):")))
		row2.addStretch(1)
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
		self.filter_edit.setPlaceholderText(self.tr("Search (supports regex) - shows flat list; clear for tree view"))
		self._filter_timer = QTimer(self)
		self._filter_timer.setSingleShot(True)
		self._filter_timer.timeout.connect(self._apply_filter_now)
		self.filter_edit.textChanged.connect(self._on_filter_text_changed)
		mid.addWidget(self.filter_edit, 1)
		self.show_unknown_cb = QCheckBox(self.tr("Include unknown entries"))
		self.show_unknown_cb.setChecked(False)
		mid.addWidget(self.show_unknown_cb)
		
		self.show_only_valid_cb = QCheckBox(self.tr("Show only valid files"))
		self.show_only_valid_cb.setChecked(False)
		self.show_only_valid_cb.toggled.connect(self._on_show_only_valid_toggled)
		mid.addWidget(self.show_only_valid_cb)

		list_container, _ = create_list_file_help_widget(button_callback=self._load_list_file)
		mid.addLayout(list_container)

		self.tree = QTreeView(self)
		self.tree.setUniformRowHeights(True)
		self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
		self.tree.setHeaderHidden(True)
		lay.addWidget(self.tree, 3)
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
		self.show_unknown_cb.toggled.connect(lambda _=False: self._recompute_display())


	def _choose_dir(self):
		d = QFileDialog.getExistingDirectory(self, self.tr("Select Game Directory"))
		if d:
			self.dir_edit.setText(d)

	def _scan_dir(self):
		root = self.dir_edit.text().strip()
		if not root:
			QMessageBox.information(self, self.tr("Scan"), self.tr("Select a directory to scan."))
			return
		paks = scan_pak_files(root, ignore_mod_paks=self.ignore_mods_cb.isChecked())
		if not paks:
			QMessageBox.information(self, self.tr("Scan"), self.tr("No .pak files found."))
			return
		

		old_paks = [self.pak_list.item(i).text() for i in range(self.pak_list.count())]
		if paks == old_paks and self._cached_reader:

			self._recompute_display()
			return
		
		self.pak_list.clear()
		for p in paks:
			self.pak_list.addItem(p)
		self._refresh_index()
		self._recompute_display()

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
	
	def _on_show_only_valid_toggled(self):
		self._apply_filter_now()

	def _on_filter_text_changed(self, _=None):
		self._filter_timer.start(120)

	def _apply_filter(self):
		self._apply_filter_now()

	def _apply_filter_now(self):
		text = self.filter_edit.text().strip()
		
		if not self._all_manifest_paths:
			model = QStandardItemModel()
			model.setHorizontalHeaderLabels([self.tr("Paths")])
			self.tree.setModel(model)
			self._tree_model = model
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
			return
		
		if hasattr(self, '_cached_tree_model') and self._cached_tree_model:
			if (self._cached_show_valid == self.show_only_valid_cb.isChecked() and 
				self._cached_show_unknown == self.show_unknown_cb.isChecked()):
				self.tree.setModel(self._cached_tree_model)
				self._tree_model = self._cached_tree_model
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
				item.setData(full if not child else None)
				parent.appendRow(item)
				if child:
					build(item, child, full + "/", False)
		
		build(model.invisibleRootItem(), root, "", True)
		self.tree.setModel(model)
		self._tree_model = model
		
		self._cached_tree_model = model
		self._cached_show_valid = self.show_only_valid_cb.isChecked()
		self._cached_show_unknown = self.show_unknown_cb.isChecked()


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

	def _refresh_index(self):
		paks = self._selected_paks()
		if not paks:
			self._cached_reader = None
			self._valid_paths = set()
			self._all_manifest_paths = []
			self._cache_outdated = False
			self._apply_filter()
			return
		
		if self._cached_reader and self._cached_reader.pak_file_priority == paks and not self._cache_outdated:
			if self._base_paths:
				try:
					self._cached_reader.assign_paths(self._base_paths)
					self._valid_paths = set()
					if self._cached_reader._cache:
						all_cached = self._cached_reader.cached_paths(include_unknown=True)
						self._valid_paths = {p.lower() for p in all_cached}
					self._recompute_display()
					return
				except RuntimeError:
					pass
		
		r = CachedPakReader()
		r.pak_file_priority = paks
		try:

			known = list(set(self._base_paths)) if self._base_paths else []
			if not known:

				manifest_only = self._auto_merge_manifest()
				if manifest_only:
					known = manifest_only
			if known:
				r.add_files(*known)
				r.cache_entries(assign_paths=True)
			else:

				r.cache_entries(assign_paths=False)
		except Exception as e:
			QMessageBox.critical(self, self.tr("Index failed"), str(e))
			return
		self._cached_reader = r
		self._cache_outdated = False
		
		self._valid_paths = set()
		if self._cached_reader and self._cached_reader._cache:
			all_cached = self._cached_reader.cached_paths(include_unknown=True)
			self._valid_paths = {p.lower() for p in all_cached}
		
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
		path, _ = QFileDialog.getOpenFileName(self, self.tr("Open list file"), filter=self.tr("List files (*.list *.txt);;All files (*)") )
		if not path:
			return
		_profile = os.getenv("REASY_PROFILE", "0").lower() in ("1", "true", "yes", "on")
		_sections = []
		_t0 = time.perf_counter()
		try:
			with open(path, "r", encoding="utf-8") as f:
				items = [ln.strip().replace("\\", "/").lower() for ln in f if ln.strip()]
		except Exception as e:
			QMessageBox.critical(self, "Read failed", str(e))
			return
		_t1 = time.perf_counter()
		if _profile:
			_sections.append(("Read & normalize .list", ( _t1 - _t0 ) * 1000.0))

		_t2a = time.perf_counter()
		manifest_paths = self._auto_merge_manifest()
		_t2b = time.perf_counter()
		if _profile:
			_sections.append(("Read manifest (if any)", ( _t2b - _t2a ) * 1000.0))
		
		merged = sorted(set(items) | set(p.lower() for p in manifest_paths))
		self._base_paths = merged

		_t3a = time.perf_counter()
		if self._cached_reader:
			try:
				self._cached_reader.assign_paths(self._base_paths)
				self._valid_paths = set()
				if self._cached_reader._cache:
					all_cached = self._cached_reader.cached_paths(include_unknown=True)
					self._valid_paths = {p.lower() for p in all_cached}
			except Exception:
				self._refresh_index()
		else:
			self._refresh_index()
		_t3b = time.perf_counter()
		if _profile:
			_sections.append(("Resolve names (assign/index)", ( _t3b - _t3a ) * 1000.0))
		self._recompute_display()
		_t4 = time.perf_counter()
		if _profile:
			_sections.append(("Build UI model", ( _t4 - _t3b ) * 1000.0))
			_total = sum(ms for _, ms in _sections)
			msg = "\n".join(f"{name}: {ms:.2f} ms" for name, ms in _sections)
			msg += f"\nTotal: {_total:.2f} ms"
			QMessageBox.information(self, self.tr("Profile – Load .list"), msg)

	def _extract_selected(self):
		targets = self._collect_selected_paths()
		self._extract(targets)

	def _extract_all(self):
		targets = list(self._all_manifest_paths)
		self._extract(targets)

	def _collect_selected_paths(self) -> List[str]:
		paths: List[str] = []
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
				item = model.itemFromIndex(idx)
				data = item.data()
				if isinstance(data, str) and data:
					paths.append(data)
		return paths

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

		if self._cache_outdated:
			self._refresh_index()
		
		if any(t.startswith("__Unknown/") for t in targets):

			rc = self._cached_reader if isinstance(self._cached_reader, CachedPakReader) else None
			if not rc:
				rc = CachedPakReader()
				rc.pak_file_priority = paks
				try:

					rc.cache_entries(assign_paths=False)
				except Exception as e:
					QMessageBox.critical(self, self.tr("Index failed"), str(e))
					return
			missing: List[str] = []
			
			from ui.extraction_progress_dialog import ExtractionProgressDialog
			progress_dialog = ExtractionProgressDialog(len(targets), self)
			
			from threading import Thread
			extraction_error = [None]
			extraction_count = [0]
			
			def do_extraction():
				try:
					extraction_count[0] = rc.extract_files_to(
						self.out_edit.text().strip(), 
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
			
			result = progress_dialog.exec()
			extraction_thread.join(timeout=2.0)
			
			if extraction_error[0]:
				QMessageBox.critical(self, self.tr("Extract failed"), str(extraction_error[0]))
				return
			
			if progress_dialog.cancelled and progress_dialog.completed_files < progress_dialog.total_files:
				QMessageBox.information(self, self.tr("Cancelled"), self.tr("Extraction was cancelled"))
				return
			
			count = extraction_count[0]
			msg = self.tr("Extracted {count} file(s) to:\n{dest}").format(count=count, dest=self.out_edit.text().strip())
			if missing:
				msg += f"\n\n{self.tr('Missing paths (not found in PAKs):')}\n" + "\n".join(missing[:50])
				if len(missing) > 50:
					msg += f"\n… {self.tr('and')} {len(missing) - 50} {self.tr('more')}"
			QMessageBox.information(self, self.tr("Done"), msg)
			return


		r = CachedPakReader()
		r.pak_file_priority = paks
		missing: List[str] = []
		
		from ui.extraction_progress_dialog import ExtractionProgressDialog
		progress_dialog = ExtractionProgressDialog(len(targets), self)
		
		from threading import Thread
		extraction_error = [None]
		extraction_count = [0]
		
		def do_extraction():
			try:
				if self._cached_reader and isinstance(self._cached_reader, CachedPakReader):
					r._cache = self._cached_reader._cache
					_r = r
				else:
					_r = r
				extraction_count[0] = _r.extract_files_to(
					outdir, 
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
		
		result = progress_dialog.exec()
		extraction_thread.join(timeout=2.0)
		
		if extraction_error[0]:
			QMessageBox.critical(self, self.tr("Extract failed"), str(extraction_error[0]))
			return
		
		if progress_dialog.cancelled and progress_dialog.completed_files < progress_dialog.total_files:
			QMessageBox.information(self, self.tr("Cancelled"), self.tr("Extraction was cancelled"))
			return
		
		count = extraction_count[0]
		msg = f"{self.tr('Extracted')} {count} {self.tr('file(s) to:')}\n{outdir}"
		if missing:
			msg += f"\n\n{self.tr('Missing paths (not found in PAKs):')}\n" + "\n".join(missing[:50])
			if len(missing) > 50:
				msg += f"\n… {self.tr('and')} {len(missing) - 50} {self.tr('more')}"
		QMessageBox.information(self, self.tr("Done"), msg)

