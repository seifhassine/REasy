import os
from shiboken6 import isValid
from PySide6.QtCore import Qt, QModelIndex, QTimer
from PySide6.QtWidgets import (
    QDockWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QCheckBox,
    QPushButton, QPlainTextEdit, QListWidget, QSplitter, QRadioButton,
    QSpinBox, QDoubleSpinBox, QWidget, QAbstractItemView, QMainWindow
)


class BetterFindDialog(QDockWidget):

    @staticmethod
    def _widget_values(widget: QWidget) -> list[str]:
        vals: list[str] = []

        for le in widget.findChildren(QLineEdit):
            vals.append(le.text())

        for sp in widget.findChildren(QSpinBox):
            vals.append(str(sp.value()))
        for sp in widget.findChildren(QDoubleSpinBox):
            vals.append(str(sp.value()))

        for chk in widget.findChildren(QCheckBox):
            vals.append(str(chk.isChecked()))

        return [v for v in vals if v not in ("", None)]

    @staticmethod
    def _item_value(item) -> str:
        try:
            if hasattr(item, "text"):
                return str(item.text(1) or "")
        except Exception:
            pass

        if not isinstance(item, object) or not hasattr(item, "raw"):
            return ""

        raw = item.raw
        if isinstance(raw, dict):
            obj = raw.get("obj")
            if obj:
                # strings
                if hasattr(obj, "string"):
                    try:
                        s = str(obj.string)
                        if s:
                            return s
                    except Exception:
                        pass

                if hasattr(obj, "guid_str"):
                    return obj.guid_str

                # numeric scalars
                if hasattr(obj, "value"):
                    return str(obj.value)

                # vectors / ranges
                if all(hasattr(obj, attr) for attr in ("x", "y")):
                    coords = [obj.x, obj.y]
                    if hasattr(obj, "z"):
                        coords.append(obj.z)
                    if hasattr(obj, "w"):
                        coords.append(obj.w)
                    return "(" + ", ".join(f"{c:.6g}" for c in coords) + ")"

                if hasattr(obj, "values") and isinstance(obj.values, (list, tuple)):
                    return " ".join(str(v) for v in obj.values)

        # fall-back (second column of the node’s data list))
        if hasattr(item, "data") and isinstance(item.data, (list, tuple)) and len(item.data) > 1:
            return str(item.data[1])

        return ""

    def _row_path(self, idx: QModelIndex) -> list[int]:
        rows = []
        cur = idx
        while cur.isValid():
            rows.insert(0, cur.row())
            cur = cur.parent()
        return rows

    def _index_from_rows(self, rows: list[int]) -> QModelIndex:
        cur = QModelIndex()
        tree = self._get_tree()
        if not tree or not isValid(tree):
            return QModelIndex()
        model = tree.model()
        if not model:
            return QModelIndex()
        for r in rows:
            cur = model.index(r, 0, cur)
            if not cur.isValid():
                return QModelIndex()
        return cur

    def _match_with_widget_values(self, tree, idx: QModelIndex, raw_val: str, needle: str, case: bool) -> tuple[bool, str]:
        value_blob = ""
        if idx and idx.isValid():
            w = tree.indexWidget(idx)
            if w:
                widget_vals = self._widget_values(w)
                value_blob = " ".join(v for v in (widget_vals + [raw_val]) if v).strip()
                cmp_val2 = value_blob if case else value_blob.lower()
                if needle in cmp_val2:
                    return True, value_blob
        return False, value_blob

    def _get_tree(self):
        if self.shared_mode:
            self._tree_for_tab = None
        
        if self._tree_for_tab is not None:
            if not isValid(self._tree_for_tab):
                self._tree_for_tab = None
            else:
                return self._tree_for_tab
        
        current_tab = self.file_tab
        if not current_tab:
            return None
            
        try:
            if current_tab.viewer and hasattr(current_tab.viewer, "tree"):
                candidate = current_tab.viewer.tree
            else:
                candidate = current_tab.tree

            if candidate and isValid(candidate):
                self._tree_for_tab = candidate
            else:
                self._tree_for_tab = None
        except Exception:
            self._tree_for_tab = None
        return self._tree_for_tab

    # ------------------------------------------------------------------ #
    # GUI
    # ------------------------------------------------------------------ #
    def __init__(self, file_tab=None, parent=None, shared_mode=False):
        super().__init__("Find in Tree", parent)

        self.setObjectName("better_find_dialog")
        self.setAllowedAreas(Qt.AllDockWidgetAreas)
        self.setFeatures(
            QDockWidget.DockWidgetClosable
            | QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
        )
        if isinstance(parent, QMainWindow):
            parent.addDockWidget(Qt.RightDockWidgetArea, self)
            try:
                self.toggleViewAction().setVisible(False)
            except Exception:
                pass

        self.resize(460, 330)
        QTimer.singleShot(0, lambda: self.setFloating(True))

        self.shared_mode = shared_mode
        self.file_tab = file_tab
        self.app = file_tab.app if file_tab else None
        self.dark_mode = self.app.dark_mode if self.app and hasattr(self.app, 'dark_mode') else False
        
        self._tree_for_tab = None

        self.results = []
        self.current_index = -1

        content = QWidget(self)
        content.setObjectName("better_find_content")
        self.setWidget(content)

        root = QVBoxLayout(content)

        # search bar
        srow = QHBoxLayout()
        srow.addWidget(QLabel(self.tr("Search:")))
        self.search_entry = QLineEdit(placeholderText=self.tr("Enter text…"))
        self.search_entry.returnPressed.connect(self.find_all)
        srow.addWidget(self.search_entry)
        root.addLayout(srow)

        # options
        opts = QHBoxLayout()
        self.opt_name = QRadioButton(self.tr("Name"))
        self.opt_value = QRadioButton(self.tr("Value"))
        self.opt_both = QRadioButton(self.tr("Both"))
        self.opt_both.setChecked(True)
        opts.addWidget(self.opt_name)
        opts.addWidget(self.opt_value)
        opts.addWidget(self.opt_both)
        self.case_box = QCheckBox(self.tr("Case sensitive"))
        opts.addWidget(self.case_box)
        self.include_advanced_box = QCheckBox("Include Advanced Information")
        self.include_advanced_box.setChecked(True)
        opts.addWidget(self.include_advanced_box)
        opts.addStretch()
        root.addLayout(opts)

        # splitter (results / preview)
        splitter = QSplitter(Qt.Vertical)

        res_col = QVBoxLayout()
        res_col.addWidget(QLabel(self.tr("Results:")))
        self.result_list = QListWidget()
        self.result_list.itemDoubleClicked.connect(lambda _: self._select(self.result_list.currentRow()))
        res_col.addWidget(self.result_list)
        rwidget = QWidget()
        rwidget.setLayout(res_col)
        splitter.addWidget(rwidget)

        prev_col = QVBoxLayout()
        prev_col.addWidget(QLabel(self.tr("Preview:")))
        self.preview = QPlainTextEdit(readOnly=True)
        prev_col.addWidget(self.preview)
        pwidget = QWidget()
        pwidget.setLayout(prev_col)
        splitter.addWidget(pwidget)

        root.addWidget(splitter, 1)

        # nav buttons
        nav = QHBoxLayout()
        nav.addWidget(QPushButton(self.tr("Find All"), clicked=self.find_all))
        nav.addStretch()
        nav.addWidget(QPushButton(self.tr("Previous"), clicked=self.find_previous))
        nav.addWidget(QPushButton(self.tr("Next"), clicked=self.find_next))
        nav.addStretch()
        nav.addWidget(QPushButton(self.tr("Close"), clicked=self.close))
        root.addLayout(nav)

        self.status = QLabel("")
        root.addWidget(self.status)
        
        self._apply_theme()
        self.search_entry.setFocus()

    def find_all(self):
        search_text = self.search_entry.text().strip()
        if not search_text:
            self.status.setText(self.tr("Please enter search text"))
            return

        tree = self._get_tree()
        if not tree or not isValid(tree):
            self.status.setText(self.tr("No tree view available"))
            return
        try:
            model = tree.model()
        except RuntimeError:
            self.invalidate_cached_tree()
            self.status.setText(self.tr("Tree view unavailable"))
            return
        if not model:
            self.status.setText(self.tr("Tree has no model"))
            return

        case  = self.case_box.isChecked()
        mode  = "name" if self.opt_name.isChecked() else "value" if self.opt_value.isChecked() else "both"
        needle= search_text if case else search_text.lower()
        include_advanced = self.include_advanced_box.isChecked()

        self.results.clear()
        self.result_list.clear()
        self.current_index = -1

        # QTreeWidget path (for now UVAR viewer)
        if hasattr(tree, "invisibleRootItem"):
            metadata_map = getattr(tree, "_metadata_map", None)
            handler = getattr(tree, "_handler", None)

            def ensure_loaded(item):
                if not item:
                    return
                children_snapshot = [item.child(i) for i in range(item.childCount())]
                for ch in children_snapshot:
                    meta = metadata_map.get(id(ch)) if metadata_map else None
                    if meta and meta.get("type") == "placeholder" and handler:
                        embed = meta.get("embedded")
                        item.removeChild(ch)
                        handler._populate_embedded_contents(item, embed, metadata_map)
                        
                for i in range(item.childCount()):
                    ensure_loaded(item.child(i))
                    
            for r in range(tree.topLevelItemCount()):
                ensure_loaded(tree.topLevelItem(r))

            def walk_qtreewidget_item(item, names_path):
                if item is None:
                    return
                name = str(item.text(0) or "")
                if not include_advanced and not names_path and name == "Advanced Information":
                    return
                cmp_name = name if case else name.lower()

                raw_val = str(item.text(1) or "")
                cmp_val = raw_val if case else raw_val.lower()

                match_name = (mode in ("both", "name")) and (needle in cmp_name)
                match_val = (mode in ("both", "value")) and (needle in cmp_val) if cmp_val else False
                value_blob = ""

                if not match_val:
                    idx = tree.indexFromItem(item, 0)
                    if idx and idx.isValid():
                        match_val, value_blob = self._match_with_widget_values(tree, idx, raw_val, needle, case)

                match = match_name or match_val

                if match:
                    if not value_blob:
                        idx = tree.indexFromItem(item, 0)
                        widget_vals = []
                        if idx and idx.isValid():
                            w = tree.indexWidget(idx)
                            if w:
                                widget_vals = self._widget_values(w)
                        value_blob = " ".join(v for v in (widget_vals + [raw_val]) if v).strip()

                    full_path = " > ".join(names_path + [name]) if names_path else name
                    rows = self._row_path(tree.indexFromItem(item, 0))
                    self.results.append({
                        "path": full_path, "name": name,
                        "value": value_blob, "rows": rows
                    })
                    disp = f"{name}: {value_blob}" if value_blob else name
                    if len(full_path.split(" > ")) > 3:
                        disp = "…" + disp
                    self.result_list.addItem(disp)

                for r in range(item.childCount()):
                    child = item.child(r)
                    if child:
                        walk_qtreewidget_item(child, names_path + [name])

            for r in range(tree.topLevelItemCount()):
                top = tree.topLevelItem(r)
                if top:
                    walk_qtreewidget_item(top, [])

        else:
            # fast path (TreeItem traversal) 
            root_item = getattr(model, "rootItem", None)
            is_tree_item_model = (root_item is not None and hasattr(root_item, "child_count") and hasattr(root_item, "data"))
            if is_tree_item_model:
                def get_item_name(item) -> str:
                    d = item.data
                    if isinstance(d, (list, tuple)):
                        return str(d[0] or "")
                    return str(d or "")

                get_index_from_item = getattr(model, "getIndexFromItem", None)

                def walk_items(item, rows_path, names_path):
                    name = get_item_name(item)
                    if not include_advanced and not names_path and name == "Advanced Information":
                        return
                    cmp_name = name if case else name.lower()

                    match_name = (mode in ("both", "name")) and (needle in cmp_name)
                    match_val = False
                    raw_val = ""
                    value_blob = ""

                    if not match_name and (mode in ("both", "value")):
                        raw_val = self._item_value(item)
                        cmp_val = raw_val if case else raw_val.lower()
                        match_val = needle in cmp_val if cmp_val else False

                        if not match_val and callable(get_index_from_item):
                            idx = get_index_from_item(item)
                            if idx and idx.isValid():
                                match_val, value_blob = self._match_with_widget_values(tree, idx, raw_val, needle, case)

                    match = match_name or match_val

                    if match:
                        if not value_blob:
                            widget_vals = []
                            if callable(get_index_from_item):
                                idx = get_index_from_item(item)
                                if idx and idx.isValid():
                                    w = tree.indexWidget(idx)
                                    if w:
                                        widget_vals = self._widget_values(w)
                            if not raw_val:
                                raw_val = self._item_value(item)
                            value_blob = " ".join(v for v in (widget_vals + [raw_val]) if v).strip()

                        full_path = " > ".join(names_path + [name]) if names_path else name
                        self.results.append({
                            "path": full_path, "name": name,
                            "value": value_blob, "rows": rows_path[:]
                        })
                        disp = f"{name}: {value_blob}" if value_blob else name
                        if len(full_path.split(" > ")) > 3:
                            disp = "…" + disp
                        self.result_list.addItem(disp)

                    child_count = item.child_count()
                    if child_count:
                        for r in range(child_count):
                            child = item.child(r)
                            if not child:
                                continue
                            walk_items(child, rows_path + [r], names_path + [name])

                for r in range(root_item.child_count()):
                    child = root_item.child(r)
                    if child:
                        walk_items(child, [r], [])

            else:
                # fetch lazy children
                def fetch(idx):
                    if hasattr(model, "canFetchMore") and model.canFetchMore(idx):
                        model.fetchMore(idx)
                    for r in range(model.rowCount(idx)):
                        fetch(model.index(r, 0, idx))
                fetch(QModelIndex())

                def walk(parent_idx, path):
                    for row in range(model.rowCount(parent_idx)):
                        idx0 = model.index(row, 0, parent_idx)
                        if not idx0.isValid():
                            continue

                        name = str(idx0.data(Qt.DisplayRole) or "")
                        if not include_advanced and not path and name == "Advanced Information":
                            continue
                        item = idx0.internalPointer()

                        cmp_name = name if case else name.lower()

                        raw_val = ""
                        widget_vals = []
                        value_blob = ""

                        match_name = (mode in ("both", "name")) and (needle in cmp_name)
                        match_val = False

                        if not match_name and (mode in ("both", "value")):
                            raw_val = self._item_value(item)
                            cmp_val = raw_val if case else raw_val.lower()
                            match_val = needle in cmp_val if cmp_val else False
                            if not match_val:
                                match_val, value_blob = self._match_with_widget_values(tree, idx0, raw_val, needle, case)

                        match = match_name or match_val

                        if match:
                            if not value_blob:
                                if not raw_val:
                                    raw_val = self._item_value(item)
                                w = tree.indexWidget(idx0)
                                if w:
                                    widget_vals = self._widget_values(w)
                                value_blob = " ".join(v for v in (widget_vals + [raw_val]) if v).strip()

                            full_path = f"{path} > {name}" if path else name
                            self.results.append({
                                "path": full_path, "name": name,
                                "value": value_blob, "rows": self._row_path(idx0)
                            })
                            disp = f"{name}: {value_blob}" if value_blob else name
                            if len(full_path.split(" > ")) > 3:
                                disp = "…" + disp
                            self.result_list.addItem(disp)

                        if model.hasChildren(idx0):
                            next_path = f"{path} > {name}" if path else name
                            walk(idx0, next_path)

                walk(QModelIndex(), "")

        if self.results:
            self.status.setText(f"Found {len(self.results)} matches")
            self._select(0)
        else:
            self.status.setText(self.tr("No matches found"))

    def _select(self, i):
        if not (0 <= i < len(self.results)):
            return
        
        self.current_index = i
        res = self.results[i]
        self.preview.setPlainText(
            f"Path:  {res['path']}\nName:  {res['name']}\nValue: {res['value']}"
        )
        self.result_list.setCurrentRow(i)

        idx = self._index_from_rows(res["rows"])
        if idx.isValid():
            tree = self._get_tree()
            if tree and isValid(tree):
                try:
                    tree.setCurrentIndex(idx)
                    tree.scrollTo(idx, QAbstractItemView.PositionAtCenter)
                except RuntimeError:
                    self.invalidate_cached_tree()

        self.status.setText(f"Result {i+1} of {len(self.results)}")

    def find_next(self):
        if not self.results: 
            self.find_all()
            return
        self._select((self.current_index + 1) % len(self.results))

    def find_previous(self):
        if not self.results: 
            self.find_all()
            return
        self._select((self.current_index - 1) % len(self.results))

    def set_file_tab(self, file_tab):
        """Update the current file tab (for shared mode)"""
        if self.file_tab == file_tab:
            return  # No change needed

        self.file_tab = file_tab
        self.app = file_tab.app if file_tab else None
        self._tree_for_tab = None
        # Clear results when switching tabs
        if self.shared_mode:
            self.results.clear()
            self.result_list.clear()
            self.current_index = -1
            self.preview.clear()
            self.status.setText("Tab switched - search cleared")
            # Update window title to show current tab
            if file_tab and hasattr(file_tab, 'filename'):
                tab_name = os.path.basename(file_tab.filename) if file_tab.filename else "Untitled"
                self.setWindowTitle(f"Find in Tree - {tab_name}")
            else:
                self.setWindowTitle(self.tr("Find in Tree"))

    def invalidate_cached_tree(self):
        self._tree_for_tab = None
        try:
            self.results.clear()
            self.current_index = -1
            if self.result_list:
                self.result_list.clear()
            if self.preview:
                self.preview.clear()
            if self.status:
                self.status.setText(self.tr("Tree reloaded - search reset"))
        except RuntimeError:
            pass
    
    def set_dark_mode(self, dark_mode):
        self.dark_mode = dark_mode
        self._apply_theme()
    
    def _apply_theme(self):
        if self.dark_mode:
            colors = {
                "bg": "#2b2b2b",
                "fg": "#ffffff",
                "input_bg": "#3b3b3b",
                "list_bg": "#353535",
                "border": "#555555",
                "highlight": "#ff851b",
                "button_bg": "#404040",
                "button_hover": "#4a4a4a",
                "selection": "rgba(255, 133, 27, 0.3)"
            }
        else:
            colors = {
                "bg": "#f5f5f5",
                "fg": "#000000",
                "input_bg": "#ffffff",
                "list_bg": "#ffffff",
                "border": "#cccccc",
                "highlight": "#ff851b",
                "button_bg": "#e0e0e0",
                "button_hover": "#d0d0d0",
                "selection": "rgba(255, 133, 27, 0.2)"
            }
        
        self.setStyleSheet(f"""
            QDockWidget#better_find_dialog {{
                background-color: {colors['bg']};
                color: {colors['fg']};
            }}
            QDockWidget#better_find_dialog QWidget#better_find_content {{
                background-color: {colors['bg']};
                color: {colors['fg']};
            }}
            QLabel {{
                color: {colors['fg']};
            }}
            QLineEdit {{
                background-color: {colors['input_bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
                padding: 5px;
                border-radius: 3px;
            }}
            QLineEdit:focus {{
                border: 1px solid {colors['highlight']};
            }}
            QRadioButton, QCheckBox {{
                color: {colors['fg']};
                spacing: 5px;
            }}
            QRadioButton::indicator, QCheckBox::indicator {{
                width: 16px;
                height: 16px;
                background-color: {colors['input_bg']};
                border: 1px solid {colors['border']};
                border-radius: 2px;
            }}
            QRadioButton::indicator:checked, QCheckBox::indicator:checked {{
                background-color: {colors['highlight']};
                border-color: {colors['highlight']};
            }}
            QRadioButton::indicator {{
                border-radius: 8px;
            }}
            QListWidget {{
                background-color: {colors['list_bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
                border-radius: 3px;
                padding: 2px;
            }}
            QListWidget::item {{
                padding: 3px;
                border-radius: 2px;
            }}
            QListWidget::item:selected {{
                background-color: {colors['selection']};
                color: {colors['fg']};
            }}
            QListWidget::item:hover {{
                background-color: {colors['button_hover']};
            }}
            QPlainTextEdit {{
                background-color: {colors['input_bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
                border-radius: 3px;
                padding: 5px;
                font-family: monospace;
            }}
            QPushButton {{
                background-color: {colors['button_bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
                padding: 6px 12px;
                border-radius: 3px;
                min-width: 80px;
            }}
            QPushButton:hover {{
                background-color: {colors['button_hover']};
            }}
            QPushButton:pressed {{
                background-color: {colors['border']};
            }}
            QSplitter::handle {{
                background-color: {colors['border']};
                height: 3px;
            }}
        """)
    
    def showEvent(self, event):
        """Ensure the search entry is focused when the dialog appears."""
        super().showEvent(event)
        if not hasattr(self, '_shown_once'):
            self._shown_once = True
        def focus_entry():
            if not hasattr(self, 'search_entry'):
                return
            self.search_entry.setFocus()
            self.activateWindow()
            self.raise_()

        QTimer.singleShot(20, focus_entry)
    
    def closeEvent(self, e):
        self.results.clear()
        self.result_list.clear()
        if not self.shared_mode:
            self.file_tab = self.app = None
        super().closeEvent(e)
