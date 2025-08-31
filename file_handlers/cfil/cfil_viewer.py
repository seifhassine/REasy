import uuid
from PySide6.QtCore import Signal, Qt, QSize
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QLabel, QPushButton, QLineEdit, QSpinBox, QFrame, QMessageBox, QFileDialog
)


class CfilViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.tree = None
        self._modified = False
        self._setup_ui()
        self._refresh_ui_from_model()

    @property
    def modified(self):
        return self._modified

    @modified.setter
    def modified(self, value):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header_frame = QFrame()
        header_layout = QVBoxLayout(header_frame)
        title_row = QHBoxLayout()
        title = QLabel("🧩 CFIL Editor")
        title_row.addWidget(title)
        title_row.addStretch()
        header_layout.addLayout(title_row)

        controls_row = QHBoxLayout()
        self.layer_index_spin = QSpinBox()
        self.layer_index_spin.setRange(0, 255)
        self.layer_index_label = QLabel("Layer Index:")
        controls_row.addWidget(self.layer_index_label)
        controls_row.addWidget(self.layer_index_spin)

        self.layer_guid_edit = QLineEdit()
        self.layer_guid_edit.setPlaceholderText("00000000-0000-0000-0000-000000000000")
        self.layer_guid_label = QLabel("Layer GUID:")
        controls_row.addWidget(self.layer_guid_label)
        controls_row.addWidget(self.layer_guid_edit)
        controls_row.addStretch()
        header_layout.addLayout(controls_row)
        layout.addWidget(header_frame)

        mask_bar = QHBoxLayout()
        self.add_btn = QPushButton("➕ Add Mask")
        self.del_btn = QPushButton("🗑️ Remove Selected")
        self.up_btn = QPushButton("⬆️ Move Up")
        self.down_btn = QPushButton("⬇️ Move Down")
        self.import_btn = QPushButton("Import")
        self.export_btn = QPushButton("Export")
        mask_bar.addWidget(self.add_btn)
        mask_bar.addWidget(self.del_btn)
        mask_bar.addWidget(self.up_btn)
        mask_bar.addWidget(self.down_btn)
        mask_bar.addWidget(self.import_btn)
        mask_bar.addWidget(self.export_btn)
        mask_bar.addStretch()
        layout.addLayout(mask_bar)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(2)
        self.tree.setHeaderLabels(["Item", "Value"])
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.setIndentation(16)
        layout.addWidget(self.tree)

        self.layer_index_spin.valueChanged.connect(self._on_layer_index_changed)
        self.layer_guid_edit.editingFinished.connect(self._on_layer_guid_edited)
        self.add_btn.clicked.connect(self._on_add_mask)
        self.del_btn.clicked.connect(self._on_delete_selected)
        self.up_btn.clicked.connect(self._on_move_up)
        self.down_btn.clicked.connect(self._on_move_down)
        self.import_btn.clicked.connect(self._on_import_guids)
        self.export_btn.clicked.connect(self._on_export_guids)
        self.tree.itemChanged.connect(self._on_item_changed)

    def _refresh_ui_from_model(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        self.layer_index_label.setVisible(cfil.version == 3)
        self.layer_index_spin.setVisible(cfil.version == 3)
        self.layer_guid_label.setVisible(cfil.version != 3)
        self.layer_guid_edit.setVisible(cfil.version != 3)
        self.import_btn.setVisible(True)
        self.export_btn.setVisible(True)
        if cfil.version == 3:
            self.import_btn.setText("Import IDs")
            self.export_btn.setText("Export IDs")
        else:
            self.import_btn.setText("Import GUIDs")
            self.export_btn.setText("Export GUIDs")
        self.layer_index_spin.blockSignals(True)
        self.layer_index_spin.setValue(getattr(cfil, 'layer_index', 0))
        self.layer_index_spin.blockSignals(False)
        self.layer_guid_edit.blockSignals(True)
        self.layer_guid_edit.setText(str(getattr(cfil, 'layer_guid', uuid.UUID(int=0))))
        self.layer_guid_edit.blockSignals(False)
        self.tree.blockSignals(True)
        self.tree.clear()
        if cfil.version == 3:
            masks = QTreeWidgetItem(self.tree, ["Mask IDs", f"{len(cfil.mask_ids)} items"])
            for i, mid in enumerate(cfil.mask_ids):
                it = QTreeWidgetItem(masks, [f"[{i}] ID", str(mid)])
                it.setFlags(it.flags() | Qt.ItemIsEditable)
                it.setData(0, Qt.UserRole, {"type": "mask_id", "index": i})
        else:
            masks = QTreeWidgetItem(self.tree, ["Mask GUIDs", f"{len(cfil.mask_guids)} items"])
            for i, g in enumerate(cfil.mask_guids):
                it = QTreeWidgetItem(masks, [f"[{i}] GUID", str(g)])
                it.setTextAlignment(0, Qt.AlignVCenter | Qt.AlignLeft)
                it.setData(0, Qt.UserRole, {"type": "mask_guid", "index": i})
                row_widget = QWidget()
                row_layout = QHBoxLayout(row_widget)
                row_layout.setContentsMargins(0, 2, 0, 2)
                row_layout.setSpacing(4)
                editor = QLineEdit(str(g))
                editor.setPlaceholderText("00000000-0000-0000-0000-000000000000")
                sample = "00000000-0000-0000-0000-000000000000"
                fm = editor.fontMetrics()
                editor.setFixedWidth(fm.horizontalAdvance(sample) + 16)
                row_layout.addWidget(editor, 0, Qt.AlignVCenter | Qt.AlignLeft)
                it.setSizeHint(0, QSize(0, editor.sizeHint().height() + 6))
                def make_handler(index: int, line: QLineEdit):
                    def _apply():
                        c = getattr(self.handler, 'cfil', None)
                        if not c or c.version == 3:
                            return
                        try:
                            val = uuid.UUID(line.text().strip())
                            if 0 <= index < len(c.mask_guids):
                                c.mask_guids[index] = val
                                self._mark_modified()
                        except Exception:
                            QMessageBox.warning(self, "Invalid GUID", "Please enter a valid GUID.")
                    return _apply
                editor.editingFinished.connect(make_handler(i, editor))
                self.tree.setItemWidget(it, 1, row_widget)
        self.tree.expandAll()
        self.tree.blockSignals(False)

    def _mark_modified(self):
        self.modified = True
        if hasattr(self.handler, 'modified'):
            self.handler.modified = True

    def _on_import_guids(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        title = "Import IDs" if cfil.version == 3 else "Import GUIDs"
        path, _ = QFileDialog.getOpenFileName(self, title, "", "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            tokens: list[str] = []
            for part in content.replace(',', ' ').replace(';', ' ').split():
                tokens.append(part.strip())
            appended = 0
            if cfil.version == 3:
                for t in tokens:
                    try:
                        val = int(t, 10)
                        if 0 <= val <= 255 and val not in cfil.mask_ids:
                            cfil.mask_ids.append(val)
                            appended += 1
                    except Exception:
                        pass
            else:
                for t in tokens:
                    s = t.strip().strip('{}')
                    try:
                        g = uuid.UUID(s) if '-' in s else uuid.UUID(hex=s)
                        if g not in cfil.mask_guids:
                            cfil.mask_guids.append(g)
                            appended += 1
                    except Exception:
                        pass
            if appended > 0:
                self._mark_modified()
                self._refresh_ui_from_model()
            else:
                QMessageBox.information(self, title, "No new entries to import.")
        except Exception as e:
            QMessageBox.critical(self, title, f"Failed to import: {e}")

    def _on_export_guids(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        title = "Export IDs" if cfil.version == 3 else "Export GUIDs"
        default = "ids.txt" if cfil.version == 3 else "guids.txt"
        path, _ = QFileDialog.getSaveFileName(self, title, default, "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                if cfil.version == 3:
                    for mid in cfil.mask_ids:
                        f.write(str(mid) + "\n")
                else:
                    for g in cfil.mask_guids:
                        f.write(str(g) + "\n")
        except Exception as e:
            QMessageBox.critical(self, title, f"Failed to export: {e}")

    def _on_layer_index_changed(self, value: int):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil or cfil.version != 3:
            return
        cfil.layer_index = int(value)
        self._mark_modified()
        self._refresh_ui_from_model()

    def _on_layer_guid_edited(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil or cfil.version == 3:
            return
        text = self.layer_guid_edit.text().strip()
        try:
            cfil.layer_guid = uuid.UUID(text)
            self._mark_modified()
            self._refresh_ui_from_model()
        except Exception:
            QMessageBox.warning(self, "Invalid GUID", "Please enter a valid GUID.")

    def _on_add_mask(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        if cfil.version == 3:
            cfil.mask_ids.append(0)
        else:
            cfil.mask_guids.append(uuid.uuid4())
        self._mark_modified()
        self._refresh_ui_from_model()

    def _selected_mask_index(self) -> int:
        sel = self.tree.selectedItems()
        if not sel:
            return -1
        item = sel[0]
        meta = item.data(0, Qt.UserRole)
        if isinstance(meta, dict) and 'index' in meta:
            return int(meta['index'])
        return -1

    def _on_delete_selected(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        idx = self._selected_mask_index()
        if idx < 0:
            return
        if cfil.version == 3:
            if 0 <= idx < len(cfil.mask_ids):
                del cfil.mask_ids[idx]
        else:
            if 0 <= idx < len(cfil.mask_guids):
                del cfil.mask_guids[idx]
        self._mark_modified()
        self._refresh_ui_from_model()

    def _on_move_up(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        idx = self._selected_mask_index()
        if idx <= 0:
            return
        if cfil.version == 3:
            cfil.mask_ids[idx-1], cfil.mask_ids[idx] = cfil.mask_ids[idx], cfil.mask_ids[idx-1]
        else:
            cfil.mask_guids[idx-1], cfil.mask_guids[idx] = cfil.mask_guids[idx], cfil.mask_guids[idx-1]
        self._mark_modified()
        self._refresh_ui_from_model()

    def _on_move_down(self):
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        idx = self._selected_mask_index()
        if idx < 0:
            return
        if cfil.version == 3:
            if idx + 1 < len(cfil.mask_ids):
                cfil.mask_ids[idx+1], cfil.mask_ids[idx] = cfil.mask_ids[idx], cfil.mask_ids[idx+1]
        else:
            if idx + 1 < len(cfil.mask_guids):
                cfil.mask_guids[idx+1], cfil.mask_guids[idx] = cfil.mask_guids[idx], cfil.mask_guids[idx+1]
        self._mark_modified()
        self._refresh_ui_from_model()

    def _on_item_changed(self, item: QTreeWidgetItem, column: int):
        if column != 1:
            return
        cfil = getattr(self.handler, 'cfil', None)
        if not cfil:
            return
        meta = item.data(0, Qt.UserRole)
        if not isinstance(meta, dict):
            return
        idx = int(meta.get('index', -1))
        if meta.get('type') == 'mask_id' and cfil.version == 3:
            try:
                val = int(item.text(1))
                if not (0 <= val <= 255):
                    raise ValueError()
                cfil.mask_ids[idx] = val
                self._mark_modified()
            except Exception:
                pass
        elif meta.get('type') == 'mask_guid' and cfil.version != 3:
            try:
                g = uuid.UUID(item.text(1).strip())
                cfil.mask_guids[idx] = g
                self._mark_modified()
            except Exception:
                pass

