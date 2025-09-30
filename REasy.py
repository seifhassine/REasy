#!/usr/bin/env python3

import os
import sys
import uuid
import struct
import weakref
import datetime
import re
from pathlib import Path
import PySide6
import subprocess

from file_handlers.factory import get_handler_for_data 
from file_handlers.msg.msg_handler import MsgHandler
from file_handlers.rsz.rsz_handler import RszHandler  
from file_handlers.mdf.mdf_handler import MdfHandler  

from ui.better_find_dialog import BetterFindDialog
from ui.guid_converter import create_guid_converter_dialog
from ui.about_dialog import create_about_dialog
from ui.keyboard_shortcuts import create_shortcuts_tab
from ui.outdated_files_dialog import OutdatedFilesDialog
from ui.update_notification import UpdateNotificationManager
from ui.rsz_differ_dialog import RszDifferDialog
from settings import DEFAULT_SETTINGS, load_settings, save_settings
from ui.changelog_dialog import ChangelogDialog

from PySide6.QtCore import (
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QIcon,
    QAction,
    QStandardItemModel,
    QStandardItem,
    QKeySequence,
    QDesktopServices,
    QColor,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QComboBox,
    QSizePolicy,
    QTabWidget,
    QTreeView,
    QMessageBox,
    QFileDialog,
    QInputDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QCheckBox,
    QPushButton,
    QDialog,
    QStatusBar,
    QDialogButtonBox,
    QAbstractItemView,
    QStyleFactory,
    QListWidget,
    QListWidgetItem,
    QColorDialog,
)

from i18n.language_manager import LanguageManager

from ui.console_logger import ConsoleWidget, ConsoleRedirector
from ui.detachable_tabs import CustomNotebook, FloatingTabWindow
from ui.directory_search import search_directory_for_type
from tools.hash_calculator import HashCalculator

from utils.native_build import ensure_fast_pakresolve, ensure_fastmesh
fast_pakresolve = ensure_fast_pakresolve()
fastmesh = ensure_fastmesh()

from ui.pak_browser_dialog import PakBrowserDialog  # noqa: E402
from ui.project_manager.source_dialog import SelectSourceDialog  # noqa: E402
from ui.project_manager import ProjectManager, PROJECTS_ROOT, ensure_projects_root  # noqa: E402

CURRENT_VERSION = "0.5.5"
GAMES = [
    "RE4", "RE2", "RE2RT", "RE8", "RE3", "RE3RT", "REResistance",
    "RE7", "RE7RT", "MHWilds", "MHRise", "DMC5", "SF6", "O2", "DD2"
]
NO_FILE_LOADED_STR = "No file loaded"
UNSAVED_CHANGES_STR = "Unsaved changes"
DEFAULT_THEME_COLOR = "#ff851b"

