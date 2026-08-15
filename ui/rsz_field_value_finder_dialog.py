"""
RSZ Field Value Finder Dialog

This dialog provides a UI for searching RSZ field values across multiple files.
"""

import glob
import os
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tools.rsz_field_value_finder import (
    format_value,
    is_rsz_path,
    replace_file_values,
    scan_file,
    value_matches,
)
from utils.type_registry import TypeRegistry


def _path_key(path):
    return os.path.normcase(os.path.abspath(os.fspath(path)))


class SearchWorkerThread(QThread):
    progress_update = Signal(int, int)
    file_found = Signal(str, list)
    search_complete = Signal()
    error_occurred = Signal(str)
    
    def __init__(self, directory, type_id, type_registry, recursive=True):
        super().__init__()
        self.directory = directory
        self.type_id = type_id
        self.type_registry = type_registry
        self.recursive = recursive
        self.cancelled = False
        self.files_scanned = 0
        self.failures = []
        
    def cancel(self):
        self.cancelled = True
        
    def run(self):
        try:
            path = Path(self.directory)
            file_iter = path.rglob('*') if self.recursive else path.glob('*')
            candidate_files = [file for file in file_iter if file.is_file() and is_rsz_path(file)]
            self.files_scanned = len(candidate_files)
            
            for idx, filepath in enumerate(candidate_files):
                if self.cancelled:
                    break
                    
                self.progress_update.emit(idx + 1, self.files_scanned)
                
                results = scan_file(
                    filepath,
                    self.type_id,
                    None,
                    self.type_registry,
                    self.failures,
                )

                if results:
                    self.file_found.emit(str(filepath), results)
            
            self.search_complete.emit()
            
        except Exception as e:
            self.error_occurred.emit(str(e))


