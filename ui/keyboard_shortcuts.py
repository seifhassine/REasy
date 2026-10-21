lazy from PySide6.QtCore import QCoreApplication, Qt
lazy from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QLineEdit,
    QPushButton, QDialogButtonBox, QDialog, QMessageBox,
    QTreeWidget, QTreeWidgetItem, QHeaderView, QSizePolicy
)
lazy from PySide6.QtGui import QBrush, QColor, QKeySequence
lazy from settings import DEFAULT_SETTINGS
lazy from ui.editor_widgets import search_field, styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role

def create_shortcuts_tab():
    """Creates and returns a widget containing the keyboard shortcuts UI"""
    shortcuts_tab = QWidget()
    shortcuts_layout = QVBoxLayout(shortcuts_tab)
    shortcuts_layout.setContentsMargins(0, 8, 0, 0)
    shortcuts_layout.setSpacing(6)

    shortcuts_info_label = styled_label(QCoreApplication.translate(
        "KeyboardShortcuts", "Configure keyboard shortcuts for common actions:"
    ), tone="muted")
    shortcuts_layout.addWidget(shortcuts_info_label)

    filter_entry = search_field(QLineEdit())
    filter_entry.setPlaceholderText(QCoreApplication.translate("KeyboardShortcuts", "Filter actions"))
    shortcuts_layout.addWidget(filter_entry)

    shortcuts_list = QTreeWidget()
    shortcuts_list.setColumnCount(2)
    shortcuts_list.setHeaderLabels([
        QCoreApplication.translate("KeyboardShortcuts", "Action"),
        QCoreApplication.translate("KeyboardShortcuts", "Shortcut"),
    ])
    shortcuts_list.setRootIsDecorated(False)
    shortcuts_list.setUniformRowHeights(True)
    shortcuts_list.setAlternatingRowColors(True)
    shortcuts_list.setStyleSheet("QTreeView::item { padding: 3px 4px; }")
    shortcuts_list.headerItem().setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
    shortcuts_list.header().setSectionResizeMode(0, QHeaderView.Stretch)
    shortcuts_list.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
    shortcuts_list.header().setStretchLastSection(False)
    shortcuts_list.setToolTip(QCoreApplication.translate(
        "KeyboardShortcuts", "Double-click an action to change its shortcut."
    ))

    shortcut_names = {
        "file_open": QCoreApplication.translate("KeyboardShortcuts", "Open File"),
        "file_save": QCoreApplication.translate("KeyboardShortcuts", "Save File"),
        "file_save_all": QCoreApplication.translate(
            "KeyboardShortcuts", "Save All Modified Files"
        ),
        "file_save_as": QCoreApplication.translate("KeyboardShortcuts", "Save File As"),
        "file_reload": QCoreApplication.translate("KeyboardShortcuts", "Reload File"),
        "file_close_tab": QCoreApplication.translate("KeyboardShortcuts", "Close Tab"),
        "file_reopen_closed": QCoreApplication.translate("KeyboardShortcuts", "Reopen Last Closed File"),
        "find_search": QCoreApplication.translate("KeyboardShortcuts", "Find"),
        "find_search_guid": QCoreApplication.translate("KeyboardShortcuts", "Search for GUID"),
        "find_search_text": QCoreApplication.translate("KeyboardShortcuts", "Search for Text"),
        "find_search_number": QCoreApplication.translate("KeyboardShortcuts", "Search for Number"),
        "find_search_hex": QCoreApplication.translate("KeyboardShortcuts", "Search for Hex"),
        "find_rsz_field_value": QCoreApplication.translate("KeyboardShortcuts", "Find/Replace RSZ Field Value"),
        "view_prev_tab": QCoreApplication.translate("KeyboardShortcuts", "Previous Tab"),
        "view_next_tab": QCoreApplication.translate("KeyboardShortcuts", "Next Tab"),
        "view_debug_console": QCoreApplication.translate("KeyboardShortcuts", "Toggle Output"),
        "view_ai_chat": QCoreApplication.translate("KeyboardShortcuts", "Toggle AI Assistant"),
        "editor_split_right": QCoreApplication.translate("KeyboardShortcuts", "Split Editor Right"),
        "editor_split_down": QCoreApplication.translate("KeyboardShortcuts", "Split Editor Down"),
    }

    shortcuts_tab.shortcut_names = shortcut_names
    shortcuts_tab.shortcuts_list = shortcuts_list
    shortcuts_layout.addWidget(shortcuts_list, 1)

    edit_shortcut_layout = QHBoxLayout()
    edit_shortcut_layout.setSpacing(6)
    edit_shortcut_btn = QPushButton(QCoreApplication.translate(
        "KeyboardShortcuts", "Edit Selected Shortcut"
    ))
    edit_shortcut_btn.setIcon(line_icon("edit"))
    reset_shortcut_btn = QPushButton(QCoreApplication.translate(
        "KeyboardShortcuts", "Reset to Default"
    ))
    reset_shortcut_btn.setIcon(line_icon("reset"))
    edit_shortcut_layout.addStretch()
    edit_shortcut_layout.addWidget(edit_shortcut_btn)
    edit_shortcut_layout.addWidget(reset_shortcut_btn)
    shortcuts_layout.addLayout(edit_shortcut_layout)

    def set_item_shortcut(item, shortcut):
        if shortcut:
            item.setText(1, shortcut)
            item.setData(1, Qt.ForegroundRole, None)
        else:
            item.setText(1, QCoreApplication.translate("KeyboardShortcuts", "Unassigned"))
            item.setForeground(1, QBrush(QColor(current_colors()["text_subtle"])))

    def apply_filter(text):
        needle = text.strip().lower()
        for i in range(shortcuts_list.topLevelItemCount()):
            item = shortcuts_list.topLevelItem(i)
            item.setHidden(bool(needle) and needle not in item.text(0).lower())
        current = shortcuts_list.currentItem()
        if current is not None and current.isHidden():
            shortcuts_list.setCurrentItem(None)

    filter_entry.textChanged.connect(apply_filter)

    def selected_shortcut_item(parent_dialog, missing_message):
        current_item = shortcuts_list.currentItem()
        if current_item is None:
            QMessageBox.warning(
                parent_dialog,
                QCoreApplication.translate("KeyboardShortcuts", "No Selection"),
                missing_message,
            )
        return current_item

    def find_shortcut_conflict(shortcuts_dict, shortcut_key, shortcut):
        for key, value in shortcuts_dict.items():
            if value == shortcut and key != shortcut_key:
                return key, shortcut_names.get(key, key)
        return None, None

    def show_shortcut_conflict(parent_dialog, message):
        msg = QMessageBox(
            QMessageBox.Warning,
            QCoreApplication.translate("KeyboardShortcuts", "Shortcut Conflict"),
            message,
            QMessageBox.Ok,
            parent_dialog,
        )
        msg.exec()

    def populate_shortcuts_list(shortcuts):
        """Populates the shortcuts list with the current shortcuts"""
        shortcuts_list.clear()
        mono = shortcuts_list.font()
        mono.setFamily(current_colors()["mono_font"])
        for key, name in shortcut_names.items():
            shortcut = shortcuts.get(key, DEFAULT_SETTINGS["keyboard_shortcuts"].get(key, ""))
            item = QTreeWidgetItem([name, ""])
            item.setData(0, Qt.UserRole, key)
            item.setFont(1, mono)
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            set_item_shortcut(item, shortcut)
            shortcuts_list.addTopLevelItem(item)
        apply_filter(filter_entry.text())

    def edit_shortcut(shortcuts_dict, parent_dialog):
        """Opens a dialog to edit the selected shortcut"""
        current_item = selected_shortcut_item(
            parent_dialog,
            QCoreApplication.translate(
                "KeyboardShortcuts", "Please select a shortcut to edit."
            ),
        )
        if current_item is None:
            return

        shortcut_key = current_item.data(0, Qt.UserRole)
        current_shortcut = shortcuts_dict.get(shortcut_key, "")
        name = shortcut_names.get(shortcut_key, shortcut_key)

        shortcut_dialog = QDialog(parent_dialog)
        shortcut_dialog.setWindowTitle(QCoreApplication.translate(
            "KeyboardShortcuts", "Edit Shortcut: {name}"
        ).format(name=name))
        shortcut_dialog.setMinimumWidth(360)

        shortcut_layout = QVBoxLayout(shortcut_dialog)
        shortcut_layout.setContentsMargins(12, 12, 12, 12)
        shortcut_layout.setSpacing(8)

        instruction_label = QLabel(QCoreApplication.translate(
            "KeyboardShortcuts", "Press the key combination for '{name}':"
        ).format(name=name))
        shortcut_layout.addWidget(instruction_label)

        shortcut_input = QLineEdit(current_shortcut)
        shortcut_input.setReadOnly(True)
        shortcut_input.setAlignment(Qt.AlignCenter)
        shortcut_input.setPlaceholderText(QCoreApplication.translate(
            "KeyboardShortcuts", "Press a key combination"
        ))
        set_style_role(shortcut_input, mono=True)
        colors = current_colors()
        # Drawn as an armed capture field: it takes keys while the dialog is open.
        shortcut_input.setStyleSheet(
            f"QLineEdit {{ padding: 4px; background-color: {colors['input_bg']};"
            f" border-color: {colors['accent_border']}; }}"
        )
        clear_btn = QPushButton(QCoreApplication.translate("KeyboardShortcuts", "Clear"))
        clear_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        set_style_role(clear_btn, compact=True)
        input_row = QHBoxLayout()
        input_row.setSpacing(4)
        input_row.addWidget(shortcut_input, 1)
        input_row.addWidget(clear_btn)
        shortcut_layout.addLayout(input_row)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        shortcut_layout.addWidget(button_box)

        captured_shortcut = [current_shortcut]

        def keyPressEvent(event):
            modifiers = []
            if event.modifiers() & Qt.ControlModifier:
                modifiers.append("Ctrl")
            if event.modifiers() & Qt.ShiftModifier:
                modifiers.append("Shift")
            if event.modifiers() & Qt.AltModifier:
                modifiers.append("Alt")

            key = QKeySequence(event.key()).toString()

            if key in ("Ctrl", "Shift", "Alt"):
                return

            if modifiers:
                shortcut_text = "+".join(modifiers) + "+" + key
            else:
                shortcut_text = key

            shortcut_input.setText(shortcut_text)
            captured_shortcut[0] = shortcut_text

        def clear_shortcut():
            shortcut_input.setText("")
            captured_shortcut[0] = ""

        shortcut_dialog.keyPressEvent = keyPressEvent
        clear_btn.clicked.connect(clear_shortcut)

        button_box.accepted.connect(lambda: check_conflicts_and_accept())
        button_box.rejected.connect(shortcut_dialog.reject)

        def check_conflicts_and_accept():
            new_shortcut = captured_shortcut[0]

            if not new_shortcut or new_shortcut == current_shortcut:
                shortcut_dialog.accept()
                return

            conflict_key, conflict_name = find_shortcut_conflict(
                shortcuts_dict, shortcut_key, new_shortcut
            )

            if conflict_key:
                show_shortcut_conflict(
                    shortcut_dialog,
                    QCoreApplication.translate(
                        "KeyboardShortcuts",
                        "The shortcut '{shortcut}' is already assigned to '{name}'.",
                    ).format(shortcut=new_shortcut, name=conflict_name),
                )
                return

            shortcuts_dict[shortcut_key] = new_shortcut
            set_item_shortcut(current_item, new_shortcut)
            shortcut_dialog.accept()

        shortcut_dialog.exec()

    def reset_shortcut(shortcuts_dict, parent_dialog):
        """Resets the selected shortcut to its default value"""
        current_item = selected_shortcut_item(
            parent_dialog,
            QCoreApplication.translate(
                "KeyboardShortcuts", "Please select a shortcut to reset."
            ),
        )
        if current_item is None:
            return

        shortcut_key = current_item.data(0, Qt.UserRole)
        default_shortcut = DEFAULT_SETTINGS["keyboard_shortcuts"].get(shortcut_key, "")

        conflict_key, conflict_name = find_shortcut_conflict(
            shortcuts_dict, shortcut_key, default_shortcut
        )

        if conflict_key:
            show_shortcut_conflict(
                parent_dialog,
                QCoreApplication.translate(
                    "KeyboardShortcuts",
                    "The default shortcut '{shortcut}' is already assigned to '{name}'.",
                ).format(shortcut=default_shortcut, name=conflict_name),
            )
            return

        shortcuts_dict[shortcut_key] = default_shortcut
        set_item_shortcut(current_item, default_shortcut)

    shortcuts_tab.populate_shortcuts_list = populate_shortcuts_list
    shortcuts_tab.edit_shortcut = edit_shortcut
    shortcuts_tab.reset_shortcut = reset_shortcut

    shortcuts_tab.edit_shortcut_btn = edit_shortcut_btn
    shortcuts_tab.reset_shortcut_btn = reset_shortcut_btn

    return shortcuts_tab
