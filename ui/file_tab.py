"""Per-file editor tab and its handler/viewer lifecycle."""

import os

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, Qt
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QInputDialog,
    QMainWindow,
    QMessageBox,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from file_handlers.factory import get_handler_for_data
from file_handlers.msg.msg_handler import MsgHandler
from file_handlers.rsz.rsz_handler import RszHandler
from services.backup_store import create_backup, find_backups
from ui.better_find_dialog import BetterFindDialog
from ui.highlight_delegate import HighlightDelegate
from ui.highlight_manager import HighlightManager


NO_FILE_LOADED_STR = QT_TRANSLATE_NOOP("FileTab", "No file loaded")
UNSAVED_CHANGES_STR = QT_TRANSLATE_NOOP("FileTab", "Unsaved changes")


class FileTab:


    def __init__(self, parent_notebook, filename=None, data=None, app=None, pak_source_path=None, pak_project_dir=None, handler=None):
        self.parent_notebook = parent_notebook
        self.notebook_widget = QWidget()
        self.notebook_widget.parent_tab = self
        self.filename = filename
        self.handler = None
        self.metadata_map = {}
        self.modified = False
        self.app = app
        self.viewer = None
        self.pak_source_path: str | None = pak_source_path
        self.pak_project_dir: str | None = pak_project_dir
        self.pak_data_loader = None

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

        self.tree.doubleClicked.connect(
            self.on_double_click
        )
        self.tree.clicked.connect(
            self.on_tree_click
        )
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)

        self.highlight_manager = HighlightManager()
        self.highlight_delegate = HighlightDelegate(self.highlight_manager, self.tree)
        self.tree.setItemDelegate(self.highlight_delegate)
        self.initial_load_complete = False

        if data is not None:
            self.initial_load_complete = self.load_file(filename, data, handler=handler)
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

    @staticmethod
    def tr(text: str) -> str:
        return QCoreApplication.translate("FileTab", text)

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

    def _prepare_handler(self, data, handler=None):
        try:
            handler = handler or get_handler_for_data(data, self.filename or "")

            if isinstance(handler, RszHandler):
                handler.set_game_version(self.app.settings.get("game_version", "RE4"))
                handler.show_advanced = self.app.settings.get("show_rsz_advanced", True)
                handler.confirmation_prompt = self.app.settings.get("confirmation_prompt", True)

            handler.filepath = self.filename or ""

            handler.refresh_tree_callback = self.refresh_tree
            handler.app = self.app
            handler.highlight_manager = self.highlight_manager

            if hasattr(handler, "setup_tree"):
                handler.setup_tree(self.tree)

            return handler

        except Exception as e:
            raise ValueError(f"Handler setup failed: {e}")

    def load_file(self, filename, data, *, replace_scene_document=False, handler=None):
        layout = self.notebook_widget.layout()
        suppress_error_dialog = False

        try:
            old_handler = self.handler
            old_viewer = self.viewer
            old_filename = self.filename

            self.filename = filename
            self.handler = None
            self.viewer = None

            self.handler = self._prepare_handler(data, handler)

            try:
                if isinstance(self.handler, RszHandler):
                    self.handler.read(data, validate_type_registry=self.app.settings.get("verify_rsz_crc_on_open", True))
                    if self.app and hasattr(self.app, "scenes"):
                        self.app.scenes.attach_tab_document(self, replace=replace_scene_document)
                else:
                    self.handler.read(data)
            except Exception as e:
                suppress_error_dialog = getattr(
                    self.handler, "suppress_load_error_dialog", False
                )
                raise ValueError(f"Failed to read file data: {e}")

            new_viewer = None
            try:
                new_viewer = self.handler.create_viewer()
            except Exception as e:
                print(f"Viewer creation failed: {e}")

            if old_viewer:
                self._cleanup_viewer(old_viewer, old_handler)
            self._cleanup_layout(layout)
            if old_handler and old_handler is not self.handler:
                self._dispose_handler(old_handler)

            self.viewer = new_viewer
            if self.viewer:
                layout.addWidget(self.viewer)
                self.viewer.modified_changed.connect(self._on_viewer_modified)
            else:
                layout.addWidget(self.tree)
                if not isinstance(self.handler, RszHandler):
                    self.refresh_tree()

            if self.app and getattr(self.app, "status_bar", None):
                self.app.status_bar.showMessage(
                    self.tr("Loaded: {filename}").format(filename=filename), 4000
                )

            self.initial_load_complete = True
            return True

        except Exception as e:
            self.initial_load_complete = False
            failed_handler = self.handler
            self.handler = old_handler
            self.viewer = old_viewer
            self.filename = old_filename
            if failed_handler and failed_handler is not old_handler:
                self._dispose_handler(failed_handler)

            if not suppress_error_dialog:
                QMessageBox.critical(
                    None,
                    self.tr("Error"),
                    self.tr("Failed to load file: {error}").format(error=e),
                )
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

    def on_tree_click(self, index):
        if not self.highlight_manager.enabled:
            return
        if not index.isValid():
            return

        item_id = self._get_index_identifier(index)

        if self.highlight_manager.is_item_highlighted(item_id):
            self.highlight_manager.remove_highlighted_item(item_id)
        else:
            self.highlight_manager.add_highlighted_item(item_id)

        self.tree.viewport().update()

    def _get_index_identifier(self, index):
        path = []
        current = index
        while current.isValid():
            path.append(current.row())
            current = current.parent()
        return tuple(reversed(path))

    def on_double_click(self, index):
        if not self.tree or not self.handler or not self.handler.supports_tree_editing():
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
            self.tree, self.tr("Edit Value"), self.tr("Value:"), text=str(old_val)
        )

        if ok and new_val != old_val:
            try:
                if self.handler.validate_edit(meta, new_val, old_val):
                    self.handler.handle_edit(meta, new_val, old_val, None, self.tree)
                    self.modified = True
                    self.update_tab_title()
            except Exception as e:
                QMessageBox.critical(
                    None,
                    self.tr("Error"),
                    self.tr("Failed to update value: {error}").format(error=e),
                )

    def on_tree_edit(self, top_left, bottom_right, roles):
        if not self.handler or not self.handler.supports_tree_editing():
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
            QMessageBox.critical(
                None,
                self.tr("Error"),
                self.tr("Invalid value: {error}").format(error=e),
            )
            item.setData(str(meta.get("original_value", "")), Qt.UserRole)

    def handle_file_save(self, file_path):
        try:
            data = None
            if hasattr(self.handler, "rebuild"):
                data = self.handler.rebuild()
            elif self.viewer and hasattr(self.viewer, "rebuild"):
                data = self.viewer.rebuild()

            if not data:
                raise ValueError(self.tr("No rebuild method available"))

            if self.app and self.app.settings.get("backup_on_save", True):
                self.create_backup(file_path, data)

            with open(file_path, "wb") as f:
                f.write(data)

            self.filename = file_path
            self.pak_source_path = None
            self.pak_data_loader = None
            self.modified = False
            self.update_tab_title()
            if self.app and hasattr(self.app, "scenes"):
                self.app.scenes.document_store.clear_handler(self.handler)
                self.app.scenes.refresh_dirty_flags()

            if self.app and hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage(
                    self.tr("Saved: {path}").format(path=file_path), 3000
                )

            return True

        except Exception as e:
            QMessageBox.critical(None, self.tr("Save Error"), str(e))
            return False

    def discard_changes(self):
        if self.app and hasattr(self.app, "scenes") and self.handler:
            self.app.scenes.document_store.discard_handler(self.handler)
        self.modified = False
        if self.viewer and hasattr(self.viewer, "modified"):
            self.viewer.modified = False
        self.update_tab_title()

    def create_backup(self, file_path, data):
        try:
            backup_path = create_backup(file_path, data)
            if hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage(
                    self.tr("Backup created: {path}").format(path=backup_path), 2000
                )

        except Exception as e:
            print(f"Backup creation failed: {e}")

    def direct_save(self):
        """Save directly to the current file without prompting"""
        if not self.handler:
            QMessageBox.critical(None, self.tr("Error"), self.tr(NO_FILE_LOADED_STR))
            return False
        session = self.app.project_workspace.sessions.session_for_tab(self)
        if self.pak_source_path and not self.app.proj_dock.prepare_pak_tab_direct_save(
            self, session.path if session else None
        ):
            return False
        if not self.filename:
            return self.on_save()
        return self.handle_file_save(self.filename)

    def on_save(self):
        if not self.handler:
            QMessageBox.critical(None, self.tr("Error"), self.tr(NO_FILE_LOADED_STR))
            return False

        file_path, _ = QFileDialog.getSaveFileName(
            self.notebook_widget,
            self.tr("Save File As"),
            self.filename or "",
            "All Files (*.*)",
        )

        if file_path:
            return self.handle_file_save(file_path)
        return False

    def reload_file(self):
        if not self.filename:
            QMessageBox.critical(None, self.tr("Error"), self.tr("No file currently loaded."))
            return

        if self.modified:
            ans = QMessageBox.question(
                None,
                self.tr(UNSAVED_CHANGES_STR),
                self.tr("File {} has unsaved changes.\nSave before reloading?").format(
                    os.path.basename(self.filename)
                ),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if ans == QMessageBox.Cancel:
                return
            if ans == QMessageBox.Yes:
                self.on_save()

        try:
            data = self._read_reload_data()
            if data is None:
                raise FileNotFoundError(self.tr(
                    "Unable to read source data for: {path}"
                ).format(path=self.filename))

            success = self.load_file(self.filename, data, replace_scene_document=True)
            if success and self.app and hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage(
                    self.tr("Reloaded: {path}").format(path=self.filename), 2000
                )

            self.modified = False
            if self.viewer and hasattr(self.viewer, "modified"):
                self.viewer.modified = False
            self.update_tab_title()
            if self.app and hasattr(self.app, "scenes"):
                self.app.scenes.sync_tab(self, reloaded=True)


        except Exception as e:
            QMessageBox.critical(
                None, self.tr("Error"), self.tr("Failed to reload file: {}").format(e)
            )
            import traceback

            traceback.print_exc()

    def _read_reload_data(self) -> bytes | None:
        if self.filename and os.path.isfile(self.filename):
            with open(self.filename, "rb") as f:
                return f.read()
        if self.pak_source_path and callable(self.pak_data_loader):
            data = self.pak_data_loader(self.pak_source_path)
            if data is not None:
                return data
        if not self.app or not self.filename:
            return None
        from utils.resource_file_utils import resolve_app_resource_data

        hit = resolve_app_resource_data(
            self.app,
            self.filename,
            None,
            allow_selection_dialog=False,
        )
        return hit[1] if hit else None

    def open_find_dialog(self):
        if isinstance(self.handler, MsgHandler):
                QMessageBox.information(self.notebook_widget, self.tr("Search in MSG"), self.tr("MSG files have a built-in search at the top of the editor. Please use that search bar."))
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
        return find_backups(self.filename) if self.filename else []

    def restore_backup(self, backup_path):
        """Restore the selected backup file"""
        try:
            with open(backup_path, "rb") as f:
                data = f.read()

            success = self.load_file(self.filename, data, replace_scene_document=True)

            if success and self.app and hasattr(self.app, "status_bar"):
                self.app.status_bar.showMessage(
                    self.tr("Backup restored successfully")
                )

            return success

        except Exception as e:
            QMessageBox.critical(
                None,
                self.tr("Error"),
                self.tr("Failed to restore backup: {}").format(e),
            )
            return False

    def replace_viewer(self, viewer):
        """Replace the active viewer while preserving the tab's tree fallback."""
        self._cleanup_viewer()
        layout = self.notebook_widget.layout()
        self._cleanup_layout(layout)
        self.viewer = viewer
        if viewer:
            layout.addWidget(viewer)
            viewer.modified_changed.connect(self._on_viewer_modified)
        else:
            layout.addWidget(self.tree)

    def _cleanup_viewer(self, viewer=None, handler=None):
        target = viewer if viewer is not None else self.viewer
        owner = handler if handler is not None else self.handler
        try:
            if target:
                cleanup_fn = getattr(target, "cleanup", None)
                if callable(cleanup_fn):
                    try:
                        cleanup_fn()
                    except Exception as e:
                        print(f"Warning: Error running viewer cleanup: {e}")
            if hasattr(target, "modified_changed"):
                try:
                    target.modified_changed.disconnect(self._on_viewer_modified)
                except (TypeError, RuntimeError):
                    pass
                except Exception as e:
                    print(f"Error disconnecting modified_changed signal: {e}")

            layout = self.notebook_widget.layout()
            if layout:
                for i in range(layout.count()):
                    item = layout.itemAt(i)
                    if item.widget() == target:
                        target.setParent(None)
                        break

            if target:
                if owner and getattr(owner, "_viewer", None) is target:
                    owner._viewer = None
                target.deleteLater()
            self._invalidate_search_dialog_tree()
            if target is self.viewer:
                self.viewer = None
        except Exception as e:
            print(f"Warning: Error cleaning up viewer: {e}")

    def _dispose_handler(self, handler=None):
        target = handler if handler is not None else self.handler
        if target:
            if self.app and hasattr(self.app, "scenes"):
                self.app.scenes.document_store.detach_handler(target)
            try:
                cleanup_fn = getattr(target, "cleanup", None)
                if callable(cleanup_fn):
                    cleanup_fn()
            except Exception as e:
                print(f"Warning: Error running handler cleanup: {e}")
            try:
                delete_later = getattr(target, "deleteLater", None)
                if callable(delete_later):
                    delete_later()
            except RuntimeError:
                pass
            except Exception as e:
                print(f"Warning: Error scheduling handler deletion: {e}")
            if target is self.handler:
                self.handler = None

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

        if self.tree:
            try:
                self.tree.deleteLater()
            except Exception:
                pass
            self.tree = None

        self._dispose_handler()

        self._invalidate_search_dialog_tree()

        self.metadata_map.clear()
        self.viewer = None

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