def resource_path(relative_path):
    base_path = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(base_path, relative_path)
    if not os.path.exists(full_path):
        full_path = os.path.join(os.getcwd(), relative_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Could not find resource: {relative_path}")

    return full_path


def set_app_icon(window):
    try:
        icon_path = resource_path("resources/icons/reasy_editor_logo.ico")
        window.setWindowIcon(QIcon(icon_path))
    except IOError as e:
        print("Failed to set window icon:", e)


def create_standard_dialog(parent, title, geometry=None):
    dialog = QDialog(parent)
    dialog.setWindowTitle(title)
    if geometry:
        try:
            w_h = geometry.lower().split("x")
            if len(w_h) == 2:
                w, h = int(w_h[0]), int(w_h[1])
                dialog.resize(w, h)
        except Exception as e:
            print(f"Error setting dialog geometry: {e}")
    bg_color = dialog.palette().color(dialog.backgroundRole()).name()
    return dialog, bg_color


def create_integer_search_dialog(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Integer Search")
    layout = QVBoxLayout(dialog)
    
    type_label = QLabel("Select integer type:")
    layout.addWidget(type_label)
    
    integer_types = {
        0: ("int32", -2147483648, 2147483647),
        1: ("uint32", 0, 4294967295),
        2: ("int64", -9223372036854775808, 9223372036854775807),
        3: ("uint64", 0, 18446744073709551615)
    }
    
    type_combo = QComboBox()
    type_combo.addItems(["int32 (signed 32-bit)", "uint32 (unsigned 32-bit)", 
                        "int64 (signed 64-bit)", "uint64 (unsigned 64-bit)"])
    layout.addWidget(type_combo)
    
    value_label = QLabel("Enter value:")
    layout.addWidget(value_label)
    
    value_input = QLineEdit()
    layout.addWidget(value_input)
    
    def update_limits():
        _, min_val, max_val = integer_types[type_combo.currentIndex()]
        value_label.setText(f"Enter value ({min_val} to {max_val}):")
    
    type_combo.currentIndexChanged.connect(update_limits)
    update_limits()
    
    button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
    button_box.accepted.connect(dialog.accept)
    button_box.rejected.connect(dialog.reject)
    layout.addWidget(button_box)
    
    result = dialog.exec()
    
    if result == QDialog.Accepted:
        int_type, min_val, max_val = integer_types[type_combo.currentIndex()]
        
        try:
            value = int(value_input.text())
            if not (min_val <= value <= max_val):
                raise ValueError(f"Value out of range for {int_type}")
            
            return (int_type, value), True
        except ValueError as e:
            QMessageBox.critical(parent, "Invalid Input", str(e))
            return None, False
    
    return None, False

def create_search_dialog(parent, search_type):
    if search_type == 'number':
        return create_integer_search_dialog(parent)
    
    prompts = {
        'text': ('Text Search', 'Enter text to search (UTF-16LE):'),
        'guid': ('GUID Search', 'Enter GUID (standard format):'),
        'hex': ('Hex Search', 'Enter hexadecimal bytes (e.g., FF A9 00 3D or FFA9003D):')
    }
    
    title, msg = prompts[search_type]
    val, ok = QInputDialog.getText(parent, title, msg)
    
    return (val, ok) if ok else (None, False)

def create_search_patterns(search_type, value):
    if search_type == 'number':
        try:
            int_type, actual_value = value
            
            format_map = {
                'int32': 'i',
                'uint32': 'I',
                'int64': 'q',
                'uint64': 'Q'
            }
            
            format_char = format_map[int_type]
            le_bytes = struct.pack('<' + format_char, actual_value)
            be_bytes = struct.pack('>' + format_char, actual_value)
            
            patterns = [le_bytes]
            if le_bytes != be_bytes:
                patterns.append(be_bytes)
            
            return patterns
        except Exception as e:
            raise ValueError(f"Could not convert number: {e}")
    elif search_type == 'text':
        p1 = value.encode('utf-16le')
        p2 = p1 + b'\x00\x00'
        return [p1, p2]
    elif search_type == 'guid':
        try:
            gobj = uuid.UUID(value.strip())
            return [gobj.bytes_le, gobj.bytes, gobj.bytes_le.hex().encode('utf-8')]
        except Exception as e:
            raise ValueError(f"Invalid GUID: {value}\n{e}")
    elif search_type == 'hex':
        try:
            from ui.directory_search import validate_hex_string, hex_string_to_bytes
            if isinstance(value, tuple) and len(value) == 2:
                hex_text, reverse_bytes = value
                hex_str = validate_hex_string(hex_text)
                
                pattern = hex_string_to_bytes(hex_str, reverse_bytes)
                print(f"Searching for hex pattern: {pattern.hex().upper()} (reversed={reverse_bytes})")
                return [pattern]
            else:
                hex_str = validate_hex_string(value)
                return [hex_string_to_bytes(hex_str)]
                
        except Exception as e:
            raise ValueError(f"Invalid hex value: {str(e)}")

class FileTab:


    def __init__(self, parent_notebook, filename=None, data=None, app=None):
        self.parent_notebook = parent_notebook
        self.notebook_widget = QWidget()
        self.notebook_widget.parent_tab = self
        self.filename = filename
        self.handler = None
        self.metadata_map = {}
        self.search_results = []
        self.current_result_index = 0
        self.modified = False
        self.app = app
        self.viewer = None 
        self.original_data = data 

        self.status_label = QLabel(NO_FILE_LOADED_STR)

        layout = QVBoxLayout(self.notebook_widget)
        layout.setContentsMargins(0, 0, 0, 0) 
        layout.setSpacing(0) 

        self.tree = QTreeView()
        self.tree.setHeaderHidden(False)
        self.tree.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setAlternatingRowColors(False)
        self.tree.setContentsMargins(0, 0, 0, 0) 
        self.tree.setStyleSheet("""
            QTreeView {
                border: none;
                margin: 0px;
                padding: 0px;
            }
        """)
        
        self.tree.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.tree.setExpandsOnDoubleClick(False)
        layout.addWidget(self.tree)

        layout.addWidget(self.status_label)

        self.tree.doubleClicked.connect(
            self.on_double_click
        )
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)

        self.dark_mode = self.app.dark_mode if self.app else False
        self.search_dialog = None
        self.result_list = None
        self.initial_load_complete = False

        if data:
            self.initial_load_complete = self.load_file(filename, data)
            if not self.app.dark_mode:
                self.tree.setStyleSheet(
                    """
                    QTreeView {
                        background-color: white;
                        color: black;
                    }
                    QTreeView::item {
                        background-color: white;
                        color: black;
                        padding: 2px;
                    }
                """
                )

        self.search_state = {
            "dialog": None,
            "entry": None,
            "case_box": None,
            "result_list": None,
            "results": [],
            "current_index": 0,
        }

        self._search_widgets = {
            "dialog": None,
            "entry": None,
            "case_box": None,
            "result_list": None,
            "tree_ref": None,
        }

    def _invalidate_search_dialog_tree(self):
        dialogs = []
        if hasattr(self, "_find_dialog") and self._find_dialog:
            dialogs.append(self._find_dialog)

        shared_dialog = getattr(self.app, "_shared_find_dialog", None) if self.app else None
        if shared_dialog and getattr(shared_dialog, "file_tab", None) is self:
            dialogs.append(shared_dialog)

        for dialog in dialogs:
            try:
                invalidate = getattr(dialog, "invalidate_cached_tree", None)
                if callable(invalidate):
                    invalidate()
                else:
                    setattr(dialog, "_tree_for_tab", None)
            except RuntimeError:
                pass

    def update_tab_title(self):
        if not self.filename:
            base_title = "Untitled"
        else:
            base_title = os.path.basename(self.filename)

        if self.modified:
            title = f"{base_title} *"
        else:
            title = base_title

        index = self.parent_notebook.indexOf(self.notebook_widget)
        if index != -1:
            self.parent_notebook.setTabText(index, title)

    def _setup_viewer(self, layout):
        if not self.viewer:
            return False
            
        self.viewer.modified_changed.connect(self._on_viewer_modified)
        
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
                
        layout.addWidget(self.viewer)
        layout.addWidget(self.status_label)
        return True

    def _cleanup_layout(self, layout):
        """Clean up layout but preserve the tree widget"""
        if not layout:
            return
            
        tree_widget = None
        for i in range(layout.count()):
            item = layout.itemAt(i)
            if item.widget() == self.tree:
                tree_widget = self.tree
                break
                
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget and widget != tree_widget:
                widget.setParent(None)
                widget.deleteLater()
                
        return tree_widget

    def _prepare_handler(self, data):
        try:
            handler = get_handler_for_data(data)
            if not handler:
                raise ValueError("No handler found for this file type")
            
            if isinstance(handler, RszHandler):
                handler.set_game_version(self.app.settings.get("game_version", "RE4"))
                handler.show_advanced = self.app.settings.get("show_rsz_advanced", True)
                handler.confirmation_prompt = self.app.settings.get("confirmation_prompt", True)
                handler.filepath = self.filename or ""
            if isinstance(handler, MdfHandler):
                handler.filepath = self.filename or ""
            
            handler.refresh_tree_callback = self.refresh_tree
            handler.app = self.app
            
            if hasattr(handler, "setup_tree"):
                handler.setup_tree(self.tree)
                
            return handler
            
        except Exception as e:
            raise ValueError(f"Handler setup failed: {e}")

    def load_file(self, filename, data):
        layout = self.notebook_widget.layout()
        
        try:
            old_handler = self.handler
            old_viewer = self.viewer
            old_status_text = self.status_label.text() if hasattr(self, "status_label") else NO_FILE_LOADED_STR
            
            self.filename = filename
            self.original_data = data
            self.handler = None
            self.viewer = None

            self.handler = self._prepare_handler(data)
            if not self.handler:
                raise ValueError("Handler initialization failed")

            try:
                self.handler.read(data)
            except Exception as e:
                raise ValueError(f"Failed to read file data: {e}")
                
            preserved_tree = self._cleanup_layout(layout)
            if preserved_tree:
                self.tree = preserved_tree
                
            try:
                self.viewer = self.handler.create_viewer()
                if self.viewer:
                    self.status_label = QLabel(f"Loaded: {filename}")
                    layout.addWidget(self.viewer)
                    layout.addWidget(self.status_label)
                    self.viewer.modified_changed.connect(self._on_viewer_modified)
                else:
                    self.status_label = QLabel(f"Loaded: {filename}")
                    layout.addWidget(self.tree)
                    layout.addWidget(self.status_label)
                    if not isinstance(self.handler, RszHandler):
                        self.refresh_tree()
            except Exception as e:
                print(f"Viewer creation failed: {e}")
                self.viewer = None
                self.status_label = QLabel(f"Loaded: {filename}")
                layout.addWidget(self.tree)
                layout.addWidget(self.status_label)
                self.refresh_tree()

            self.initial_load_complete = True
            return True

        except Exception as e:
            self.initial_load_complete = False
            self.handler = old_handler
            self.viewer = old_viewer
            
            # Clean up layout and recreate it
            self._cleanup_layout(layout)
            
            if old_viewer:
                layout.addWidget(old_viewer)
            else:
                layout.addWidget(self.tree)
                
            # Create a new status label
            self.status_label = QLabel(old_status_text)
            layout.addWidget(self.status_label)
            
            QMessageBox.critical(None, "Error", f"Failed to load file: {e}")
            return False

    def refresh_tree(self):
        if not self.handler:
            self.tree.setModel(None)
            return

        old_model = self.tree.model()
        self.tree.setUpdatesEnabled(False)

        try:
            if hasattr(self.handler, "populate_treeview"):
                self.handler.populate_treeview(self.tree, None, self.metadata_map)
            else:
                self._populate_tree_from_handler()

            model = self.tree.model()
            if model:
                if hasattr(self, '_connected_model') and self._connected_model and self._connected_model != model:
                    try:
                        self._connected_model.dataChanged.disconnect(self.on_tree_edit)
                    except (RuntimeError, TypeError):
                        pass
                        
                if not hasattr(self, '_connected_model') or self._connected_model != model:
                    model.dataChanged.connect(self.on_tree_edit)
                    self._connected_model = model
                    
        except Exception as e:
            print(f"Tree refresh failed: {e}")
            self.tree.setModel(old_model)
            
        finally:
            self.tree.setUpdatesEnabled(True)

    def _on_viewer_modified(self, modified):
        self.modified = modified
        self.update_tab_title()

    def _populate_tree_from_handler(self):
        if hasattr(self.handler, "get_tree_data"):
            try:
                items = self.handler.get_tree_data()
                model = QStandardItemModel()
                for item in items:
                    tree_item = QStandardItem(str(item.get("name", "")))
                    tree_item.setData(str(item.get("value", "")), Qt.UserRole)
                    model.appendRow(tree_item)
                self.tree.setModel(model)
            except Exception as e:
                print(f"Error populating tree: {e}")

    def on_double_click(self, index):
        if not self.tree:
            return
        if not index.isValid():
            return

        item = index.internalPointer()
        if not item or not isinstance(item.raw, dict):
            return

        old_val = item.data[1] if len(item.data) > 1 else ""

        meta = item.raw.get("meta")
        if not meta:
            return

        new_val, ok = QInputDialog.getText(
            self.tree, "Edit Value", "Value:", text=str(old_val)
        )

        if ok and new_val != old_val and self.handler:
            try:
                if self.handler.validate_edit(meta, new_val, old_val):
                    self.handler.handle_edit(meta, new_val, old_val, None, self.tree)
                    self.modified = True
                    self.update_tab_title()
            except Exception as e:
                QMessageBox.critical(None, "Error", f"Failed to update value: {e}")

    def on_tree_edit(self, top_left, bottom_right, roles):
        if not self.handler or not self.handler.supports_editing():
            return

        if Qt.UserRole not in roles:
            return

        index = top_left
        item = self.tree.model().itemFromIndex(index)
        meta = self.metadata_map.get(item)
        if not meta:
            return

        new_val = item.data(Qt.UserRole)
        try:
            if self.handler.validate_edit(meta, new_val):
                self.modified = True
                self.update_tab_title()
            else:
                item.setData(str(meta.get("original_value", "")), Qt.UserRole)
        except Exception as e:
            QMessageBox.critical(None, "Error", f"Invalid value: {e}")
            item.setData(str(meta.get("original_value", "")), Qt.UserRole)

    def handle_file_save(self, file_path):
        try:
            data = None
            if hasattr(self.handler, "rebuild"):
                data = self.handler.rebuild()
            elif self.viewer and hasattr(self.viewer, "rebuild"):
                data = self.viewer.rebuild()
            
            if not data:
                raise ValueError("No rebuild method available")

            if self.app and self.app.settings.get("backup_on_save", True):
                self.create_backup(file_path, data)

            with open(file_path, "wb") as f:
                f.write(data)
                
            self.filename = file_path
            self.original_data = data
            self.modified = False
            self.update_tab_title()
            
            if self.app and hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage(f"Saved: {file_path}", 3000)
                
            return True
            
        except Exception as e:
            QMessageBox.critical(None, "Save Error", str(e))
            return False

    def create_backup(self, file_path, data):
        try:
            backups_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
            os.makedirs(backups_dir, exist_ok=True)
            
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.basename(file_path)
            backup_path = os.path.join(backups_dir, f"{timestamp}_{filename}")
            
            with open(backup_path, "wb") as f:
                f.write(data)
                
            if hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage(f"Backup created: {backup_path}", 2000)
                
        except Exception as e:
            print(f"Backup creation failed: {e}")

    def direct_save(self):
        """Save directly to the current file without prompting"""
        if not self.handler:
            QMessageBox.critical(None, "Error", NO_FILE_LOADED_STR)
            return False
        if not self.filename:
            return self.on_save()
        return self.handle_file_save(self.filename)
        
    def on_save(self):
        if not self.handler:
            QMessageBox.critical(None, "Error", NO_FILE_LOADED_STR)
            return False

        file_path, _ = QFileDialog.getSaveFileName(
            self.notebook_widget,
            "Save File As",
            self.filename or "",
            "All Files (*.*)",
        )
        
        if file_path:
            return self.handle_file_save(file_path)
        return False

    def reload_file(self):
        if not self.filename:
            QMessageBox.critical(None, "Error", "No file currently loaded.")
            return

        if self.modified:
            ans = QMessageBox.question(
                None,
                UNSAVED_CHANGES_STR,
                f"File {os.path.basename(self.filename)} has unsaved changes.\nSave before reloading?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                self.on_save()
                
        self._release_resources_before_reload()

        try:
            with open(self.filename, "rb") as f:
                data = f.read()

            success = self.load_file(self.filename, data)
            if success and self.app and hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage(f"Reloaded: {self.filename}", 2000)
                
            self.modified = False
            self.viewer.modified = False
            self.update_tab_title()


        except Exception as e:
            QMessageBox.critical(None, "Error", f"Failed to reload file: {e}")
            import traceback

            traceback.print_exc()

    def open_find_dialog(self):
        if isinstance(self.handler, MsgHandler):
                QMessageBox.information(self.notebook_widget, "Search in MSG", "MSG files have a built-in search at the top of the editor. Please use that search bar.")
                return
        parent_window = self.notebook_widget.window()
        if isinstance(parent_window, QMainWindow) and parent_window.__class__.__name__ == 'FloatingTabWindow':
            if hasattr(self, "_find_dialog") and self._find_dialog:
                try:
                    if self._find_dialog.isVisible():
                        if not self._find_dialog.isFloating():
                            self._find_dialog.raise_()
                            self._find_dialog.activateWindow()
                            return
                        return
                    else:
                        self._find_dialog.close()
                except RuntimeError:
                    pass
            self._find_dialog = BetterFindDialog(self, parent=parent_window, shared_mode=False)
            if self.app and hasattr(self.app, 'dark_mode'):
                self._find_dialog.set_dark_mode(self.app.dark_mode)
            self._find_dialog.show()
        else:
            if self.app:
                self.app.open_find_dialog()

    def find_matching_backups(self):
        """Find all backup files that match the current filename"""
        if not self.filename:
            return []
            
        backups_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "backups")
        if not os.path.exists(backups_dir):
            return []
            
        base_filename = os.path.basename(self.filename)
        backup_pattern = r"(\d{8}_\d{6})_" + re.escape(base_filename) + "$"
        
        result = []
        for filename in os.listdir(backups_dir):
            match = re.match(backup_pattern, filename)
            if match:
                timestamp = match.group(1)
                try:
                    dt = datetime.datetime.strptime(timestamp, "%Y%m%d_%H%M%S")
                    friendly_time = dt.strftime("%Y-%m-%d %H:%M:%S")
                    full_path = os.path.join(backups_dir, filename)
                    result.append((friendly_time, full_path, filename))
                except Exception as e:
                    print(f"Error formatting timestamp: {e}")
                    continue
                    
        return sorted(result, reverse=True)

    def restore_backup(self, backup_path):
        """Restore the selected backup file"""
        try:
            with open(backup_path, "rb") as f:
                data = f.read()
                
            if self.viewer:
                self._cleanup_viewer()
            
            success = self.load_file(self.filename, data)
            
            if success and self.app and hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage("Backup restored successfully")
                
            return success
            
        except Exception as e:
            QMessageBox.critical(None, "Error", f"Failed to restore backup: {e}")
            return False
            
    def _cleanup_viewer(self):
        try:
            if self.viewer:
                cleanup_fn = getattr(self.viewer, "cleanup", None)
                if callable(cleanup_fn):
                    try:
                        cleanup_fn()
                    except Exception as e:
                        print(f"Warning: Error running viewer cleanup: {e}")
            if hasattr(self.viewer, "modified_changed"):
                try:
                    self.viewer.modified_changed.disconnect(self._on_viewer_modified)
                except (TypeError, RuntimeError):
                    pass
                except Exception as e:
                    print(f"Error disconnecting modified_changed signal: {e}")

            layout = self.notebook_widget.layout()
            if layout:
                for i in range(layout.count()):
                    item = layout.itemAt(i)
                    if item.widget() == self.viewer:
                        self.viewer.setParent(None)
                        break
            
            if self.viewer:
                self.viewer.deleteLater()
            self._invalidate_search_dialog_tree()
            self.viewer = None
        except Exception as e:
            print(f"Warning: Error cleaning up viewer: {e}")
    def _release_resources_before_reload(self):
        """Release heavy resources prior to reloading a file."""
        try:
            if self.viewer:
                self._cleanup_viewer()
        except Exception as e:
            print(f"Warning: Error while releasing viewer resources: {e}")

        if self.handler:
            try:
                cleanup_fn = getattr(self.handler, "cleanup", None)
                if callable(cleanup_fn):
                    cleanup_fn()
            except Exception as e:
                print(f"Warning: Error running handler cleanup before reload: {e}")
            try:
                delete_later = getattr(self.handler, "deleteLater", None)
                if callable(delete_later):
                    delete_later()
            except Exception as e:
                print(f"Warning: Error scheduling handler deletion before reload: {e}")

    def cleanup(self):
        """Release resources held by this tab to avoid lingering memory use."""
        try:
            if self.viewer:
                self._cleanup_viewer()
        except Exception as e:
            print(f"Warning: Error cleaning up viewer during tab cleanup: {e}")

        try:
            if self.tree:
                self.tree.setModel(None)
        except Exception as e:
            print(f"Warning: Error clearing tree model: {e}")

        layout = self.notebook_widget.layout() if self.notebook_widget else None
        if layout:
            while layout.count():
                item = layout.takeAt(0)
                widget = item.widget()
                if widget:
                    widget.setParent(None)
                    widget.deleteLater()

        if self.status_label:
            try:
                self.status_label.deleteLater()
            except Exception:
                pass
            self.status_label = None

        if self.tree:
            try:
                self.tree.deleteLater()
            except Exception:
                pass
            self.tree = None

        if self.handler:
            try:
                cleanup_fn = getattr(self.handler, "cleanup", None)
                if callable(cleanup_fn):
                    cleanup_fn()
            except Exception as e:
                print(f"Warning: Error running handler cleanup: {e}")
            try:
                delete_later = getattr(self.handler, "deleteLater", None)
                if callable(delete_later):
                    delete_later()
            except Exception as e:
                print(f"Warning: Error scheduling handler deletion: {e}")
            self.handler = None

        self._invalidate_search_dialog_tree()

        self.metadata_map.clear()
        self.search_results.clear()
        self.search_state["results"] = []
        self.search_state["current_index"] = 0
        self.search_dialog = None
        self.result_list = None
        self.original_data = None
        self.viewer = None

        for key in list(self._search_widgets.keys()):
            self._search_widgets[key] = None

        if hasattr(self.notebook_widget, "parent_tab"):
            self.notebook_widget.parent_tab = None

        if self.notebook_widget:
            try:
                self.notebook_widget.deleteLater()
            except Exception:
                pass
            self.notebook_widget = None

        if hasattr(self, "_find_dialog") and self._find_dialog:
            try:
                self._find_dialog.close()
            except RuntimeError:
                pass
            self._find_dialog = None

        self.parent_notebook = None
        self.app = None

class REasyEditorApp(QMainWindow):
    def __init__(self):
        super().__init__()       
        self.current_game = None   
        self.setWindowTitle(f"REasy Editor v{CURRENT_VERSION}")
        set_app_icon(self)

        try:
            self.settings = load_settings()
        except Exception as e:
            self.settings = DEFAULT_SETTINGS.copy()
            print(f"Error loading settings: {e}")

        if "keyboard_shortcuts" not in self.settings:
            self.settings["keyboard_shortcuts"] = DEFAULT_SETTINGS["keyboard_shortcuts"].copy()
        else:
            for key, value in DEFAULT_SETTINGS["keyboard_shortcuts"].items():
                if key not in self.settings["keyboard_shortcuts"]:
                    self.settings["keyboard_shortcuts"][key] = value

        ensure_projects_root()
        self.current_project = None
        self.proj_dock = ProjectManager(self, None)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.proj_dock)
        self.proj_dock.hide()

        self.dark_mode = self.settings.get("dark_mode", False)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0) 
        main_layout.setSpacing(0) 

        self.notebook = CustomNotebook()
        self.notebook.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.notebook.setMinimumSize(50, 50)    
        self.notebook.app_instance = self
        self.notebook._set_icon_callback = set_app_icon
        main_layout.addWidget(self.notebook)

        self.tabs = weakref.WeakValueDictionary()
        self._shared_find_dialog = None

        self.update_notification = UpdateNotificationManager(self, CURRENT_VERSION)
        self._update_menu = None
        self._create_menus()

        self.status_bar = QStatusBar()
        self.status_bar.setContentsMargins(0, 0, 0, 0)
        self.status_bar.setMaximumHeight(20) 
        self.status_bar.setStyleSheet("""
            QStatusBar {
                margin: 0;
                padding: 0;
                border-top: 1px solid #cccccc;
            }
            QStatusBar::item {
                border: none;
            }
        """)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready")

        self.set_dark_mode(self.dark_mode)

        self.console_widget = ConsoleWidget()
        self.console_widget.setMaximumHeight(100)
        self.console_widget.setVisible(self.settings.get("show_debug_console", True))
        main_layout.addWidget(self.console_widget)

        if self.settings.get("show_debug_console", True):
            sys.stdout = ConsoleRedirector(self.console_widget, sys.stdout)
            sys.stderr = ConsoleRedirector(self.console_widget, sys.stderr)
            print("Debug console started.")

        self.resize(1160, 920)

        self.setAcceptDrops(True)
 
        last_seen = self.settings.get("last_seen_version", "")
        if last_seen != CURRENT_VERSION:
            QTimer.singleShot(600, self._show_changelog_if_needed)

    def _internal_drag(self, event):
        return event.mimeData().hasFormat("application/x-qabstractitemmodeldatalist")

    def _show_changelog_if_needed(self):
        last_seen = self.settings.get("last_seen_version", "")
        if last_seen != CURRENT_VERSION:
            dlg = ChangelogDialog(self, CURRENT_VERSION, self.dark_mode)
            dlg.exec()
            self.settings["last_seen_version"] = CURRENT_VERSION
            save_settings(self.settings)
 
    def dragEnterEvent(self, event):
        if self._internal_drag(event):
            event.ignore()
            return

        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self._internal_drag(event):
            event.ignore()
            return

        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                self._open_path(url.toLocalFile())
            event.acceptProposedAction()

    def _open_path(self, path: str):
        file_path = path
        if os.path.isfile(file_path):
            try:
                with open(file_path, "rb") as f:
                    data = f.read()
                self.add_tab(file_path, data)
            except Exception as e:
                QMessageBox.critical(
                    self, "Error", f"Failed to load {file_path}: {str(e)}"
                )
    def _create_menus(self):
        menubar = self.menuBar()
        self.update_notification.update_update_menu(force=True, menubar=menubar)

        file_menu = menubar.addMenu(self.tr("File"))

        open_act = QAction(self.tr("Open File..."), self)
        open_act.setObjectName("file_open")
        open_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_open", "Ctrl+O")))
        open_act.triggered.connect(self.on_open)

        new_proj_act = QAction(self.tr("New Project (Create Mod)..."), self)
        open_proj_act = QAction(self.tr("Open Project..."), self)
        close_proj_act = QAction(self.tr("Close Project"), self)
        new_proj_act.triggered.connect(self.new_project)
        open_proj_act.triggered.connect(self.open_project)
        close_proj_act.triggered.connect(self.close_project)
        file_menu.insertSeparator(open_act)  
        file_menu.insertAction(open_act, new_proj_act)
        file_menu.insertAction(open_act, open_proj_act)
        file_menu.insertAction(open_act, close_proj_act)

        file_menu.addSeparator()

        file_menu.addAction(open_act)

        save_act = QAction(self.tr("Save"), self)
        save_act.setObjectName("file_save")
        save_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_save", "Ctrl+S")))
        save_act.triggered.connect(self.on_direct_save)
        file_menu.addAction(save_act)

        save_as_act = QAction(self.tr("Save As..."), self)
        save_as_act.setObjectName("file_save_as")
        save_as_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_save_as", "Ctrl+Shift+S")))
        save_as_act.triggered.connect(self.on_save)
        file_menu.addAction(save_as_act)
        
        restore_backup_act = QAction(self.tr("Restore Backup..."), self)
        restore_backup_act.triggered.connect(self.on_restore_backup)
        file_menu.addAction(restore_backup_act)

        reload_act = QAction(self.tr("Reload"), self)
        reload_act.setObjectName("file_reload")
        reload_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_reload", "Ctrl+R")))
        reload_act.triggered.connect(self.reload_file)
        file_menu.addAction(reload_act)

        close_tab_act = QAction(self.tr("Close Tab"), self)
        close_tab_act.setObjectName("file_close_tab")
        close_tab_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_close_tab", "Ctrl+W")))
        close_tab_act.triggered.connect(self.close_current_tab)
        file_menu.addAction(close_tab_act)

        file_menu.addSeparator()

        settings_act = QAction(self.tr("Settings"), self)
        settings_act.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_act)

        exit_act = QAction(self.tr("Exit"), self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        find_menu = menubar.addMenu(self.tr("Find"))

        find_act = QAction(self.tr("Find"), self)
        find_act.setObjectName("find_search")
        find_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search", "Ctrl+F")))
        find_act.triggered.connect(self.open_find_dialog)
        find_menu.addAction(find_act)

        guid_act = QAction(self.tr("Search Directory for GUID"), self)
        guid_act.setObjectName("find_search_guid")
        guid_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_guid", "Ctrl+G")))
        guid_act.triggered.connect(self.search_directory_for_guid)
        find_menu.addAction(guid_act)

        text_act = QAction(self.tr("Search Directory for Text"), self)
        text_act.setObjectName("find_search_text")
        text_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_text", "Ctrl+T")))
        text_act.triggered.connect(self.search_directory_for_text)
        find_menu.addAction(text_act)

        num_act = QAction(self.tr("Search Directory for Number"), self)
        num_act.setObjectName("find_search_number")
        num_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_number", "Ctrl+N")))
        num_act.triggered.connect(self.search_directory_for_number)
        find_menu.addAction(num_act)
        
        hex_act = QAction(self.tr("Search Directory for Hex"), self)
        hex_act.setObjectName("find_search_hex")
        hex_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_hex", "Ctrl+H")))
        hex_act.triggered.connect(self.search_directory_for_hex)
        find_menu.addAction(hex_act)
        
        rsz_field_act = QAction("Find RSZ Field Value", self)
        rsz_field_act.setObjectName("find_rsz_field_value")
        rsz_field_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_rsz_field_value", "Ctrl+Shift+F")))
        rsz_field_act.triggered.connect(self.open_rsz_field_value_finder)
        find_menu.addAction(rsz_field_act)

        view_menu = menubar.addMenu(self.tr("View"))

        dark_act = QAction(self.tr("Toggle Dark Mode"), self)
        dark_act.setObjectName("view_dark_mode")
        dark_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_dark_mode", "Ctrl+D")))
        dark_act.triggered.connect(self.toggle_dark_mode)
        view_menu.addAction(dark_act)

        prev_tab_act = QAction(self.tr("Previous Tab"), self)
        prev_tab_act.setObjectName("view_prev_tab")
        prev_tab_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_prev_tab", "PgDown")))
        prev_tab_act.triggered.connect(self.goto_previous_tab)
        view_menu.addAction(prev_tab_act)

        next_tab_act = QAction(self.tr("Next Tab"), self)
        next_tab_act.setObjectName("view_next_tab")
        next_tab_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_next_tab", "PgUp")))
        next_tab_act.triggered.connect(self.goto_next_tab)
        view_menu.addAction(next_tab_act)

        dbg_act = QAction(self.tr("Toggle Debug Console"), self)
        dbg_act.setObjectName("view_debug_console")
        dbg_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_debug_console", "Ctrl+Shift+D")))
        dbg_act.triggered.connect(
            lambda: self.toggle_debug_console(
                not self.settings.get("show_debug_console", True)
            )
        )
        view_menu.addAction(dbg_act)

        tools_menu = menubar.addMenu(self.tr("Tools"))
        guid_conv_act = QAction(self.tr("GUID Converter"), self)
        guid_conv_act.triggered.connect(self.open_guid_converter)
        tools_menu.addAction(guid_conv_act)

        hash_calc_act = QAction(self.tr("Hash Calculator"), self)
        hash_calc_act.triggered.connect(self.open_hash_calculator)
        tools_menu.addAction(hash_calc_act)

        outdated_files_action = QAction(self.tr("Outdated Files Detector"), self)
        outdated_files_action.triggered.connect(self.open_outdated_files_detector)
        tools_menu.addAction(outdated_files_action)

        rsz_differ_act = QAction(self.tr("RSZ Diff Viewer"), self)
        rsz_differ_act.triggered.connect(self.open_rsz_differ)
        tools_menu.addAction(rsz_differ_act)

        script_creator_act = QAction(self.tr("REF Script Creator"), self)
        script_creator_act.triggered.connect(self.open_script_creator)
        tools_menu.addAction(script_creator_act)
        
        pak_browser_act = QAction(self.tr("PAK Browser"), self)
        pak_browser_act.triggered.connect(self.open_pak_browser)
        tools_menu.addAction(pak_browser_act)

        tools_menu.addSeparator()

        help_menu = menubar.addMenu(self.tr("Help"))
        about_act = QAction(self.tr("About"), self)
        wiki_act = QAction(self.tr("REasy Wiki"), self)
        about_act.triggered.connect(self.show_about)
        wiki_act.triggered.connect(self.show_wiki)
        help_menu.addAction(about_act)
        help_menu.addAction(wiki_act)
        
        donate_menu = menubar.addMenu(self.tr("Donate"))
        donate_act = QAction(self.tr("Support REasy"), self)
        donate_act.triggered.connect(self.show_donate_dialog)
        donate_menu.addAction(donate_act)

    def new_project(self):
        name, ok = QInputDialog.getText(self, "New Project", "Project name:")
        if not ok or not name.strip():
            return

        game = self.proj_dock._choose_game()
        if not game:
            return

        choose_paks = SelectSourceDialog.prompt(self, game, unpacked_checked=True, paks_checked=False)
        if choose_paks is None:
            return

        use_paks = bool(choose_paks)

        if not use_paks:
            start_dir = str(self.settings.get("unpacked_path", ""))
            folder = QFileDialog.getExistingDirectory(
                self,
                f"Locate unpacked files for {game}",
                start_dir,
                QFileDialog.ShowDirsOnly
            )
            if not folder:
                return

            expected = self.proj_dock.expected_native_tuple(game)
            if expected:
                test = os.path.join(folder, *expected)
                if not os.path.isdir(test):
                    QMessageBox.warning(
                        self, "Invalid unpacked folder",
                        f"The folder you selected doesn't contain:\n"
                        f"  {os.path.join(*expected)}\n"
                        f"Please select the correct unpacked game directory.")
                    return

            self.settings["unpacked_path"] = folder
            self.save_settings()
        else:
            start_dir = str(self.settings.get("unpacked_path", ""))
            folder = QFileDialog.getExistingDirectory(
                self,
                "Locate game directory (contains .pak)",
                start_dir,
                QFileDialog.ShowDirsOnly
            )
            if not folder:
                return
            
            if not self.proj_dock.has_valid_paks(folder, ignore_mod_paks=True):
                QMessageBox.warning(self, "Invalid game folder", "No .pak files found in the selected directory.")
                return

        mod_dir = os.path.join(PROJECTS_ROOT, game, name.strip())
        os.makedirs(mod_dir, exist_ok=True)

        self.current_game = game
        
        self._activate_project(mod_dir)
        if use_paks:
            self.proj_dock.switch_tab("pak")
            self.proj_dock.apply_pak_root(folder)
        else:
            self.proj_dock.apply_unpacked_root(folder)

    def open_project(self):
        start_root = str(PROJECTS_ROOT)
        dir_ = QFileDialog.getExistingDirectory(
            self,
            "Open REasy Project",
            start_root,
            QFileDialog.ShowDirsOnly
        )
        if not dir_:
            return

        project_path = Path(dir_).resolve()
        game = self.proj_dock.infer_project_game(project_path)

        if not game:
            QMessageBox.warning(
                self, "Invalid selection",
                "Please pick a mod folder *directly* inside one of the game "
                "directories (e.g. projects/RE4/YourMod)."
            )

        self.current_game = game
        self.proj_dock.current_game = game

        self.settings["last_game"] = game
        self.save_settings()

        self._activate_project(str(project_path))

    def close_project(self):
        if not self.current_project: 
            return
        self.current_project = None
        self.proj_dock.set_project(None)
        self.proj_dock.hide()
        self.status_bar.showMessage("Project closed", 3000)

    def _activate_project(self, path: str):
        """Make <path> the current project and show the dock."""
        self.current_project = path
        self.proj_dock.current_game = self.current_game
        self.proj_dock.set_project(path)
        self.proj_dock.show()     
        self.status_bar.showMessage(
            f"Project: {os.path.basename(path)}", 3000)

    def open_script_creator(self):
        """
        Opens (or focuses) the standalone REF Script Creator window.
        We keep a single instance around so multiple clicks just raise it.
        """
        if not hasattr(self, "_script_creator") or self._script_creator is None:
            from tools.ref_script_creator import ScriptCreatorWindow
            self._script_creator = ScriptCreatorWindow(self)
        self._script_creator.show()
        self._script_creator.raise_()
        self._script_creator.activateWindow()
    
    def open_pak_browser(self):
        dialog = PakBrowserDialog(self)
        dialog.exec()

    def set_dark_mode(self, state):
        self.dark_mode = state
        self.settings["dark_mode"] = state
        self.save_settings()

        colors = self._build_theme_colors(state)
        self._apply_style(colors)

        self.notebook.set_dark_mode(state)
        self._update_tab_viewers(state)

        if hasattr(self, '_shared_find_dialog') and self._shared_find_dialog:
            self._shared_find_dialog.set_dark_mode(state)

    def _theme_accent_color(self) -> QColor:
        color_value = self.settings.get("tree_highlight_color", DEFAULT_THEME_COLOR)
        color = QColor(color_value)
        if not color.isValid():
            color = QColor(DEFAULT_THEME_COLOR)
        return color

    def _build_theme_colors(self, dark_mode: bool) -> dict:
        accent = self._theme_accent_color()
        highlight_light = accent.name()
        highlight_dark = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, 0.5)"
        if dark_mode:
            return {
                "bg": "#2b2b2b",
                "tree_bg": "#2b2b2b",
                "fg": "white",
                "highlight": highlight_dark,
                "input_bg": "#3b3b3b",
                "disabled_bg": "#404040",
                "border": "#555555",
            }
        return {
            "bg": "#ffffff",
            "tree_bg": "#ffffff",
            "fg": "#000000",
            "highlight": highlight_light,
            "input_bg": "#ffffff",
            "disabled_bg": "#f0f0f0",
            "border": "#cccccc",
        }

    def _apply_style(self, colors):
        self.setStyleSheet(f"""
            QMainWindow, QDialog, QWidget {{
                background-color: {colors['bg']}; color: {colors['fg']};
            }}
            QTreeView {{
                background-color: {colors['tree_bg']}; color: {colors['fg']};
                border: 1px solid {colors['border']};
            }}
            QTreeView::item:selected {{ background-color: {colors['highlight']}; }}
            QLineEdit, QPlainTextEdit {{
                background-color: {colors['input_bg']}; color: {colors['fg']};
                border: 1px solid {colors['border']}; padding: 2px;
            }}
            QPushButton {{
                background-color: {colors['input_bg']}; color: {colors['fg']};
                border: 1px solid {colors['border']}; padding: 5px; min-width: 80px;
            }}
            QPushButton:disabled {{ background-color: {colors['disabled_bg']}; }}
            QLabel, QCheckBox {{ color: {colors['fg']}; }}
            QCheckBox::indicator {{
                width: 15px; height: 15px; background-color: {colors['input_bg']};
                border: 1px solid {colors['border']}; border-radius: 2px;
            }}
            QCheckBox::indicator:checked {{
                background-color: {colors['highlight']}; border-color: {colors['highlight']};
            }}
            QMenuBar, QMenu, QTabWidget::pane, QStatusBar, QProgressDialog, QListWidget {{
                background-color: {colors['bg']}; color: {colors['fg']};
                border: 1px solid {colors['border']};
            }}
            QMenuBar::item:selected, QMenu::item:selected, QTabBar::tab:selected, QListWidget::item:selected {{
                background-color: {colors['highlight']};
            }}
        """)

    def _update_tab_viewers(self, dark_mode):
        for tab in self.tabs.values():
            if hasattr(tab, "dark_mode"):
                tab.dark_mode = dark_mode

            if hasattr(tab, '_find_dialog') and tab._find_dialog:
                try:
                    tab._find_dialog.set_dark_mode(dark_mode)
                except RuntimeError:
                    pass
            
            if tab.handler:
                tab.handler.dark_mode = dark_mode
                
                try:
                    if hasattr(tab.handler, "create_viewer"):
                        new_viewer = tab.handler.create_viewer()
                        if new_viewer:
                            self._replace_tab_viewer(tab, new_viewer)
                except Exception as e:
                    print(f"Error updating viewer: {e}")

    def _replace_tab_viewer(self, tab, new_viewer):
        layout = tab.notebook_widget.layout()
        if not layout:
            return

        try:
            status_text = tab.status_label.text()
        except (RuntimeError, AttributeError):
            status_text = "Ready"

        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.deleteLater()
        
        new_status_label = QLabel(status_text)
        layout.addWidget(new_viewer)
        layout.addWidget(new_status_label)
        
        tab.viewer = new_viewer
        tab.status_label = new_status_label
        
        if hasattr(tab, "_find_dialog") and tab._find_dialog and tab._find_dialog.isVisible():
            tab._find_dialog.close()
            tab._find_dialog = None
            QTimer.singleShot(100, tab.open_find_dialog)

    def toggle_dark_mode(self):
        self.set_dark_mode(not self.dark_mode)

    def toggle_debug_console(self, show: bool):
        if hasattr(self, "console_widget"):
            self.console_widget.setVisible(show)

            if show:
                if isinstance(sys.stdout, ConsoleRedirector):
                    return
                sys.stdout = ConsoleRedirector(self.console_widget, sys.stdout)
                sys.stderr = ConsoleRedirector(self.console_widget, sys.stderr)
                print("Debug console started.")
            else:
                if hasattr(sys.stdout, "original_stream"):
                    sys.stdout = sys.stdout.original_stream
                if hasattr(sys.stderr, "original_stream"):
                    sys.stderr = sys.stderr.original_stream

            self.settings["show_debug_console"] = show
            self.save_settings()

    def save_settings(self):
        save_settings(self.settings)

    def closeEvent(self, event):
        if hasattr(self, '_shared_find_dialog') and self._shared_find_dialog:
            try:
                self._shared_find_dialog.close()
            except RuntimeError:
                pass
        for tab in self.tabs.values():
            if tab.modified:
                ans = QMessageBox.question(
                    self,
                    UNSAVED_CHANGES_STR,
                    f"File {os.path.basename(tab.filename)} has unsaved changes.\nSave before closing?",
                    QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                )
                if ans == QMessageBox.Cancel:
                    event.ignore()
                    return
                elif ans == QMessageBox.Yes:
                    tab.on_save()
                    if tab.modified:
                        event.ignore()
                        return
                tab.modified = False
        
        event.accept()

    def update_from_app_settings(self):
        """Update handler settings from the application settings"""
        for tab in self.tabs.values():
            if hasattr(tab, 'handler') and isinstance(tab.handler, RszHandler):
                    tab.handler.set_advanced_mode(self.settings.get("show_rsz_advanced", True))
                    tab.handler.set_confirmation_prompts(self.settings.get("confirmation_prompt", True))
                    tab.handler.set_game_version(self.settings.get("game_version", "RE4"))
        
    def open_settings_dialog(self):
        dialog, _ = create_standard_dialog(self, "Settings", "500x400")
        
        main_layout = QVBoxLayout(dialog)
        
        tab_widget = QTabWidget()
        main_layout.addWidget(tab_widget)
        
        general_tab = QWidget()
        tab_widget.addTab(general_tab, "General")
        
        general_layout = QVBoxLayout(general_tab)
        general_layout.setSpacing(15)

        label = QLabel("RSZ JSON Path:")
        general_layout.addWidget(label, 0, Qt.AlignBottom)

        json_path_layout = QHBoxLayout()
        json_path_layout.setContentsMargins(0, 0, 0, 0)
        json_entry = QLineEdit(self.settings.get("rcol_json_path", ""))
        json_path_layout.addWidget(json_entry)

        browse_btn = QPushButton("Browse...")
        json_path_layout.addWidget(browse_btn)
        general_layout.addLayout(json_path_layout)

        # Game Version selection
        game_version_layout = QHBoxLayout()
        game_version_layout.setContentsMargins(0, 0, 0, 0)
        game_version_label = QLabel("Game Version (Reload Required):")
        game_version_layout.addWidget(game_version_label)
        
        game_version_combo = QComboBox()
        for g in GAMES:
            game_version_combo.addItem(g)
        current_version = self.settings.get("game_version", "RE4")
        game_version_combo.setCurrentText(current_version)
        game_version_layout.addWidget(game_version_combo)
        general_layout.addLayout(game_version_layout)
        
        translation_layout = QHBoxLayout()
        translation_layout.setContentsMargins(0, 0, 0, 0)
        translation_label = QLabel("Translation Target Language:")
        translation_layout.addWidget(translation_label)
        
        translation_combo = QComboBox()
        languages = [
            ("en", "English"),
            ("ar", "Arabic"),
            ("es", "Spanish"),
            ("fr", "French"),
            ("de", "German"),
            ("it", "Italian"),
            ("ja", "Japanese"),
            ("ko", "Korean"),
            ("pt", "Portuguese"),
            ("ru", "Russian"),
            ("zh-CN", "Chinese (Simplified)"),
            ("zh-TW", "Chinese (Traditional)")
        ]
        
        for code, name in languages:
            translation_combo.addItem(name, code)
        
        current_lang_code = self.settings.get("translation_target_language", "en")
        for i, (code, _) in enumerate(languages):
            if code == current_lang_code:
                translation_combo.setCurrentIndex(i)
                break
                
        translation_layout.addWidget(translation_combo)
        general_layout.addLayout(translation_layout)

        theme_color_layout = QHBoxLayout()
        theme_color_layout.setContentsMargins(0, 0, 0, 0)
        theme_color_label = QLabel("Theme Color:")
        theme_color_layout.addWidget(theme_color_label)

        selected_theme_color = self.settings.get("tree_highlight_color", DEFAULT_THEME_COLOR)

        theme_color_button = QPushButton()
        theme_color_button.setFixedWidth(140)

        def update_theme_color_button(color_value: str):
            theme_color_button.setText(color_value.upper())
            color = QColor(color_value)
            text_color = "#000000"
            if color.isValid():
                brightness = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
                if brightness < 186:
                    text_color = "#ffffff"
            theme_color_button.setStyleSheet(
                f"background-color: {color_value}; border: 1px solid #555555; color: {text_color};"
            )

        update_theme_color_button(selected_theme_color)

        def choose_theme_color():
            nonlocal selected_theme_color
            initial = QColor(selected_theme_color)
            color = QColorDialog.getColor(initial, dialog, "Select Theme Color")
            if color.isValid():
                selected_theme_color = color.name()
                update_theme_color_button(selected_theme_color)

        theme_color_button.clicked.connect(choose_theme_color)
        theme_color_layout.addWidget(theme_color_button)
        theme_color_layout.addStretch()
        general_layout.addLayout(theme_color_layout)

        dark_box = QCheckBox("Dark Mode")
        dark_box.setChecked(self.dark_mode)
        general_layout.addWidget(dark_box)

        debug_box = QCheckBox("Show Debug Console")
        debug_box.setChecked(self.settings.get("show_debug_console", True))
        general_layout.addWidget(debug_box)
        
        rsz_advanced_box = QCheckBox("Show advanced settings for RSZ files (Reload Required)")
        rsz_advanced_box.setChecked(self.settings.get("show_rsz_advanced", True))
        general_layout.addWidget(rsz_advanced_box)
        
        backup_box = QCheckBox("Create backup on save")
        backup_box.setChecked(self.settings.get("backup_on_save", True))
        general_layout.addWidget(backup_box)

        confirmation_prompt_box = QCheckBox("Show confirmation prompts for RSZ actions")
        confirmation_prompt_box.setChecked(self.settings.get("confirmation_prompt", True))
        general_layout.addWidget(confirmation_prompt_box)

        ui_lang_layout = QHBoxLayout()
        ui_lang_layout.setContentsMargins(0, 0, 0, 0)
        ui_lang_label = QLabel("UI Language (Restart Recommended):")
        ui_lang_layout.addWidget(ui_lang_label)

        ui_lang_combo = QComboBox()
        ui_lang_combo.addItem("System", "system")
        for info in LanguageManager.instance().available_languages():
            ui_lang_combo.addItem(info.name, info.code)
        current_ui_lang = self.settings.get("ui_language", "system")
        for i in range(ui_lang_combo.count()):
            if ui_lang_combo.itemData(i) == current_ui_lang:
                ui_lang_combo.setCurrentIndex(i)
                break
        ui_lang_layout.addWidget(ui_lang_combo)
        general_layout.addLayout(ui_lang_layout)

        general_layout.addStretch()
        
        shortcuts_tab = create_shortcuts_tab()
        tab_widget.addTab(shortcuts_tab, "Keyboard Shortcuts")
        
        shortcuts = self.settings.get("keyboard_shortcuts", {}).copy()
        for key in shortcuts_tab.shortcut_names:
            if key not in shortcuts:
                shortcuts[key] = DEFAULT_SETTINGS["keyboard_shortcuts"].get(key, "")
        
        shortcuts_tab.populate_shortcuts_list(shortcuts)
        
        shortcuts_tab.edit_shortcut_btn.clicked.connect(
            lambda: shortcuts_tab.edit_shortcut(shortcuts, dialog)
        )
        shortcuts_tab.reset_shortcut_btn.clicked.connect(
            lambda: shortcuts_tab.reset_shortcut(shortcuts, dialog)
        )
        shortcuts_tab.shortcuts_list.itemDoubleClicked.connect(
            lambda item: shortcuts_tab.edit_shortcut(shortcuts, dialog)
        )
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        main_layout.addWidget(button_box)

        def on_ok():
            new_json_path = json_entry.text().strip()
            if new_json_path and not os.path.exists(new_json_path):
                QMessageBox.critical(
                    dialog, "Error", "The specified JSON file does not exist."
                )
                return

            self.settings["rcol_json_path"] = new_json_path
            self.settings["dark_mode"] = dark_box.isChecked()
            self.settings["show_debug_console"] = debug_box.isChecked()
            self.settings["show_rsz_advanced"] = rsz_advanced_box.isChecked()
            self.settings["backup_on_save"] = backup_box.isChecked()
            self.settings["confirmation_prompt"] = confirmation_prompt_box.isChecked()
            self.settings["keyboard_shortcuts"] = shortcuts
            self.settings["tree_highlight_color"] = selected_theme_color

            new_version = game_version_combo.currentText()
            self.settings["game_version"] = new_version
            
            selected_index = translation_combo.currentIndex()
            if selected_index >= 0:
                lang_code = translation_combo.itemData(selected_index)
                self.settings["translation_target_language"] = lang_code
            
            if self.dark_mode != dark_box.isChecked():
                self.set_dark_mode(dark_box.isChecked())

            self.toggle_debug_console(debug_box.isChecked())

            self._apply_style(self._build_theme_colors(self.dark_mode))

            self.update_from_app_settings()

            self.apply_keyboard_shortcuts()

            new_ui_lang = ui_lang_combo.currentData()
            lang_changed = new_ui_lang != self.settings.get("ui_language", "system")

            self.settings["ui_language"] = new_ui_lang

            self.save_settings()
            if lang_changed:
                QMessageBox.information(
                    dialog,
                    "Language Changed",
                    "UI language will be applied after restart."
                )
            dialog.accept()

        def on_cancel():
            dialog.reject()

        def browse():
            file_path, _ = QFileDialog.getOpenFileName(
                dialog,
                "Select JSON file",
                os.path.dirname(json_entry.text()) if json_entry.text() else "",
                "JSON Files (*.json)",
            )
            if file_path:
                json_entry.setText(file_path)

        button_box.accepted.connect(on_ok)
        button_box.rejected.connect(on_cancel)
        browse_btn.clicked.connect(browse)

        dialog.exec()

    def apply_keyboard_shortcuts(self):
        shortcuts = self.settings.get("keyboard_shortcuts", {})
        for action in self.findChildren(QAction):
            action_name = action.objectName()
            if action_name in shortcuts:
                shortcut_text = shortcuts[action_name]
                if shortcut_text:
                    try:
                        action.setShortcut(QKeySequence(shortcut_text))
                        print(f"Applied shortcut: {action_name} -> {shortcut_text}")
                    except Exception as e:
                        print(f"Error setting shortcut for {action_name}: {e}")
        
        if hasattr(self, "menuBar"):
            menubar = self.menuBar()
            if menubar:
                menubar.update()

    def open_guid_converter(self):
        create_guid_converter_dialog(self)

    def open_hash_calculator(self):
        self.hash_calculator = HashCalculator()
        self.hash_calculator.show()

    def open_outdated_files_detector(self):
        """Open the Outdated Files Detector dialog"""
        registry_path = self.settings.get("rcol_json_path", None)
        dialog = OutdatedFilesDialog(self, registry_path)
        dialog.exec()
        
    def open_rsz_differ(self):
        game_version = self.game_dropdown.currentText() if hasattr(self, 'game_dropdown') else "RE4"
        json_path = self.settings.get("rcol_json_path", None)
        dialog = RszDifferDialog(self, game_version, json_path)
        dialog.exec()
        
    def search_directory_for_number(self):
        search_directory_for_type(self, 'number', create_search_dialog, create_search_patterns)

    def search_directory_for_text(self):
        search_directory_for_type(self, 'text', create_search_dialog, create_search_patterns)

    def search_directory_for_guid(self):
        search_directory_for_type(self, 'guid', create_search_dialog, create_search_patterns)

    def search_directory_for_hex(self):
        search_directory_for_type(self, 'hex', create_search_dialog, create_search_patterns)
    
    def open_rsz_field_value_finder(self):
        """Open the RSZ field value finder dialog"""
        from ui.rsz_field_value_finder_dialog import RszFieldValueFinderDialog
        dialog = RszFieldValueFinderDialog(self, self.settings)
        dialog.exec()

    def open_find_dialog(self):
        active = self.get_active_tab()
        if not active:
            QMessageBox.critical(self, "Error", "No active tab for searching.")
            return
        
        if isinstance(active.handler, MsgHandler):
            QMessageBox.information(self, "Search in MSG", "MSG files have a built-in search at the top of the editor. Please use that search bar.")
            return
            
        for window in self.notebook._floating_windows:
            if window.page == active.notebook_widget:
                active.open_find_dialog()
                return
        if not self._shared_find_dialog or not isinstance(self._shared_find_dialog, BetterFindDialog):
            self._shared_find_dialog = BetterFindDialog(file_tab=active, parent=self, shared_mode=True)
            self._shared_find_dialog.set_dark_mode(self.dark_mode)
            self.notebook.currentChanged.connect(self._on_tab_changed_for_find)
        else:
            self._shared_find_dialog.set_file_tab(active)
            
        self._shared_find_dialog.show()
        if not self._shared_find_dialog.isFloating():
            self._shared_find_dialog.raise_()
            self._shared_find_dialog.activateWindow()

    def _on_tab_changed_for_find(self):
        """Update the shared find dialog when tab changes"""
        if hasattr(self, '_shared_find_dialog') and self._shared_find_dialog and self._shared_find_dialog.isVisible():
            active = self.get_active_tab()
            if active:
                is_detached = False
                for window in self.notebook._floating_windows:
                    if window.page == active.notebook_widget:
                        is_detached = True
                        break
                
                if not is_detached:
                    self._shared_find_dialog.set_file_tab(active)
    
    def _check_and_close_shared_find_dialog(self):
        """Close the shared find dialog if no tabs are left in the main window"""
        has_main_tabs = False
        for i in range(self.notebook.count()):
            widget = self.notebook.widget(i)
            if widget:
                is_detached = False
                for window in self.notebook._floating_windows:
                    if window.page == widget:
                        is_detached = True
                        break
                if not is_detached:
                    has_main_tabs = True
                    break
        
        if not has_main_tabs and hasattr(self, '_shared_find_dialog') and self._shared_find_dialog:
            try:
                if self._shared_find_dialog.isVisible():
                    self._shared_find_dialog.close()
            except RuntimeError:
                pass

    def add_tab(self, filename=None, data=None):
        if filename:
            abs_fn = os.path.abspath(filename)
            for tab in self.tabs.values():
                if tab.filename and os.path.abspath(tab.filename) == abs_fn:
                    if tab.modified:
                        ans = QMessageBox.question(
                            self,
                            UNSAVED_CHANGES_STR,
                            f"The file {os.path.basename(filename)} has unsaved changes.\nSave before reopening?",
                            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                        )
                        if ans == QMessageBox.Cancel:
                            return
                        elif ans == QMessageBox.Yes:
                            tab.on_save()
                        else:
                            tab.modified = False
                            tab.update_tab_title()
                    index = self.notebook.indexOf(tab.notebook_widget)
                    if index != -1:
                        self.notebook.setCurrentIndex(index)
                    return

        tab = None
        try:
            handler = get_handler_for_data(data)
            if not handler:
                QMessageBox.critical(self, "Error", "Unsupported file type")
                return
            
            if hasattr(handler, 'needs_json_path') and handler.needs_json_path():
                if not self.settings.get("rcol_json_path"):
                    msg = QMessageBox(QMessageBox.Warning, 
                        "JSON Path Not Set",
                        "RSZ type registry JSON path is not set.\nWould you like to set it now?",
                        QMessageBox.Yes | QMessageBox.No)
                    if msg.exec() == QMessageBox.Yes:
                        self.open_settings_dialog()
                    return

            tab = FileTab(None, filename, data, app=self)
            if data is not None and not getattr(tab, "initial_load_complete", True):
                if tab.notebook_widget:
                    tab.notebook_widget.deleteLater()
                return
            tab.parent_notebook = self.notebook
            tab_label = os.path.basename(filename) if filename else "Untitled"
            _ = self.notebook.addTab(tab.notebook_widget, tab_label)
            self.tabs[tab.notebook_widget] = tab
            self.notebook.setCurrentWidget(tab.notebook_widget)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open file: {str(e)}")
            if tab and hasattr(tab, 'notebook_widget') and tab.notebook_widget:
                try:
                    tab.notebook_widget.deleteLater()
                except Exception as e:
                    print(f"Error closing tab: {e}")

    def get_active_tab(self):
        aw = QApplication.activeWindow()
        if isinstance(aw, FloatingTabWindow):
            tab = self._resolve_tab_from_widget(aw.centralWidget())
            if tab:
                return tab
        current_widget = self.notebook.currentWidget()
        tab = self.tabs.get(current_widget, None)
        if tab:
            return tab
        fw = QApplication.focusWidget()
        tab = self._resolve_tab_from_widget(fw)
        if tab:
            return tab
        return None

    def get_active_tree(self):
        active_tab = self.get_active_tab()
        if not active_tab:
            return None

        if active_tab.viewer and hasattr(active_tab.viewer, "tree"):
            return active_tab.viewer.tree
        return active_tab.tree

    def on_open(self):
        fn, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open File"),
            "",
            self.tr("RE Files (*.uvar* *.scn* *.user* *.pfb* *.msg* *.efx* *.cfil* *.motbank* *.tex* *.mesh* *.mdf2*);;SCN Files (*.scn*);;User Files (*.user*);;UVAR Files (*.uvar*);;PFB Files (*.pfb*);;MSG Files (*.msg*);;EFX Files (*.efx*);;CFIL Files (*.cfil*);;MOTBANK Files (*.motbank*);;Texture Files (*.tex*);;DDS Files (*.dds*);;Mesh Files (*.mesh*);;Material Definition Files (*.mdf2*);;All Files (*.*)")
        )
        if not fn:
            return
        try:
            with open(fn, "rb") as f:
                data = f.read()

            handler = get_handler_for_data(data)
            if handler:
                self.add_tab(fn, data) 
            else:
                QMessageBox.critical(self, "Error", "Unsupported file type")

        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

    def on_direct_save(self):
        active = self.get_active_tab()
        if active:
            active.direct_save()
        else:
            QMessageBox.critical(self, "Error", "No active tab to save.")

    def on_save(self):
        active = self.get_active_tab()
        if active:
            active.on_save()
        else:
            QMessageBox.critical(self, "Error", "No active tab to save.")

    def reload_file(self):
        active = self.get_active_tab()
        if active:
            active.reload_file()
        else:
            QMessageBox.critical(self, "Error", "No active tab to reload.")

    def close_current_tab(self):
        aw = QApplication.activeWindow()
        if aw is not None and hasattr(aw, 'centralWidget'):
            tab = self._resolve_tab_from_widget(aw.centralWidget())
            if tab is not None and hasattr(tab, 'notebook_widget'):
                if aw is not self:
                    try:
                        aw.close()
                    except Exception:
                        pass
                idx = self.notebook.indexOf(tab.notebook_widget)
                if idx >= 0:
                    self.close_tab(idx)
                    return
        current_index = self.notebook.currentIndex()
        if current_index >= 0:
            self.close_tab(current_index)

    def _resolve_tab_from_widget(self, widget):
        w = widget
        while w is not None:
            if w in self.tabs:
                return self.tabs.get(w)
            ft = getattr(w, "_reasy_file_tab", None)
            if ft is not None:
                return ft
            if hasattr(w, 'parentWidget'):
                w = w.parentWidget()
            else:
                break
        return None
    
    def close_tab(self, index):
        widget = self.notebook.widget(index)
        tab = self.tabs.get(widget)
        
        if tab and tab.modified:
            ans = QMessageBox.question(
                self,
                UNSAVED_CHANGES_STR,
                f"The file {os.path.basename(tab.filename)} has unsaved changes.\nSave before closing?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Cancel:
                return
            elif ans == QMessageBox.Yes:
                tab.on_save()
            else:
                tab.modified = False
                tab.update_tab_title()

        if widget in self.tabs:
            del self.tabs[widget]
            
        self.notebook.removeTab(index)
        if tab is not None:
            tab.cleanup()
        elif widget is not None:
            widget.deleteLater()
        self._check_and_close_shared_find_dialog()

    def show_about(self):
        create_about_dialog(self)

    def show_wiki(self):
        QDesktopServices.openUrl(QUrl("https://github.com/seifhassine/REasy-Wiki"))

    def show_donate_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Support REasy Editor")
        layout = QVBoxLayout(dialog)
        
        thank_you_label = QLabel("Thank you for your feedback and support!\nYour contributions help keep this project going.")
        thank_you_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(thank_you_label)
        
        link_label = QLabel('<a href="https://linktr.ee/seifhassine">https://linktr.ee/seifhassine</a>')
        link_label.setAlignment(Qt.AlignCenter)
        link_label.setOpenExternalLinks(True)
        layout.addWidget(link_label)
        
        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)
        
        dialog.setMinimumWidth(300)
        dialog.exec()

    def update_status(self, message, timeout=5000):
        if hasattr(self, 'status_bar'):
            self.status_bar.showMessage(message, timeout)

    def on_restore_backup(self):
        """Show dialog with available backups for the current file"""
        active = self.get_active_tab()
        if not active:
            QMessageBox.critical(self, "Error", "No active tab to restore the backup of.")
            return
            
        if not active.filename:
            QMessageBox.critical(self, "Error", "File has not been saved yet.")
            return
            
        backups = active.find_matching_backups()
        if not backups:
            QMessageBox.information(self, "No Backups", "No backup files found for this file.")
            return
            
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Available Backups for {os.path.basename(active.filename)}")
        dialog.setMinimumWidth(400)
        
        layout = QVBoxLayout(dialog)
        
        backup_list = QListWidget()
        for friendly_time, path, filename in backups:
            item = QListWidgetItem(f"{friendly_time}")
            item.setData(Qt.UserRole, path) 
            item.setToolTip(filename)
            backup_list.addItem(item)
            
        layout.addWidget(QLabel("Select a backup to restore:"))
        layout.addWidget(backup_list)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(button_box)
        
        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)
        
        if dialog.exec() == QDialog.Accepted:
            selected = backup_list.currentItem()
            if not selected:
                QMessageBox.critical(self, "Error", "No backup selected.")
                return
                
            backup_path = selected.data(Qt.UserRole)
            friendly_time = selected.text()
            
            confirm_msg = f"Are you sure you want to restore the backup from:\n{friendly_time}?\n\nCurrent changes will be lost."
            confirm = QMessageBox.question(
                self, 
                "Confirm Restore", 
                confirm_msg,
                QMessageBox.Yes | QMessageBox.No
            )
            
            if confirm == QMessageBox.Yes:
                success = active.restore_backup(backup_path)
                if success:
                    QMessageBox.information(self, "Success", "Backup restored successfully")

    def goto_previous_tab(self):
        current_index = self.notebook.currentIndex()
        if current_index > 0:
            self.notebook.setCurrentIndex(current_index - 1)

    def goto_next_tab(self):
        current_index = self.notebook.currentIndex()
        if current_index < self.notebook.count() - 1:
            self.notebook.setCurrentIndex(current_index + 1)


