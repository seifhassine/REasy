from __future__ import annotations
import os
import shutil
from pathlib import Path
import json
import sys
from PySide6.QtCore import Qt, QModelIndex, QTimer, QSortFilterProxyModel, QRegularExpression, QStringListModel
from PySide6.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QToolButton,
    QPushButton, QLabel, QFileDialog, QFileSystemModel, QMessageBox,
    QHeaderView, QMenu, QDialogButtonBox, QDialog, QComboBox, QTextEdit, QProgressBar,
    QTreeView, QAbstractItemView, QCheckBox, QLineEdit, QStyle, QSizePolicy
)
from PySide6.QtGui import QStandardItemModel, QStandardItem
from tools.pak_exporter import packer_status, _EXE_PATH, _ensure_packer, run_packer

from .constants  import EXPECTED_NATIVE, PROJECTS_ROOT
from .delegate   import _ActionsDelegate, _PakActionsDelegate
from .trees      import _DndTree, _DropTree

from ui.project_manager.project_settings_dialog import ProjectSettingsDialog
from tools.fluffy_exporter import create_fluffy_zip

from PySide6.QtCore import qInstallMessageHandler

from file_handlers.pak import scan_pak_files
from file_handlers.pak.reader import PakReader, CachedPakReader
from file_handlers.pak.utils import guess_extension_from_header
from ui.widgets_utils import create_list_file_help_label

def _custom_message_handler(mode, ctx, msg):
    if "QFileSystemWatcher: FindNextChangeNotification failed" in msg:
        return None
    if _prev_handler:
        return _prev_handler(mode, ctx, msg)
    return None

_prev_handler = qInstallMessageHandler(_custom_message_handler)

def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.argv[0]).resolve().parent
    else:
        return Path(__file__).resolve().parent.parent.parent
    
__all__ = ["ProjectManager"]  

