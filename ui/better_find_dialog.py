from PySide6.QtCore import Qt, QModelIndex
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QCheckBox,
    QPushButton, QPlainTextEdit, QListWidget, QSplitter, QRadioButton,
    QSpinBox, QDoubleSpinBox, QCheckBox as QtCheckBox, QWidget
)


class BetterFindDialog(QDialog):
    """Modal dialog that searches names AND values (widgets or raw data) in the active tree."""

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _widget_values(widget: QWidget) -> list[str]:
        """Grab *all* user-visible values inside a custom index-widget."""
        vals: list[str] = []

        for le in widget.findChildren(QLineEdit):
            vals.append(le.text())

        for sp in widget.findChildren(QSpinBox):
            vals.append(str(sp.value()))
        for sp in widget.findChildren(QDoubleSpinBox):
            vals.append(str(sp.value()))

        for chk in widget.findChildren(QtCheckBox):
            vals.append(str(chk.isChecked()))

        return [v for v in vals if v not in ("", None)]

    @staticmethod
    def _item_value(item) -> str:
        """
        Return a human-readable value directly from the model item
        (without relying on a widget).  Handles the common RSZ data
        objects your app uses (value, guid_str, x/y/z, etc.).
        """
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
        model = self.app.get_active_tree().model()
        for r in rows:
            cur = model.index(r, 0, cur)
            if not cur.isValid():
                return QModelIndex()
        return cur

    # ------------------------------------------------------------------ #
    # GUI
    # ------------------------------------------------------------------ #
    def __init__(self, file_tab, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find in Tree")
        self.resize(460, 330)

        self.file_tab = file_tab
        self.app = file_tab.app

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
        self.opt_both = QRadioButton("Both"); self.opt_both.setChecked(True)
        opts.addWidget(self.opt_name); opts.addWidget(self.opt_value); opts.addWidget(self.opt_both)
        self.case_box = QCheckBox("Case sensitive"); opts.addWidget(self.case_box)
        opts.addStretch()
        root.addLayout(opts)

        # splitter (results / preview)
        splitter = QSplitter(Qt.Vertical)

        res_col = QVBoxLayout()
        res_col.addWidget(QLabel("Results:"))
        self.result_list = QListWidget()
        self.result_list.itemDoubleClicked.connect(lambda _: self._select(self.result_list.currentRow()))
        res_col.addWidget(self.result_list)
        rwidget = QWidget(); rwidget.setLayout(res_col)
        splitter.addWidget(rwidget)

        prev_col = QVBoxLayout()
        prev_col.addWidget(QLabel("Preview:"))
        self.preview = QPlainTextEdit(readOnly=True)
        prev_col.addWidget(self.preview)
        pwidget = QWidget(); pwidget.setLayout(prev_col)
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

    # ------------------------------------------------------------------ #
    # search
    # ------------------------------------------------------------------ #
    def find_all(self):
        search_text = self.search_entry.text().strip()
        if not search_text:
            self.status.setText("Please enter search text"); return

        tree = self.app.get_active_tree()
        if not tree:
            self.status.setText("No tree view available"); return
        model = tree.model()
        if not model:
            self.status.setText("Tree has no model"); return

        # fetch lazy children
        def fetch(idx):
            if hasattr(model, "canFetchMore") and model.canFetchMore(idx):
                model.fetchMore(idx)
            for r in range(model.rowCount(idx)):
                fetch(model.index(r, 0, idx))
        fetch(QModelIndex())

        case  = self.case_box.isChecked()
        mode  = "name" if self.opt_name.isChecked() else "value" if self.opt_value.isChecked() else "both"
        needle= search_text if case else search_text.lower()

        self.results.clear(); self.result_list.clear(); self.current_index = -1

        def walk(parent_idx, path):
            for row in range(model.rowCount(parent_idx)):
                idx0 = model.index(row, 0, parent_idx)
                if not idx0.isValid(): continue

                name = str(idx0.data(Qt.DisplayRole) or "")
                full_path = f"{path} > {name}" if path else name

                item = idx0.internalPointer()

                # widget values (if the node has been expanded already)
                widget_vals = []
                w = tree.indexWidget(idx0)
                if w:
                    widget_vals = self._widget_values(w)

                # raw value
                raw_val = self._item_value(item)

                all_values = widget_vals + [raw_val]
                value_blob = " ".join(v for v in all_values if v).strip()

                cmp_name = name if case else name.lower()
                cmp_val  = value_blob if case else value_blob.lower()

                match = ((mode in ("both", "name")  and needle in cmp_name) or
                         (mode in ("both", "value") and needle in cmp_val))
                if match:
                    self.results.append({
                        "path": full_path, "name": name,
                        "value": value_blob, "rows": self._row_path(idx0)
                    })
                    disp = f"{name}: {value_blob}" if value_blob else name
                    if len(full_path.split(" > ")) > 3:
                        disp = "…" + disp
                    self.result_list.addItem(disp)

                if model.hasChildren(idx0):
                    walk(idx0, full_path)

        walk(QModelIndex(), "")

        if self.results:
            self.status.setText(f"Found {len(self.results)} matches")
            self._select(0)
        else:
            self.status.setText("No matches found")

    # navigation
    def _select(self, i):
        if not (0 <= i < len(self.results)): return
        self.current_index = i
        res = self.results[i]
        self.preview.setPlainText(
            f"Path:  {res['path']}\nName:  {res['name']}\nValue: {res['value']}"
        )
        self.result_list.setCurrentRow(i)

        idx = self._index_from_rows(res["rows"])
        if idx.isValid():
            tree = self.app.get_active_tree()
            tree.setCurrentIndex(idx)
            tree.scrollTo(idx)

        self.status.setText(f"Result {i+1} of {len(self.results)}")

    def find_next(self):
        if not self.results: self.find_all(); return
        self._select((self.current_index + 1) % len(self.results))

    def find_previous(self):
        if not self.results: self.find_all(); return
        self._select((self.current_index - 1) % len(self.results))

    # Qt events
    def showEvent(self, e): super().showEvent(e); self.search_entry.setFocus()
    def closeEvent(self, e):
        self.results.clear(); self.result_list.clear()
        self.file_tab = self.app = None
        super().closeEvent(e)
