import os
from pathlib import Path
from typing import Dict, List, Tuple, Any

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QListWidget,
    QListWidgetItem,
    QGroupBox,
    QSplitter,
    QCheckBox,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QProgressDialog,
    QTreeWidget,
    QTreeWidgetItem,
)
from PySide6.QtWidgets import QComboBox

from utils.type_registry import TypeRegistry
from tools.rsz_field_value_finder import format_value
from tools.rsz_data_matcher import scan_directory_single_pass

_REGISTRY_CACHE: Dict[str, TypeRegistry] = {}

class MatcherWorker(QThread):
    progress_update = Signal(int, int)
    error_occurred = Signal(str)
    preview_ready = Signal()
    finished_ok = Signal()

    def __init__(
        self,
        root_dir: str,
        type_registry: TypeRegistry,
        a_cfg: dict,
        b_cfg: dict,
        key_pairs: List[Tuple[str, str]],
    ):
        super().__init__()
        self.root_dir = root_dir
        self.type_registry = type_registry
        self.a_cfg = a_cfg
        self.b_cfg = b_cfg
        self.key_pairs = key_pairs
        self.cancelled = False
        self.result_a_map = {}
        self.result_b_map = {}

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            def cb(cur: int, total: int):
                self.progress_update.emit(cur, total)
            a_map, b_map = scan_directory_single_pass(
                root_dir=self.root_dir,
                pattern_a=self.a_cfg.get("pattern", "*"),
                pattern_b=self.b_cfg.get("pattern", "*"),
                recursive=self.a_cfg.get("recursive", True) or self.b_cfg.get("recursive", True),
                type_id_a=self.a_cfg.get("type_id"),
                type_id_b=self.b_cfg.get("type_id"),
                key_pairs=self.key_pairs,
                cap_fields_a=self.a_cfg.get("capture_fields", []),
                cap_fields_b=self.b_cfg.get("capture_fields", []),
                type_registry=self.type_registry,
                progress_cb=cb,
                cancel_cb=lambda: self.cancelled,
                allow_scn_a=self.a_cfg.get("allow_scn", True),
                allow_pfb_a=self.a_cfg.get("allow_pfb", True),
                allow_user_a=self.a_cfg.get("allow_user", True),
                allow_scn_b=self.b_cfg.get("allow_scn", True),
                allow_pfb_b=self.b_cfg.get("allow_pfb", True),
                allow_user_b=self.b_cfg.get("allow_user", True),
                constraints_a=self.a_cfg.get("constraints", []),
                constraints_b=self.b_cfg.get("constraints", []),
            )
            if self.cancelled:
                return
            
            self.result_a_map = a_map
            self.result_b_map = b_map
            
            self.preview_ready.emit()
            self.finished_ok.emit()
        except Exception as e:
            self.error_occurred.emit(str(e))


class TypeRegistryLoader(QThread):
    loaded_ok = Signal()
    error_occurred = Signal(str)

    def __init__(self, json_path: str):
        super().__init__()
        self.json_path = json_path
        self.result_registry: TypeRegistry | None = None
        self.result_names: List[str] = []
        self.result_names_lower: List[str] = []
        self.result_name_to_id: Dict[str, int] = {}
        self.result_name_to_id_lower: Dict[str, int] = {}

    def run(self):
        try:
            reg = TypeRegistry(self.json_path)
            names: List[str] = []
            name_to_id: Dict[str, int] = {}
            for hex_key, tinfo in reg.registry.items():
                if not tinfo:
                    continue
                name = tinfo.get("name")
                try:
                    tid = int(hex_key, 16)
                except Exception:
                    continue
                names.append(name)
                name_to_id[name] = tid
            names.sort(key=lambda s: s.lower())
            self.result_registry = reg
            self.result_names = names
            self.result_names_lower = [n.lower() for n in names]
            self.result_name_to_id = name_to_id
            self.result_name_to_id_lower = {n.lower(): tid for n, tid in name_to_id.items()}
            self.loaded_ok.emit()
        except Exception as e:
            self.error_occurred.emit(str(e))