class ProjectManager(QDockWidget):
    """
      • constants.py   – paths & icons
      • trees.py       – drag / drop QTreeView subclasses
      • delegate.py    – icon handling
    """
    def _expected_native(self):        
        return EXPECTED_NATIVE.get(self.current_game or "", ())
    
    def __init__(self, app_window, unpacked_root: str | None = None):
        super().__init__(self.tr("Project Browser"), app_window)
        self.app_win       = app_window
        self.current_game  = getattr(app_window, "current_game", None)
        self.unpacked_dir  = os.path.abspath(unpacked_root) if unpacked_root else None
        self.pak_dir = None
        self.project_dir   = None
        self._active_tab   = "sys"
        self._pak_list_path: str | None = None

        # -- UI scaffold -----------------------------------------------------
        c = QWidget(self)
        self.setWidget(c)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(2,2,2,2)

        # Top: path display + browse (contextual for Unpacked vs PAK)
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.path_label = QLabel()
        bar.addWidget(self.path_label, 1)
        self.path_label.setMinimumSize(20, 20)
        bar.addWidget(QPushButton(self.tr("Browse…"), clicked=self._browse))
        # PAK-specific controls
        self.pak_ignore_mods_cb = QCheckBox(self.tr("Ignore mod PAKs (not 100% accurate)"))
        self.pak_ignore_mods_cb.setChecked(True)
        self.btn_scan_paks = QPushButton(self.tr("Scan PAKs"), clicked=self._scan_paks)
        bar.addWidget(self.pak_ignore_mods_cb)
        bar.addWidget(self.btn_scan_paks)
        self.btn_load_list = QPushButton(self.tr("Load .list…"), clicked=self._choose_pak_list)
        bar.addWidget(self.btn_load_list)
        self.pak_list_edit = QLineEdit(self)
        self.pak_list_edit.setPlaceholderText(self.tr("List file (.list/.txt)"))
        self.pak_list_edit.setReadOnly(True)
        bar.addWidget(self.pak_list_edit, 1)
        self._update_path_label()

        self.list_help_label = create_list_file_help_label()
        self.list_help_label.setAlignment(Qt.AlignRight)
        lay.addWidget(self.list_help_label)

        # Project label
        self.project_label = QLabel("<i>No project open</i>")
        lay.addWidget(self.project_label)

        actions = QHBoxLayout()
        lay.addLayout(actions)    

        self.btn_conf = QPushButton(self.tr("Fluffy Settings…"), clicked=self._proj_settings)
        self.btn_zip  = QPushButton(self.tr("Export Fluffy ZIP"), clicked=self._export_zip)
        self.btn_pak  = QPushButton(self.tr("Export .PAK"),       clicked=self._export_mod)

        actions.addWidget(self.btn_conf)
        actions.addWidget(self.btn_zip)
        actions.addWidget(self.btn_pak)

        toggles = QHBoxLayout()
        lay.addLayout(toggles)

        self.btn_sys       = QToolButton(text=self.tr("System Files"),  checkable=True, checked=True)
        self.btn_proj      = QToolButton(text=self.tr("Project Files"), checkable=True)
        self.btn_pak_files = QToolButton(text=self.tr("PAK Files"),     checkable=True)

        toggles.addWidget(self.btn_sys)
        toggles.addWidget(self.btn_proj)
        toggles.addWidget(self.btn_pak_files)
        toggles.addStretch(1)

        # PAK search bar (visible only on PAK tab)
        pak_search = QHBoxLayout()
        lay.addLayout(pak_search)
        self.pak_filter_label = QLabel(self.tr("Filter:"))
        pak_search.addWidget(self.pak_filter_label)
        self.pak_filter_edit = QLineEdit(self)
        self.pak_filter_edit.setPlaceholderText(self.tr("Search (regex) – shows flat list; clear for tree view"))
        self._pak_filter_timer = QTimer(self)
        self._pak_filter_timer.setSingleShot(True)
        self._pak_filter_timer.timeout.connect(self._apply_pak_filter_now)
        self.pak_filter_edit.textChanged.connect(self._on_pak_filter_text_changed)
        pak_search.addWidget(self.pak_filter_edit, 1)

        for b in (self.btn_conf, self.btn_zip, self.btn_pak):
            b.setEnabled(False)
            
        # models + views -----------------------------------------------------
        self.model_sys,  self.tree_sys  = QFileSystemModel(), _DndTree()
        self.model_proj, self.tree_proj = QFileSystemModel(), _DropTree(self)
        self.tree_proj.hide()
        # PAK tree (virtual)
        self.tree_pak = QTreeView()
        self.tree_pak.setUniformRowHeights(True)
        self.tree_pak.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree_pak.setHeaderHidden(True)
        self.tree_pak.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.tree_pak.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree_pak.setIndentation(8)
        self.tree_pak.hide()
        self._pak_tree_model = None
        self._pak_all_paths: list[str] = []
        self._pak_base_paths: list[str] = []
        self._pak_selected_paks: list[str] = []
        self._pak_cached_reader: CachedPakReader | None = None
        self._pak_population_paths: list[str] = []
        self._pak_flat_model: QStringListModel | None = None
        self._pak_filter_proxy = QSortFilterProxyModel(self)
        self._pak_filter_proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self._pak_filter_proxy.setFilterKeyColumn(0)
        hdr_p = self.tree_pak.header()
        hdr_p.setSectionResizeMode(0, QHeaderView.Interactive)
        hdr_p.setMinimumSectionSize(160)
        hdr_p.sectionResized.connect(self._on_section_resized)

        for tree, model in ((self.tree_sys, self.model_sys), (self.tree_proj, self.model_proj)):
            tree.setModel(model)
            tree.setContextMenuPolicy(Qt.CustomContextMenu)
            tree.setIndentation(8)
            tree.hideColumn(1)
            tree.hideColumn(2)
            
            hdr = tree.header()
            hdr.setSectionResizeMode(0, QHeaderView.Stretch)
            hdr.setSectionResizeMode(3, QHeaderView.Fixed)
            hdr.resizeSection(3, 100)
            hdr.setStretchLastSection(False)
            hdr.setMinimumSectionSize(100)
            hdr.sectionResized.connect(self._on_section_resized)

        # icon delegate
        self.tree_sys .setItemDelegateForColumn(0, _ActionsDelegate(self, False))
        self.tree_proj.setItemDelegateForColumn(0, _ActionsDelegate(self, True))
        # Hide dock toggle from global context menus
        try:
            self.toggleViewAction().setVisible(False)
        except Exception:
            pass

        # double‑click open
        self.tree_sys.doubleClicked .connect(lambda idx: self._on_double(idx, False))
        self.tree_proj.doubleClicked.connect(lambda idx: self._on_double(idx, True))
        self.tree_pak.doubleClicked.connect(self._on_pak_double)
        self.tree_pak.setItemDelegate(_PakActionsDelegate(self))

        lay.addWidget(self.tree_sys)
        lay.addWidget(self.tree_proj)
        lay.addWidget(self.tree_pak)
        # Placeholders for missing configuration
        self.sys_placeholder = QLabel(self.tr("Please choose unpacked game directory using the Browse button above"))
        self.sys_placeholder.setAlignment(Qt.AlignCenter)
        self.sys_placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.pak_placeholder = QLabel("")
        self.pak_placeholder.setAlignment(Qt.AlignCenter)
        self.pak_placeholder.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        lay.addWidget(self.sys_placeholder)
        lay.addWidget(self.pak_placeholder)

        # Toggle buttons
        self.btn_sys .clicked.connect(lambda: self._switch(True))
        self.btn_proj.clicked.connect(lambda: self._switch(False))
        self.btn_pak_files.clicked.connect(lambda: self._switch_tab("pak"))

        # context menus
        self.tree_sys .customContextMenuRequested.connect(self._sys_menu)
        self.tree_proj.customContextMenuRequested.connect(self._proj_menu)
        self.tree_pak.customContextMenuRequested.connect(self._pak_menu)

        # pre‑load unpacked root
        if self.unpacked_dir and os.path.isdir(self.unpacked_dir):
            self._apply_unpacked_root(self.unpacked_dir)
        # Initialize PAK controls state and placeholders
        self._update_pak_controls_state()
        self._update_placeholders()

    def infer_project_game(self, project_path: Path | str) -> str | None:
        """Return the associated game for *project_path* if it can be inferred."""
        path = Path(project_path).resolve()

        try:
            from REasy import GAMES  # Local import to avoid circular dependency
        except Exception:
            GAMES = []

        parent_name = path.parent.name.upper()
        if parent_name in GAMES:
            return parent_name

        try:
            rel_parts = path.relative_to(PROJECTS_ROOT).parts
        except ValueError:
            rel_parts = ()
        if len(rel_parts) == 2:
            candidate = rel_parts[0].upper()
            if candidate in GAMES:
                return candidate

        cfg_path = path / ".reasy_project.json"
        if cfg_path.is_file():
            try:
                cfg = json.loads(cfg_path.read_text())
            except Exception:
                cfg = None
            if isinstance(cfg, dict):
                candidate = cfg.get("game")
                if isinstance(candidate, str):
                    candidate = candidate.upper()
                    if candidate in GAMES:
                        return candidate

        return None

    def _update_project_cfg(self, updates: dict):
        """Merge updates into per-project config file if a project is active."""
        if not self.project_dir or not updates:
            return
        try:
            cfg_path = Path(self.project_dir) / ".reasy_project.json"
            cfg = {}
            if cfg_path.exists():
                try:
                    cfg = json.loads(cfg_path.read_text())
                except Exception:
                    cfg = {}
            cfg.update(updates)
            cfg_path.write_text(json.dumps(cfg, indent=2))
        except Exception:
            pass

    def _update_path_label(self):
        if self._active_tab == "pak":
            self.path_label.setText(f"Game folder (PAKs): {self.pak_dir or '<i>not set</i>'}")
        else:
            self.path_label.setText(self.tr("Unpacked Game folder: {}").format(self.unpacked_dir or self.tr('<i>not set</i>')))

    def _browse(self):
        if self._active_tab == "pak":
            d = QFileDialog.getExistingDirectory(self, self.tr("Select Game Directory (contains .pak)"), self.pak_dir or "")
            if d:
                self._apply_pak_root(d)
        else:
            d = QFileDialog.getExistingDirectory(self, self.tr("Select unpacked Game folder"), self.unpacked_dir or "")
            if d:
                self._apply_unpacked_root(d)

    def _apply_unpacked_root(self, path):
        self.unpacked_dir = os.path.abspath(path)
        ok = self._check_folder(path)
        tick,color = ("✓","green") if ok else ("✗","red")
        self.path_label.setText(self.tr("Unpacked Game folder: <span style='color:{color}'>{tick}</span> {dir}").format(color=color, tick=tick, dir=self.unpacked_dir))

        if ok:
            self.model_sys.setRootPath(self.unpacked_dir)
            self.tree_sys.setRootIndex(self.model_sys.index(self.unpacked_dir))
            self._update_project_cfg({"unpacked_dir": self.unpacked_dir})
        else:
            QMessageBox.warning(
                self,
                self.tr("Invalid folder"),
                self.tr("Expected sub‑folder \"{}\".").format('/'.join(self._expected_native()))
            )
        self._update_placeholders()

    # Public wrapper for use by by main window
    def apply_unpacked_root(self, path: str):
        self._apply_unpacked_root(path)

    def _apply_pak_root(self, path):
        self.pak_dir = os.path.abspath(path)
        
        try:
            paks = scan_pak_files(self.pak_dir, ignore_mod_paks=self.pak_ignore_mods_cb.isChecked())
        except Exception as e:
            QMessageBox.critical(self, self.tr("Scan failed"), str(e))
            return
        if not paks:
            QMessageBox.warning(self, self.tr("Invalid folder"), self.tr("No .pak files found in the selected directory."))
        
        self._update_project_cfg({"pak_game_dir": self.pak_dir})
        self._active_tab = "pak"
        self._update_path_label()
        self._scan_paks()
        self._update_placeholders()

    # Public wrapper for use by by main window
    def apply_pak_root(self, path: str):
        self._apply_pak_root(path)

    def _check_folder(self, root):  
        exp = self._expected_native()
        return not exp or os.path.isdir(os.path.join(root,*exp))

    def check_unpacked_folder(self, root: str, game: str | None = None) -> bool:
        exp = EXPECTED_NATIVE.get(game or (self.current_game or ""), ())
        return not exp or os.path.isdir(os.path.join(root, *exp))

    def expected_native_tuple(self, game: str | None = None) -> tuple[str, ...]:
        return EXPECTED_NATIVE.get(game or (self.current_game or ""), ())

    def has_valid_paks(self, path: str | None, ignore_mod_paks: bool | None = None) -> bool:
        if not path:
            return False
        try:
            ignore = self.pak_ignore_mods_cb.isChecked() if ignore_mod_paks is None else ignore_mod_paks
            paks = scan_pak_files(path, ignore_mod_paks=ignore)
            return bool(paks)
        except Exception:
            return False

    def _switch(self, to_system):
        self._switch_tab("sys" if to_system else "proj")

    def _switch_tab(self, tab: str):
        self._active_tab = tab
        self.btn_sys.setChecked(tab == "sys")
        self.btn_proj.setChecked(tab == "proj")
        self.btn_pak_files.setChecked(tab == "pak")
        self.tree_sys.setVisible(tab == "sys")
        self.tree_proj.setVisible(tab == "proj")
        self.tree_pak.setVisible(tab == "pak")
        self._update_pak_controls_state()
        self._update_path_label()
        self._update_placeholders()

    def switch_tab(self, tab: str):
        self._switch_tab(tab)

    def _update_placeholders(self):
        # System Files placeholder
        sys_ok = bool(self.unpacked_dir) and self._check_folder(self.unpacked_dir)
        if self._active_tab == "sys":
            self.tree_sys.setVisible(sys_ok)
            self.sys_placeholder.setVisible(not sys_ok)
        else:
            self.sys_placeholder.setVisible(False)
        # PAK Files placeholder
        if self._active_tab == "pak":
            if not self.pak_dir:
                self.pak_placeholder.setText(self.tr("Please choose game directory (contains .pak) using the Browse button above"))
                self.pak_placeholder.setVisible(True)
                self.tree_pak.setVisible(False)
            elif not self._pak_base_paths:
                self.pak_placeholder.setText(self.tr("Please load a list using the Load .list… button above"))
                self.pak_placeholder.setVisible(True)
                self.tree_pak.setVisible(False)
            else:
                self.pak_placeholder.setVisible(False)
                self.tree_pak.setVisible(True)
        else:
            self.pak_placeholder.setVisible(False)

    def _update_pak_controls_state(self):
        on_pak = (self._active_tab == "pak")
        widgets_to_control = [self.pak_ignore_mods_cb, self.btn_scan_paks, self.btn_load_list, self.pak_list_edit, self.pak_filter_label, self.pak_filter_edit]
        if self.list_help_label:
            widgets_to_control.append(self.list_help_label)
        for w in widgets_to_control:
            w.setVisible(on_pak)
            w.setEnabled(on_pak)

    def set_project(self, proj_dir):
        self.project_dir = proj_dir
        for b in (self.btn_conf, self.btn_zip, self.btn_pak):
            b.setEnabled(bool(proj_dir))
        if proj_dir:
            if not self.current_game:
                inferred = self.infer_project_game(proj_dir)
                if inferred:
                    self.current_game = inferred
            if self.current_game:
                self._update_project_cfg({"game": self.current_game})
            self.project_label.setText(f"<b>{self.tr('Project')}: {os.path.basename(proj_dir)}</b>")
            self.model_proj.setRootPath(proj_dir)
            self.tree_proj .setRootIndex(self.model_proj.index(proj_dir))

            self._pak_base_paths = []
            self._pak_list_path = None
            self.pak_list_edit.setText("")
            self._pak_selected_paks = []
            self._pak_cached_reader = None
            self.pak_dir = None
            
            self.unpacked_dir = None
            self.model_sys.setRootPath("")
            self.tree_sys.setRootIndex(self.model_sys.index(""))
            self._update_path_label()
            # Restore saved PAK config if present
            try:
                cfg_path = Path(proj_dir) / ".reasy_project.json"
                if cfg_path.exists():
                    cfg = json.loads(cfg_path.read_text())
                    udir = cfg.get("unpacked_dir")
                    if udir and os.path.isdir(udir):
                        self.unpacked_dir = udir
                        self.model_sys.setRootPath(self.unpacked_dir)
                        self.tree_sys.setRootIndex(self.model_sys.index(self.unpacked_dir))
                        
                    self._update_path_label()
                    gdir = cfg.get("pak_game_dir")
                    if gdir and os.path.isdir(gdir):
                        self.pak_dir = gdir
                        self._scan_paks()
                        self._update_path_label()  # Update label after setting pak_dir
                    path = cfg.get("pak_list_path")
                    if path and os.path.isfile(path):
                        self._pak_list_path = path
                        self.pak_list_edit.setText(path)
                        self._load_pak_list_file(path)
            except Exception:
                pass
        else:
            self.project_label.setText(self.tr("<i>No project open</i>"))
            self.tree_proj .setRootIndex(QModelIndex())
            
            self._pak_base_paths = []
            self._pak_list_path = None
            self.pak_list_edit.setText("")
            self._pak_selected_paks = []
            self._pak_cached_reader = None
            self.pak_dir = None
            
            self.unpacked_dir = None
            self.model_sys.setRootPath("")
            self.tree_sys.setRootIndex(self.model_sys.index(""))
            self._update_path_label()
        self._update_placeholders()

    # ---------------- PAK integration ----------------
    def _scan_paks(self):
        if not self.pak_dir:
            QMessageBox.information(self, self.tr("Scan"), self.tr("Select a game directory first."))
            return
        try:
            paks = scan_pak_files(self.pak_dir, ignore_mod_paks=self.pak_ignore_mods_cb.isChecked())
        except Exception as e:
            QMessageBox.critical(self, self.tr("Scan failed"), str(e))
            return
        if not paks:
            QMessageBox.information(self, self.tr("Scan"), self.tr("No .pak files found."))
            self._pak_selected_paks = []
            self._pak_tree_model = None
            self.tree_pak.setModel(None)
            return
        self._pak_selected_paks = paks
        
        self._pak_cached_reader = None
        self._rebuild_pak_index()

    def _choose_pak_list(self):
        path, _ = QFileDialog.getOpenFileName(self, self.tr("Open list file"), filter=self.tr("List files (*.list *.txt);;All files (*)"))
        if not path:
            return
        self._load_pak_list_file(path)
        self._update_placeholders()

    def _load_pak_list_file(self, path: str):
        try:
            with open(path, "r", encoding="utf-8") as f:
                items = [ln.strip().replace("\\", "/").lower() for ln in f if ln.strip()]
        except Exception as e:
            QMessageBox.critical(self, self.tr("Read failed"), str(e))
            return
        self._pak_base_paths = items
        self.pak_list_edit.setText(path)
        self._pak_list_path = path
        
        self._update_project_cfg({"pak_list_path": path})
        
        self._pak_cached_reader = None
        self._rebuild_pak_index()

    def _rebuild_pak_index(self):
        
        if not self._pak_selected_paks:
            
            self._pak_tree_model = None
            self.tree_pak.setModel(None)
            self._update_placeholders()
            return
        if not self._pak_base_paths:
            
            self._pak_tree_model = None
            self.tree_pak.setModel(None)
            self._update_placeholders()
            return
        try:
            r = self._pak_cached_reader if isinstance(self._pak_cached_reader, CachedPakReader) else None
            if not r:
                r = CachedPakReader()
                r.pak_file_priority = list(self._pak_selected_paks)
            r.reset_file_list()
            if self._pak_base_paths:
                r.add_files(*self._pak_base_paths)
                r.cache_entries(assign_paths=True)
            else:
                r.cache_entries(assign_paths=False)
            self._pak_cached_reader = r
            
            valid = set(p.lower() for p in r.cached_paths(include_unknown=False))
            base = set(self._pak_base_paths)
            display = sorted(p for p in base if p in valid)
            self._pak_all_paths = display
            pop = set(display)
            try:
                if self._pak_cached_reader and self._pak_cached_reader._cache:
                    for p in self._pak_cached_reader.cached_paths(include_unknown=True):
                        if p.startswith("__Unknown/"):
                            pop.add(p)
            except Exception:
                pass
            self._pak_population_paths = sorted(pop)
            self._build_pak_tree_model(display)
            self._apply_pak_filter_now()
            self._update_placeholders()
        except Exception as e:
            QMessageBox.critical(self, self.tr("Index failed"), str(e))
            self._pak_tree_model = None
            self.tree_pak.setModel(None)
            self._update_placeholders()

    def _build_pak_tree_model(self, paths: list[str]):
        model = QStandardItemModel()
        model.setHorizontalHeaderLabels([self.tr("Paths")])
        
        display_paths = list(paths)
        try:
            if self._pak_cached_reader and self._pak_cached_reader._cache:
                all_cached = self._pak_cached_reader.cached_paths(include_unknown=True)
                for p in all_cached:
                    if p.startswith("__Unknown/"):
                        display_paths.append(p)
        except Exception:
            pass
        
        root: dict[str, dict] = {}
        for p in sorted(set(display_paths)):
            parts = p.split('/')
            node = root
            for part in parts:
                node = node.setdefault(part, {})
                
        style = self.style()
        dir_icon  = style.standardIcon(QStyle.SP_DirIcon)
        file_icon = style.standardIcon(QStyle.SP_FileIcon)
        
        def build(parent, node, prefix=""):
            for name, child in node.items():
                item = QStandardItem(name)
                item.setEditable(False)
                full = prefix + name
                if child:
                    item.setIcon(dir_icon)
                    
                    item.setData(full + "/", Qt.UserRole + 2)
                    parent.appendRow(item)
                    build(item, child, full + "/")
                else:
                    item.setIcon(file_icon)
                    item.setData(full, Qt.UserRole + 1)
                    parent.appendRow(item)
        build(model.invisibleRootItem(), root, "")
        self.tree_pak.setModel(model)
        self._pak_tree_model = model

    def _apply_pak_filter(self):
        """Immediate application for external triggers; debounced during typing."""
        self._apply_pak_filter_now()

    def _on_pak_filter_text_changed(self, _=None):
        self._pak_filter_timer.start(120)

    def _apply_pak_filter_now(self):
        """Apply regex/text filter to PAK paths and show flat results with actions."""
        if not hasattr(self, "pak_filter_edit"):
            return
        text = self.pak_filter_edit.text().strip()
        if not text:
            if self._pak_tree_model is not None:
                self.tree_pak.setModel(self._pak_tree_model)
                self.tree_pak.setEditTriggers(QAbstractItemView.NoEditTriggers)
            return

        if self._pak_flat_model is None:
            self._pak_flat_model = QStringListModel(self)
        if not self._pak_population_paths:
            self._pak_population_paths = list(self._pak_all_paths or [])
        self._pak_flat_model.setStringList(self._pak_population_paths)

        proxy = self._pak_filter_proxy
        proxy.setSourceModel(self._pak_flat_model)
        pat = QRegularExpression(text)
        if not pat.isValid():
            pat = QRegularExpression(QRegularExpression.escape(text))
        pat.setPatternOptions(QRegularExpression.CaseInsensitiveOption)
        proxy.setFilterRegularExpression(pat)
        self.tree_pak.setModel(proxy)
        self.tree_pak.setEditTriggers(QAbstractItemView.NoEditTriggers)

    def _collect_selected_pak_paths(self) -> list[str]:
        paths: list[str] = []
        model = self.tree_pak.model()
        if model is None:
            return paths
        if isinstance(model, (QSortFilterProxyModel, QStringListModel)):
            for idx in self.tree_pak.selectedIndexes():
                val = idx.data(Qt.DisplayRole)
                if isinstance(val, str) and val:
                    paths.append(val)
            return sorted(set(paths))
        def collect_from_index(idx):
            if model.rowCount(idx) == 0:
                p = idx.data(Qt.UserRole + 1)
                if isinstance(p, str) and p:
                    paths.append(p)
                return
            # Folder: collect files under it
            rows = model.rowCount(idx)
            for i in range(rows):
                child = model.index(i, 0, idx)
                collect_from_index(child)
        for idx in self.tree_pak.selectedIndexes():
            collect_from_index(idx)
        return sorted(set(paths))

    def _extract_folder_by_index(self, index):
        if not self._pak_tree_model or not index.isValid():
            return
        model = self._pak_tree_model
        to_add: list[str] = []
        def collect_desc(idx):
            rows = model.rowCount(idx)
            if rows == 0:
                p = idx.data(Qt.UserRole + 1)
                if isinstance(p, str) and p:
                    to_add.append(p)
                return
            for i in range(rows):
                child = model.index(i, 0, idx)
                collect_desc(child)
        collect_desc(index)
        if to_add:
            self._extract_from_paks_to_project(sorted(set(to_add)))

    def _extract_folder_by_prefix(self, folder_prefix: str):
        if not self._pak_cached_reader:
            return
        try:
            base_name = os.path.basename(folder_prefix.rstrip('/'))
            if QMessageBox.question(
                self,
                self.tr("Confirm Add"),
                self.tr(f"Add entire folder\n\"{base_name}\" and all its contents?"),
                QMessageBox.Yes | QMessageBox.No
            ) != QMessageBox.Yes:
                return
            if self._pak_cached_reader._cache is None:
                self._pak_cached_reader.cache_entries(assign_paths=False)
            named = set(self._pak_cached_reader.cached_paths(include_unknown=True))
            targets = [p for p in named if p.startswith(folder_prefix)]
            if self._pak_base_paths:
                for p in self._pak_base_paths:
                    if p.startswith(folder_prefix):
                        targets.append(p)
            targets = sorted(set(targets))
            if targets:
                self._extract_from_paks_to_project(targets)
        except Exception as e:
            QMessageBox.critical(self, self.tr("Add failed"), str(e))

    def _pak_menu(self, pos):
        model = self.tree_pak.model()
        if model is None:
            return
        idx = self.tree_pak.indexAt(pos)
        if not idx.isValid():
            return
        menu = QMenu(self)
        add_act = menu.addAction(self.tr("Add to project"))
        open_act = menu.addAction(self.tr("Open"))
        chosen = menu.exec(self.tree_pak.viewport().mapToGlobal(pos))
        if chosen is add_act:
            folder_prefix = idx.data(Qt.UserRole + 2)
            is_folder = False
            if isinstance(folder_prefix, str) and folder_prefix.endswith('/'):
                is_folder = True
            else:
                try:
                    is_folder = model.hasChildren(idx) or model.rowCount(idx) > 0
                except Exception:
                    is_folder = False
            if is_folder and isinstance(folder_prefix, str) and folder_prefix.endswith('/'):
                self._extract_folder_by_prefix(folder_prefix)
            else:
                sel = self._collect_selected_pak_paths()
                self._extract_from_paks_to_project(sel)
        elif chosen is open_act:
            sel = self._collect_selected_pak_paths()
            if sel:
                self._open_pak_path_in_editor(sel[0])

    def _on_pak_double(self, idx):
        model = self.tree_pak.model()
        if model is None or not idx.isValid():
            return
        if isinstance(model, (QSortFilterProxyModel, QStringListModel)):
            path = idx.data(Qt.DisplayRole)
        else:
            path = idx.data(Qt.UserRole + 1)
        if isinstance(path, str) and path:
            self._open_pak_path_in_editor(path)

    def _open_pak_path_in_editor(self, path: str):
        if not self._pak_selected_paks:
            return
        try:
            r = PakReader()
            r.pak_file_priority = list(self._pak_selected_paks)
            r.add_files(path)
            found = None
            for pth, stream in r.find_files():
                if pth.lower() == path.lower():
                    found = (pth, stream)
                    break
            if not found:
                QMessageBox.information(self, self.tr("Open"), f"{self.tr('Path')} not found in {self.tr('PAKs')}: {path}")
                return
            pth, stream = found
            data = stream.read()
            try:
                ext = guess_extension_from_header(data[:64])
            except Exception:
                ext = None
            name = pth if ('.' in os.path.basename(pth)) else (pth + ('.' + ext.lower() if ext else ''))
            self.app_win.add_tab(name, data)
        except Exception as e:
            QMessageBox.critical(self, self.tr("Open failed"), str(e))

    def _extract_from_paks_to_project(self, paths: list[str]):
        if not paths:
            return
        if not self.project_dir:
            QMessageBox.information(self, self.tr("Add to project"), self.tr("Open a project first."))
            return
        if not self._pak_selected_paks:
            QMessageBox.information(self, self.tr("Add to project"), self.tr("Scan for .pak files first."))
            return
        try:
            r = self._pak_cached_reader if isinstance(self._pak_cached_reader, CachedPakReader) else None
            if not r:
                r = CachedPakReader()
                r.pak_file_priority = list(self._pak_selected_paks)
                if self._pak_base_paths:
                    r.add_files(*self._pak_base_paths)
                    r.cache_entries(assign_paths=True)
                else:
                    r.cache_entries(assign_paths=False)
                self._pak_cached_reader = r
            missing: list[str] = []
            targets = sorted(set(paths))
            count = r.extract_files_to(self.project_dir, targets, missing_files=missing)
            self._refresh_proj()
            msg = f"{self.tr('Added')} {count} {self.tr('file(s) to project.')}"
            if missing:
                msg += "\n\n" + self.tr("Missing paths (not found in PAKs):") + "\n" + "\n".join(missing[:50])
            QMessageBox.information(self, self.tr("Done"), msg)
        except Exception as e:
            QMessageBox.critical(self, self.tr("Add failed"), str(e))

    def _sys_menu(self, pos):
        idx = self.tree_sys.indexAt(pos)
        if not idx.isValid() or self.model_sys.isDir(idx):
            return

        menu = QMenu(self)
        add_act = menu.addAction(self.tr("Add to project"))

        chosen = menu.exec(self.tree_sys.viewport().mapToGlobal(pos))
        if chosen is add_act:
            self._copy_to_project(self.model_sys.filePath(idx))

    def _proj_menu(self, pos):
        idx = self.tree_proj.indexAt(pos)
        if not idx.isValid() or self.model_proj.isDir(idx):
            return

        menu = QMenu(self)
        remove_act = menu.addAction(self.tr("Remove"))

        chosen = menu.exec(self.tree_proj.viewport().mapToGlobal(pos))
        if chosen is remove_act:
            self._remove_from_project(self.model_proj.filePath(idx))

    def _copy_to_project(self, src):
        if not self.project_dir or not self._check_folder(self.unpacked_dir):
            return
        rel = os.path.relpath(src, self.unpacked_dir)
        dst = os.path.join(self.project_dir, rel)

        if os.path.isdir(src) and QMessageBox.question(
                self, self.tr("Confirm Add"), self.tr(f"Add entire folder\n\"{os.path.basename(src)}\" and all its contents?"),
                QMessageBox.Yes|QMessageBox.No) != QMessageBox.Yes:
            return

        if os.path.exists(dst) and QMessageBox.question(
                self, self.tr("Confirm Overwrite"), self.tr(f"""\"{rel}\" already exists — overwrite?"""),
                QMessageBox.Yes|QMessageBox.No) != QMessageBox.Yes:
            return

        try:
            if os.path.isdir(src): 
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:                   
                (os.makedirs(os.path.dirname(dst), exist_ok=True), shutil.copy2(src, dst))
            self._refresh_proj()
            QMessageBox.information(self, self.tr("Added"), f"{rel}\n{self.tr('was copied successfully.')}")
        except Exception as e:
            QMessageBox.critical(self, self.tr("Copy failed"), str(e))

    def _prune_empty_dirs(self, start_path: str) -> bool:
        removed = False
        p = start_path
        while (p and p.startswith(self.project_dir)
            and p != self.project_dir
            and os.path.isdir(p) and not os.listdir(p)):
            try:
                os.rmdir(p)
                removed = True
            except OSError:
                break       
            p = os.path.dirname(p)
        return removed

    def _remove_from_project(self, path: str):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
                self._prune_empty_dirs(os.path.dirname(path))
        except Exception as e:
            QMessageBox.critical(self, self.tr("Remove failed"), str(e))
            return

        parent = os.path.dirname(path)
        while parent and parent.startswith(self.project_dir):
            idx = self.model_proj.index(parent)
            if idx.isValid() and self.model_proj.rowCount(idx) == 0:
                self.tree_proj.collapse(idx)
            parent = os.path.dirname(parent)
        self.tree_proj.setModel(self.model_proj)
        self.model_proj.setRootPath(self.project_dir or "")
        if self.project_dir:
            self.tree_proj.setRootIndex(self.model_proj.index(self.project_dir))

    def _refresh_proj(self):
        self.model_proj = QFileSystemModel()
        self.model_proj.setRootPath(self.project_dir or "")
        
        self.tree_proj.setModel(self.model_proj)
        self.tree_proj.setItemDelegateForColumn(0, _ActionsDelegate(self, True))
        self.tree_proj.setIndentation(8)
        self.tree_proj.hideColumn(1)
        self.tree_proj.hideColumn(2)
        self.tree_proj.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree_proj.header().setSectionResizeMode(3, QHeaderView.Fixed)
        self.tree_proj.header().resizeSection(3, 100)
        self.tree_proj.header().setStretchLastSection(False)
        self.tree_proj.header().setMinimumSectionSize(100)
        if self.project_dir:
            self.tree_proj.setRootIndex(self.model_proj.index(self.project_dir))

    def _open_in_editor(self, path):
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
                self.app_win.add_tab(path, data)
            except Exception as e:
                QMessageBox.critical(self, self.tr("Open failed"), str(e))

    def _on_double(self, idx, in_project):
        path = (self.model_proj if in_project else self.model_sys).filePath(idx)
        if os.path.isfile(path):
            self._open_in_editor(path)

    def _choose_game(self) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr("Select Game"))
        lay = QVBoxLayout(dlg)

        info_lbl = QLabel()                  
        info_lbl.setStyleSheet("font-weight:bold;")
        lay.addWidget(info_lbl)

        combo = QComboBox()
        from REasy import GAMES
        combo.addItems(GAMES)
        lay.addWidget(combo)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        lay.addWidget(btns)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)

        def _upd(idx: int):
            g = combo.itemText(idx)
            hint = "/".join(EXPECTED_NATIVE.get(g, ()))
            info_lbl.setText(f"{self.tr('Expected sub‑folder for')} <b>{g}</b>: "
                             f"<code>{hint or '‑‑ any ‑‑'}</code>"
                             f"<br>{self.tr('Note')}: {self.tr('Make sure your directory contains that folder.')}")
        _upd(0)
        combo.currentIndexChanged.connect(_upd)

        return combo.currentText() if dlg.exec() == QDialog.Accepted else None
    
    def _on_section_resized(self, logical_index: int, old_size: int, new_size: int):
        """Handle manual column resizing with minimum width enforcement."""
        if logical_index != 0:
            return
        
        sender_header = self.sender()
        min_width = sender_header.minimumSectionSize()
        
        if new_size < min_width:
            sender_header.resizeSection(0, min_width)
        
        for tree in (self.tree_sys, self.tree_proj, self.tree_pak):
            if tree.header() == sender_header:
                delegate = tree.itemDelegateForColumn(0)
                if hasattr(delegate, 'set_column_width'):
                    delegate.set_column_width(max(new_size, min_width))
                tree.viewport().update() 
                break

    def _proj_settings(self):
        if not self.project_dir:
            return
        dlg = ProjectSettingsDialog(Path(self.project_dir), self)
        dlg.exec()

    def _export_zip(self):
        if not self.project_dir:
            QMessageBox.information(self, self.tr("Export Fluffy ZIP"), self.tr("Open a project first."))
            return

        proj     = Path(self.project_dir)
        cfg_path = proj / ".reasy_project.json"
        if not cfg_path.exists():
            if QMessageBox.question(
                self, self.tr("Missing info"),
                self.tr("Project settings not configured yet.\nOpen the settings dialog now?"),
                QMessageBox.Yes | QMessageBox.No
            ) == QMessageBox.Yes:
                self._proj_settings()
            if not cfg_path.exists():
                return

        base_dir    = _get_base_dir()
        mods_folder = base_dir / "Mods"
        mods_folder.mkdir(parents=True, exist_ok=True)
        cfg  = json.loads(cfg_path.read_text())
        name = cfg.get("name", proj.name)
        zip_path = mods_folder / f"{name}.zip"

        try:
            create_fluffy_zip(proj, zip_path)
            QMessageBox.information(
                self, self.tr("Done"),
                self.tr(f"Fluffy mod ZIP created.\nSaved to:\n{zip_path}")
            )
        except Exception as e:
            QMessageBox.critical(self, self.tr("ZIP failed"), str(e))

    def _export_mod(self):

        need, latest = packer_status()
        if need:
            tag_txt = latest or "latest"
            msg = (
                self.tr(f"The REE.PAK packer ({tag_txt}) is not downloaded yet\n")
                if not _EXE_PATH.exists()
                else self.tr(f"A newer packer release ({tag_txt}) is available\n")
            ) + self.tr("Do you want to download it now?")
            if QMessageBox.question(self, self.tr("Download packer?"), msg,
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            try:
                _ensure_packer(auto_download=True)
            except Exception as e:
                QMessageBox.critical(self, self.tr("Download failed"), str(e))
                return

        base_dir    = _get_base_dir()
        mods_folder = base_dir  / "Mods"
        mods_folder.mkdir(parents=True, exist_ok=True)

        from ui.project_manager.manager import quitely_get_pak_name
        pak_name = quitely_get_pak_name(Path(self.project_dir)) or Path(self.project_dir).name
        if not pak_name.lower().endswith(".pak"):
            pak_name += ".pak"
        dest_path = mods_folder / pak_name

        dlg = QDialog(self)
        dlg.setWindowTitle(self.tr("REE.Packer output"))
        v = QVBoxLayout(dlg)
        log = QTextEdit(readOnly=True, lineWrapMode=QTextEdit.NoWrap)
        v.addWidget(log)
        bar = QProgressBar()
        bar.setRange(0, 0)
        v.addWidget(bar)
        dlg.resize(600, 400)
        dlg.show()

        from concurrent.futures import ThreadPoolExecutor
        exec_ = ThreadPoolExecutor(max_workers=1)

        def _work():
            return run_packer(self.project_dir, str(dest_path))

        fut = exec_.submit(_work)

        def _poll():
            if fut.done():
                code, out = fut.result()
                bar.hide()
                log.append(out)
                if code == 0:
                    QMessageBox.information(
                        self, self.tr("Done"),
                        self.tr(f"Export completed.\nPAK file saved to:\n{dest_path}")
                    )
                else:
                    QMessageBox.critical(self, self.tr("Error"),
                                         self.tr("PAK packer returned an error."))
            else:
                QTimer.singleShot(150, _poll)

        QTimer.singleShot(100, _poll)
        
def quitely_get_pak_name(project_dir: Path) -> str | None:
    try:
        cfg = json.loads((project_dir/".reasy_project.json").read_text())
        return cfg.get("pak_name")
    except Exception:
        return None