class RszFieldValueFinderDialog(QDialog):
    
    def __init__(self, parent=None, settings=None):
        super().__init__(parent)
        self.settings = settings or {}
        self.type_registry = None
        self.search_thread = None
        self.results = {}
        self.all_results = {}
        self.last_selected_item = None
        self.progress_dialog: Optional[QProgressDialog] = None

        self.setWindowTitle(self.tr("Find/Replace RSZ Field Value"))
        self.setMinimumSize(850, 560)
        self.resize(1000, 650)
        self.setup_ui()
        if self.json_path_edit.text():
            self.load_type_registry()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        search_group = QGroupBox(self.tr("Search"))
        search_layout = QGridLayout(search_group)
        search_layout.setColumnStretch(1, 1)

        self.json_path_edit = QLineEdit(self.settings.get("rcol_json_path", ""))
        search_layout.addWidget(QLabel(self.tr("Type Registry:")), 0, 0)
        search_layout.addWidget(self.json_path_edit, 0, 1)
        search_layout.addWidget(
            QPushButton(self.tr("Browse..."), clicked=self.browse_json_path), 0, 2
        )
        search_layout.addWidget(
            QPushButton(self.tr("Reload"), clicked=self.load_type_registry), 0, 3
        )

        self.dir_edit = QLineEdit()
        search_layout.addWidget(QLabel(self.tr("Directory:")), 1, 0)
        search_layout.addWidget(self.dir_edit, 1, 1)
        search_layout.addWidget(
            QPushButton(self.tr("Browse..."), clicked=self.browse_directory), 1, 2
        )
        self.recursive_check = QCheckBox(self.tr("Recursive"))
        self.recursive_check.setChecked(True)
        search_layout.addWidget(self.recursive_check, 1, 3)

        self.type_combo = QComboBox()
        self.type_combo.setEditable(True)
        self.type_combo.currentTextChanged.connect(self.on_type_changed)
        search_layout.addWidget(QLabel(self.tr("Type:")), 2, 0)
        search_layout.addWidget(self.type_combo, 2, 1)
        self.type_id_label = QLabel(self.tr("ID: -"))
        search_layout.addWidget(self.type_id_label, 2, 2)
        search_button = QPushButton(self.tr("Search"), clicked=self.start_search)
        search_button.setDefault(True)
        search_layout.addWidget(search_button, 2, 3)
        layout.addWidget(search_group)

        options_tabs = QTabWidget()
        options_tabs.setMinimumWidth(290)
        options_tabs.setMaximumWidth(340)

        fields_page = QWidget()
        fields_layout = QVBoxLayout(fields_page)
        fields_layout.setContentsMargins(8, 8, 8, 8)
        self.fields_list = QListWidget()
        self.fields_list.setSelectionMode(QListWidget.NoSelection)
        self.fields_list.itemChanged.connect(self.on_display_fields_changed)
        self.fields_list.setToolTip(self.tr("Checked fields are shown in the details pane."))
        fields_layout.addWidget(self.fields_list)
        options_tabs.addTab(fields_page, self.tr("Fields"))

        filters_page = QWidget()
        filters_layout = QVBoxLayout(filters_page)
        filters_layout.setContentsMargins(8, 8, 8, 8)

        self.constraint_field_combo = QComboBox()
        self.constraint_value_edit = QLineEdit()
        self.constraint_value_edit.setPlaceholderText(self.tr("Text to match"))
        self.replace_value_edit = QLineEdit()
        filter_form = QFormLayout()
        filter_form.addRow(self.tr("Field:"), self.constraint_field_combo)
        filter_form.addRow(self.tr("Find:"), self.constraint_value_edit)
        filter_form.addRow(self.tr("Replace:"), self.replace_value_edit)
        filters_layout.addLayout(filter_form)

        filter_actions = QHBoxLayout()
        filter_actions.addWidget(
            QPushButton(self.tr("Add Filter"), clicked=self.add_constraint)
        )
        filter_actions.addWidget(
            QPushButton(self.tr("Replace All"), clicked=self.replace_all)
        )
        filters_layout.addLayout(filter_actions)

        self.constraints_list = QListWidget()
        filters_layout.addWidget(self.constraints_list, 1)
        filters_layout.addWidget(
            QPushButton(self.tr("Remove Filter"), clicked=self.remove_constraint)
        )
        options_tabs.addTab(filters_page, self.tr("Find / Replace"))

        results_group = QGroupBox(self.tr("Results"))
        results_layout = QVBoxLayout(results_group)
        self.results_splitter = QSplitter(Qt.Vertical)
        self.results_splitter.setChildrenCollapsible(False)

        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels([self.tr("File"), self.tr("Instances")])
        self.file_tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.file_tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.file_tree.itemExpanded.connect(self.on_file_expanded)
        self.file_tree.itemClicked.connect(self.on_item_selected)
        self.file_tree.itemDoubleClicked.connect(self.open_result_item)
        self.results_splitter.addWidget(self.file_tree)

        self.details_tree = QTreeWidget()
        self.details_tree.setHeaderLabels([self.tr("Field"), self.tr("Value")])
        self.details_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.details_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.details_tree.itemDoubleClicked.connect(self.open_result_item)
        self.results_splitter.addWidget(self.details_tree)
        self.results_splitter.setStretchFactor(0, 3)
        self.results_splitter.setStretchFactor(1, 2)
        self.results_splitter.setSizes([360, 220])
        results_layout.addWidget(self.results_splitter)

        self.main_splitter = QSplitter(Qt.Horizontal)
        self.main_splitter.setChildrenCollapsible(False)
        self.main_splitter.addWidget(options_tabs)
        self.main_splitter.addWidget(results_group)
        self.main_splitter.setStretchFactor(0, 0)
        self.main_splitter.setStretchFactor(1, 1)
        self.main_splitter.setSizes([300, 700])
        layout.addWidget(self.main_splitter, 1)

        footer = QHBoxLayout()
        self.status_bar = QLabel(self.tr("Ready"))
        self.status_bar.setStyleSheet("QLabel { color: gray; }")
        footer.addWidget(self.status_bar, 1)
        button_box = QDialogButtonBox(QDialogButtonBox.Close)
        button_box.rejected.connect(self.reject)
        footer.addWidget(button_box)
        layout.addLayout(footer)
        
    def browse_json_path(self):
        current_path = self.json_path_edit.text()

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select JSON Type Data File"),
            os.path.dirname(current_path) if current_path else "",
            "JSON Files (*.json);;All Files (*.*)"
        )

        if file_path:
            self.json_path_edit.setText(file_path)
            self.load_type_registry()

    def browse_directory(self):
        path = QFileDialog.getExistingDirectory(
            self,
            self.tr("Select Directory to Search"),
            self.dir_edit.text()
        )
        if path:
            self.dir_edit.setText(path)
            
    def load_type_registry(self):
        json_path = self.json_path_edit.text()
        if not json_path:
            QMessageBox.warning(
                self, self.tr("Warning"), self.tr("Please specify a JSON path")
            )
            return

        if os.path.isdir(json_path):
            possible_files = ['rsz.json', 'type_data.json', 'types.json']
            json_file = None
            for filename in possible_files:
                test_path = os.path.join(json_path, filename)
                if os.path.exists(test_path):
                    json_file = test_path
                    break
            
            if not json_file:
                json_files = glob.glob(os.path.join(json_path, '*.json'))
                if json_files:
                    json_file = json_files[0]
                else:
                    QMessageBox.warning(
                        self, self.tr("Warning"),
                        self.tr("No JSON files found in {path}").format(path=json_path),
                    )
                    return
            json_path = json_file
        elif not os.path.exists(json_path):
            QMessageBox.warning(
                self, self.tr("Warning"),
                self.tr("Path does not exist: {path}").format(path=json_path),
            )
            return

        json_path = os.path.abspath(json_path)
        try:
            self.type_registry = TypeRegistry(json_path)
            self.json_path_edit.setText(json_path)
            self.populate_type_combo()
        except Exception as e:
            self.type_registry = None
            self.type_combo.clear()
            self.fields_list.clear()
            QMessageBox.critical(
                self, self.tr("Error"),
                self.tr("Failed to load type registry: {error}").format(error=e),
            )
            
    def populate_type_combo(self):
        if not self.type_registry:
            return
            
        self.type_combo.clear()
        self.type_combo.setEnabled(False)
        
        QApplication.setOverrideCursor(Qt.WaitCursor)
        
        try:
            type_items = []
            processed = 0
            
            for hex_key, type_info in self.type_registry.registry.items():
                if type_info and 'name' in type_info:
                    try:
                        type_id = int(hex_key, 16)
                        type_items.append((type_info['name'], type_id))
                    except ValueError:
                        continue
                
                processed += 1
                if processed % 1000 == 0:
                    QApplication.processEvents()
                    
            type_items.sort(key=lambda x: x[0])
            
            for i in range(0, len(type_items), 100):
                batch = type_items[i:i+100]
                for name, type_id in batch:
                    self.type_combo.addItem(name, type_id)
                QApplication.processEvents()
                
            if type_items:
                self.type_id_label.setText(
                    self.tr("Loaded {count} types").format(count=len(type_items))
                )
        finally:
            QApplication.restoreOverrideCursor()
            self.type_combo.setEnabled(True)
            
    def on_type_changed(self, text):
        if not text or not self.type_registry:
            return

        type_id = self._resolve_type_id(text)

        if type_id is not None:
            self.type_id_label.setText(self.tr("ID: 0x{type_id:08X}").format(type_id=type_id))
            self.populate_fields(type_id)
        else:
            self.type_id_label.setText(self.tr("ID: -"))

    def _resolve_type_id(self, text: str) -> Optional[int]:
        text = text.strip()
        if not text:
            return None

        if text.startswith('0x'):
            try:
                return int(text, 16)
            except ValueError:
                return None

        try:
            return int(text)
        except ValueError:
            for i in range(self.type_combo.count()):
                if self.type_combo.itemText(i) == text:
                    return self.type_combo.itemData(i)
        return None
            
    def populate_fields(self, type_id):
        self.constraint_field_combo.clear()
        self.fields_list.clear()
        
        type_info = self.type_registry.get_type_info(type_id)
        if not type_info or 'fields' not in type_info:
            return
            
        for field in type_info['fields']:
            field_name = field['name']
            field_type = field.get('type', 'Unknown')
            
            self.constraint_field_combo.addItem(field_name, field_name)
            
            item = QListWidgetItem(f"{field_name} ({field_type})")
            item.setData(Qt.UserRole, field_name)
            item.setCheckState(Qt.Checked)
            self.fields_list.addItem(item)
            
    def get_selected_display_fields(self):
        fields = []
        for i in range(self.fields_list.count()):
            item = self.fields_list.item(i)
            if item.checkState() == Qt.Checked:
                fields.append(item.data(Qt.UserRole))
        return fields
    
    def add_constraint(self):
        field = self.constraint_field_combo.currentData()
        value = self.constraint_value_edit.text().strip()
        
        if not field or not value:
            return
        
        constraint_text = self.tr("{field} contains '{value}'").format(
            field=field, value=value
        )
        item = QListWidgetItem(constraint_text)
        item.setData(Qt.UserRole, (field, value))
        self.constraints_list.addItem(item)

        if self.all_results:
            self.apply_filters()
    
    def remove_constraint(self):
        current_item = self.constraints_list.currentItem()
        if current_item:
            row = self.constraints_list.row(current_item)
            self.constraints_list.takeItem(row)
            
            if self.all_results:
                self.apply_filters()

    def replace_all(self):
        field = self.constraint_field_combo.currentData()
        find = self.constraint_value_edit.text()
        replacement = self.replace_value_edit.text()
        if not self.all_results or not field or not find:
            message = (
                self.tr("Run a search first.") if not self.all_results
                else self.tr("Select a field and enter a value to find.")
            )
            QMessageBox.warning(self, self.tr("Replace"), message)
            return

        targets = {
            filepath: {
                iid for _, iid, name, value in results
                if name == field and value_matches(value, find)
            }
            for filepath, results in self.results.items()
        }
        targets = {filepath: ids for filepath, ids in targets.items() if ids}
        if not targets:
            QMessageBox.information(self, self.tr("Replace"), self.tr("No scalar values match."))
            return

        open_paths = {
            _path_key(tab.filename) for tab in getattr(self.parent(), "tabs", {}).values()
            if getattr(tab, "filename", None)
        }
        if open_paths & {_path_key(filepath) for filepath in targets}:
            QMessageBox.warning(
                self, self.tr("Replace"),
                self.tr("Close matching files in the editor before replacing their values."),
            )
            return

        count = sum(map(len, targets.values()))
        prompt = self.tr(
            "Replace {count} values across {files} files? Backups will be created."
        ).format(count=count, files=len(targets))
        if QMessageBox.question(
            self, self.tr("Confirm Replace"), prompt,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) != QMessageBox.Yes:
            return

        progress = QProgressDialog(
            self.tr("Replacing values..."), self.tr("Cancel"), 0, len(targets), self
        )
        progress.setWindowModality(Qt.WindowModal)
        replaced, errors = 0, []
        for index, (filepath, instance_ids) in enumerate(targets.items(), 1):
            if progress.wasCanceled():
                break
            try:
                replaced += replace_file_values(
                    filepath, self.search_thread.type_id, field, find, replacement,
                    self.search_thread.type_registry, instance_ids,
                )
            except ValueError:
                errors.append(self.tr("Invalid replacement for {file}.").format(
                    file=Path(filepath).name
                ))
            except Exception as exc:
                errors.append(f"{Path(filepath).name}: {exc}")
            progress.setValue(index)
            QApplication.processEvents()
        progress.close()

        message = self.tr("Replaced {count} values. Backups were created.").format(
            count=replaced
        ) if replaced else self.tr("No values were replaced.")
        if errors:
            message += "\n\n" + "\n".join(errors[:5])
        show_message = QMessageBox.warning if errors else QMessageBox.information
        show_message(self, self.tr("Replace Complete"), message)
        if replaced:
            self.start_search()
    
    def get_constraints(self):
        constraints = []
        for i in range(self.constraints_list.count()):
            data = self.constraints_list.item(i).data(Qt.UserRole)
            if isinstance(data, tuple) and len(data) == 2:
                constraints.append(data)
        return constraints
    
    def on_display_fields_changed(self, item):
        if self.last_selected_item:
            self.on_item_selected(self.last_selected_item, 0)

    def apply_filters(self):
        if not self.all_results:
            return

        constraints = self.get_constraints()

        self.file_tree.clear()
        self.results.clear()

        for filepath in self.all_results:
            self.apply_filters_for_file(filepath, constraints)

        self.update_tree_display(constraints)

    def apply_filters_for_file(self, filepath, constraints=None):
        if filepath not in self.all_results:
            return

        all_results = self.all_results[filepath]
        constraints = constraints if constraints is not None else self.get_constraints()

        instances = {}
        for file_path, instance_id, field_name, value in all_results:
            if instance_id not in instances:
                instances[instance_id] = {}
            instances[instance_id][field_name] = value

        filtered_results = []
        self.results.pop(filepath, None)
        for instance_id, fields in instances.items():
            meets_all_constraints = True
            if constraints:
                for constraint_field, constraint_value in constraints:
                    if constraint_field in fields:
                        field_value = fields[constraint_field]
                        formatted_value = str(format_value(field_value)).lower()
                        if constraint_value.lower() not in formatted_value:
                            meets_all_constraints = False
                            break
                    else:
                        meets_all_constraints = False
                        break
            if meets_all_constraints:
                for field_name, value in fields.items():
                    filtered_results.append((filepath, instance_id, field_name, value))
        if filtered_results:
            self.results[filepath] = filtered_results

    def update_tree_display(self, constraints=None):
        self.file_tree.clear()

        total_instances = 0
        for filepath, filtered_results in self.results.items():
            unique_instances = len(set(iid for _, iid, _, _ in filtered_results))
            if unique_instances == 0:
                continue
                
            total_instances += unique_instances
            
            file_item = QTreeWidgetItem(self.file_tree)
            file_item.setText(0, os.path.basename(filepath))
            file_item.setText(1, str(unique_instances))
            file_item.setData(0, Qt.UserRole, filepath)
            placeholder = QTreeWidgetItem(file_item)
            placeholder.setText(0, self.tr("Loading..."))

        if constraints is None:
            constraints = self.get_constraints()
        if constraints:
            self.status_bar.setText(self.tr(
                "Filtered: {instances} instances in {files} files"
            ).format(instances=total_instances, files=len(self.results)))
        else:
            self.status_bar.setText(self.tr(
                "Total: {instances} instances in {files} files"
            ).format(instances=total_instances, files=len(self.results)))
        
    def start_search(self):
        directory = self.dir_edit.text().strip()
        if not os.path.isdir(directory):
            QMessageBox.warning(
                self, self.tr("Warning"), self.tr("Please select a valid directory to search")
            )
            return

        registry_path = self.json_path_edit.text().strip()
        loaded_path = getattr(self.type_registry, "json_path", "")
        if registry_path and _path_key(registry_path) != _path_key(loaded_path):
            self.load_type_registry()
            registry_path = self.json_path_edit.text().strip()
            loaded_path = getattr(self.type_registry, "json_path", "")
        if not self.type_registry or _path_key(registry_path) != _path_key(loaded_path):
            QMessageBox.warning(
                self, self.tr("Warning"), self.tr("Please load the selected type registry")
            )
            return

        if not self.type_combo.currentText():
            QMessageBox.warning(
                self, self.tr("Warning"), self.tr("Please select a type")
            )
            return
            
        text = self.type_combo.currentText()
        type_id = self._resolve_type_id(text) if text else None

        if type_id is None:
            QMessageBox.warning(
                self, self.tr("Warning"), self.tr("Invalid type selection")
            )
            return
            
        self.file_tree.clear()
        self.details_tree.clear()
        self.results.clear()
        self.all_results = {}
        self.last_selected_item = None
        
        self.search_thread = SearchWorkerThread(
            directory,
            type_id,
            self.type_registry,
            self.recursive_check.isChecked()
        )

        self.search_thread.progress_update.connect(self.update_progress)
        self.search_thread.file_found.connect(self.add_file_result)
        self.search_thread.search_complete.connect(self.search_complete)
        self.search_thread.error_occurred.connect(self.search_error)

        self._close_progress_dialog()
        self.progress_dialog = QProgressDialog(
            self.tr("Searching files..."), self.tr("Cancel"), 0, 100, self
        )
        self.progress_dialog.setWindowTitle(self.tr("Search Progress"))
        self.progress_dialog.setWindowModality(Qt.WindowModal)
        self.progress_dialog.canceled.connect(self.cancel_search)
        self.progress_dialog.show()

        self.search_thread.start()

    def update_progress(self, current, total):
        dialog = self.progress_dialog
        if not dialog or not dialog.isVisible():
            return

        dialog.setMaximum(total)
        dialog.setValue(current)
        dialog.setLabelText(self.tr("Searching files... ({current}/{total})").format(
            current=current, total=total
        ))
            
    def add_file_result(self, filepath, results):
        self.all_results[filepath] = results

        constraints = self.get_constraints()
        if constraints:
            self.apply_filters_for_file(filepath, constraints)
            self.update_tree_display(constraints)
            return

        self.results[filepath] = results

        unique_instances = len(set(r[1] for r in results))
        file_item = QTreeWidgetItem(self.file_tree)
        file_item.setText(0, os.path.basename(filepath))
        file_item.setText(1, str(unique_instances))
        file_item.setData(0, Qt.UserRole, filepath)

        placeholder = QTreeWidgetItem(file_item)
        placeholder.setText(0, self.tr("Loading..."))
        
    def on_file_expanded(self, item):
        filepath = item.data(0, Qt.UserRole)
        if not filepath or filepath not in self.results:
            return
            
        item.takeChildren()
        
        instances = {}
        for file_path, instance_id, field_name, value in self.results[filepath]:
            if instance_id not in instances:
                instances[instance_id] = []
            instances[instance_id].append((field_name, value))
        
        for instance_id, fields in instances.items():
            instance_item = QTreeWidgetItem(item)
            instance_item.setText(0, self.tr("Instance {instance_id}").format(
                instance_id=instance_id
            ))
            instance_item.setData(0, Qt.UserRole, (filepath, instance_id))
            
            preview_fields = []
            for field_name, value in fields[:3]:
                formatted = format_value(value)
                preview_fields.append(f"{field_name}: {formatted[:50]}")
            
            if preview_fields:
                instance_item.setText(1, " | ".join(preview_fields))
                

        
    def on_item_selected(self, item, column):
        data = item.data(0, Qt.UserRole)
        if not data or not isinstance(data, tuple):
            return
            
        filepath, instance_id = data
        
        self.last_selected_item = item
        
        self.details_tree.clear()
        
        display_fields = self.get_selected_display_fields()
        
        if filepath not in self.results:
            return
            
        for file_path, iid, field_name, value in self.results[filepath]:
            if iid == instance_id:
                if not display_fields or field_name in display_fields:
                    field_item = QTreeWidgetItem(self.details_tree)
                    field_item.setText(0, field_name)
                    field_item.setText(1, format_value(value))
                    field_item.setData(0, Qt.UserRole, data)

        self.details_tree.resizeColumnToContents(0)

    def open_result_item(self, item, column):
        data = item.data(0, Qt.UserRole)
        if isinstance(data, tuple):
            self.parent().open_rsz_instance(*data, self.search_thread.type_registry)
        
    def _close_progress_dialog(self):
        dialog = self.progress_dialog
        if dialog:
            self.progress_dialog = None
            dialog.close()

    def cancel_search(self):
        if self.search_thread:
            self.search_thread.cancel()
        self._close_progress_dialog()

    def search_complete(self):
        self._close_progress_dialog()

        if self.search_thread.cancelled:
            self.status_bar.setText(self.tr("Search canceled"))
            return

        constraints = self.get_constraints()
        result_source = self.results if constraints else self.all_results

        total_instances = 0
        for results in result_source.values():
            unique_instances = len(set(r[1] for r in results))
            total_instances += unique_instances

        total_files = len(result_source)

        if constraints:
            self.status_bar.setText(
                self.tr(
                    "Search complete (filtered): {instances} instances in {files} files"
                ).format(instances=total_instances, files=total_files)
            )
        else:
            self.status_bar.setText(self.tr(
                "Search complete: {instances} instances in {files} files"
            ).format(instances=total_instances, files=total_files))

        if total_files == 0:
            if constraints and self.all_results:
                message = self.tr("No matches found for the current constraints.")
            elif self.search_thread.files_scanned == 0:
                message = self.tr("No SCN, PFB, or USER files were found in the selected directory.")
            elif len(self.search_thread.failures) == self.search_thread.files_scanned:
                message = self.tr(
                    "None of the {count} files could be parsed. Check that the selected type registry matches these files."
                ).format(count=self.search_thread.files_scanned)
            else:
                message = self.tr(
                    "No matching instances found in {count} files."
                ).format(count=self.search_thread.files_scanned)
                if self.search_thread.failures:
                    message += self.tr(" {count} files could not be parsed.").format(
                        count=len(self.search_thread.failures)
                    )
            QMessageBox.information(self, self.tr("Search Complete"), message)
        
    def search_error(self, error_msg):
        self._close_progress_dialog()

        QMessageBox.critical(
            self, self.tr("Search Error"),
            self.tr("An error occurred: {error}").format(error=error_msg),
        )