class RszCsvExtractorDialog(QDialog):
    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings or {}
        self.type_registry: TypeRegistry = None
        self.worker: MatcherWorker = None
        self._type_names: List[str] = []
        self._type_names_lower: List[str] = []
        self._type_name_to_id: Dict[str, int] = {}
        self._type_name_to_id_lower: Dict[str, int] = {}
        
        self.a_map = {}
        self.b_map = {}
        self.fields_A: List[str] = []
        self.fields_B: List[str] = []
        self.field_types_A: Dict[str, str] = {}
        self.field_types_B: Dict[str, str] = {}
        self._last_signature = None
        self._export_after_scan = False
        self._scanning = False

        self.setWindowTitle("CSV Extractor (RSZ Data Matcher)")
        self.setMinimumSize(980, 760)
        self._build_ui()
        jp = self.json_path_edit.text().strip()
        if jp and os.path.isfile(jp):
            self._start_async_load_types(jp)

    def _build_ui(self):
        layout = QVBoxLayout(self)

        types_group = QGroupBox("Type Data Source")
        tlay = QHBoxLayout()
        self.json_path_edit = QLineEdit(self.settings.get("rcol_json_path", ""))
        tlay.addWidget(QLabel("JSON Path:"))
        tlay.addWidget(self.json_path_edit)
        browse = QPushButton("Browse...")
        tlay.addWidget(browse)
        types_group.setLayout(tlay)
        layout.addWidget(types_group)

        browse.clicked.connect(self._browse_json)
        self.json_path_edit.editingFinished.connect(lambda: self._start_async_load_types(self.json_path_edit.text().strip()))

        root_group = QGroupBox("Search Root")
        rlay = QHBoxLayout()
        self.root_dir_edit = QLineEdit(self.settings.get("unpacked_path", ""))
        rlay.addWidget(QLabel("Directory:"))
        rlay.addWidget(self.root_dir_edit)
        root_browse = QPushButton("Browse...")
        rlay.addWidget(root_browse)
        self.recursive_check = QCheckBox("Recursive")
        self.recursive_check.setChecked(True)
        rlay.addWidget(self.recursive_check)
        
        root_group.setLayout(rlay)
        layout.addWidget(root_group)
        root_browse.clicked.connect(self._browse_root)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_side("A"))
        splitter.addWidget(self._build_side("B"))
        splitter.setSizes([480, 480])
        layout.addWidget(splitter)

        rules_group = QGroupBox("Match Rules & Field Constraints")
        rg_lay = QVBoxLayout()
        
        add_lay = QHBoxLayout()
        
        add_lay.addWidget(QLabel("A Field:"))
        self.a_field_combo = QComboBox()
        self.a_field_combo.setEditable(False)
        add_lay.addWidget(self.a_field_combo)
        
        self.a_constraint_type_combo = QComboBox()
        self.a_constraint_type_combo.addItems([
            "Any value",
            "Not empty (strings)",
            "Is empty (strings)", 
            "Greater than",
            "Less than",
            "Equal to",
            "Not equal to"
        ])
        add_lay.addWidget(self.a_constraint_type_combo)
        
        self.a_constraint_value_edit = QLineEdit()
        self.a_constraint_value_edit.setPlaceholderText("Value (if needed)")
        add_lay.addWidget(self.a_constraint_value_edit)
        
        add_lay.addWidget(QLabel("↔"))
        
        add_lay.addWidget(QLabel("B Field:"))
        self.b_field_combo = QComboBox()
        self.b_field_combo.setEditable(False)
        add_lay.addWidget(self.b_field_combo)
        
        self.b_constraint_type_combo = QComboBox()
        self.b_constraint_type_combo.addItems([
            "Any value",
            "Not empty (strings)",
            "Is empty (strings)",
            "Greater than", 
            "Less than",
            "Equal to",
            "Not equal to"
        ])
        add_lay.addWidget(self.b_constraint_type_combo)
        
        self.b_constraint_value_edit = QLineEdit()
        self.b_constraint_value_edit.setPlaceholderText("Value (if needed)")
        add_lay.addWidget(self.b_constraint_value_edit)
        
        add_btn = QPushButton("Add Rule")
        remove_btn = QPushButton("Remove Selected")
        add_lay.addWidget(add_btn)
        add_lay.addWidget(remove_btn)
        
        rg_lay.addLayout(add_lay)

        self.rules_list = QListWidget()
        self.rules_list.setMaximumHeight(120)
        rg_lay.addWidget(self.rules_list)
        
        rules_group.setLayout(rg_lay)
        layout.addWidget(rules_group)

        run_lay = QHBoxLayout()
        self.preview_btn = QPushButton("Preview Matches")
        self.export_btn = QPushButton("Export CSV…")
        self.export_btn.setEnabled(False)
        run_lay.addWidget(self.preview_btn)
        run_lay.addWidget(self.export_btn)
        layout.addLayout(run_lay)
        
        export_options_lay = QHBoxLayout()
        self.include_filenames_check = QCheckBox("Include file names in CSV")
        self.include_filenames_check.setChecked(True)
        self.include_filenames_check.setToolTip("Include file paths and instance IDs in CSV columns")
        export_options_lay.addWidget(self.include_filenames_check)
        
        self.allow_same_file_check = QCheckBox("Allow matches within same file")
        self.allow_same_file_check.setChecked(False)
        self.allow_same_file_check.setToolTip("Allow A and B entries from the same file to match")
        export_options_lay.addWidget(self.allow_same_file_check)
        self.allow_same_file_check.stateChanged.connect(self._on_allow_same_file_changed)
        
        export_options_lay.addStretch()
        layout.addLayout(export_options_lay)

        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(["Key", "A Count", "B Count"])
        layout.addWidget(self.results_tree)

        self.status_label = QLabel("Ready")
        self.status_label.setStyleSheet("QLabel { color: gray; }")
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.preview_btn.clicked.connect(self._start_preview)
        self.export_btn.clicked.connect(self._export_csv)

        self._setup_type_autocomplete(self.type_edit_A)
        self._setup_type_autocomplete(self.type_edit_B)
        self.type_edit_A.editingFinished.connect(lambda: self._on_type_resolved('A'))
        self.type_edit_B.editingFinished.connect(lambda: self._on_type_resolved('B'))

        add_btn.clicked.connect(self._add_rule)
        remove_btn.clicked.connect(self._remove_rule)

    def _build_side(self, label: str):
        box = QGroupBox(f"Set {label}")
        lay = QVBoxLayout()

        pat_lay = QHBoxLayout()
        self.__dict__[f"pattern_{label}"] = QLineEdit("*")
        pat_lay.addWidget(QLabel("Filename Pattern:"))
        pat_lay.addWidget(self.__dict__[f"pattern_{label}"])
        lay.addLayout(pat_lay)

        typelay = QHBoxLayout()
        self.__dict__[f"scn_{label}"] = QCheckBox("SCN")
        self.__dict__[f"pfb_{label}"] = QCheckBox("PFB")
        self.__dict__[f"user_{label}"] = QCheckBox("USER")
        self.__dict__[f"scn_{label}"].setChecked(True)
        self.__dict__[f"pfb_{label}"].setChecked(True)
        self.__dict__[f"user_{label}"].setChecked(True)
        typelay.addWidget(QLabel("Types:"))
        typelay.addWidget(self.__dict__[f"scn_{label}"])
        typelay.addWidget(self.__dict__[f"pfb_{label}"])
        typelay.addWidget(self.__dict__[f"user_{label}"])
        lay.addLayout(typelay)

        type_lay = QHBoxLayout()
        self.__dict__[f"type_edit_{label}"] = QLineEdit()
        self.__dict__[f"type_id_label_{label}"] = QLabel("ID: -")
        type_lay.addWidget(QLabel("RSZ Type:"))
        type_lay.addWidget(self.__dict__[f"type_edit_{label}"], 1)
        type_lay.addWidget(self.__dict__[f"type_id_label_{label}"])
        lay.addLayout(type_lay)

        fields_group = QGroupBox(f"Fields to Output (from {label})")
        fields_v = QVBoxLayout()
        self.__dict__[f"fields_list_{label}"] = QListWidget()
        self.__dict__[f"fields_list_{label}"].setSelectionMode(QListWidget.MultiSelection)
        fields_v.addWidget(QLabel("Choose fields to include as CSV columns"))
        fields_v.addWidget(self.__dict__[f"fields_list_{label}"])
        fields_group.setLayout(fields_v)
        lay.addWidget(fields_group)
        self.__dict__[f"cap_fields_{label}"] = self.__dict__[f"fields_list_{label}"]
        self.__dict__[f"fields_list_{label}"].itemSelectionChanged.connect(self._mark_dirty)

        box.setLayout(lay)

        self.__dict__[f"type_edit_{label}"].editingFinished.connect(lambda L=label: self._on_type_resolved(L))

        return box

    def _browse_json(self):
        cur = self.json_path_edit.text()
        file_path, _ = QFileDialog.getOpenFileName(self, "Select JSON Type Data", os.path.dirname(cur) if cur else "", "JSON Files (*.json)")
        if file_path:
            self.json_path_edit.setText(file_path)
            self._load_types()
            return

    def _browse_root(self):
        path = QFileDialog.getExistingDirectory(self, "Select Root Directory", self.root_dir_edit.text())
        if path:
            self.root_dir_edit.setText(path)

    def _get_or_cache_registry(self, json_path: str) -> TypeRegistry:
        reg = _REGISTRY_CACHE.get(json_path)
        if reg is None:
            reg = TypeRegistry(json_path)
            _REGISTRY_CACHE[json_path] = reg
        return reg

    def _ensure_registry_loaded(self):
        return

    def _load_types(self):
        json_path = self.json_path_edit.text().strip()
        if not json_path:
            QMessageBox.warning(self, "Warning", "Please specify a JSON path")
            return
        if not os.path.isfile(json_path):
            QMessageBox.warning(self, "Warning", "Please select a valid JSON file (not a directory)")
            return

        try:
            self._start_async_load_types(json_path)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to start type loading: {e}")
            return

    def _start_async_load_types(self, json_path: str):
        if not json_path or not os.path.isfile(json_path):
            return
        if json_path in _REGISTRY_CACHE:
            self.type_registry = _REGISTRY_CACHE[json_path]
            self._refresh_type_cache_from_registry()
            return
        self._type_loader = TypeRegistryLoader(json_path)
        def on_loaded():
            _REGISTRY_CACHE[json_path] = self._type_loader.result_registry
            self.type_registry = self._type_loader.result_registry
            self._type_names = self._type_loader.result_names
            self._type_name_to_id = self._type_loader.result_name_to_id
            self._type_name_to_id_lower = self._type_loader.result_name_to_id_lower
            
        def on_error(msg: str):
            QMessageBox.critical(self, "Error", f"Failed to load type registry: {msg}")
        self._type_loader.loaded_ok.connect(on_loaded)
        self._type_loader.error_occurred.connect(on_error)
        self._type_loader.start()

    def _refresh_type_cache_from_registry(self):
        if not self.type_registry:
            return
        names: List[str] = []
        name_to_id: Dict[str, int] = {}
        name_to_id_lower: Dict[str, int] = {}
        for hex_key, tinfo in self.type_registry.registry.items():
            if not tinfo:
                continue
            name = tinfo.get("name")
            if not name:
                continue
            try:
                tid = int(hex_key, 16)
            except ValueError:
                continue
            names.append(name)
            name_to_id[name] = tid
            name_to_id_lower[name.lower()] = tid
        names.sort(key=lambda s: s.lower())
        self._type_names = names
        self._type_name_to_id = name_to_id
        self._type_name_to_id_lower = name_to_id_lower
        

    def _resolve_type_id_from_text(self, text: str) -> int:
        text = text.strip()
        try:
            return int(text, 16 if text.startswith("0x") else 10)
        except ValueError:
            return self._type_name_to_id.get(text) or self._type_name_to_id_lower.get(text.lower())

    def _on_type_resolved(self, side_label: str):
        if not self.type_registry:
            self._load_types()
            if not self.type_registry:
                return
        edit: QLineEdit = self.__dict__[f"type_edit_{side_label}"]
        id_label: QLabel = self.__dict__[f"type_id_label_{side_label}"]
        tid = self._resolve_type_id_from_text(edit.text())
        if tid is not None:
            id_label.setText(f"ID: 0x{tid:08X}")
        else:
            id_label.setText("ID: -")
            return

        tinfo = self.type_registry.get_type_info(tid)
        fields = []
        if tinfo and "fields" in tinfo:
            fields = [f.get("name") for f in tinfo["fields"] if f.get("name")]
        self._apply_type_fields(side_label, tinfo, fields)

    def _apply_type_fields(self, side_label: str, tinfo: dict, fields: List[str]):
        setattr(self, f"fields_{side_label}", fields)
        ftypes = {}
        if tinfo and "fields" in tinfo:
            for f in tinfo["fields"]:
                name = f.get("name")
                if name:
                    ftypes[name] = str(f.get("type")) if f.get("type") is not None else ""
        setattr(self, f"field_types_{side_label}", ftypes)
        lst: QListWidget = self.__dict__[f"fields_list_{side_label}"]
        lst.clear()
        for fname in fields:
            lst.addItem(QListWidgetItem(fname))
        combo: QComboBox = getattr(self, f"{side_label.lower()}_field_combo")
        combo.clear() 
        combo.addItem("(none)")
        combo.addItems(fields)

    def _setup_type_autocomplete(self, edit: QLineEdit):
        popup = QListWidget(self)
        popup.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        popup.setAttribute(Qt.WA_ShowWithoutActivating, True)
        popup.setFocusPolicy(Qt.NoFocus)
        popup.setUniformItemSizes(True)
        popup.setMinimumWidth(360)
        popup.setMaximumHeight(240)
        popup.hide()

        debounce = QTimer(edit)
        debounce.setSingleShot(True)
        debounce.setInterval(70)

        state = {"last_text": "", "max": 150}

        def ensure_loaded():
            if not self.type_registry:
                jp = self.json_path_edit.text().strip()
                if jp and os.path.isfile(jp):
                    self._start_async_load_types(jp)

        def apply_filter():
            text = state["last_text"]
            needle = (text or "").lower()
            popup.clear()
            if len(needle) < 2:
                popup.hide()
                return
            ensure_loaded()
            names = self._type_names
            lowers = getattr(self, "_type_names_lower", []) or [n.lower() for n in names]
            count = 0
            for name, low in zip(names, lowers):
                if low.startswith(needle):
                    QListWidgetItem(name, popup)
                    count += 1
                    if count >= state["max"]:
                        break
            if count == 0:
                popup.hide()
                return
            try:
                pos = edit.mapToGlobal(edit.rect().bottomLeft())
                popup.move(pos)
            except Exception:
                pass
            popup.setCurrentRow(0)
            popup.show()

        def on_text_changed(text: str):
            state["last_text"] = text or ""
            debounce.stop()
            debounce.start()

        def accept_current():
            it = popup.currentItem()
            if it:
                edit.setText(it.text())
            popup.hide()

        popup.itemClicked.connect(lambda *_: accept_current())
        edit.editingFinished.connect(popup.hide)
        debounce.timeout.connect(apply_filter)

        orig_keypress = edit.keyPressEvent

        def keypress(ev):
            if popup.isVisible():
                key = ev.key()
                if key in (Qt.Key_Down, Qt.Key_Up, Qt.Key_PageDown, Qt.Key_PageUp, Qt.Key_Home, Qt.Key_End):
                    ev.accept()
                    row = popup.currentRow()
                    if key == Qt.Key_Down:
                        popup.setCurrentRow(min(row + 1, popup.count() - 1))
                    elif key == Qt.Key_Up:
                        popup.setCurrentRow(max(row - 1, 0))
                    elif key == Qt.Key_PageDown:
                        popup.setCurrentRow(min(row + 10, popup.count() - 1))
                    elif key == Qt.Key_PageUp:
                        popup.setCurrentRow(max(row - 10, 0))
                    elif key == Qt.Key_Home:
                        popup.setCurrentRow(0)
                    elif key == Qt.Key_End:
                        popup.setCurrentRow(popup.count() - 1)
                    return
                if key in (Qt.Key_Return, Qt.Key_Enter):
                    ev.accept()
                    accept_current()
                    return
                if key == Qt.Key_Escape:
                    ev.accept()
                    popup.hide()
                    return
            orig_keypress(ev)

        edit.keyPressEvent = keypress
        edit.textEdited.connect(on_text_changed)

    def _add_rule(self):
        a_field = self.a_field_combo.currentText().strip()
        b_field = self.b_field_combo.currentText().strip()
        a_constraint_type = self.a_constraint_type_combo.currentText()
        b_constraint_type = self.b_constraint_type_combo.currentText()
        a_constraint_value = self.a_constraint_value_edit.text().strip()
        b_constraint_value = self.b_constraint_value_edit.text().strip()
        
        if a_field == "(none)":
            a_field = ""
        if b_field == "(none)":
            b_field = ""
        
        has_a_field = bool(a_field)
        has_b_field = bool(b_field)
        has_a_constraint = a_constraint_type != "Any value"
        has_b_constraint = b_constraint_type != "Any value"
        
        if not has_a_field and not has_b_field and not has_a_constraint and not has_b_constraint:
            QMessageBox.warning(self, "Warning", "Must specify at least one field or constraint.")
            return
        
        if (has_a_constraint and not has_a_field) or (has_b_constraint and not has_b_field):
            QMessageBox.warning(self, "Warning", "Cannot have constraint without specifying the field.")
            return
            
        def needs_value(constraint_type):
            return constraint_type in ["Greater than", "Less than", "Equal to", "Not equal to"]
        
        if needs_value(a_constraint_type) and not a_constraint_value:
            QMessageBox.warning(self, "Warning", f"A side '{a_constraint_type}' requires a value.")
            return
            
        if needs_value(b_constraint_type) and not b_constraint_value:
            QMessageBox.warning(self, "Warning", f"B side '{b_constraint_type}' requires a value.")
            return
        
        if has_a_field:
            ta = self.field_types_A.get(a_field)
            if not ta:
                QMessageBox.warning(self, "Warning", "Load types and fields for A before adding rules.")
                return
        
        if has_b_field:
            tb = self.field_types_B.get(b_field)
            if not tb:
                QMessageBox.warning(self, "Warning", "Load types and fields for B before adding rules.")
                return
        
        def format_constraint(constraint_type, value):
            if constraint_type == "Any value":
                return "any"
            elif needs_value(constraint_type):
                return f"{constraint_type.lower()} '{value}'"
            else:
                return constraint_type.lower()
        
        def format_side(field, constraint_type, constraint_value):
            if not field:
                return "(none)"
            constraint_str = format_constraint(constraint_type, constraint_value)
            return f"{field} ({constraint_str})"
        
        a_side_str = format_side(a_field, a_constraint_type, a_constraint_value)
        b_side_str = format_side(b_field, b_constraint_type, b_constraint_value)
        
        display_text = f"{a_side_str} ↔ {b_side_str}"
        
        rule_data = {
            'a_field': a_field,
            'b_field': b_field,
            'a_constraint_type': a_constraint_type if a_constraint_type != "Any value" else None,
            'a_constraint_value': a_constraint_value,
            'b_constraint_type': b_constraint_type if b_constraint_type != "Any value" else None,
            'b_constraint_value': b_constraint_value,
        }
        
        item = QListWidgetItem(display_text)
        item.setData(Qt.UserRole, rule_data)
        self.rules_list.addItem(item)
        
        self.a_constraint_value_edit.clear()
        self.b_constraint_value_edit.clear()
        self._mark_dirty()

    def _remove_rule(self):
        it = self.rules_list.currentItem()
        if not it:
            return
        row = self.rules_list.row(it)
        self.rules_list.takeItem(row)
        self._mark_dirty()

    def _collect_side_cfg(self, label: str) -> dict:
        tid = self._resolve_type_id_from_text(self.__dict__[f"type_edit_{label}"].text())
        if tid is None:
            raise ValueError(f"Set {label}: Invalid RSZ Type")
        cap_fields = [i.text() for i in self.__dict__[f"cap_fields_{label}"].selectedItems()]
        constraint_key = f"{label.lower()}_constraint_type"
        value_key = f"{label.lower()}_constraint_value"
        constraints = [
            {
                'field': rd[f'{label.lower()}_field'],
                'type': rd[constraint_key],
                'value': rd[value_key],
            }
            for i in range(self.rules_list.count())
            if (rd := self.rules_list.item(i).data(Qt.UserRole)) and rd.get(constraint_key)
        ]
        return {
            "pattern": self.__dict__[f"pattern_{label}"].text().strip() or "*",
            "allow_scn": self.__dict__[f"scn_{label}"].isChecked(),
            "allow_pfb": self.__dict__[f"pfb_{label}"].isChecked(),
            "allow_user": self.__dict__[f"user_{label}"].isChecked(),
            "recursive": self.recursive_check.isChecked(),
            "type_id": tid,
            "capture_fields": cap_fields,
            "constraints": constraints,
        }

    def _signature(self, a_cfg: dict, b_cfg: dict, key_pairs: List[Tuple[str, str]]):
        def norm_cfg(cfg: dict):
            return (
                cfg.get("pattern"), cfg.get("allow_scn"), cfg.get("allow_pfb"), cfg.get("allow_user"),
                cfg.get("recursive"), cfg.get("type_id"),
                tuple(cfg.get("capture_fields") or []),
                tuple((c['field'], c['type'], c.get('value')) for c in cfg.get("constraints") or []),
            )
        return (norm_cfg(a_cfg), norm_cfg(b_cfg), tuple(key_pairs))

    def _mark_dirty(self):
        self._last_signature = None
        self.export_btn.setEnabled(False)
        self.status_label.setText("Ready")

    def _start_preview(self):
        if not self.type_registry:
            QMessageBox.warning(self, "Warning", "Load a type registry first")
            return
        root_dir = self.root_dir_edit.text().strip()
        if not root_dir or not os.path.isdir(root_dir):
            QMessageBox.warning(self, "Warning", "Select a valid root directory")
            return
        try:
            a_cfg = self._collect_side_cfg("A")
            b_cfg = self._collect_side_cfg("B")
        except ValueError as e:
            QMessageBox.warning(self, "Warning", str(e))
            return
        key_pairs: List[Tuple[str, str]] = []
        for i in range(self.rules_list.count()):
            it = self.rules_list.item(i)
            rule_data = it.data(Qt.UserRole)
            if rule_data and rule_data.get('a_field') and rule_data.get('b_field'):
                key_pairs.append((rule_data['a_field'], rule_data['b_field']))
        
        if self.rules_list.count() == 0:
            QMessageBox.warning(self, "Warning", "Add at least one rule")
            return
        
        if not key_pairs:
            QMessageBox.warning(self, "Warning", "Need at least one matching rule (A field ↔ B field) to find matches.\nConstraint-only rules can only filter, not match.")
            return
        if not self.field_types_A or not self.field_types_B:
            QMessageBox.warning(self, "Warning", "Load fields for both A and B types before running.")
            return
        sig = self._signature(a_cfg, b_cfg, key_pairs)
        if self._last_signature == sig and self.a_map and self.b_map:
            self._populate_results_tree()
            self.export_btn.setEnabled(True)
            self.status_label.setText("Ready. Results are up-to-date.")
            return
        if self._scanning:
            return

        self.results_tree.clear()
        self.export_btn.setEnabled(False)
        self.status_label.setText("Scanning…")
        self.worker = MatcherWorker(root_dir, self.type_registry, a_cfg, b_cfg, key_pairs)
        self.worker.progress_update.connect(self._on_progress)
        self.worker.preview_ready.connect(self._on_preview_ready)
        self.worker.error_occurred.connect(self._on_error)
        self.worker.finished_ok.connect(self._on_finished)
        if not hasattr(self, "_progress") or self._progress is None or not self._progress.isVisible():
            self._progress = QProgressDialog("Scanning files…", "Cancel", 0, 100, self)
            self._progress.setWindowTitle("Scan Progress")
            self._progress.setWindowModality(Qt.WindowModal)
            self._progress.canceled.connect(self._cancel_worker)
            self._progress.show()
        self._last_signature = sig
        self._scanning = True
        self.worker.start()

    def _on_progress(self, cur: int, total: int):
        if getattr(self, "_progress", None):
            self._progress.setMaximum(max(1, total))
            self._progress.setValue(cur)
            self._progress.setLabelText(f"Scanning files… ({cur}/{total})")

    def _cancel_worker(self):
        if self.worker:
            self.worker.cancel()
            self.worker.wait()
            self.worker = None
        if hasattr(self, "_progress") and self._progress:
            self._progress.close()
            self._progress = None
        self._scanning = False
        self.status_label.setText("Scan canceled.")

    def _on_error(self, msg: str):
        if hasattr(self, "_progress") and self._progress:
            self._progress.close()
            self._progress = None
        QMessageBox.critical(self, "Error", msg)
        self._scanning = False

    def _on_preview_ready(self):
        if self.worker:
            self.a_map = self.worker.result_a_map
            self.b_map = self.worker.result_b_map
        a_count, b_count, common = self._populate_results_tree()
        self.status_label.setText(
            f"A: {a_count} instances, B: {b_count} instances, Common keys: {common}"
        )

    def _on_finished(self):
        if hasattr(self, "_progress") and self._progress:
            self._progress.close()
            self._progress = None

        if self.worker:
            self.a_map = self.worker.result_a_map
            self.b_map = self.worker.result_b_map

        self.export_btn.setEnabled(True)
        self._scanning = False
        _, _, common = self._populate_results_tree()
        self.status_label.setText(f"Ready. {common} matching key(s).")
        if self._export_after_scan:
            self._export_after_scan = False
            self._export_csv(force_use_current=True)

    def _on_allow_same_file_changed(self):
        if self.a_map and self.b_map:
            a_count, b_count, common = self._populate_results_tree()
            self.status_label.setText(
                f"A: {a_count} instances, B: {b_count} instances, Common keys: {common}"
            )

    def _populate_results_tree(self):
        try:
            self.results_tree.clear()
            allow_same = self.allow_same_file_check.isChecked()
            common_keys = set(self.a_map) & set(self.b_map)
            fmt = {
                "str": lambda v: f'"{v}"',
                "int": str,
                "float": lambda v: f"{v:.3f}",
                "bool": lambda v: "true" if v else "false",
                "bytes": lambda v: f"bytes[{len(v)}]",
                "vec": lambda v: f"({','.join(str(cv) for _, cv in v)})",
            }
            total_a = total_b = total_keys = 0
            processed_pairs = set()
            for key in sorted(common_keys):
                a_used, b_used = set(), set()
                for a in self.a_map.get(key, []):
                    for b in self.b_map.get(key, []):
                        if (a["path"] == b["path"] and a["instance_id"] == b["instance_id"]) or (
                            not allow_same and a["path"] == b["path"]
                        ):
                            continue
                        pair_id = tuple(sorted([(a["path"], a["instance_id"]), (b["path"], b["instance_id"]) ]))
                        if pair_id in processed_pairs:
                            continue
                        processed_pairs.add(pair_id)
                        a_used.add((a["path"], a["instance_id"]))
                        b_used.add((b["path"], b["instance_id"]))
                if a_used and b_used:
                    total_keys += 1
                    total_a += len(a_used)
                    total_b += len(b_used)
                    key_parts = []
                    for part in key:
                        if isinstance(part, tuple) and len(part) == 2:
                            t, val = part
                            key_parts.append(fmt.get(t, lambda x: f"{t}({x})")(val))
                        else:
                            key_parts.append(str(part))
                    item = QTreeWidgetItem(self.results_tree)
                    item.setText(0, " | ".join(key_parts))
                    item.setText(1, str(len(a_used)))
                    item.setText(2, str(len(b_used)))
            return total_a, total_b, total_keys
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to populate results: {e}")
            return 0, 0, 0

    def _export_csv(self, force_use_current: bool = False):
        if (len(self.a_map) == 0 or len(self.b_map) == 0) and not force_use_current:
            self._export_after_scan = True
            if not self._scanning:
                self._start_preview()
            return
        out_path, _ = QFileDialog.getSaveFileName(self, "Save CSV", "", "CSV Files (*.csv)")
        if not out_path:
            return

        try:
            include_filenames = self.include_filenames_check.isChecked()
            allow_same_file = self.allow_same_file_check.isChecked()
            a_cap = [i.text() for i in self.cap_fields_A.selectedItems()]
            b_cap = [i.text() for i in self.cap_fields_B.selectedItems()]
            cols = (
                (["A_file", "A_instance"] if include_filenames else [])
                + [f"A_{f}" for f in a_cap]
                + (["B_file", "B_instance"] if include_filenames else [])
                + [f"B_{f}" for f in b_cap]
            )
            lines: List[str] = [",".join(_escape_csv(c) for c in cols)]
            common_keys = set(self.a_map) & set(self.b_map)
            processed_pairs = set()
            for key in common_keys:
                for a in self.a_map[key]:
                    for b in self.b_map[key]:
                        if (a["path"] == b["path"] and a["instance_id"] == b["instance_id"]) or (
                            not allow_same_file and a["path"] == b["path"]
                        ):
                            continue
                        pair_id = tuple(sorted([(a["path"], a["instance_id"]), (b["path"], b["instance_id"]) ]))
                        if pair_id in processed_pairs:
                            continue
                        processed_pairs.add(pair_id)
                        row: List[str] = []
                        if include_filenames:
                            row += [a["path"], str(a["instance_id"]) ]
                        a_fields = a.get("all_fields", {})
                        row += [ _escape_csv(format_value(a_fields.get(f))) if a_fields.get(f) is not None else "" for f in a_cap ]
                        if include_filenames:
                            row += [ b["path"], str(b["instance_id"]) ]
                        b_fields = b.get("all_fields", {})
                        row += [ _escape_csv(format_value(b_fields.get(f))) if b_fields.get(f) is not None else "" for f in b_cap ]
                        lines.append(",".join(row))
            Path(out_path).write_text("\n".join(lines), encoding="utf-8")
            QMessageBox.information(self, "Export", f"CSV written to {out_path}")
        except Exception as e:
            QMessageBox.critical(self, "Export Error", str(e))

def _escape_csv(s: Any) -> str:
    if s is None:
        return ""
    text = str(s)
    if any(ch in text for ch in [",", "\n", "\r", '"']):
        text = '"' + text.replace('"', '""') + '"'
    return text
