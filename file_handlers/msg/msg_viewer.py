import uuid
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeView, QComboBox,
    QLabel, QPushButton, QLineEdit, QMessageBox, QSplitter,
    QTextEdit, QGroupBox, QFrame, QCheckBox, QFileDialog,
    QSpinBox, QFormLayout, QHeaderView, QMenu, QScrollArea, QStyle, QDialog, QDialogButtonBox, QInputDialog
)
from PySide6.QtCore import QT_TRANSLATE_NOOP, QSignalBlocker, Qt, Signal
lazy from PySide6.QtGui import (
    QColor, QKeySequence, QShortcut, QStandardItem,
    QStandardItemModel,
)

lazy from ui.editor_widgets import editor_strip, pane_header, search_field, set_invalid, styled_label, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role
lazy from utils.number_format import format_full_float
lazy from file_handlers.sound.sound_waveform import WaveformWidget
lazy from file_handlers.sound import sound_profile_for_handler
lazy from file_handlers.msg.msg_sound_player import MsgSoundPreviewSession, configured_vgmstream


CHAR_COUNT_TEXT = QT_TRANSLATE_NOOP("MsgViewer", "{count} chars")
SOUNDID_TOOLTIP = QT_TRANSLATE_NOOP(
    "MsgViewer", "Resolved through Wwise trigger and event metadata during sound scans"
)

_AUDIO_LOCALE_PREFERENCES = {
    0: ("ja",), 1: ("en",), 2: ("fr",), 3: ("it",), 4: ("de",),
    5: ("es", "es419"), 6: ("ru",), 7: ("pl",), 8: ("nl",),
    9: ("pt", "ptbr"), 10: ("ptbr", "pt"), 11: ("ko",),
    12: ("zhtw", "zhcn"), 13: ("zhcn", "zhtw"), 32: ("es419", "es"),
}