def main():
    app = QApplication(sys.argv)

    app.setStyle(QStyleFactory.create("Fusion"))

    try:
        from settings import load_settings
        settings = load_settings()
    except Exception:
        settings = {}

    base_dir = Path(sys.argv[0]).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    ts_dir = base_dir / "resources" / "i18n"
    if getattr(sys, "frozen", False):
        exe_candidates = [
            str(base_dir / ("lrelease.exe" if os.name == "nt" else "lrelease")),
        ]
    else:
        base_pkg = Path(PySide6.__file__).resolve().parent
        exe_candidates = [
            str(base_pkg / ("lrelease.exe" if os.name == "nt" else "lrelease")),
            "lrelease",
        ]
    exe = next((p for p in exe_candidates if p and os.path.exists(p)), None)
    preferred = settings.get("ui_language", "system")
    selected_code = LanguageManager.instance().detect(preferred)
    if exe and selected_code != "en":
        ts = str(ts_dir / f"REasy_{selected_code}.ts")
        qm = str(ts_dir / f"REasy_{selected_code}.qm")
        if os.path.exists(ts):
            if not os.path.exists(qm) or os.path.getmtime(qm) < os.path.getmtime(ts):
                print(f"i18n: compiling {os.path.basename(ts)} -> {os.path.basename(qm)} using {exe}")
                subprocess.check_call([exe, ts, "-qm", qm])

    LanguageManager.instance().initialize(app, settings)

    window = REasyEditorApp()

    if len(sys.argv) > 1:
        fn = sys.argv[1]
        try:
            with open(fn, "rb") as f:
                data = f.read()
            window.add_tab(fn, data)
        except Exception as e:
            QMessageBox.critical(None, "Error", str(e))

    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

