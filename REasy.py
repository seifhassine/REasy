#!/usr/bin/env python3

import os
import sys
import uuid
import json
import struct
import weakref
import datetime
import shutil
import re

from file_handlers.factory import get_handler_for_data 
from file_handlers.rsz.rsz_handler import RszHandler  

from ui.better_find_dialog import BetterFindDialog
from ui.guid_converter import create_guid_converter_dialog
from ui.about_dialog import create_about_dialog
from ui.keyboard_shortcuts import create_shortcuts_tab
from settings import DEFAULT_SETTINGS, load_settings, save_settings

from PySide6.QtCore import (
    Qt,
    QTimer,
)
from PySide6.QtGui import (
    QIcon,
    QAction,
    QGuiApplication,
    QStandardItemModel,
    QStandardItem,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QComboBox,
    QTabWidget,
    QTreeView, 
    QMenu,
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
)

from ui.console_logger import ConsoleWidget, ConsoleRedirector
from ui.directory_search import search_directory_for_type
from tools.hash_calculator import HashCalculator  # Import the hash calculator

# Remove the direct EnumManager import
# from utils.enum_manager import EnumManager

def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
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
        except:
            pass
    bg_color = dialog.palette().color(dialog.backgroundRole()).name()
    return dialog, bg_color


def create_search_dialog(parent, search_type):
    prompts = {
        'number': ('Number Search', 'Enter number to search for (32-bit integer):', -2147483648, 2147483647),
        'text': ('Text Search', 'Enter text to search (UTF-16LE):', None, None),
        'guid': ('GUID Search', 'Enter GUID (standard format):', None, None),
        'hex': ('Hex Search', 'Enter hexadecimal bytes (e.g., FF A9 00 3D or FFA9003D):', None, None)
    }
    
    title, msg, min_val, max_val = prompts[search_type]
    
    if search_type == 'number':
        val, ok = QInputDialog.getInt(parent, title, msg, 0, min_val, max_val)
    else:
        val, ok = QInputDialog.getText(parent, title, msg)
        
    return (val, ok) if ok else (None, False)

def create_search_patterns(search_type, value):
    if search_type == 'number':
        try:
            sbytes = struct.pack("<i", value)
            return [sbytes]
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