class MsgViewer(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, handler):
        super().__init__()
        self.setObjectName("msgViewer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.handler = handler
        self.current_language = 0
        self.modified = False
        self.tree = None
        self.original_entries = []
        self._sound_session = None
        self._sound_catalog = {}
        self._sound_references = {}
        self._sound_scanned = False
        self._sound_scan_pending = False

        self._setup_ui()
        self._populate_tree()
        self._connect_signals()
        self._setup_shortcuts()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar, controls_row = editor_strip(margins=(6, 4, 6, 4))

        language_icon = QLabel()
        language_icon.setPixmap(line_icon("globe").pixmap(16, 16))
        language_icon.setToolTip(self.tr("Language"))
        controls_row.addWidget(language_icon)
        self.language_combo = QComboBox()
        self.language_combo.setMinimumWidth(150)
        self.language_combo.setToolTip(self.tr("Language"))

        for i, lang_code in enumerate(self.handler.useLanguages):
            lang_name = self.handler.get_language_name(lang_code)
            self.language_combo.addItem(f"{lang_name} ({lang_code})", i)

        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        controls_row.addWidget(self.language_combo)

        controls_row.addWidget(self._create_separator())

        self.add_btn = tool_button("add", self.tr("Add Entry"), self._on_add_entry)
        self.del_btn = tool_button("delete", self.tr("Delete Entry"), self._on_delete_entry)
        self.duplicate_btn = tool_button(
            "copy", self.tr("Duplicate Entry"), self._on_duplicate_entry
        )
        for button in (self.add_btn, self.del_btn, self.duplicate_btn):
            controls_row.addWidget(button)

        controls_row.addWidget(self._create_separator())

        self.import_btn = tool_button(
            "import", self.tr("Import entries from JSON"), self._on_import_json, text=self.tr("Import JSON")
        )
        controls_row.addWidget(self.import_btn)
        self.export_btn = tool_button(
            "export", self.tr("Export entries to JSON"), self._on_export_json, text=self.tr("Export JSON")
        )
        controls_row.addWidget(self.export_btn)

        controls_row.addWidget(self._create_separator())

        self.scan_sounds_btn = tool_button(
            "sound", self._sound_scan_tooltip(), self._on_scan_sounds, text=self.tr("Scan Sounds")
        )
        controls_row.addWidget(self.scan_sounds_btn)

        controls_row.addStretch()

        self.entry_count_label = styled_label(tone="muted")
        self.entry_count_label.setToolTip(self.tr("Entry count"))
        controls_row.addWidget(self.entry_count_label)
        controls_row.addSpacing(8)
        self.lang_count_label = styled_label(tone="muted")
        self.lang_count_label.setToolTip(self.tr("Language count"))
        controls_row.addWidget(self.lang_count_label)
        self._update_stats()

        controls_row.addWidget(self._create_separator())

        self.status_label = styled_label(self.tr("Ready"), tone="muted")
        controls_row.addWidget(self.status_label)
        layout.addWidget(toolbar, 0)

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)

        tree_widget = QWidget()
        tree_layout = QVBoxLayout(tree_widget)
        tree_layout.setContentsMargins(0, 0, 0, 0)
        tree_layout.setSpacing(0)

        self.search_results_label = styled_label("", tone="muted")
        entries_header = pane_header(self.tr("Message Entries"), self.search_results_label)
        entries_header.layout().setSpacing(6)
        tree_layout.addWidget(entries_header)

        search_bar = QWidget()
        search_layout = QHBoxLayout(search_bar)
        search_layout.setContentsMargins(6, 6, 6, 6)
        search_layout.setSpacing(8)

        self.search_edit = search_field(QLineEdit())
        self.search_edit.setPlaceholderText(self.tr("Search entries by name, content, or UUID..."))
        self.search_edit.textChanged.connect(self._on_search_text_changed)
        search_layout.addWidget(self.search_edit, 1)

        self.case_sensitive_cb = QCheckBox(self.tr("Case sensitive"))
        search_layout.addWidget(self.case_sensitive_cb)
        self.case_sensitive_cb.toggled.connect(self._perform_search)
        tree_layout.addWidget(search_bar)

        self.tree = QTreeView()
        self.tree.setStyleSheet("QTreeView { border: none; }")
        self.tree.setEditTriggers(QTreeView.DoubleClicked | QTreeView.EditKeyPressed)
        self.tree.setSelectionBehavior(QTreeView.SelectRows)
        self.tree.setRootIsDecorated(False)
        self.tree.setSortingEnabled(True)
        self.tree.clicked.connect(self._on_tree_clicked)
        tree_layout.addWidget(self.tree, 1)

        splitter.addWidget(tree_widget)

        details_widget = QWidget()
        details_outer = QVBoxLayout(details_widget)
        details_outer.setContentsMargins(0, 0, 0, 0)
        details_outer.setSpacing(0)
        details_outer.addWidget(pane_header(self.tr("Entry Details")))

        details_body = QWidget()
        details_layout = QVBoxLayout(details_body)
        details_layout.setContentsMargins(8, 6, 8, 8)
        details_layout.setSpacing(8)
        details_outer.addWidget(details_body, 1)

        info_group = QGroupBox(self.tr("Entry Information"))
        info_layout = QFormLayout(info_group)
        info_layout.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        info_layout.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        info_layout.setHorizontalSpacing(8)
        info_layout.setVerticalSpacing(6)

        self.uuid_edit = QLineEdit()
        self.uuid_edit.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
        self.uuid_edit.setProperty("mono", True)
        self.uuid_edit.ensurePolished()
        self.uuid_edit.setMinimumWidth(self.uuid_edit.fontMetrics().horizontalAdvance("0" * 36) + 18)
        self.uuid_edit.textChanged.connect(self._on_uuid_changed)
        info_layout.addRow(self.tr("UUID:"), self.uuid_edit)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Entry name...")
        self.name_edit.textChanged.connect(self._on_name_changed)
        info_layout.addRow(self.tr("Name:"), self.name_edit)

        self.soundid_edit = QLineEdit()
        self.soundid_edit.setPlaceholderText("0")
        self.soundid_edit.setToolTip(self.tr(SOUNDID_TOOLTIP))
        self.soundid_edit.textChanged.connect(self._on_soundid_changed)
        info_layout.addRow(self.tr("SoundID:"), self.soundid_edit)

        self.index_label = styled_label("—", tone="muted")
        info_layout.addRow(self.tr("Index/Hash:"), self.index_label)

        details_layout.addWidget(info_group)

        self.msg_waveform = WaveformWidget()
        self.msg_waveform.setFixedHeight(40)
        self.msg_waveform.seek_requested.connect(self._on_sound_seek)
        self.msg_waveform.hide()
        details_layout.addWidget(self.msg_waveform)

        content_group = QGroupBox(self.tr("Content"))
        content_layout = QVBoxLayout(content_group)
        content_layout.setSpacing(4)

        content_header = QHBoxLayout()
        content_header.addWidget(QLabel(self.tr("Message text:")))
        content_header.addStretch()

        self.char_count_label = styled_label(self.tr(CHAR_COUNT_TEXT).format(count=0), tone="subtle")
        content_header.addWidget(self.char_count_label)
        content_layout.addLayout(content_header)

        self.content_edit = QTextEdit()
        self.content_edit.setMinimumHeight(80)
        self.content_edit.textChanged.connect(self._on_content_text_changed)
        self.content_edit.textChanged.connect(self._update_char_count)
        content_layout.addWidget(self.content_edit, 1)
        details_layout.addWidget(content_group, 1)

        self.attributes_group = QGroupBox(self.tr("Attributes"))
        attributes_main_layout = QVBoxLayout(self.attributes_group)
        attributes_main_layout.setSpacing(4)

        attr_controls = QHBoxLayout()
        attr_controls.setSpacing(6)
        self.add_attr_btn = QPushButton(line_icon("add"), self.tr("Add Attribute"))
        set_style_role(self.add_attr_btn, compact=True)
        self.add_attr_btn.clicked.connect(self._on_add_attribute)
        attr_controls.addWidget(self.add_attr_btn)

        self.remove_attr_btn = QPushButton(line_icon("remove", current_colors()["danger"]), self.tr("Remove Attribute"))
        set_style_role(self.remove_attr_btn, compact=True, variant="danger")
        self.remove_attr_btn.clicked.connect(self._on_remove_attribute)
        attr_controls.addWidget(self.remove_attr_btn)
        attr_controls.addStretch()

        attributes_main_layout.addLayout(attr_controls)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.NoFrame)

        scroll_widget = QWidget()
        self.attributes_layout = QVBoxLayout(scroll_widget)
        self.attributes_layout.setContentsMargins(0, 0, 0, 0)
        self.attributes_layout.setSpacing(4)
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

    def _tree_row(self, entry_index, entry):
        name_item = QStandardItem(entry.get("name") or self.tr("(Unnamed)"))
        content = entry.get("content", ())
        preview = content[self.current_language] if self.current_language < len(content) else ""
        preview_item = QStandardItem(
            preview if len(preview) <= 50 else preview[:47] + "..."
        )
        uuid_item = QStandardItem(entry.get("uuid", ""))
        sound_item = self._make_sound_item(entry)

        name_item.setData(
            {"entry_index": entry_index, "field_type": "name"}, Qt.UserRole
        )
        preview_item.setData({
            "entry_index": entry_index,
            "field_type": "content",
            "lang_index": self.current_language,
        }, Qt.UserRole)
        uuid_item.setData(
            {"entry_index": entry_index, "field_type": "uuid"}, Qt.UserRole
        )
        sound_item.setData(
            {"entry_index": entry_index, "field_type": "sound"}, Qt.UserRole
        )
        if not entry.get("name"):
            name_item.setForeground(QColor(current_colors()["text_subtle"]))
        return [name_item, preview_item, uuid_item, sound_item]

    def _install_tree_model(self, model):
        previous = self.tree.model()
        self.tree.setModel(model)
        if previous is not None and previous is not model:
            previous.deleteLater()
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Fixed)
        header.resizeSection(3, 40)
        self._connect_signals()
        model.dataChanged.connect(self._on_tree_data_changed)

    def _populate_tree(self):
        model = QStandardItemModel(self.tree)
        model.setHorizontalHeaderLabels([
            self.tr("Name"), self.tr("Preview"), "UUID", self.tr("Sound")
        ])

        self.original_entries = list(enumerate(self.handler.entries))
        for entry_index, entry in self.original_entries:
            model.appendRow(self._tree_row(entry_index, entry))
        self._install_tree_model(model)
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

        model = QStandardItemModel(self.tree)
        model.setHorizontalHeaderLabels([
            self.tr("Name"), self.tr("Preview"), "UUID", self.tr("Sound")
        ])

        matches = 0
        for original_index, entry in self.original_entries:
            name = entry.get("name", "")
            values = entry.get("content", ())
            content = values[self.current_language] if self.current_language < len(values) else ""
            uuid_str = entry.get("uuid", "")

            search_fields = [name, content, uuid_str]
            if not case_sensitive:
                search_fields = [field.lower() for field in search_fields]

            if any(search_text in field for field in search_fields):
                model.appendRow(self._tree_row(original_index, entry))
                matches += 1

        self._install_tree_model(model)
        self._update_search_results(matches)

    def _show_all_entries(self):
        self._populate_tree()

    def _update_search_results(self, matches=None):
        if matches is None:
            self.search_results_label.setText("")
        else:
            total = len(self.handler.entries)
            self.search_results_label.setText(
                self.tr("Found {matches} of {total} entries").format(matches=matches, total=total)
            )

    def _update_char_count(self):
        text = self.content_edit.toPlainText()
        char_count = len(text)
        self.char_count_label.setText(self.tr(CHAR_COUNT_TEXT).format(count=char_count))

    def _on_export_json(self):
        default_name = "messages.json"
        path, _ = QFileDialog.getSaveFileName(
            self,
            self.tr("Export MSG to JSON"),
            default_name,
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        try:
            self.handler.export_json(path)
            QMessageBox.information(
                self,
                self.tr("Export JSON"),
                self.tr("Exported to:\n{path}").format(path=path),
            )
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Export JSON Failed"), str(exc))

    def _on_import_json(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Import JSON to MSG"),
            "",
            "JSON Files (*.json);;All Files (*)",
        )
        if not path:
            return
        try:
            self.handler.import_json(path)
            self._refresh_after_import()
            self._set_modified(True)
            QMessageBox.information(
                self,
                self.tr("Import JSON"),
                self.tr("Imported from:\n{path}").format(path=path),
            )
        except Exception as exc:
            QMessageBox.critical(self, self.tr("Import JSON Failed"), str(exc))

    def _refresh_after_import(self):
        self._invalidate_sound_scan()
        self.language_combo.blockSignals(True)
        self.language_combo.clear()
        for i, lang_code in enumerate(self.handler.useLanguages):
            lang_name = self.handler.get_language_name(lang_code)
            self.language_combo.addItem(f"{lang_name} ({lang_code})", i)
        if self.handler.useLanguages:
            self.current_language = min(self.current_language, len(self.handler.useLanguages) - 1)
        else:
            self.current_language = 0
        self.language_combo.setCurrentIndex(self.current_language)
        self.language_combo.blockSignals(False)
        self._update_stats()
        self._populate_tree()
        self._update_details_panel()

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
        self._set_field_invalid(self.uuid_edit, None)
        self._set_field_invalid(self.soundid_edit, None, self.tr(SOUNDID_TOOLTIP))

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
        self._set_field_invalid(self.uuid_edit, None)
        self._set_field_invalid(self.soundid_edit, None, self.tr(SOUNDID_TOOLTIP))
        self.content_edit.clear()
        self.index_label.setText("—")
        self.char_count_label.setText(self.tr(CHAR_COUNT_TEXT).format(count=0))
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
            no_attrs_label = QLabel(self.tr("No attributes defined"))
            no_attrs_label.setObjectName("readOnlyInfo")
            no_attrs_label.setProperty("tone", "subtle")
            self.attributes_layout.insertWidget(0, no_attrs_label)
            return

        for idx, (name, value) in enumerate(zip(self.handler.userParamNames, entry.get("attributes", []))):
            attr_frame = QFrame()
            attr_layout = QHBoxLayout(attr_frame)
            attr_layout.setContentsMargins(0, 0, 0, 0)
            attr_layout.setSpacing(6)
            
            name_edit = QLineEdit()
            name_edit.setText(name)
            name_edit.setMinimumWidth(100)
            name_edit.setMaximumWidth(150)
            name_edit.textChanged.connect(lambda v, i=idx: self._on_attribute_name_changed(v, i))
            attr_layout.addWidget(name_edit)

            attr_type = self.handler.userParamTypes[idx] if idx < len(self.handler.userParamTypes) else -1
            
            if attr_type == 0:
                editor = QSpinBox()
                editor.setRange(-2147483648, 2147483647)
                editor.setValue(int(value) if value else 0)
                editor.valueChanged.connect(lambda v, i=idx: self._on_attribute_changed_typed(str(v), entry_idx, i))
            elif attr_type == 1:
                editor = QLineEdit()
                editor.setText(format_full_float(value) if value else "0.0")
                editor.setPlaceholderText("0.0")
                editor.textChanged.connect(lambda v, i=idx: self._on_attribute_changed_typed(v, entry_idx, i))
            else:
                editor = QLineEdit()
                editor.setText(str(value) if value else "")
                editor.textChanged.connect(lambda v, i=idx: self._on_attribute_changed_typed(v, entry_idx, i))
            
            attr_layout.addWidget(editor)
            
            delete_btn = tool_button(
                "delete",
                self.tr("Delete attribute '{name}'").format(name=name),
                lambda checked, i=idx, n=name: self._on_delete_single_attribute(i, n),
            )
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
        self._invalidate_sound_scan()
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
        self.entry_count_label.setText(
            self.tr("Entries: {count}").format(count=len(self.handler.entries))
        )
        self.lang_count_label.setText(
            self.tr("Languages: {count}").format(count=len(self.handler.useLanguages))
        )

    def _set_modified(self, m: bool):
        if self.modified != m:
            self.modified = m
            self.modified_changed.emit(m)
            
            if not (
                self._sound_session and self._sound_session.active_message_id
            ):
                self.status_label.setText(self._idle_status_text())

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
            self._invalidate_sound_scan()
            self._set_modified(True)
            uuid_item = self.tree.model().item(idx_model.row(), 2)
            if uuid_item:
                uuid_item.setText(text)
            self._set_field_invalid(self.uuid_edit, None)
        else:
            self._set_field_invalid(
                self.uuid_edit,
                self.tr("Please enter a valid GUID format:\nxxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"),
            )

    @staticmethod
    def _set_field_invalid(field, message, tooltip=""):
        invalid = message is not None
        field.setToolTip(message if invalid else tooltip)
        set_invalid(field, invalid)

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
                name_item.setText(txt or self.tr("(Unnamed)"))

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
        if isinstance(meta, dict) and meta.get("field_type") != "sound":
            new_value = item.text()
            if self.handler.validate_edit(meta, new_value):
                self.handler.handle_edit(meta, new_value, "", item, self.tree)
                if meta.get("field_type") == "uuid":
                    self._invalidate_sound_scan()
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
        self._invalidate_sound_scan()
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
        entry_name = self.handler.entries[entry_idx].get("name") or self.tr("Entry {index}").format(index=entry_idx)
        
        reply = QMessageBox.question(
            self, 
            self.tr("Delete Entry"),
            self.tr("Are you sure you want to delete:\n\n'{name}'?\n\nThis action cannot be undone.").format(name=entry_name),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply != QMessageBox.Yes:
            return
        
        next_row = min(idx_model.row(), len(self.handler.entries) - 2)

        self.handler.remove_entry(entry_idx)
        self._invalidate_sound_scan()
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
            self._invalidate_sound_scan()
            self._set_modified(True)
            self._set_field_invalid(self.soundid_edit, None, self.tr(SOUNDID_TOOLTIP))
        else:
            self._set_field_invalid(
                self.soundid_edit, self.tr("Please enter a valid integer value."), self.tr(SOUNDID_TOOLTIP)
            )

    def _on_attribute_name_changed(self, text, attr_idx):
        if self.handler.validate_edit({"field_type": "attribute_name", "attr_index": attr_idx}, text):
            self.handler.handle_edit({"field_type": "attribute_name", "attr_index": attr_idx}, text, "", None, self.tree)
            self._set_modified(True)

    def _on_add_attribute(self):
        """Add a new user parameter (attribute)"""
        
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr("Add New Attribute"))
        layout = QVBoxLayout(dialog)
        
        form = QFormLayout()
        
        name_edit = QLineEdit()
        name_edit.setText("NewParam")
        form.addRow(self.tr("Name:"), name_edit)
        
        type_combo = QComboBox()
        type_combo.addItem(self.tr("String"), 2)
        type_combo.addItem(self.tr("Integer"), 0)
        type_combo.addItem(self.tr("Float"), 1)
        form.addRow(self.tr("Type:"), type_combo)
        
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
            self.tr("Remove Attribute"),
            self.tr("Are you sure you want to remove attribute '{name}'?\n\nThis will remove it from all entries.").format(name=name),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            self.handler.remove_user_param(idx)
            self._set_modified(True)
            self._update_details_panel()
    
    def _on_remove_attribute(self):
        """Remove a user parameter (attribute)"""
        
        if not self.handler.userParamNames:
            QMessageBox.information(
                self,
                self.tr("No Attributes"),
                self.tr("There are no attributes to remove."),
            )
            return
        
        attr_name, ok = QInputDialog.getItem(
            self, 
            self.tr("Remove Attribute"),
            self.tr("Select attribute to remove:"),
            self.handler.userParamNames,
            0,
            False
        )
        
        if ok and attr_name:
            idx = self.handler.userParamNames.index(attr_name)
            self._on_delete_single_attribute(idx, attr_name)

    # ── Sound playback ─────────────────────────────────────────────────

    @staticmethod
    def _message_key(entry) -> str:
        return str(entry.get("uuid", "")).strip().strip("{}").casefold()

    def _sound_scan_tooltip(self) -> str:
        return self.tr(
            "Find exact message, timeline, and Wwise references and inspect only "
            "their BNK/PCK files"
        )

    def _ensure_sound_session(self) -> bool:
        if self._sound_session is not None:
            return True

        profile = sound_profile_for_handler(self.handler)
        if profile is None:
            QMessageBox.information(
                self,
                self.tr("Scan Sounds"),
                self.tr("No sound profile matches the current project game."),
            )
            return False
        try:
            self._sound_session = MsgSoundPreviewSession(self.handler, profile, self)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            QMessageBox.warning(self, self.tr("Scan Sounds"), str(exc))
            return False
        self._sound_session.scan_finished.connect(self._on_sound_scan_finished)
        self._sound_session.scan_failed.connect(self._on_sound_scan_failed)
        self._sound_session.preparing.connect(self._on_sound_preparing)
        self._sound_session.playback_started.connect(self._on_sound_started)
        self._sound_session.playback_stopped.connect(self._on_sound_stopped)
        self._sound_session.playback_failed.connect(self._on_sound_failed)
        self._sound_session.waveform_ready.connect(self._on_sound_waveform_ready)
        self._sound_session.position_changed.connect(self.msg_waveform.set_position)
        return True

    def _cleanup_sound(self) -> None:
        if self._sound_session is not None:
            self._sound_session.cleanup()
            self._sound_session = None
        self._sound_catalog.clear()
        self._sound_references.clear()

    def _make_sound_item(self, entry: dict) -> QStandardItem:
        item = QStandardItem()
        item.setTextAlignment(Qt.AlignCenter)
        item.setEditable(False)
        self._update_sound_item(item, entry)
        return item

    def _update_sound_item(self, item: QStandardItem, entry: dict) -> None:
        message_id = self._message_key(entry)
        candidates = self._sound_catalog.get(message_id, ())
        references = self._sound_references.get(message_id, ())
        active = bool(
            self._sound_session
            and self._sound_session.active_message_id == message_id
        )
        icon = (
            QStyle.StandardPixmap.SP_MediaStop
            if active else QStyle.StandardPixmap.SP_MediaPlay
        )
        item.setIcon(self.style().standardIcon(icon))
        item.setText("")
        if active:
            item.setEnabled(True)
            item.setToolTip(self.tr("Stop playback"))
        elif candidates:
            item.setEnabled(True)
            item.setToolTip(self.tr(
                "Play {available} available preview(s) from {referenced} referenced "
                "source(s); the selected container stays cached until this MSG closes"
            ).format(
                available=len(candidates),
                referenced=len(references),
            ))
        elif references:
            item.setEnabled(False)
            item.setToolTip(self.tr(
                "This message references {count} sound(s), but their media is unavailable"
            ).format(count=len(references)))
        elif self._sound_scanned:
            item.setEnabled(False)
            item.setToolTip(self.tr("This message has no exact sound reference"))
        else:
            item.setEnabled(False)
            item.setToolTip(self.tr("Scan sounds to check exact message references"))

    # ── scan ────────────────────────────────────────────────────────────

    def _on_scan_sounds(self) -> None:
        if self._sound_scan_pending or not self._ensure_sound_session():
            return
        self._sound_session.stop()
        self._sound_scan_pending = True
        self.scan_sounds_btn.setEnabled(False)
        self.scan_sounds_btn.setText(self.tr("Scanning Sounds…"))
        self.status_label.setText(self.tr("Scanning exact sound references…"))
        self._sound_session.scan(
            (entry.get("uuid", ""), entry.get("SoundID", 0))
            for entry in self.handler.entries
        )

    def _on_sound_scan_finished(self, result) -> None:
        self._sound_scan_pending = False
        self._sound_scanned = True
        self._sound_catalog = result.catalog
        self._sound_references = result.references
        self.scan_sounds_btn.setEnabled(True)
        self.scan_sounds_btn.setText(self.tr("Scan Sounds"))
        self.scan_sounds_btn.setToolTip(self._sound_scan_tooltip())
        self._refresh_sound_column()

        playable = sum(bool(value) for value in result.catalog.values())
        referenced = len(result.references)
        self.status_label.setText(self.tr(
            "Sounds: {playable}/{referenced} referenced messages, {files} container(s) checked"
        ).format(
            playable=playable,
            referenced=referenced,
            files=result.inspected_file_count,
        ))
        if not referenced:
            QMessageBox.information(
                self,
                self.tr("Scan Sounds"),
                self.tr(
                    "No exact UUID, timeline, or SoundID-to-trigger references were "
                    "found for this MSG. SoundID is never guessed to be a Wwise media ID."
                ),
            )
        elif result.unavailable_source_count:
            self.scan_sounds_btn.setToolTip(self.tr(
                "{count} referenced source(s) were not available in this installation"
            ).format(count=result.unavailable_source_count))

    def _on_sound_scan_failed(self, message: str) -> None:
        self._sound_scan_pending = False
        self.scan_sounds_btn.setEnabled(True)
        self.scan_sounds_btn.setText(self.tr("Scan Sounds"))
        self.scan_sounds_btn.setToolTip(self._sound_scan_tooltip())
        self.status_label.setText(self._idle_status_text())
        QMessageBox.warning(self, self.tr("Sound Scan Error"), message)

    def _invalidate_sound_scan(self) -> None:
        if self._sound_session is not None:
            self._sound_session.cancel_scan()
            self._sound_session.stop()
        self._sound_catalog.clear()
        self._sound_references.clear()
        self._sound_scanned = False
        self._sound_scan_pending = False
        if hasattr(self, "scan_sounds_btn"):
            self.scan_sounds_btn.setEnabled(True)
            self.scan_sounds_btn.setText(self.tr("Scan Sounds"))
            self.scan_sounds_btn.setToolTip(self._sound_scan_tooltip())
        self._refresh_sound_column()

    def _refresh_sound_column(self) -> None:
        model = self.tree.model()
        if model is None:
            return
        blocker = QSignalBlocker(model)
        for row in range(model.rowCount()):
            item = model.item(row, 3)
            if item is None:
                continue
            meta = item.data(Qt.UserRole)
            entry_idx = meta.get("entry_index") if isinstance(meta, dict) else None
            if entry_idx is None or entry_idx >= len(self.handler.entries):
                continue
            self._update_sound_item(item, self.handler.entries[entry_idx])
        del blocker

    # ── play / stop ─────────────────────────────────────────────────────

    def _on_tree_clicked(self, index) -> None:
        if index.column() != 3:
            return
        meta = self.tree.model().item(index.row(), 0).data(Qt.UserRole)
        if not isinstance(meta, dict):
            return
        entry_idx = meta["entry_index"]
        entry = self.handler.entries[entry_idx]
        message_id = self._message_key(entry)
        session = self._sound_session
        if session is None:
            return
        if session.active_message_id == message_id:
            session.stop()
            return
        candidates = self._ordered_sound_candidates(
            self._sound_catalog.get(message_id, ())
        )
        if not candidates:
            return
        if len(candidates) == 1:
            self._play_sound(message_id, candidates[0])
            return

        menu = QMenu(self)
        play_icon = self.style().standardIcon(QStyle.StandardPixmap.SP_MediaPlay)
        for candidate in candidates:
            action = menu.addAction(play_icon, self._sound_candidate_label(candidate))
            action.triggered.connect(
                lambda _checked=False, value=candidate: self._play_sound(
                    message_id, value
                )
            )
        rect = self.tree.visualRect(index)
        menu.exec(self.tree.viewport().mapToGlobal(rect.bottomLeft()))

    def _ordered_sound_candidates(self, candidates):
        candidates = tuple(candidates)
        language_code = (
            self.handler.useLanguages[self.current_language]
            if self.current_language < len(self.handler.useLanguages) else -1
        )
        preferences = _AUDIO_LOCALE_PREFERENCES.get(language_code, ())
        locales = {
            self._sound_candidate_locale(candidate) for candidate in candidates
        }
        preferred = next(
            (locale for locale in preferences if locale in locales), ""
        )
        ordered = sorted(
            candidates,
            key=lambda candidate: (
                self._sound_candidate_locale(candidate) != preferred,
                candidate.source_id,
                candidate.path,
            ),
        )
        localized = tuple(
            candidate for candidate in ordered
            if self._sound_candidate_locale(candidate) == preferred
        ) if preferred else ()
        return localized or tuple(ordered)

    @staticmethod
    def _sound_candidate_locale(candidate) -> str:
        parts = candidate.path.rsplit("/", 1)[-1].casefold().split(".")
        return parts[-1] if len(parts) > 1 and parts[-2] in {"stm", "x64"} else ""

    def _sound_candidate_label(self, candidate) -> str:
        locale = self._sound_candidate_locale(candidate).upper()
        filename = candidate.path.rsplit("/", 1)[-1]
        interval = (
            self.tr(" · {start:.2f}–{end:.2f} s").format(
                start=candidate.start_ms / 1000,
                end=candidate.end_ms / 1000,
            )
            if candidate.is_segment else ""
        )
        copies = (
            self.tr(" (+{count} identical copies)").format(
                count=len(candidate.paths) - 1
            )
            if len(candidate.paths) > 1 else ""
        )
        prefix = f"{locale} — " if locale else ""
        return (
            f"{prefix}{self.tr('Source')} {candidate.source_id} — "
            f"{filename}{interval}{copies}"
        )

    def _play_sound(self, message_id, candidate) -> None:
        if self._sound_session is None:
            return
        executable = configured_vgmstream(self.handler)
        if not executable:
            QMessageBox.information(
                self,
                self.tr("VGMStream Required"),
                self.tr(
                    "Configure the VGMStream CLI path in Settings before previewing sounds."
                ),
            )
            return
        self._sound_session.play(
            message_id, candidate, executable, self.msg_waveform.width()
        )

    def _on_sound_preparing(self, candidate) -> None:
        self.msg_waveform.clear()
        self.msg_waveform.show()
        self.status_label.setText(
            self.tr("Loading source {id} from {file}…").format(
                id=candidate.source_id,
                file=candidate.path.rsplit("/", 1)[-1],
            )
        )
        self._refresh_sound_column()

    def _on_sound_started(self, candidate) -> None:
        self.status_label.setText(
            self.tr("Playing source {id} from {file}").format(
                id=candidate.source_id,
                file=candidate.path.rsplit("/", 1)[-1],
            )
        )
        self._refresh_sound_column()

    def _on_sound_stopped(self) -> None:
        self.msg_waveform.clear()
        self.msg_waveform.hide()
        self.status_label.setText(self._idle_status_text())
        self._refresh_sound_column()

    def _on_sound_waveform_ready(self, payload) -> None:
        if payload is not None:
            self.msg_waveform.set_data(payload["peaks"], payload["ranges"])

    def _on_sound_failed(self, message: str) -> None:
        self.msg_waveform.clear()
        self.msg_waveform.hide()
        self.status_label.setText(self._idle_status_text())
        self._refresh_sound_column()
        QMessageBox.warning(self, self.tr("Sound Decode Error"), message)

    def _on_sound_seek(self, per_mille: int) -> None:
        if self._sound_session is not None:
            self._sound_session.seek(per_mille)

    def _idle_status_text(self) -> str:
        return self.tr("Modified") if self.modified else self.tr("Ready")

    def cleanup(self) -> None:
        self._cleanup_sound()

    def closeEvent(self, event) -> None:
        self.cleanup()
        super().closeEvent(event)

    def rebuild(self) -> bytes:
        return self.handler.rebuild()
