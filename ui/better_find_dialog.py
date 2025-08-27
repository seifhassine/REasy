from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QCheckBox,
    QPushButton, QPlainTextEdit, QListWidget, QSplitter, QRadioButton,
    QSpinBox, QDoubleSpinBox, QWidget, QAbstractItemView
)


class BetterFindDialog(QDialog):

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
                # numeric scalars
                if hasattr(obj, "value"):
                    return str(obj.value)

                # strings
                if hasattr(obj, "guid_str"):
                    return obj.guid_str
                if hasattr(obj, "string"):
                    return str(obj.string)

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
        model = self._get_tree().model()
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
        if self._tree_for_tab is not None:
            return self._tree_for_tab
        try:
            if self.file_tab.viewer and hasattr(self.file_tab.viewer, "tree"):
                self._tree_for_tab = self.file_tab.viewer.tree
            else:
                self._tree_for_tab = self.file_tab.tree
        except Exception:
            self._tree_for_tab = None
        return self._tree_for_tab

    # ------------------------------------------------------------------ #
    # GUI
    # ------------------------------------------------------------------ #
    def __init__(self, file_tab, parent=None):
        super().__init__(parent, Qt.Window | Qt.WindowStaysOnTopHint)
        self.setWindowTitle("Find in Tree")
        self.resize(460, 330)

        self.file_tab = file_tab
        self.app = file_tab.app
        
        self._tree_for_tab = None

        self.results = []
        self.current_index = -1

        root = QVBoxLayout(self)

        # search bar
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Search:"))
        self.search_entry = QLineEdit(placeholderText="Enter text…")
        self.search_entry.returnPressed.connect(self.find_all)
        srow.addWidget(self.search_entry)
        root.addLayout(srow)

        # options
        opts = QHBoxLayout()
        self.opt_name = QRadioButton("Name")
        self.opt_value = QRadioButton("Value")
        self.opt_both = QRadioButton("Both")
        self.opt_both.setChecked(True)
        opts.addWidget(self.opt_name)
        opts.addWidget(self.opt_value)
        opts.addWidget(self.opt_both)
        self.case_box = QCheckBox("Case sensitive")
        opts.addWidget(self.case_box)
        opts.addStretch()
        root.addLayout(opts)

        # splitter (results / preview)
        splitter = QSplitter(Qt.Vertical)

        res_col = QVBoxLayout()
        res_col.addWidget(QLabel("Results:"))
        self.result_list = QListWidget()
        self.result_list.itemDoubleClicked.connect(lambda _: self._select(self.result_list.currentRow()))
        res_col.addWidget(self.result_list)
        rwidget = QWidget()
        rwidget.setLayout(res_col)
        splitter.addWidget(rwidget)

        prev_col = QVBoxLayout()
        prev_col.addWidget(QLabel("Preview:"))
        self.preview = QPlainTextEdit(readOnly=True)
        prev_col.addWidget(self.preview)
        pwidget = QWidget()
        pwidget.setLayout(prev_col)
        splitter.addWidget(pwidget)

        root.addWidget(splitter, 1)

        # nav buttons
        nav = QHBoxLayout()
        nav.addWidget(QPushButton("Find All", clicked=self.find_all))
        nav.addStretch()
        nav.addWidget(QPushButton("Previous", clicked=self.find_previous))
        nav.addWidget(QPushButton("Next", clicked=self.find_next))
        nav.addStretch()
        nav.addWidget(QPushButton("Close", clicked=self.close))
        root.addLayout(nav)

        # status
        self.status = QLabel("")
        root.addWidget(self.status)

    def find_all(self):
        search_text = self.search_entry.text().strip()
        if not search_text:
            self.status.setText("Please enter search text")
            return

        tree = self._get_tree()
        if not tree:
            self.status.setText("No tree view available")
            return
        model = tree.model()
        if not model:
            self.status.setText("Tree has no model")
            return

        case  = self.case_box.isChecked()
        mode  = "name" if self.opt_name.isChecked() else "value" if self.opt_value.isChecked() else "both"
        needle= search_text if case else search_text.lower()

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
            self.status.setText("No matches found")

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
            tree.setCurrentIndex(idx)
            tree.scrollTo(idx, QAbstractItemView.PositionAtCenter)

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

    def showEvent(self, e): 
        super().showEvent(e)
        self.search_entry.setFocus()
    def closeEvent(self, e):
        self.results.clear()
        self.result_list.clear()
        self.file_tab = self.app = None
        super().closeEvent(e)
