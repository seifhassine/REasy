import uuid
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QLabel, QPushButton, QLineEdit, QSpinBox, QFrame, QMessageBox, QFileDialog, QGroupBox
)


class CfilViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.mask_tree = None
        self.mat_attr_tree = None
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
        
        v7_row1 = QHBoxLayout()
        self.status_edit = QLineEdit()
        self.status_edit.setPlaceholderText("0")
        self.status_edit.setMaximumWidth(150)
        self.status_tooltip = (
            "Format compatibility flag:\n\n"
            "• 0 = Modern CFIL format (no legacy processing)\n"
            "• 1+ = Legacy format (requires additional material attribute handling)\n\n"
            "When status ≠ 0, the engine performs extra runtime processing to set\n"
            "material information on collision shapes for backward compatibility."
        )
        self.status_edit.setToolTip(self.status_tooltip)
        self.status_label = QLabel("Status (uint32):")
        v7_row1.addWidget(self.status_label)
        
        self.status_info_icon = QLabel("ℹ️")
        self.status_info_icon.setToolTip(self.status_tooltip)
        self.status_info_icon.setStyleSheet("QLabel { color: #6495ED; font-size: 14px; }")
        self.status_info_icon.setCursor(Qt.WhatsThisCursor)
        self.status_info_icon.mousePressEvent = lambda event: self._show_status_info()
        v7_row1.addWidget(self.status_info_icon)
        
        v7_row1.addWidget(self.status_edit)
        
        self.material_id_guid_edit = QLineEdit()
        self.material_id_guid_edit.setPlaceholderText("00000000-0000-0000-0000-000000000000")
        self.material_id_guid_label = QLabel("Material ID GUID:")
        v7_row1.addWidget(self.material_id_guid_label)
        v7_row1.addWidget(self.material_id_guid_edit)
        v7_row1.addStretch()
        header_layout.addLayout(v7_row1)
        layout.addWidget(header_frame)

        self.mask_group = QGroupBox("Mask GUIDs")
        mask_group_layout = QVBoxLayout(self.mask_group)
        
        mask_bar = QHBoxLayout()
        self.add_mask_btn = QPushButton("➕ Add")
        self.del_mask_btn = QPushButton("🗑️ Remove")
        self.up_mask_btn = QPushButton("⬆️ Move Up")
        self.down_mask_btn = QPushButton("⬇️ Move Down")
        self.import_mask_btn = QPushButton("Import")
        self.export_mask_btn = QPushButton("Export")
        mask_bar.addWidget(self.add_mask_btn)
        mask_bar.addWidget(self.del_mask_btn)
        mask_bar.addWidget(self.up_mask_btn)
        mask_bar.addWidget(self.down_mask_btn)
        mask_bar.addWidget(self.import_mask_btn)
        mask_bar.addWidget(self.export_mask_btn)
        mask_bar.addStretch()
        mask_group_layout.addLayout(mask_bar)
        
        self.mask_tree = QTreeWidget()
        self.mask_tree.setColumnCount(2)
        self.mask_tree.setHeaderLabels(["Item", "Value"])
        header = self.mask_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mask_tree.setIndentation(16)
        mask_group_layout.addWidget(self.mask_tree)
        layout.addWidget(self.mask_group)
        
        self.mat_attr_group = QGroupBox("Material Attribute GUIDs")
        mat_attr_group_layout = QVBoxLayout(self.mat_attr_group)
        
        mat_attr_bar = QHBoxLayout()
        self.add_mat_attr_btn = QPushButton("➕ Add")
        self.del_mat_attr_btn = QPushButton("🗑️ Remove")
        self.up_mat_attr_btn = QPushButton("⬆️ Move Up")
        self.down_mat_attr_btn = QPushButton("⬇️ Move Down")
        self.import_mat_attr_btn = QPushButton("Import")
        self.export_mat_attr_btn = QPushButton("Export")
        mat_attr_bar.addWidget(self.add_mat_attr_btn)
        mat_attr_bar.addWidget(self.del_mat_attr_btn)
        mat_attr_bar.addWidget(self.up_mat_attr_btn)
        mat_attr_bar.addWidget(self.down_mat_attr_btn)
        mat_attr_bar.addWidget(self.import_mat_attr_btn)
        mat_attr_bar.addWidget(self.export_mat_attr_btn)
        mat_attr_bar.addStretch()
        mat_attr_group_layout.addLayout(mat_attr_bar)
        
        self.mat_attr_tree = QTreeWidget()
        self.mat_attr_tree.setColumnCount(2)
        self.mat_attr_tree.setHeaderLabels(["Item", "Value"])
        header = self.mat_attr_tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.mat_attr_tree.setIndentation(16)
        mat_attr_group_layout.addWidget(self.mat_attr_tree)
        layout.addWidget(self.mat_attr_group)

        self.layer_index_spin.valueChanged.connect(self._on_layer_index_changed)
        self.layer_guid_edit.editingFinished.connect(self._on_layer_guid_edited)
        self.status_edit.editingFinished.connect(self._on_status_changed)
        self.material_id_guid_edit.editingFinished.connect(self._on_material_id_guid_edited)
        self.add_mask_btn.clicked.connect(self._on_add_mask)
        self.del_mask_btn.clicked.connect(self._on_delete_mask)
        self.up_mask_btn.clicked.connect(self._on_move_mask_up)
        self.down_mask_btn.clicked.connect(self._on_move_mask_down)
        self.import_mask_btn.clicked.connect(self._on_import_masks)
        self.export_mask_btn.clicked.connect(self._on_export_masks)
        self.add_mat_attr_btn.clicked.connect(self._on_add_mat_attr)
        self.del_mat_attr_btn.clicked.connect(self._on_delete_mat_attr)
        self.up_mat_attr_btn.clicked.connect(self._on_move_mat_attr_up)
        self.down_mat_attr_btn.clicked.connect(self._on_move_mat_attr_down)
        self.import_mat_attr_btn.clicked.connect(self._on_import_mat_attrs)
        self.export_mat_attr_btn.clicked.connect(self._on_export_mat_attrs)

    def _refresh_ui_from_model(self):
        cfil = self.handler.cfil
        if not cfil:
            return
        is_v7 = cfil.version != 3
        
        self.layer_index_label.setVisible(cfil.version == 3)
        self.layer_index_spin.setVisible(cfil.version == 3)
        self.layer_guid_label.setVisible(is_v7)
        self.layer_guid_edit.setVisible(is_v7)
        self.status_label.setVisible(is_v7)
        self.status_edit.setVisible(is_v7)
        self.status_info_icon.setVisible(is_v7)
        self.material_id_guid_label.setVisible(is_v7)
        self.material_id_guid_edit.setVisible(is_v7)
        self.mat_attr_group.setVisible(is_v7)
        
        if cfil.version == 3:
            self.mask_group.setTitle("Mask IDs")
            self.import_mask_btn.setText("Import")
            self.export_mask_btn.setText("Export")
        else:
            self.mask_group.setTitle("Mask GUIDs")
            self.import_mask_btn.setText("Import")
            self.export_mask_btn.setText("Export")
        
        self.layer_index_spin.blockSignals(True)
        self.layer_index_spin.setValue(cfil.layer_index)
        self.layer_index_spin.blockSignals(False)
        self.layer_guid_edit.blockSignals(True)
        self.layer_guid_edit.setText(str(cfil.layerGuid))
        self.layer_guid_edit.blockSignals(False)
        self.status_edit.blockSignals(True)
        self.status_edit.setText(str(cfil.status))
        self.status_edit.blockSignals(False)
        self.material_id_guid_edit.blockSignals(True)
        self.material_id_guid_edit.setText(str(cfil.materialIdGuid))
        self.material_id_guid_edit.blockSignals(False)
        
        self.mask_tree.clear()
        if cfil.version == 3:
            for i, mid in enumerate(cfil.mask_ids):
                it = QTreeWidgetItem(self.mask_tree, [f"[{i}]", str(mid)])
                it.setFlags(it.flags() | Qt.ItemIsEditable)
                it.setData(0, Qt.UserRole, i)
        else:
            for i, g in enumerate(cfil.mask_guids):
                it = QTreeWidgetItem(self.mask_tree, [f"[{i}]", ""])
                it.setData(0, Qt.UserRole, i)
                widget = self._create_guid_widget(g, i, 'mask_guids')
                self.mask_tree.setItemWidget(it, 1, widget)
        
        self.mat_attr_tree.clear()
        if is_v7:
            for i, g in enumerate(cfil.materialAttributeGuids):
                it = QTreeWidgetItem(self.mat_attr_tree, [f"[{i}]", ""])
                it.setData(0, Qt.UserRole, i)
                widget = self._create_guid_widget(g, i, 'materialAttributeGuids')
                self.mat_attr_tree.setItemWidget(it, 1, widget)

    def _create_guid_widget(self, guid: uuid.UUID, index: int, attr_name: str):
        row_widget = QWidget()
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(4)
        editor = QLineEdit(str(guid))
        editor.setPlaceholderText("00000000-0000-0000-0000-000000000000")
        sample = "00000000-0000-0000-0000-000000000000"
        fm = editor.fontMetrics()
        editor.setFixedWidth(fm.horizontalAdvance(sample) + 16)
        row_layout.addWidget(editor, 0, Qt.AlignVCenter | Qt.AlignLeft)
        
        def on_text_changed():
            cfil = self.handler.cfil
            if not cfil or cfil.version == 3:
                return
            try:
                val = uuid.UUID(editor.text().strip())
                guid_list = getattr(cfil, attr_name)
                if 0 <= index < len(guid_list) and guid_list[index] != val:
                    guid_list[index] = val
                    self._mark_modified()
            except Exception:
                pass
        
        editor.textChanged.connect(on_text_changed)
        return row_widget
    
    def _mark_modified(self):
        self.modified = True
        self.handler.modified = True
    
    def _show_status_info(self):
        """Show detailed information about the status field"""
        QMessageBox.information(
            self,
            "Status Field Information",
            self.status_tooltip
        )

    def _on_status_changed(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        text = self.status_edit.text().strip()
        try:
            if text.lower().startswith('0x'):
                value = int(text, 16)
            else:
                value = int(text, 10)
            if not (0 <= value <= 0xFFFFFFFF):
                raise ValueError("Value out of range for uint32")
            if cfil.status != value:
                cfil.status = value
                self._mark_modified()
        except Exception:
            QMessageBox.warning(self, "Invalid Value", "Please enter a valid uint32 value (0-4294967295 or 0x0-0xFFFFFFFF).")
    
    def _on_material_id_guid_edited(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        try:
            new_guid = uuid.UUID(self.material_id_guid_edit.text().strip())
            if cfil.materialIdGuid != new_guid:
                cfil.materialIdGuid = new_guid
                self._mark_modified()
        except Exception:
            QMessageBox.warning(self, "Invalid GUID", "Please enter a valid GUID.")
            self.material_id_guid_edit.setText(str(cfil.materialIdGuid))
    
    def _on_layer_guid_edited(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        try:
            new_guid = uuid.UUID(self.layer_guid_edit.text().strip())
            if cfil.layerGuid != new_guid:
                cfil.layerGuid = new_guid
                self._mark_modified()
        except Exception:
            QMessageBox.warning(self, "Invalid GUID", "Please enter a valid GUID.")
            self.layer_guid_edit.setText(str(cfil.layerGuid))

    def _on_layer_index_changed(self, value: int):
        cfil = self.handler.cfil
        if not cfil or cfil.version != 3:
            return
        if cfil.layer_index != value:
            cfil.layer_index = value
            self._mark_modified()

    def _import_guids(self, guid_list: list, title: str):
        path, _ = QFileDialog.getOpenFileName(self, title, "", "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()
            appended = 0
            for part in content.replace(',', ' ').replace(';', ' ').split():
                s = part.strip().strip('{}')
                try:
                    g = uuid.UUID(s) if '-' in s else uuid.UUID(hex=s)
                    if g not in guid_list:
                        guid_list.append(g)
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

    def _export_guids(self, guid_list: list, title: str, default_filename: str):
        path, _ = QFileDialog.getSaveFileName(self, title, default_filename, "Text Files (*.txt);;All Files (*)")
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                for g in guid_list:
                    f.write(str(g) + "\n")
        except Exception as e:
            QMessageBox.critical(self, title, f"Failed to export: {e}")

    def _on_import_masks(self):
        cfil = self.handler.cfil
        if not cfil:
            return
        if cfil.version == 3:
            path, _ = QFileDialog.getOpenFileName(self, "Import IDs", "", "Text Files (*.txt);;All Files (*)")
            if not path:
                return
            try:
                with open(path, 'r', encoding='utf-8') as f:
                    content = f.read()
                appended = 0
                for part in content.replace(',', ' ').replace(';', ' ').split():
                    try:
                        val = int(part, 10)
                        if 0 <= val <= 255:
                            cfil.mask_ids.append(val)
                            appended += 1
                    except Exception:
                        pass
                if appended > 0:
                    self._mark_modified()
                    self._refresh_ui_from_model()
                else:
                    QMessageBox.information(self, "Import IDs", "No new entries to import.")
            except Exception as e:
                QMessageBox.critical(self, "Import IDs", f"Failed to import: {e}")
        else:
            self._import_guids(cfil.mask_guids, "Import Mask GUIDs")

    def _on_export_masks(self):
        cfil = self.handler.cfil
        if not cfil:
            return
        if cfil.version == 3:
            path, _ = QFileDialog.getSaveFileName(self, "Export IDs", "mask_ids.txt", "Text Files (*.txt);;All Files (*)")
            if not path:
                return
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    for mid in cfil.mask_ids:
                        f.write(str(mid) + "\n")
            except Exception as e:
                QMessageBox.critical(self, "Export IDs", f"Failed to export: {e}")
        else:
            self._export_guids(cfil.mask_guids, "Export Mask GUIDs", "mask_guids.txt")

    def _on_add_mask(self):
        cfil = self.handler.cfil
        if not cfil:
            return
        if cfil.version == 3:
            cfil.mask_ids.append(0)
        else:
            cfil.mask_guids.append(uuid.uuid4())
        self._mark_modified()
        self._refresh_ui_from_model()

    def _get_selected_index(self, tree: QTreeWidget) -> int:
        sel = tree.selectedItems()
        if not sel:
            return -1
        return sel[0].data(0, Qt.UserRole)

    def _on_delete_mask(self):
        cfil = self.handler.cfil
        if not cfil:
            return
        idx = self._get_selected_index(self.mask_tree)
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

    def _on_move_mask_up(self):
        cfil = self.handler.cfil
        if not cfil:
            return
        idx = self._get_selected_index(self.mask_tree)
        if idx <= 0:
            return
        if cfil.version == 3:
            cfil.mask_ids[idx-1], cfil.mask_ids[idx] = cfil.mask_ids[idx], cfil.mask_ids[idx-1]
        else:
            cfil.mask_guids[idx-1], cfil.mask_guids[idx] = cfil.mask_guids[idx], cfil.mask_guids[idx-1]
        self._mark_modified()
        self._refresh_ui_from_model()

    def _on_move_mask_down(self):
        cfil = self.handler.cfil
        if not cfil:
            return
        idx = self._get_selected_index(self.mask_tree)
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
    
    def _on_add_mat_attr(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        cfil.materialAttributeGuids.append(uuid.uuid4())
        self._mark_modified()
        self._refresh_ui_from_model()
    
    def _on_delete_mat_attr(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        idx = self._get_selected_index(self.mat_attr_tree)
        if idx < 0 or idx >= len(cfil.materialAttributeGuids):
            return
        del cfil.materialAttributeGuids[idx]
        self._mark_modified()
        self._refresh_ui_from_model()
    
    def _on_move_mat_attr_up(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        idx = self._get_selected_index(self.mat_attr_tree)
        if idx <= 0:
            return
        cfil.materialAttributeGuids[idx-1], cfil.materialAttributeGuids[idx] = cfil.materialAttributeGuids[idx], cfil.materialAttributeGuids[idx-1]
        self._mark_modified()
        self._refresh_ui_from_model()
    
    def _on_move_mat_attr_down(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        idx = self._get_selected_index(self.mat_attr_tree)
        if idx < 0 or idx + 1 >= len(cfil.materialAttributeGuids):
            return
        cfil.materialAttributeGuids[idx+1], cfil.materialAttributeGuids[idx] = cfil.materialAttributeGuids[idx], cfil.materialAttributeGuids[idx+1]
        self._mark_modified()
        self._refresh_ui_from_model()
    
    def _on_import_mat_attrs(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        self._import_guids(cfil.materialAttributeGuids, "Import Material Attribute GUIDs")
    
    def _on_export_mat_attrs(self):
        cfil = self.handler.cfil
        if not cfil or cfil.version == 3:
            return
        self._export_guids(cfil.materialAttributeGuids, "Export Material Attribute GUIDs", "material_attr_guids.txt")
