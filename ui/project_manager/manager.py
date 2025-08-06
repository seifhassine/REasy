from __future__ import annotations
import os
import shutil
from pathlib import Path
import json
import sys
from PySide6.QtCore    import Qt, QModelIndex, QTimer
from PySide6.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QToolButton,
    QPushButton, QLabel, QFileDialog, QFileSystemModel, QMessageBox,
    QHeaderView, QMenu, QDialogButtonBox, QDialog, QComboBox, QTextEdit, QProgressBar
)
from tools.pak_exporter import packer_status, _EXE_PATH, _ensure_packer, run_packer

from .constants  import EXPECTED_NATIVE
from .delegate   import _ActionsDelegate
from .trees      import _DndTree, _DropTree

from ui.project_manager.project_settings_dialog import ProjectSettingsDialog
from tools.fluffy_exporter import create_fluffy_zip

from PySide6.QtCore import qInstallMessageHandler

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
        super().__init__("Project Browser", app_window)
        self.app_win       = app_window
        self.current_game  = getattr(app_window, "current_game", None)
        self.unpacked_dir  = os.path.abspath(unpacked_root) if unpacked_root else None
        self.project_dir   = None

        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self._delayed_column_resize)

        # -- UI scaffold -----------------------------------------------------
        c = QWidget(self)
        self.setWidget(c)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(2,2,2,2)

        # top: unpacked‑path display + browse
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.path_label = QLabel()
        bar.addWidget(self.path_label, 1)
        self.path_label.setMinimumSize(20, 20)
        bar.addWidget(QPushButton("Browse…", clicked=self._browse))
        self._update_path_label()

        # project label
        self.project_label = QLabel("<i>No project open</i>")
        lay.addWidget(self.project_label)

        actions = QHBoxLayout()
        lay.addLayout(actions)    

        self.btn_conf = QPushButton("Fluffy Settings…", clicked=self._proj_settings)
        self.btn_zip  = QPushButton("Export Fluffy ZIP", clicked=self._export_zip)
        self.btn_pak  = QPushButton("Export .PAK",       clicked=self._export_mod)

        actions.addWidget(self.btn_conf)
        actions.addWidget(self.btn_zip)
        actions.addWidget(self.btn_pak)

        toggles = QHBoxLayout()
        lay.addLayout(toggles)

        self.btn_sys  = QToolButton(text="System Files",  checkable=True, checked=True)
        self.btn_proj = QToolButton(text="Project Files", checkable=True)

        toggles.addWidget(self.btn_sys)
        toggles.addWidget(self.btn_proj)
        toggles.addStretch(1)

        for b in (self.btn_conf, self.btn_zip, self.btn_pak):
            b.setEnabled(False)
            
        # models + views -----------------------------------------------------
        self.model_sys,  self.tree_sys  = QFileSystemModel(), _DndTree()
        self.model_proj, self.tree_proj = QFileSystemModel(), _DropTree(self)
        self.tree_proj.hide()

        for tree, model in ((self.tree_sys, self.model_sys), (self.tree_proj, self.model_proj)):
            tree.setModel(model)
            tree.setContextMenuPolicy(Qt.CustomContextMenu)
            tree.hideColumn(1) 
            tree.hideColumn(2)
            
            model.directoryLoaded.connect(lambda: self._schedule_column_resize())
            model.rowsInserted.connect(lambda: self._schedule_column_resize())
            model.rowsRemoved.connect(lambda: self._schedule_column_resize())
            
            hdr = tree.header()
            hdr.setSectionResizeMode(0, QHeaderView.Interactive)
            hdr.setMinimumSectionSize(160) 
            hdr.sectionResized.connect(self._on_section_resized)

        # icon delegate
        self.tree_sys .setItemDelegateForColumn(0, _ActionsDelegate(self, False))
        self.tree_proj.setItemDelegateForColumn(0, _ActionsDelegate(self, True))

        # double‑click open
        self.tree_sys.doubleClicked .connect(lambda idx: self._on_double(idx, False))
        self.tree_proj.doubleClicked.connect(lambda idx: self._on_double(idx, True))

        lay.addWidget(self.tree_sys)
        lay.addWidget(self.tree_proj)

        # toggle buttons
        self.btn_sys .clicked.connect(lambda: self._switch(True))
        self.btn_proj.clicked.connect(lambda: self._switch(False))

        # context menus
        self.tree_sys .customContextMenuRequested.connect(self._sys_menu)
        self.tree_proj.customContextMenuRequested.connect(self._proj_menu)

        # pre‑load unpacked root
        if self.unpacked_dir and os.path.isdir(self.unpacked_dir):
            self._apply_unpacked_root(self.unpacked_dir)

    def _schedule_column_resize(self):
        """Schedule a delayed column resize to avoid excessive updates."""
        self._resize_timer.start(100) 

    def _delayed_column_resize(self):
        """Perform the actual column resize after delay."""
        if self.btn_sys.isChecked():
            self._adjust_column_widths(self.tree_sys)
        else:
            self._adjust_column_widths(self.tree_proj)
    
    def force_column_resize(self):
        """Force an immediate column resize - useful for very long filenames."""
        active_tree = self.tree_sys if self.btn_sys.isChecked() else self.tree_proj
        self._adjust_column_widths_aggressive(active_tree)
    
    def _adjust_column_widths_aggressive(self, tree):
        """More aggressive column width calculation for edge cases."""
        if tree.model() is None:
            return
        
        fm = tree.fontMetrics()
        min_width = 160
        max_width = 800
        
        root_idx = tree.rootIndex()
        if not root_idx.isValid():
            root_idx = tree.model().index(tree.model().rootPath())
        
        def check_all_items(parent_idx, depth=0):
            if depth > 5:
                return min_width
            
            current_max = min_width
            row_count = tree.model().rowCount(parent_idx)
            
            for i in range(row_count):
                idx = tree.model().index(i, 0, parent_idx)
                text = tree.model().data(idx, Qt.DisplayRole) or ""
                
                base_ui_space = 50 + 25 + 25 + 40 
                indentation_space = depth * 30    
                text_width = fm.horizontalAdvance(text)
                
                total_width = text_width + base_ui_space + indentation_space
                current_max = max(current_max, total_width)
                
                if tree.model().hasChildren(idx):
                    child_max = check_all_items(idx, depth + 1)
                    current_max = max(current_max, child_max)
            
            return min(current_max, max_width)
        
        optimal_width = check_all_items(root_idx)
        
        optimal_width = min(optimal_width + 60, max_width)
        
        tree.header().setMinimumSectionSize(min_width)
        tree.header().resizeSection(0, optimal_width)
        tree.header().setSectionResizeMode(0, QHeaderView.Interactive)

    def _update_path_label(self):
        self.path_label.setText(f"Unpacked Game folder: {self.unpacked_dir or '<i>not set</i>'}")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "Select unpacked Game folder", self.unpacked_dir or "")
        if d:
            self._apply_unpacked_root(d)

    def _apply_unpacked_root(self, path):
        self.unpacked_dir = os.path.abspath(path)
        ok = self._check_folder(path)
        tick,color = ("✓","green") if ok else ("✗","red")
        self.path_label.setText(f"Unpacked Game folder: <span style='color:{color}'>{tick}</span> {self.unpacked_dir}")

        if ok:
            self.model_sys.setRootPath(self.unpacked_dir)
            self.tree_sys.setRootIndex(self.model_sys.index(self.unpacked_dir))
            self._schedule_column_resize()
            if hasattr(self.app_win, "settings"):
                self.app_win.settings["unpacked_path"] = self.unpacked_dir
                self.app_win.save_settings()
        else:
            QMessageBox.warning(self, "Invalid folder",
                                f"Expected sub‑folder \"{'/'.join(self._expected_native())}\".")

    def _check_folder(self, root):  
        exp = self._expected_native()
        return not exp or os.path.isdir(os.path.join(root,*exp))

    def _switch(self, to_system):
        self.btn_sys.setChecked(to_system)
        self.btn_proj.setChecked(not to_system)
        self.tree_sys.setVisible(to_system)
        self.tree_proj.setVisible(not to_system)
        self._schedule_column_resize()

    def set_project(self, proj_dir):
        self.project_dir = proj_dir
        for b in (self.btn_conf, self.btn_zip, self.btn_pak):
            b.setEnabled(bool(proj_dir))
        if proj_dir:
            self.project_label.setText(f"<b>Project: {os.path.basename(proj_dir)}</b>")
            self.model_proj.setRootPath(proj_dir)
            self.tree_proj .setRootIndex(self.model_proj.index(proj_dir))
            self._schedule_column_resize()
        else:
            self.project_label.setText("<i>No project open</i>")
            self.tree_proj .setRootIndex(QModelIndex())

    def _sys_menu(self, pos):
        idx = self.tree_sys.indexAt(pos)
        if not idx.isValid() or self.model_sys.isDir(idx):
            return

        menu = QMenu(self)
        add_act = menu.addAction("Add to project")

        chosen = menu.exec(self.tree_sys.viewport().mapToGlobal(pos))
        if chosen is add_act:
            self._copy_to_project(self.model_sys.filePath(idx))

    def _proj_menu(self, pos):
        idx = self.tree_proj.indexAt(pos)
        if not idx.isValid() or self.model_proj.isDir(idx):
            return

        menu = QMenu(self)
        remove_act = menu.addAction("Remove")

        chosen = menu.exec(self.tree_proj.viewport().mapToGlobal(pos))
        if chosen is remove_act:
            self._remove_from_project(self.model_proj.filePath(idx))

    def _copy_to_project(self, src):
        if not self.project_dir or not self._check_folder(self.unpacked_dir):
            return
        rel = os.path.relpath(src, self.unpacked_dir)
        dst = os.path.join(self.project_dir, rel)

        if os.path.isdir(src) and QMessageBox.question(
                self, "Confirm Add", f"Add entire folder\n\"{os.path.basename(src)}\" and all its contents?",
                QMessageBox.Yes|QMessageBox.No) != QMessageBox.Yes:
            return

        if os.path.exists(dst) and QMessageBox.question(
                self, "Confirm Overwrite", f"""{rel}" already exists — overwrite?""",
                QMessageBox.Yes|QMessageBox.No) != QMessageBox.Yes:
            return

        try:
            if os.path.isdir(src): 
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:                   
                (os.makedirs(os.path.dirname(dst), exist_ok=True), shutil.copy2(src, dst))
            self._refresh_proj()
            QMessageBox.information(self, "Added", f"{rel}\nwas copied successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Copy failed", str(e))

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
            QMessageBox.critical(self, "Remove failed", str(e))
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
            self._schedule_column_resize()

    def _refresh_proj(self):
        self.model_proj = QFileSystemModel()
        self.model_proj.setRootPath(self.project_dir or "")
        
        self.model_proj.directoryLoaded.connect(lambda: self._schedule_column_resize())
        self.model_proj.rowsInserted.connect(lambda: self._schedule_column_resize())
        self.model_proj.rowsRemoved.connect(lambda: self._schedule_column_resize())
        
        self.tree_proj.setModel(self.model_proj)
        self.tree_proj.setItemDelegateForColumn(0, _ActionsDelegate(self, True))
        self.tree_proj.hideColumn(1)
        self.tree_proj.hideColumn(2)
        self.tree_proj.header().setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree_proj.header().setMinimumSectionSize(160)
        if self.project_dir:
            self.tree_proj.setRootIndex(self.model_proj.index(self.project_dir))
            self._schedule_column_resize()

    def _open_in_editor(self, path):
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
                self.app_win.add_tab(path, data)
            except Exception as e:
                QMessageBox.critical(self, "Open failed", str(e))

    def _on_double(self, idx, in_project):
        path = (self.model_proj if in_project else self.model_sys).filePath(idx)
        if os.path.isfile(path):
            self._open_in_editor(path)

    def _choose_game(self) -> str | None:
        dlg = QDialog(self)
        dlg.setWindowTitle("Select Game")
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
            info_lbl.setText(f"Expected sub‑folder for <b>{g}</b>: "
                             f"<code>{hint or '‑‑ any ‑‑'}</code>"
                             f"<br>Note: Make sure your directory contains that folder.")
        _upd(0)
        combo.currentIndexChanged.connect(_upd)

        return combo.currentText() if dlg.exec() == QDialog.Accepted else None
    
    def _adjust_column_widths(self, tree):
        """Adjust column widths to fit content with minimum constraints."""
        if tree.model() is None:
            return
        
        fm = tree.fontMetrics()
        min_width = 160 
        max_width = 600 
        
        root_idx = tree.rootIndex()
        if not root_idx.isValid():
            root_idx = tree.model().index(tree.model().rootPath())
        
        def check_items(parent_idx, depth=0):
            if depth > 3:
                return min_width
            
            current_max = min_width
            for i in range(min(tree.model().rowCount(parent_idx), 50)): 
                idx = tree.model().index(i, 0, parent_idx)
                text = tree.model().data(idx, Qt.DisplayRole) or ""
                
                base_ui_space = 40 + 20 + 20 + 30 
                indentation_space = depth * 25
                text_width = fm.horizontalAdvance(text)
                
                total_width = text_width + base_ui_space + indentation_space
                current_max = max(current_max, total_width)
                
                if tree.isExpanded(idx):
                    child_max = check_items(idx, depth + 1)
                    current_max = max(current_max, child_max)
            
            return min(current_max, max_width)
        
        optimal_width = check_items(root_idx)
        
        if optimal_width > 300:
            optimal_width = min(optimal_width + 50, max_width)
        
        tree.header().setMinimumSectionSize(min_width)
        tree.header().resizeSection(0, optimal_width)
        tree.header().setSectionResizeMode(0, QHeaderView.Interactive)

    def _on_section_resized(self, logical_index: int, old_size: int, new_size: int):
        """Handle manual column resizing with minimum width enforcement."""
        if logical_index != 0:
            return
        
        sender_header = self.sender()
        min_width = sender_header.minimumSectionSize()
        
        if new_size < min_width:
            sender_header.resizeSection(0, min_width)
        
        for tree in (self.tree_sys, self.tree_proj):
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
            QMessageBox.information(self, "Export Fluffy ZIP", "Open a project first.")
            return

        proj     = Path(self.project_dir)
        cfg_path = proj / ".reasy_project.json"
        if not cfg_path.exists():
            if QMessageBox.question(
                self, "Missing info",
                "Project settings not configured yet.\nOpen the settings dialog now?",
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
                self, "Done",
                f"Fluffy mod ZIP created.\nSaved to:\n{zip_path}"
            )
        except Exception as e:
            QMessageBox.critical(self, "ZIP failed", str(e))

    def _export_mod(self):

        need, latest = packer_status()
        if need:
            tag_txt = latest or "latest"
            msg = (
                f"The REE.PAK packer ({tag_txt}) is not downloaded yet\n"
                if not _EXE_PATH.exists()
                else f"A newer packer release ({tag_txt}) is available\n"
            ) + "Do you want to download it now?"
            if QMessageBox.question(self, "Download packer?", msg,
                                    QMessageBox.Yes | QMessageBox.No) != QMessageBox.Yes:
                return
            try:
                _ensure_packer(auto_download=True)
            except Exception as e:
                QMessageBox.critical(self, "Download failed", str(e))
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
        dlg.setWindowTitle("REE.Packer output")
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
                        self, "Done",
                        f"Export completed.\nPAK file saved to:\n{dest_path}"
                    )
                else:
                    QMessageBox.critical(self, "Error",
                                         "PAK packer returned an error.")
            else:
                QTimer.singleShot(150, _poll)

        QTimer.singleShot(100, _poll)
        
def quitely_get_pak_name(project_dir: Path) -> str | None:
    try:
        cfg = json.loads((project_dir/".reasy_project.json").read_text())
        return cfg.get("pak_name")
    except Exception:
        return None