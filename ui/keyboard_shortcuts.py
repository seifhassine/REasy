from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QWidget, QLabel, QLineEdit,
    QPushButton, QDialogButtonBox, QDialog, QMessageBox,
    QListWidget, QListWidgetItem
)
from PySide6.QtGui import QKeySequence
from settings import DEFAULT_SETTINGS

def create_shortcuts_tab():
    """Creates and returns a widget containing the keyboard shortcuts UI"""
    shortcuts_tab = QWidget()
    shortcuts_layout = QVBoxLayout(shortcuts_tab)
    
    shortcuts_info_label = QLabel("Configure keyboard shortcuts for common actions:")
    shortcuts_layout.addWidget(shortcuts_info_label)
    
    shortcuts_list = QListWidget()
    
    shortcut_names = {
        "file_open": "Open File",
        "file_save": "Save File",
        "file_save_as": "Save File As",
        "file_reload": "Reload File",
        "file_close_tab": "Close Tab",
        "edit_copy": "Copy",
        "find_search": "Find",
        "find_search_guid": "Search for GUID",
        "find_search_text": "Search for Text",
        "find_search_number": "Search for Number",
        "view_dark_mode": "Toggle Dark Mode",
        "view_prev_tab": "Previous Tab",
        "view_next_tab": "Next Tab",
        "view_debug_console": "Toggle Debug Console"
    }
    
    shortcuts_tab.shortcut_names = shortcut_names
    shortcuts_tab.shortcuts_list = shortcuts_list
    
    # Edit button
    edit_shortcut_layout = QHBoxLayout()
    edit_shortcut_btn = QPushButton("Edit Selected Shortcut")
    reset_shortcut_btn = QPushButton("Reset to Default")
    edit_shortcut_layout.addWidget(edit_shortcut_btn)
    edit_shortcut_layout.addWidget(reset_shortcut_btn)
    shortcuts_layout.addLayout(edit_shortcut_layout)
    
    shortcuts_layout.addWidget(shortcuts_list)
    
    def populate_shortcuts_list(shortcuts):
        """Populates the shortcuts list with the current shortcuts"""
        shortcuts_list.clear()
        for key, name in shortcut_names.items():
            shortcut = shortcuts.get(key, DEFAULT_SETTINGS["keyboard_shortcuts"].get(key, ""))
            item = QListWidgetItem(f"{name}: {shortcut}")
            item.setData(Qt.UserRole, key)  
            shortcuts_list.addItem(item)
    
    def edit_shortcut(shortcuts_dict, parent_dialog):
        """Opens a dialog to edit the selected shortcut"""
        current_item = shortcuts_list.currentItem()
        if not current_item:
            QMessageBox.warning(parent_dialog, "No Selection", "Please select a shortcut to edit.")
            return
            
        shortcut_key = current_item.data(Qt.UserRole)
        current_shortcut = shortcuts_dict.get(shortcut_key, "")
        name = shortcut_names.get(shortcut_key, shortcut_key)
        
        shortcut_dialog = QDialog(parent_dialog)
        shortcut_dialog.setWindowTitle(f"Edit Shortcut: {name}")
        shortcut_dialog.setMinimumWidth(300)
        
        shortcut_layout = QVBoxLayout(shortcut_dialog)
        
        instruction_label = QLabel(f"Press the key combination for '{name}':")
        shortcut_layout.addWidget(instruction_label)
        
        shortcut_input = QLineEdit(current_shortcut)
        shortcut_input.setReadOnly(True) 
        shortcut_layout.addWidget(shortcut_input)
    
        clear_btn = QPushButton("Clear")
        shortcut_layout.addWidget(clear_btn)
        
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
                
            conflict_key = None
            conflict_name = None
            
            for k, v in shortcuts_dict.items():
                if v == new_shortcut and k != shortcut_key:
                    conflict_key = k
                    conflict_name = shortcut_names.get(k, k)
                    break
                    
            if conflict_key:
                msg = QMessageBox(
                    QMessageBox.Warning,
                    "Shortcut Conflict", 
                    f"The shortcut '{new_shortcut}' is already assigned to '{conflict_name}'.",
                    QMessageBox.Ok,
                    shortcut_dialog
                )
                msg.exec()
                return
                
            shortcuts_dict[shortcut_key] = new_shortcut
            current_item.setText(f"{name}: {new_shortcut}")
            shortcut_dialog.accept()
        
        shortcut_dialog.exec()
    
    def reset_shortcut(shortcuts_dict, parent_dialog):
        """Resets the selected shortcut to its default value"""
        current_item = shortcuts_list.currentItem()
        if not current_item:
            QMessageBox.warning(parent_dialog, "No Selection", "Please select a shortcut to reset.")
            return
            
        shortcut_key = current_item.data(Qt.UserRole)
        default_shortcut = DEFAULT_SETTINGS["keyboard_shortcuts"].get(shortcut_key, "")
        name = shortcut_names.get(shortcut_key, shortcut_key)
        
        conflict_key = None
        conflict_name = None
        
        for k, v in shortcuts_dict.items():
            if v == default_shortcut and k != shortcut_key:
                conflict_key = k
                conflict_name = shortcut_names.get(k, k)
                break
                
        if conflict_key:
            msg = QMessageBox(
                QMessageBox.Warning,
                "Shortcut Conflict", 
                f"The default shortcut '{default_shortcut}' is already assigned to '{conflict_name}'.",
                QMessageBox.Ok,
                parent_dialog
            )
            msg.exec()
            return
        
        shortcuts_dict[shortcut_key] = default_shortcut
        current_item.setText(f"{name}: {default_shortcut}")
    
    shortcuts_tab.populate_shortcuts_list = populate_shortcuts_list
    shortcuts_tab.edit_shortcut = edit_shortcut
    shortcuts_tab.reset_shortcut = reset_shortcut

    edit_shortcut_btn.clicked.connect(lambda: None)
    reset_shortcut_btn.clicked.connect(lambda: None)
    
    shortcuts_tab.edit_shortcut_btn = edit_shortcut_btn
    shortcuts_tab.reset_shortcut_btn = reset_shortcut_btn
    
    return shortcuts_tab
