# ui/project_manager/manager.py

import os
import shutil
from PySide6.QtCore    import QModelIndex
from PySide6.QtWidgets import (
    QDockWidget, QWidget, QVBoxLayout, QHBoxLayout, QToolButton,
    QPushButton, QLabel, QFileDialog, QFileSystemModel, QMessageBox,
    QHeaderView, QMenu
)
from .proxy    import ActionsProxyModel
from .delegate import _ActionsDelegate
from .trees    import _DndTree, _DropTree
from .constants import EXPECTED_NATIVE

class ProjectManager(QDockWidget):
    def __init__(self, app_window, unpacked_root: str | None = None):
        super().__init__("Project Browser", app_window)
        self.app_win      = app_window
        self.current_game = getattr(app_window, "current_game", None)
        self.unpacked_dir = os.path.abspath(unpacked_root) if unpacked_root else None
        self.project_dir  = None

        # — UI scaffold —
        c = QWidget(self)
        self.setWidget(c)
        lay = QVBoxLayout(c)
        lay.setContentsMargins(2,2,2,2)

        # path bar
        bar = QHBoxLayout()
        lay.addLayout(bar)
        self.path_label = QLabel()
        bar.addWidget(self.path_label, 1)
        bar.addWidget(QPushButton("Browse…", clicked=self._browse))
        self._update_path_label()

        # project label
        self.project_label = QLabel("<i>No project open</i>")
        lay.addWidget(self.project_label)

        # toggle buttons
        toggles = QHBoxLayout()
        lay.addLayout(toggles)
        self.btn_sys  = QToolButton(text="System Files",  checkable=True, checked=True)
        self.btn_proj = QToolButton(text="Project Files", checkable=True)
        toggles.addWidget(self.btn_sys)
        toggles.addWidget(self.btn_proj)
        toggles.addStretch(1)

        # — System‑pane models/views —
        self.source_model_sys = QFileSystemModel(self)
        self.model_sys        = ActionsProxyModel(self)
        self.model_sys.setSourceModel(self.source_model_sys)
        self.tree_sys = _DndTree()
        self.tree_sys.setModel(self.model_sys)

        # hide unwanted columns (Size/Type)
        for col in (2,3,4):
            self.tree_sys.hideColumn(col)

        hdr = self.tree_sys.header()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.resizeSection(0, 40)    # actions column
        hdr.setSectionResizeMode(1, QHeaderView.Interactive)

        # — Project‑pane models/views —
        self.source_model_proj = QFileSystemModel(self)
        self.model_proj        = ActionsProxyModel(self)
        self.model_proj.setSourceModel(self.source_model_proj)
        self.tree_proj = _DropTree(self)
        self.tree_proj.setModel(self.model_proj)
        self.tree_proj.hide()

        for col in (2,3,4):
            self.tree_proj.hideColumn(col)

        hdr2 = self.tree_proj.header()
        hdr2.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr2.resizeSection(0, 40)
        hdr2.setSectionResizeMode(1, QHeaderView.Interactive)

        # install delegates
        self.tree_sys .setItemDelegateForColumn(0, _ActionsDelegate(self, False))
        self.tree_proj.setItemDelegateForColumn(0, _ActionsDelegate(self, True))

        # double‑click → open
        self.tree_sys .doubleClicked.connect(lambda idx: self._on_double(idx, False))
        self.tree_proj.doubleClicked.connect(lambda idx: self._on_double(idx, True))

        lay.addWidget(self.tree_sys)
        lay.addWidget(self.tree_proj)

        # hook up signals
        self.btn_sys .clicked.connect(lambda: self._switch(True))
        self.btn_proj.clicked.connect(lambda: self._switch(False))
        self.tree_sys .customContextMenuRequested.connect(self._sys_menu)
        self.tree_proj.customContextMenuRequested.connect(self._proj_menu)

        # if we already had an unpacked path saved
        if self.unpacked_dir and os.path.isdir(self.unpacked_dir):
            self._apply_unpacked_root(self.unpacked_dir)

    def _expected_native(self):
        return EXPECTED_NATIVE.get(self.current_game or "", ())

    def _update_path_label(self):
        text = self.unpacked_dir or "<i>not set</i>"
        self.path_label.setText(f"Unpacked Game folder: {text}")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select unpacked Game folder", self.unpacked_dir or ""
        )
        if d:
            self._apply_unpacked_root(d)

    def _apply_unpacked_root(self, path: str):
        self.unpacked_dir = os.path.abspath(path)
        ok = self._check_folder(path)
        tick, color = ("✓","green") if ok else ("✗","red")
        self.path_label.setText(
            f"Unpacked Game folder: <span style='color:{color}'>{tick}</span> {self.unpacked_dir}"
        )

        if not ok:
            QMessageBox.warning(
                self, "Invalid folder",
                f"Expected sub‑folder “{'/'.join(self._expected_native())}”."
            )
            return

        # set and map through the proxy
        src_idx   = self.source_model_sys.index(self.unpacked_dir)
        proxy_idx = self.model_sys.mapFromSource(src_idx)
        self.tree_sys.setRootIndex(proxy_idx)
        self.tree_sys.resizeColumnToContents(1)

        if hasattr(self.app_win, "settings"):
            self.app_win.settings["unpacked_path"] = self.unpacked_dir
            self.app_win.save_settings()

    def _check_folder(self, root: str) -> bool:
        exp = self._expected_native()
        return not exp or os.path.isdir(os.path.join(root, *exp))

    def _switch(self, to_system: bool):
        self.btn_sys .setChecked(to_system)
        self.btn_proj.setChecked(not to_system)
        self.tree_sys .setVisible(to_system)
        self.tree_proj.setVisible(not to_system)

    def set_project(self, proj_dir: str | None):
        self.project_dir = proj_dir
        if not proj_dir:
            self.project_label.setText("<i>No project open</i>")
            self.tree_proj.setRootIndex(QModelIndex())
            return

        self.project_label.setText(f"<b>Project: {os.path.basename(proj_dir)}</b>")

        src_idx   = self.source_model_proj.index(proj_dir)
        proxy_idx = self.model_proj.mapFromSource(src_idx)
        self.tree_proj.setRootIndex(proxy_idx)
        self.tree_proj.resizeColumnToContents(1)
        self.tree_proj.show()

    def _sys_menu(self, pos):
        idx = self.tree_sys.indexAt(pos)
        if not idx.isValid() or self.model_sys.isDir(idx):
            return
        if QMenu(self).addAction("Add to project")\
                   .exec(self.tree_sys.viewport().mapToGlobal(pos)):
            self._copy_to_project(self.model_sys.mapToSource(idx).filePath())

    def _proj_menu(self, pos):
        idx = self.tree_proj.indexAt(pos)
        if not idx.isValid() or self.model_proj.isDir(idx):
            return
        if QMenu(self).addAction("Remove")\
                   .exec(self.tree_proj.viewport().mapToGlobal(pos)):
            src = self.model_proj.mapToSource(idx).filePath()
            self._remove_from_project(src)

    def _copy_to_project(self, src: str):
        if not self.project_dir or not self._check_folder(self.unpacked_dir):
            return

        rel = os.path.relpath(src, self.unpacked_dir)
        dst = os.path.join(self.project_dir, rel)

        if os.path.isdir(src):
            ans = QMessageBox.question(
                self, "Confirm Add",
                f"Add entire folder “{os.path.basename(src)}” and all its contents?",
                QMessageBox.Yes|QMessageBox.No
            )
            if ans != QMessageBox.Yes:
                return

        if os.path.exists(dst):
            ans = QMessageBox.question(
                self, "Confirm Overwrite",
                f"“{rel}” already exists — overwrite?",
                QMessageBox.Yes|QMessageBox.No
            )
            if ans != QMessageBox.Yes:
                return

        try:
            if os.path.isdir(src):
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                
            self.set_project(self.project_dir)
            QMessageBox.information(self, "Added", f"{rel}\nwas copied successfully.")
        except Exception as e:
            QMessageBox.critical(self, "Copy failed", str(e))

    def _remove_from_project(self, path: str):
        try:
            if os.path.isdir(path):
                shutil.rmtree(path)
            else:
                os.remove(path)
        except Exception as e:
            QMessageBox.critical(self, "Remove failed", str(e))
            return

        # collapse any now-empty parents
        parent = os.path.dirname(path)
        while parent and parent.startswith(self.project_dir):
            src_idx   = self.source_model_proj.index(parent)
            if self.source_model_proj.rowCount(src_idx) == 0:
                proxy_idx = self.model_proj.mapFromSource(src_idx)
                self.tree_proj.collapse(proxy_idx)
            parent = os.path.dirname(parent)

        # re‑show current project root
        self.set_project(self.project_dir)

    def _open_in_editor(self, path: str):
        if os.path.isfile(path):
            try:
                with open(path, "rb") as f:
                    data = f.read()
                self.app_win.add_tab(path, data)
            except Exception as e:
                QMessageBox.critical(self, "Open failed", str(e))

    def _on_double(self, idx: QModelIndex, in_project: bool):
        model = self.model_proj if in_project else self.model_sys
        src_idx = model.mapToSource(idx)
        filepath = src_idx.model().filePath(src_idx)
        if os.path.isfile(filepath):
            self._open_in_editor(filepath)
