from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QComboBox,
    QLabel, QPushButton, QLineEdit, QMessageBox, QSplitter,
    QTextEdit, QGroupBox, QFrame, QCheckBox,
    QSpinBox, QFormLayout, QHeaderView, QScrollArea
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QStandardItemModel, QStandardItem, QFont, QPalette, QKeySequence, QShortcut


class MsgViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self.current_language = 0
        self.modified = False
        self.tree = None
        self.original_entries = []
        
        self._setup_ui()
        self._populate_tree()
        self._connect_signals()
        self._setup_shortcuts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        header_frame = QFrame()
        header_frame.setFrameStyle(QFrame.StyledPanel)
        header_frame.setMaximumHeight(70)
        header_layout = QVBoxLayout(header_frame)
        header_layout.setContentsMargins(6, 4, 6, 4)
        header_layout.setSpacing(3)

        controls_row = QHBoxLayout()
        
        title_label = QLabel("📝")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        title_label.setFont(title_font)
        controls_row.addWidget(title_label)
        
        self.status_label = QLabel("● Ready")
        controls_row.addWidget(self.status_label)
        
        controls_row.addWidget(self._create_separator())
        
        lang_group = QHBoxLayout()
        lang_group.addWidget(QLabel("🌐"))
        self.language_combo = QComboBox()
        self.language_combo.setMinimumWidth(150)
        
        for i, lang_code in enumerate(self.handler.useLanguages):
            lang_name = self.handler.get_language_name(lang_code)
            self.language_combo.addItem(f"{lang_name} ({lang_code})", i)
            
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        lang_group.addWidget(self.language_combo)
        controls_row.addLayout(lang_group)

        controls_row.addWidget(self._create_separator())

        entry_group = QHBoxLayout()
        self.add_btn = QPushButton("➕")
        self.add_btn.setToolTip("Add Entry")
        self.add_btn.setMaximumWidth(35)
        self.add_btn.clicked.connect(self._on_add_entry)
        entry_group.addWidget(self.add_btn)

        self.del_btn = QPushButton("🗑️")
        self.del_btn.setToolTip("Delete Entry")
        self.del_btn.setMaximumWidth(35)
        self.del_btn.clicked.connect(self._on_delete_entry)
        entry_group.addWidget(self.del_btn)

        self.duplicate_btn = QPushButton("📋")
        self.duplicate_btn.setToolTip("Duplicate Entry")
        self.duplicate_btn.setMaximumWidth(35)
        self.duplicate_btn.clicked.connect(self._on_duplicate_entry)
        entry_group.addWidget(self.duplicate_btn)
        controls_row.addLayout(entry_group)

        controls_row.addWidget(self._create_separator())

        stats_group = QHBoxLayout()
        self.entry_count_label = QLabel(f"📊 {len(self.handler.entries)}")
        self.entry_count_label.setToolTip("Entry count")
        stats_group.addWidget(self.entry_count_label)
        
        self.lang_count_label = QLabel(f"🌍 {len(self.handler.useLanguages)}")
        self.lang_count_label.setToolTip("Language count")
        stats_group.addWidget(self.lang_count_label)
        controls_row.addLayout(stats_group)

        controls_row.addStretch()
        header_layout.addLayout(controls_row)
        layout.addWidget(header_frame, 0)

        search_frame = QFrame()
        search_frame.setFrameStyle(QFrame.StyledPanel)
        search_frame.setMaximumHeight(40)
        search_layout = QHBoxLayout(search_frame)
        search_layout.setContentsMargins(6, 3, 6, 3)
        search_layout.setSpacing(6)

        search_label = QLabel("🔍")
        search_layout.addWidget(search_label)
        
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search entries by name, content, or UUID...")
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(self.search_edit)

        self.case_sensitive_cb = QCheckBox("Case sensitive")
        search_layout.addWidget(self.case_sensitive_cb)
        self.case_sensitive_cb.toggled.connect(self._perform_search)

        self.clear_search_btn = QPushButton("✖️")
        self.clear_search_btn.setMaximumWidth(30)
        self.clear_search_btn.setToolTip("Clear search")
        self.clear_search_btn.clicked.connect(self._clear_search)
        search_layout.addWidget(self.clear_search_btn)

        self.search_results_label = QLabel("")
        self.search_results_label.setMinimumWidth(100)
        search_layout.addWidget(self.search_results_label)
        layout.addWidget(search_frame, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        tree_widget = QWidget()
        tree_layout = QVBoxLayout(tree_widget)
        tree_layout.setContentsMargins(4, 4, 4, 4)
        tree_layout.setSpacing(2)
        
        tree_header = QLabel("📋 Message Entries")
        tree_header_font = QFont()
        tree_header_font.setBold(True)
        tree_header.setFont(tree_header_font)
        tree_layout.addWidget(tree_header)
        
        self.tree = QTreeView()
        self.tree.setEditTriggers(QTreeView.DoubleClicked | QTreeView.EditKeyPressed)
        self.tree.setSelectionBehavior(QTreeView.SelectRows)
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        tree_layout.addWidget(self.tree)
        
        splitter.addWidget(tree_widget)

        details_widget = QWidget()
        details_layout = QVBoxLayout(details_widget)
        details_layout.setContentsMargins(4, 4, 4, 4)
        details_layout.setSpacing(4)
        
        details_header = QLabel("✏️ Entry Details")
        details_header_font = QFont()
        details_header_font.setBold(True)
        details_header.setFont(details_header_font)
        details_layout.addWidget(details_header)

        info_group = QGroupBox("📄 Entry Information")
        info_layout = QFormLayout(info_group)
        info_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.uuid_edit = QLineEdit()
        self.uuid_edit.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.uuid_edit.textChanged.connect(self._on_uuid_changed)
        info_layout.addRow("🆔 UUID:", self.uuid_edit)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Entry name...")
        self.name_edit.textChanged.connect(self._on_name_changed)
        info_layout.addRow("📛 Name:", self.name_edit)

        self.soundid_edit = QLineEdit()
        self.soundid_edit.setPlaceholderText("0")
        self.soundid_edit.textChanged.connect(self._on_soundid_changed)
        info_layout.addRow("🔊 SoundID:", self.soundid_edit)

        self.index_label = QLabel("—")
        info_layout.addRow("🔢 Index/Hash:", self.index_label)

        details_layout.addWidget(info_group)

        content_group = QGroupBox("💬 Content")
        content_layout = QVBoxLayout(content_group)
        
        content_header = QHBoxLayout()
        content_header.addWidget(QLabel("Message text:"))
        content_header.addStretch()
        
        self.char_count_label = QLabel("0 chars")
        content_header.addWidget(self.char_count_label)
        content_layout.addLayout(content_header)
        
        self.content_edit = QTextEdit()
        self.content_edit.setMaximumHeight(100)
        self.content_edit.setMinimumHeight(60)
        self.content_edit.textChanged.connect(self._on_content_text_changed)
        self.content_edit.textChanged.connect(self._update_char_count)
        content_layout.addWidget(self.content_edit)
        details_layout.addWidget(content_group)

        self.attributes_group = QGroupBox("⚙️ Attributes")
        attributes_main_layout = QVBoxLayout(self.attributes_group)
        
        attr_controls = QHBoxLayout()
        self.add_attr_btn = QPushButton("➕ Add Attribute")
        self.add_attr_btn.clicked.connect(self._on_add_attribute)
        attr_controls.addWidget(self.add_attr_btn)
        
        self.remove_attr_btn = QPushButton("➖ Remove Attribute")
        self.remove_attr_btn.clicked.connect(self._on_remove_attribute)
        attr_controls.addWidget(self.remove_attr_btn)
        attr_controls.addStretch()
        
        attributes_main_layout.addLayout(attr_controls)
        
        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)
        
        scroll_widget = QWidget()
        self.attributes_layout = QVBoxLayout(scroll_widget)
        self.attributes_layout.addStretch()
        
        scroll_area.setWidget(scroll_widget)
        attributes_main_layout.addWidget(scroll_area)
        
        details_layout.addWidget(self.attributes_group, 1)

        splitter.addWidget(details_widget)
        
        splitter.setSizes([800, 500])
        layout.addWidget(splitter, 1)

    def _create_separator(self):
        separator = QFrame()
        separator.setFrameShape(QFrame.VLine)
        separator.setFrameShadow(QFrame.Sunken)
        return separator

    def _connect_signals(self):
        if self.tree and self.tree.model() and self.tree.selectionModel():
            try:
                self.tree.selectionModel().currentChanged.disconnect()
            except (RuntimeError, TypeError):
                pass
            self.tree.selectionModel().currentChanged.connect(self._on_tree_selection_changed)

    def _setup_shortcuts(self):
        delete_shortcut = QShortcut(QKeySequence(Qt.Key_Delete), self)
        delete_shortcut.activated.connect(self._on_delete_entry)
        
        shift_delete_shortcut = QShortcut(QKeySequence("Shift+Delete"), self)
        shift_delete_shortcut.activated.connect(self._on_delete_entry)

    def _populate_tree(self):
        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Name", "Preview", "UUID"])
        
        self.original_entries = list(enumerate(self.handler.entries))
        
        for i, e in enumerate(self.handler.entries):
            name_item = QStandardItem(e.get("name", f"Entry_{i}"))
            
            preview_text = e.get("content", [""])[self.current_language]
            if len(preview_text) > 50:
                preview_text = preview_text[:47] + "..."
            preview_item = QStandardItem(preview_text)
            
            uuid_item = QStandardItem(e.get("uuid", ""))
            
            name_item.setData({"entry_index": i, "field_type": "name"}, Qt.UserRole)
            preview_item.setData({"entry_index": i, "field_type": "content", "lang_index": self.current_language}, Qt.UserRole)
            uuid_item.setData({"entry_index": i, "field_type": "uuid"}, Qt.UserRole)
            
            if not e.get("name"):
                name_item.setText("(Unnamed)")
                name_item.setForeground(QPalette().color(QPalette.Disabled, QPalette.Text))
            
            model.appendRow([name_item, preview_item, uuid_item])
        
        self.tree.setModel(model)
        
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        
        self._connect_signals()
        model.dataChanged.connect(self._on_tree_data_changed)
        self._update_search_results()

    def _on_search_text_changed(self, text):
        self._perform_search()

    def _perform_search(self):
        search_text = self.search_edit.text().strip()
        case_sensitive = self.case_sensitive_cb.isChecked()
        
        if not search_text:
            self._show_all_entries()
            return
        
        if not case_sensitive:
            search_text = search_text.lower()
        
        model = QStandardItemModel()
        model.setHorizontalHeaderLabels(["Name", "Preview", "UUID"])
        
        matches = 0
        for original_index, entry in self.original_entries:
            name = entry.get("name", "")
            content = entry.get("content", [""])[self.current_language]
            uuid_str = entry.get("uuid", "")
            
            search_fields = [name, content, uuid_str]
            if not case_sensitive:
                search_fields = [field.lower() for field in search_fields]
            
            if any(search_text in field for field in search_fields):
                name_item = QStandardItem(name or f"Entry_{original_index}")
                preview_text = content
                if len(preview_text) > 50:
                    preview_text = preview_text[:47] + "..."
                preview_item = QStandardItem(preview_text)
                uuid_item = QStandardItem(uuid_str)
                
                name_item.setData({"entry_index": original_index, "field_type": "name"}, Qt.UserRole)
                preview_item.setData({"entry_index": original_index, "field_type": "content", "lang_index": self.current_language}, Qt.UserRole)
                uuid_item.setData({"entry_index": original_index, "field_type": "uuid"}, Qt.UserRole)
                
                model.appendRow([name_item, preview_item, uuid_item])
                matches += 1
        
        self.tree.setModel(model)
        
        self._connect_signals()
        model.dataChanged.connect(self._on_tree_data_changed)
        self._update_search_results(matches)

    def _show_all_entries(self):
        self._populate_tree()

    def _clear_search(self):
        self.search_edit.clear()
        self._show_all_entries()

    def _update_search_results(self, matches=None):
        if matches is None:
            self.search_results_label.setText("")
        else:
            total = len(self.handler.entries)
            self.search_results_label.setText(f"Found {matches} of {total} entries")

    def _update_char_count(self):
        text = self.content_edit.toPlainText()
        char_count = len(text)
        self.char_count_label.setText(f"{char_count} chars")

    def _on_language_changed(self, idx):
        self.current_language = idx
        self._populate_tree()
        self._update_details_panel()

    def _on_tree_selection_changed(self, cur, _):
        self._update_details_panel()

    def _on_content_text_changed(self):
        self._update_char_count()
        self._on_content_changed()

    def _update_details_panel(self):
        sel = self.tree.selectionModel()
        if not sel:
            return self._clear_details_panel()
        idx = sel.currentIndex()
        if not idx.isValid():
            return self._clear_details_panel()
        
        item = self.tree.model().item(idx.row(), 0)
        if not item:
            return self._clear_details_panel()
        
        meta = item.data(Qt.UserRole)
        if not meta:
            return self._clear_details_panel()
            
        entry_idx = meta["entry_index"]
        entry = self.handler.entries[entry_idx]

        self.uuid_edit.blockSignals(True)
        self.uuid_edit.setText(entry.get("uuid", ""))
        self.uuid_edit.blockSignals(False)

        self.name_edit.blockSignals(True)
        self.name_edit.setText(entry.get("name", ""))
        self.name_edit.blockSignals(False)

        self.soundid_edit.blockSignals(True)
        self.soundid_edit.setText(str(entry.get("SoundID", 0)))
        self.soundid_edit.blockSignals(False)

        if self.handler._by_hash(self.handler.header["version"]):
            self.index_label.setText(f"Hash: {entry.get('nameHash', 0)}")
        else:
            self.index_label.setText(f"Index: {entry.get('index', entry_idx)}")

        current_text = self.content_edit.toPlainText()
        new_text = entry.get("content", [""])[self.current_language]
        
        if current_text != new_text:
            self.content_edit.blockSignals(True)
            cursor_position = self.content_edit.textCursor().position()
            self.content_edit.setPlainText(new_text)
            
            if cursor_position <= len(new_text):
                cursor = self.content_edit.textCursor()
                cursor.setPosition(cursor_position)
                self.content_edit.setTextCursor(cursor)
            
            self.content_edit.blockSignals(False)
            
        self._update_char_count()

        self._update_attributes_panel(entry, entry_idx)

    def _clear_details_panel(self):
        self.uuid_edit.clear()
        self.name_edit.clear()
        self.soundid_edit.clear()
        self.content_edit.clear()
        self.index_label.setText("—")
        self.char_count_label.setText("0 chars")
        while self.attributes_layout.count() > 1:
            c = self.attributes_layout.takeAt(0)
            if w := c.widget():
                w.deleteLater()

    def _update_attributes_panel(self, entry, entry_idx):
        while self.attributes_layout.count() > 1:
            c = self.attributes_layout.takeAt(0)
            if w := c.widget():
                w.deleteLater()
        
        if not self.handler.userParamNames:
            no_attrs_label = QLabel("No attributes defined")
            no_attrs_label.setObjectName("readOnlyInfo")
            self.attributes_layout.insertWidget(0, no_attrs_label)
            return
        
        for idx, (name, value) in enumerate(zip(self.handler.userParamNames, entry.get("attributes", []))):
            attr_frame = QFrame()
            attr_frame.setFrameStyle(QFrame.StyledPanel)
            attr_layout = QHBoxLayout(attr_frame)
            attr_layout.setContentsMargins(5, 5, 5, 5)
            
            name_edit = QLineEdit()
            name_edit.setText(name)
            name_edit.setMinimumWidth(100)
            name_edit.setMaximumWidth(150)
            name_edit.textChanged.connect(lambda v, i=idx: self._on_attribute_name_changed(v, i))
            attr_layout.addWidget(name_edit)
            
            attr_layout.addWidget(QLabel(":"))
            
            attr_type = self.handler.userParamTypes[idx] if idx < len(self.handler.userParamTypes) else -1
            
            if attr_type == 0:
                editor = QSpinBox()
                editor.setRange(-2147483648, 2147483647)
                editor.setValue(int(value) if value else 0)
                editor.valueChanged.connect(lambda v, i=idx: self._on_attribute_changed_typed(str(v), entry_idx, i))
            elif attr_type == 1:
                editor = QLineEdit()
                editor.setText(str(float(value)) if value else "0.0")
                editor.setPlaceholderText("0.0")
                editor.textChanged.connect(lambda v, i=idx: self._on_attribute_changed_typed(v, entry_idx, i))
            else:
                editor = QLineEdit()
                editor.setText(str(value) if value else "")
                editor.textChanged.connect(lambda v, i=idx: self._on_attribute_changed_typed(v, entry_idx, i))
            
            attr_layout.addWidget(editor)
            
            delete_btn = QPushButton("🗑️")
            delete_btn.setMaximumWidth(30)
            delete_btn.setToolTip(f"Delete attribute '{name}'")
            delete_btn.clicked.connect(lambda checked, i=idx, n=name: self._on_delete_single_attribute(i, n))
            attr_layout.addWidget(delete_btn)
            
            self.attributes_layout.insertWidget(idx, attr_frame)

    def _on_attribute_changed_typed(self, value, entry_idx, attr_idx):
        meta = {"entry_index": entry_idx, "field_type": "attribute", "attr_index": attr_idx}
        if self.handler.validate_edit(meta, value):
            self.handler.handle_edit(meta, value, "", None, self.tree)
            self._set_modified(True)

    def _on_duplicate_entry(self):
        sel = self.tree.selectionModel()
        if not sel:
            return
        
        idx = sel.currentIndex()
        if not idx.isValid():
            return
        
        item = self.tree.model().item(idx.row(), 0)
        if not item:
            return
        
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        
        entry_idx = meta["entry_index"]
        original_entry = self.handler.entries[entry_idx]
        
        import uuid
        new_entry = {
            "uuid": str(uuid.uuid4()),
            "name": original_entry.get("name", "") + " (Copy)",
            "content": list(original_entry.get("content", ["" for _ in self.handler.useLanguages])),
            "attributes": list(original_entry.get("attributes", ["" for _ in self.handler.userParamTypes])),
            "SoundID": original_entry.get("SoundID", 0)
        }
        
        if self.handler._by_hash(self.handler.header["version"]):
            new_entry["nameHash"] = 0
        else:
            new_entry["index"] = len(self.handler.entries)
        
        self.handler.entries.append(new_entry)
        self._populate_tree()
        self._set_modified(True)
        self._update_stats()
        
        model = self.tree.model()
        last_row = model.rowCount() - 1
        if last_row >= 0:
            new_index = model.index(last_row, 0)
            self.tree.setCurrentIndex(new_index)
            self._update_details_panel()

    def _update_stats(self):
        self.entry_count_label.setText(f"📊 {len(self.handler.entries)}")
        self.entry_count_label.setToolTip(f"Entries: {len(self.handler.entries)}")
        self.lang_count_label.setText(f"🌍 {len(self.handler.useLanguages)}")
        self.lang_count_label.setToolTip(f"Languages: {len(self.handler.useLanguages)}")

    def _set_modified(self, m: bool):
        if self.modified != m:
            self.modified = m
            self.modified_changed.emit(m)
            
            if m:
                self.status_label.setText("● Modified")
            else:
                self.status_label.setText("● Ready")

    def _on_uuid_changed(self, text):
        sel = self.tree.selectionModel()
        if not sel:
            return
        idx_model = sel.currentIndex()
        if not idx_model.isValid():
            return
        
        item = self.tree.model().item(idx_model.row(), 0)
        if not item:
            return
        
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        
        entry_idx = meta["entry_index"]
        
        if self.handler.validate_edit({"entry_index": entry_idx, "field_type": "uuid"}, text):
            self.handler.handle_edit({"entry_index": entry_idx, "field_type": "uuid"}, text, "", None, self.tree)
            self._set_modified(True)
            uuid_item = self.tree.model().item(idx_model.row(), 2)
            if uuid_item:
                uuid_item.setText(text)
        else:
            QMessageBox.warning(self, "Invalid UUID", "Please enter a valid GUID format:\nxxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")

    def _on_name_changed(self):
        sel = self.tree.selectionModel()
        if not sel:
            return
        idx_model = sel.currentIndex()
        if not idx_model.isValid():
            return
        
        item = self.tree.model().item(idx_model.row(), 0)
        if not item:
            return
        
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        
        entry_idx = meta["entry_index"]
        txt = self.name_edit.text()
        
        if self.handler.validate_edit({"entry_index": entry_idx, "field_type": "name"}, txt):
            self.handler.handle_edit({"entry_index": entry_idx, "field_type": "name"}, txt, "", None, self.tree)
            self._set_modified(True)
            name_item = self.tree.model().item(idx_model.row(), 0)
            if name_item:
                name_item.setText(txt or "(Unnamed)")

    def _on_content_changed(self):
        sel = self.tree.selectionModel()
        if not sel:
            return
        idx_model = sel.currentIndex()
        if not idx_model.isValid():
            return
        
        item = self.tree.model().item(idx_model.row(), 0)
        if not item:
            return
        
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        
        entry_idx = meta["entry_index"]
        txt = self.content_edit.toPlainText()
        
        content_meta = {"entry_index": entry_idx, "field_type": "content", "lang_index": self.current_language}
        if self.handler.validate_edit(content_meta, txt):
            self.handler.handle_edit(content_meta, txt, "", None, self.tree)
            self._set_modified(True)
            
            preview = txt if len(txt) <= 50 else txt[:47] + "..."
            preview_item = self.tree.model().item(idx_model.row(), 1)
            if preview_item:
                model = self.tree.model()
                model.blockSignals(True)
                preview_item.setText(preview)
                model.blockSignals(False)

    def _on_tree_data_changed(self, top_left, _, roles):
        if Qt.EditRole not in roles:
            return

        item = self.tree.model().item(top_left.row(), top_left.column())
        meta = item.data(Qt.UserRole)
        if meta:
            new_value = item.text()
            if self.handler.validate_edit(meta, new_value):
                self.handler.handle_edit(meta, new_value, "", item, self.tree)
                self._set_modified(True)
                self._update_details_panel()
            else:
                field = meta["field_type"]
                entry_idx = meta["entry_index"]
                if field == "name":
                    item.setText(self.handler.entries[entry_idx].get("name", ""))
                elif field == "uuid":
                    item.setText(self.handler.entries[entry_idx].get("uuid", ""))
                elif field == "content":
                    lang_idx = meta.get("lang_index", 0)
                    content = self.handler.entries[entry_idx].get("content", [""])[lang_idx]
                    preview = content if len(content) <= 50 else content[:47] + "..."
                    item.setText(preview)

    def _on_add_entry(self):
        
        self.handler.add_entry()
        self._populate_tree()
        self._set_modified(True)
        self._update_stats()
        
        model = self.tree.model()
        last_row = model.rowCount() - 1
        if last_row >= 0:
            new_index = model.index(last_row, 0)
            self.tree.setCurrentIndex(new_index)
            self._update_details_panel()

    def _on_delete_entry(self):
        sel = self.tree.selectionModel()
        if not sel:
            return
        
        idx_model = sel.currentIndex()
        if not idx_model.isValid():
            return
        
        item = self.tree.model().item(idx_model.row(), 0)
        if not item:
            return
        
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        
        entry_idx = meta["entry_index"]
        entry_name = self.handler.entries[entry_idx].get("name", f"Entry {entry_idx}")
        
        reply = QMessageBox.question(
            self, 
            "Delete Entry", 
            f"Are you sure you want to delete:\n\n'{entry_name}'?\n\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        next_row = min(idx_model.row(), len(self.handler.entries) - 2)
        
        self.handler.remove_entry(entry_idx)
        self._populate_tree()
        self._set_modified(True)
        self._update_stats()
        
        model = self.tree.model()
        if model.rowCount() > 0 and next_row >= 0:
            next_index = model.index(min(next_row, model.rowCount() - 1), 0)
            self.tree.setCurrentIndex(next_index)
            self._update_details_panel()
        else:
            self._clear_details_panel()

    def _on_soundid_changed(self, text):
        sel = self.tree.selectionModel()
        if not sel:
            return
        idx_model = sel.currentIndex()
        if not idx_model.isValid():
            return
        
        item = self.tree.model().item(idx_model.row(), 0)
        if not item:
            return
        
        meta = item.data(Qt.UserRole)
        if not meta:
            return
        
        entry_idx = meta["entry_index"]
        
        if self.handler.validate_edit({"entry_index": entry_idx, "field_type": "SoundID"}, text):
            self.handler.handle_edit({"entry_index": entry_idx, "field_type": "SoundID"}, text, "", None, self.tree)
            self._set_modified(True)
        else:
            QMessageBox.warning(self, "Invalid SoundID", "Please enter a valid integer value.")

    def _on_attribute_name_changed(self, text, attr_idx):
        if self.handler.validate_edit({"field_type": "attribute_name", "attr_index": attr_idx}, text):
            self.handler.handle_edit({"field_type": "attribute_name", "attr_index": attr_idx}, text, "", None, self.tree)
            self._set_modified(True)

    def _on_add_attribute(self):
        """Add a new user parameter (attribute)"""
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QFormLayout, QDialogButtonBox, QComboBox
        
        dialog = QDialog(self)
        dialog.setWindowTitle("Add New Attribute")
        layout = QVBoxLayout(dialog)
        
        form = QFormLayout()
        
        name_edit = QLineEdit()
        name_edit.setText("NewParam")
        form.addRow("Name:", name_edit)
        
        type_combo = QComboBox()
        type_combo.addItem("String", 2)
        type_combo.addItem("Integer", 0)
        type_combo.addItem("Float", 1)
        form.addRow("Type:", type_combo)
        
        layout.addLayout(form)
        
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        
        if dialog.exec() == QDialog.Accepted:
            param_name = name_edit.text()
            param_type = type_combo.currentData()
            
            self.handler.add_user_param(param_name, param_type)
            self._set_modified(True)
            self._update_details_panel()
    
    def _on_delete_single_attribute(self, idx, name):
        """Delete a specific attribute by index"""
        reply = QMessageBox.question(
            self,
            "Remove Attribute",
            f"Are you sure you want to remove attribute '{name}'?\n\nThis will remove it from all entries.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.handler.remove_user_param(idx)
            self._set_modified(True)
            self._update_details_panel()
    
    def _on_remove_attribute(self):
        """Remove a user parameter (attribute)"""
        from PySide6.QtWidgets import QInputDialog
        
        if not self.handler.userParamNames:
            QMessageBox.information(self, "No Attributes", "There are no attributes to remove.")
            return
        
        attr_name, ok = QInputDialog.getItem(
            self, 
            "Remove Attribute", 
            "Select attribute to remove:",
            self.handler.userParamNames,
            0,
            False
        )
        
        if ok and attr_name:
            idx = self.handler.userParamNames.index(attr_name)
            self._on_delete_single_attribute(idx, attr_name)

    def rebuild(self) -> bytes:
        return self.handler.rebuild()