class CustomNotebook(QTabWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setTabsClosable(True)
        self.setMovable(True)  # Allow reordering tabs by drag and drop
        self.tabCloseRequested.connect(self.on_tab_close_requested)

        self.dark_mode = False
        self.app_instance = None

    def set_dark_mode(self, is_dark):
        self.dark_mode = is_dark
        if is_dark:
            self.setStyleSheet("QTabWidget { background-color: #2b2b2b; }")
        else:
            self.setStyleSheet("QTabWidget { background-color: white; }")

    def on_tab_close_requested(self, index):
        if self.app_instance:
            self.app_instance.close_tab(index)
        else:
            self.removeTab(index)


class FileTab:
    def __init__(self, parent_notebook, filename=None, data=None, app=None):
        self.parent_notebook = parent_notebook
        self.notebook_widget = QWidget()
        self.filename = filename
        self.handler = None
        self.metadata_map = {}
        self.search_results = []
        self.current_result_index = 0
        self.modified = False
        self.app = app
        self.viewer = None 
        self.original_data = data 

        self.status_label = QLabel("No file loaded")

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
        self.tree.customContextMenuRequested.connect(self.show_context_menu)

        self.dark_mode = self.app.dark_mode if self.app else False
        self.search_dialog = None
        self.result_list = None

        if data:
            self.load_file(filename, data)
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
            old_data = self.original_data
            old_status_text = self.status_label.text() if hasattr(self, "status_label") else "No file loaded"
            
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
                
            return True

        except Exception as e:
            self.handler = old_handler
            self.viewer = old_viewer
            self.original_data = old_data
            
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
                if not hasattr(self, '_connected_model'):
                    self._connected_model = None
                
                if self._connected_model is not None and self._connected_model != model:
                    try:
                        self._connected_model.dataChanged.disconnect(self.on_tree_edit)
                    except (RuntimeError, TypeError):
                        pass
                
                if self._connected_model != model:
                    model.dataChanged.connect(self.on_tree_edit)
                    self._connected_model = model

        except Exception as e:
            print(f"Tree refresh failed: {e}")
            if old_model:
                self.tree.setModel(old_model)
            else:
                self.tree.setModel(None)
            
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

    def show_context_menu(self, pos):
        if not self.tree:
            return
        index = self.tree.indexAt(pos)
        if not index.isValid():
            return

        item = index.internalPointer()
        if not item:
            return

        meta = item.raw.get("meta") if isinstance(item.raw, dict) else None
        if meta is None:
            return

        if self.handler and hasattr(self.handler, "get_context_menu"):
            menu = self.handler.get_context_menu(self.tree, None, meta)
            if menu:
                menu.exec(self.tree.mapToGlobal(pos))
        else:
            menu = QMenu(self.tree)
            copy_action = menu.addAction("Copy")
            copy_action.triggered.connect(lambda: self.copy_to_clipboard())
            menu.exec(self.tree.mapToGlobal(pos))

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

    def on_tree_edit(self, topLeft, bottomRight, roles):
        if not self.handler or not self.handler.supports_editing():
            return

        if Qt.UserRole not in roles:
            return

        index = topLeft
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
            QMessageBox.critical(None, "Error", "No file loaded")
            return False
        if not self.filename:
            return self.on_save()
        return self.handle_file_save(self.filename)
        
    def on_save(self):
        if not self.handler:
            QMessageBox.critical(None, "Error", "No file loaded")
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
                "Unsaved Changes",
                f"File {os.path.basename(self.filename)} has unsaved changes.\nSave before reloading?",
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                self.on_save()

        try:
            with open(self.filename, "rb") as f:
                data = f.read()

            current_index = self.parent_notebook.indexOf(self.notebook_widget)

            new_tab = FileTab(self.parent_notebook, self.filename, data, self.app)
            self.parent_notebook.addTab(
                new_tab.notebook_widget, os.path.basename(self.filename)
            )

            self.app.tabs[new_tab.notebook_widget] = new_tab

            if self.notebook_widget in self.app.tabs:
                del self.app.tabs[self.notebook_widget]

            self.parent_notebook.removeTab(current_index)

            self.parent_notebook.insertTab(
                current_index, new_tab.notebook_widget, os.path.basename(self.filename)
            )
            self.parent_notebook.setCurrentIndex(current_index)

        except Exception as e:
            QMessageBox.critical(None, "Error", f"Failed to reload file: {e}")
            import traceback

            traceback.print_exc()

    def _get_current_tree(self):
        if self.viewer and hasattr(self.viewer, "tree"):
            return self.viewer.tree
        return self.tree

    def copy_to_clipboard(self):
        tree = self.app.get_active_tree()
        if not tree:
            return

        index = tree.currentIndex()
        if not index.isValid():
            return

        value = index.data(Qt.UserRole)
        item = index.internalPointer()
        if item and hasattr(item, "data") and len(item.data) > 1:
            value = item.data[1]

        if value:
            QGuiApplication.clipboard().setText(str(value))
            QMessageBox.information(None, "Copied", f"Copied: {value}")

    def open_find_dialog(self):
        if hasattr(self, "_find_dialog") and self._find_dialog:
            try:
                if self._find_dialog.isVisible():
                    self._find_dialog.close()
            except RuntimeError:
                pass
        self._find_dialog = BetterFindDialog(self, parent=self.notebook_widget)
        self._find_dialog.show()

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
                except:
                    continue
                    
        return sorted(result, reverse=True)

    def restore_backup(self, backup_path):
        """Restore the selected backup file"""
        try:
            with open(backup_path, "rb") as f:
                data = f.read()
                
            if self.viewer:
                try:
                    if hasattr(self.viewer, "modified_changed"):
                        try:
                            self.viewer.modified_changed.disconnect()
                        except:
                            pass
                
                    layout = self.notebook_widget.layout()
                    if layout:
                        for i in range(layout.count()):
                            item = layout.itemAt(i)
                            if item.widget() == self.viewer:
                                self.viewer.setParent(None)
                                break
                    
                    self.viewer.deleteLater()
                    self.viewer = None
                except Exception as e:
                    print(f"Warning: Error cleaning up viewer: {e}")
            
            success = self.load_file(self.filename, data)
            
            if success and self.app and hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage("Backup restored successfully")
                
            return success
            
        except Exception as e:
            print(f"Failed to restore backup: {e}")
            QMessageBox.critical(None, "Error", f"Failed to restore backup: {e}")
            return False


class REasyEditorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("REasy Editor v0.2.3")
        set_app_icon(self)

        try:
            self.settings = load_settings()
        except:
            self.settings = DEFAULT_SETTINGS.copy()

        if "keyboard_shortcuts" not in self.settings:
            self.settings["keyboard_shortcuts"] = DEFAULT_SETTINGS["keyboard_shortcuts"].copy()
        else:
            for key, value in DEFAULT_SETTINGS["keyboard_shortcuts"].items():
                if key not in self.settings["keyboard_shortcuts"]:
                    self.settings["keyboard_shortcuts"][key] = value

        self.dark_mode = self.settings.get("dark_mode", False)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0) 
        main_layout.setSpacing(0) 

        self.notebook = CustomNotebook()
        self.notebook.app_instance = self
        main_layout.addWidget(self.notebook)

        self.tabs = weakref.WeakValueDictionary()

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

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            urls = event.mimeData().urls()
            if any(ext in url.toLocalFile().lower() for url in urls for ext in (".uvar", ".scn", ".user", ".pfb", ".msg", ".efx", ".efxpreset")):
                event.acceptProposedAction()

    def dropEvent(self, event):
        for url in event.mimeData().urls():
            file_path = url.toLocalFile()
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

        file_menu = menubar.addMenu("File")

        open_act = QAction("Open...", self)
        open_act.setObjectName("file_open")
        open_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_open", "Ctrl+O")))
        open_act.triggered.connect(self.on_open)
        file_menu.addAction(open_act)

        save_act = QAction("Save", self)
        save_act.setObjectName("file_save")
        save_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_save", "Ctrl+S")))
        save_act.triggered.connect(self.on_direct_save)
        file_menu.addAction(save_act)

        save_as_act = QAction("Save As...", self)
        save_as_act.setObjectName("file_save_as")
        save_as_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_save_as", "Ctrl+Shift+S")))
        save_as_act.triggered.connect(self.on_save)
        file_menu.addAction(save_as_act)
        
        restore_backup_act = QAction("Restore Backup...", self)
        restore_backup_act.triggered.connect(self.on_restore_backup)
        file_menu.addAction(restore_backup_act)

        reload_act = QAction("Reload", self)
        reload_act.setObjectName("file_reload")
        reload_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_reload", "Ctrl+R")))
        reload_act.triggered.connect(self.reload_file)
        file_menu.addAction(reload_act)

        close_tab_act = QAction("Close Tab", self)
        close_tab_act.setObjectName("file_close_tab")
        close_tab_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("file_close_tab", "Ctrl+W")))
        close_tab_act.triggered.connect(self.close_current_tab)
        file_menu.addAction(close_tab_act)

        file_menu.addSeparator()

        settings_act = QAction("Settings", self)
        settings_act.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_act)

        exit_act = QAction("Exit", self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        edit_menu = menubar.addMenu("Edit")
        copy_act = QAction("Copy", self)
        copy_act.setObjectName("edit_copy")
        copy_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("edit_copy", "Ctrl+C")))
        copy_act.triggered.connect(self.copy_to_clipboard)
        edit_menu.addAction(copy_act)

        find_menu = menubar.addMenu("Find")

        find_act = QAction("Find", self)
        find_act.setObjectName("find_search")
        find_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search", "Ctrl+F")))
        find_act.triggered.connect(self.open_find_dialog)
        find_menu.addAction(find_act)

        guid_act = QAction("Search Directory for GUID", self)
        guid_act.setObjectName("find_search_guid")
        guid_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_guid", "Ctrl+G")))
        guid_act.triggered.connect(self.search_directory_for_guid)
        find_menu.addAction(guid_act)

        text_act = QAction("Search Directory for Text", self)
        text_act.setObjectName("find_search_text")
        text_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_text", "Ctrl+T")))
        text_act.triggered.connect(self.search_directory_for_text)
        find_menu.addAction(text_act)

        num_act = QAction("Search Directory for Number", self)
        num_act.setObjectName("find_search_number")
        num_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_number", "Ctrl+N")))
        num_act.triggered.connect(self.search_directory_for_number)
        find_menu.addAction(num_act)
        
        hex_act = QAction("Search Directory for Hex", self)
        hex_act.setObjectName("find_search_hex")
        hex_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("find_search_hex", "Ctrl+H")))
        hex_act.triggered.connect(self.search_directory_for_hex)
        find_menu.addAction(hex_act)

        view_menu = menubar.addMenu("View")

        dark_act = QAction("Toggle Dark Mode", self)
        dark_act.setObjectName("view_dark_mode")
        dark_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_dark_mode", "Ctrl+D")))
        dark_act.triggered.connect(self.toggle_dark_mode)
        view_menu.addAction(dark_act)

        prev_tab_act = QAction("Previous Tab", self)
        prev_tab_act.setObjectName("view_prev_tab")
        prev_tab_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_prev_tab", "PgDown")))
        prev_tab_act.triggered.connect(self.goto_previous_tab)
        view_menu.addAction(prev_tab_act)

        next_tab_act = QAction("Next Tab", self)
        next_tab_act.setObjectName("view_next_tab")
        next_tab_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_next_tab", "PgUp")))
        next_tab_act.triggered.connect(self.goto_next_tab)
        view_menu.addAction(next_tab_act)

        dbg_act = QAction("Toggle Debug Console", self)
        dbg_act.setObjectName("view_debug_console")
        dbg_act.setShortcut(QKeySequence(self.settings.get("keyboard_shortcuts", {}).get("view_debug_console", "Ctrl+Shift+D")))
        dbg_act.triggered.connect(
            lambda: self.toggle_debug_console(
                not self.settings.get("show_debug_console", True)
            )
        )
        view_menu.addAction(dbg_act)

        tools_menu = menubar.addMenu("Tools")
        guid_conv_act = QAction("GUID Converter", self)
        guid_conv_act.triggered.connect(self.open_guid_converter)
        tools_menu.addAction(guid_conv_act)

        hash_calc_act = QAction("Hash Calculator", self)
        hash_calc_act.triggered.connect(self.open_hash_calculator)
        tools_menu.addAction(hash_calc_act)

        help_menu = menubar.addMenu("Help")
        about_act = QAction("About", self)
        about_act.triggered.connect(self.show_about)
        help_menu.addAction(about_act)
        
        donate_menu = menubar.addMenu("Donate")
        donate_act = QAction("Support REasy", self)
        donate_act.triggered.connect(self.show_donate_dialog)
        donate_menu.addAction(donate_act)

    def set_dark_mode(self, state):
        self.dark_mode = state
        self.settings["dark_mode"] = state

        self.save_settings()

        if state:
            colors = {
                "bg": "#2b2b2b",
                "tree_bg": "#2b2b2b",
                "fg": "white",
                "highlight": "rgba(255, 133, 51, 0.5)",
                "input_bg": "#3b3b3b",
                "disabled_bg": "#404040",
                "border": "#555555",
            }
        else:
            colors = {
                "bg": "#ffffff",
                "tree_bg": "#ffffff",
                "fg": "#000000",
                "highlight": "#ff851b",
                "input_bg": "#ffffff",
                "disabled_bg": "#f0f0f0",
                "border": "#cccccc",
            }

        self.setStyleSheet(
            f"""
            QMainWindow, QDialog, QWidget {{ 
                background-color: {colors['bg']}; 
                color: {colors['fg']}; 
            }}
            QTreeView {{
                background-color: {colors['tree_bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
            }}
            QTreeView::item:selected {{
                background-color: {colors['highlight']};
            }}
            QLineEdit, QPlainTextEdit {{
                background-color: {colors['input_bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
                padding: 2px;
            }}
            QPushButton {{
                background-color: {colors['input_bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
                padding: 5px;
                min-width: 80px;
            }}
            QPushButton:disabled {{
                background-color: {colors['disabled_bg']};
            }}
            QLabel, QCheckBox {{
                color: {colors['fg']};
            }}
            QCheckBox::indicator {{
                width: 15px;
                height: 15px;
                background-color: {colors['input_bg']};
                border: 1px solid {colors['border']};
                border-radius: 2px;
            }}
            QCheckBox::indicator:checked {{
                background-color: {colors['highlight']};
                border-color: {colors['highlight']};
            }}
            QMenuBar, QMenu, QTabWidget::pane, QStatusBar, QProgressDialog, QListWidget {{
                background-color: {colors['bg']};
                color: {colors['fg']};
                border: 1px solid {colors['border']};
            }}
            QMenuBar::item:selected, QMenu::item:selected, QTabBar::tab:selected, QListWidget::item:selected {{
                background-color: {colors['highlight']};
            }}
        """
        )

        self.notebook.set_dark_mode(state)

        new_viewers = {}

        for tab in self.tabs.values():
            if hasattr(tab, "dark_mode"):
                tab.dark_mode = state

            if tab.handler:
                tab.handler.dark_mode = state
                if hasattr(tab.handler, "create_viewer"):
                    try:
                        viewer = tab.handler.create_viewer()
                        if viewer:
                            new_viewers[tab] = viewer
                    except Exception as e:
                        print(f"Error creating new viewer: {e}")

        for tab, new_viewer in new_viewers.items():
            try:
                layout = tab.notebook_widget.layout()
                if not layout:
                    continue

                try:
                    status_text = tab.status_label.text()
                except (RuntimeError, AttributeError):
                    status_text = "Ready"

                new_status_label = QLabel(status_text)

                while layout.count():
                    item = layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()

                layout.addWidget(new_viewer)
                layout.addWidget(new_status_label)

                tab.viewer = new_viewer
                tab.status_label = new_status_label

                if (
                    hasattr(tab, "_find_dialog")
                    and tab._find_dialog
                    and tab._find_dialog.isVisible()
                ):
                    old_dialog = tab._find_dialog
                    tab._find_dialog = None
                    old_dialog.close()
                    QTimer.singleShot(100, tab.open_find_dialog)

            except Exception as e:
                print(f"Error updating viewer: {e}")

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
        for tab in list(self.tabs.values()):
            if tab.modified:
                ans = QMessageBox.question(
                    self,
                    "Unsaved Changes",
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
            if hasattr(tab, 'handler'):
                if isinstance(tab.handler, RszHandler):
                    tab.handler.set_advanced_mode(self.settings.get("show_rsz_advanced", True))
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
        game_version_combo.addItem("RE4") 
        game_version_combo.addItem("RE2") 
        game_version_combo.addItem("RE2RT") 
        game_version_combo.addItem("RE8") 
        game_version_combo.addItem("RE3") 
        game_version_combo.addItem("RE7") 
        game_version_combo.addItem("RE7RT") 
        game_version_combo.addItem("MHWS") 
        game_version_combo.addItem("DMC5") 
        game_version_combo.addItem("SF6") 
        game_version_combo.addItem("O2") 
        game_version_combo.addItem("DD2") 
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
            self.settings["keyboard_shortcuts"] = shortcuts
            
            old_version = self.settings.get("game_version", "RE4")
            new_version = game_version_combo.currentText()
            self.settings["game_version"] = new_version
            
            selected_index = translation_combo.currentIndex()
            if selected_index >= 0:
                lang_code = translation_combo.itemData(selected_index)
                self.settings["translation_target_language"] = lang_code
            
            if self.dark_mode != dark_box.isChecked():
                self.set_dark_mode(dark_box.isChecked())

            self.toggle_debug_console(debug_box.isChecked())
            
            self.update_from_app_settings()
            
            self.apply_keyboard_shortcuts()

            self.save_settings()
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

    def search_directory_for_number(self):
        search_directory_for_type(self, 'number', create_search_dialog, create_search_patterns)

    def search_directory_for_text(self):
        search_directory_for_type(self, 'text', create_search_dialog, create_search_patterns)

    def search_directory_for_guid(self):
        search_directory_for_type(self, 'guid', create_search_dialog, create_search_patterns)

    def search_directory_for_hex(self):
        search_directory_for_type(self, 'hex', create_search_dialog, create_search_patterns)

    def open_find_dialog(self):
        active = self.get_active_tab()
        if active:
            active.open_find_dialog()
        else:
            QMessageBox.critical(self, "Error", "No active tab for searching.")

    def add_tab(self, filename=None, data=None):
        if filename:
            abs_fn = os.path.abspath(filename)
            for tab in self.tabs.values():
                if tab.filename and os.path.abspath(tab.filename) == abs_fn:
                    if tab.modified:
                        ans = QMessageBox.question(
                            self,
                            "Unsaved Changes",
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
            tab.parent_notebook = self.notebook
            tab_label = os.path.basename(filename) if filename else "Untitled"
            tab_index = self.notebook.addTab(tab.notebook_widget, tab_label)
            self.tabs[tab.notebook_widget] = tab
            self.notebook.setCurrentWidget(tab.notebook_widget)
            
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to open file: {str(e)}")
            if tab and hasattr(tab, 'notebook_widget') and tab.notebook_widget:
                try:
                    tab.notebook_widget.deleteLater()
                except:
                    pass

    def get_active_tab(self):
        current_widget = self.notebook.currentWidget()
        return self.tabs.get(current_widget, None)

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
            "Open File",
            "",
            "RE Files (*.uvar* *.scn* *.user* *.pfb* *.msg* *.efx*);;SCN Files (*.scn*);;User Files (*.user*);;UVAR Files (*.uvar*);;PFB Files (*.pfb*);;MSG Files (*.msg*);;EFX Files (*.efx*);;All Files (*.*)",
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
        current_index = self.notebook.currentIndex()
        if current_index >= 0:
            self.close_tab(current_index)

    def close_tab(self, index):
        widget = self.notebook.widget(index)
        tab = self.tabs.get(widget)
        
        if tab and tab.modified:
            ans = QMessageBox.question(
                self,
                "Unsaved Changes",
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
            
        if tab is not None:
            tab.tree = None
            
        self.notebook.removeTab(index)

    def copy_to_clipboard(self):
        active = self.get_active_tab()
        if active:
            active.copy_to_clipboard()
        else:
            QMessageBox.critical(self, "Error", "No active tab.")

    def show_about(self):
        create_about_dialog(self)

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

