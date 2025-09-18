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
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressDialog,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from tools.rsz_field_value_finder import format_value, scan_file
from utils.type_registry import TypeRegistry

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
        
    def cancel(self):
        self.cancelled = True
        
    def run(self):
        try:
            path = Path(self.directory)
            candidate_files = []
            
            file_iter = path.rglob('*') if self.recursive else path.glob('*')
            for filepath in file_iter:
                if filepath.is_file():
                    filename = filepath.name.lower()
                    is_match = any(filename.endswith(ext) or ('.' + filename.split('.')[-2]) == ext 
                                  for ext in ['.scn', '.pfb', '.user'])
                    if is_match:
                        candidate_files.append(filepath)
            
            total_files = len(candidate_files)
            failures = []
            
            for idx, filepath in enumerate(candidate_files):
                if self.cancelled:
                    break
                    
                self.progress_update.emit(idx + 1, total_files)
                
                results = scan_file(
                    filepath,
                    self.type_id,
                    None,
                    self.type_registry,
                    failures,
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

        self.setWindowTitle("Find RSZ Field Value")
        self.setMinimumSize(900, 700)
        self.setup_ui()
        if self.json_path_edit.text():
            self.load_type_registry()
        
    def setup_ui(self):
        layout = QVBoxLayout(self)
        
        json_group = QGroupBox("Type Data Source")
        json_layout = QHBoxLayout()
        
        self.json_path_edit = QLineEdit()
        json_layout.addWidget(QLabel("JSON Path:"))
        json_layout.addWidget(self.json_path_edit)
        
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self.browse_json_path)
        json_layout.addWidget(browse_btn)
        
        reload_btn = QPushButton("Reload Types")
        reload_btn.clicked.connect(self.load_type_registry)
        json_layout.addWidget(reload_btn)
        
        json_group.setLayout(json_layout)
        layout.addWidget(json_group)
        
        search_group = QGroupBox("Search Configuration")
        search_layout = QVBoxLayout()
        
        dir_layout = QHBoxLayout()
        self.dir_edit = QLineEdit()
        dir_layout.addWidget(QLabel("Search Directory:"))
        dir_layout.addWidget(self.dir_edit)
        
        dir_browse_btn = QPushButton("Browse...")
        dir_browse_btn.clicked.connect(self.browse_directory)
        dir_layout.addWidget(dir_browse_btn)
        
        self.recursive_check = QCheckBox("Recursive")
        self.recursive_check.setChecked(True)
        dir_layout.addWidget(self.recursive_check)
        
        search_layout.addLayout(dir_layout)
        
        type_layout = QHBoxLayout()
        type_layout.addWidget(QLabel("Type:"))
        
        self.type_combo = QComboBox()
        self.type_combo.setEditable(True)
        self.type_combo.currentTextChanged.connect(self.on_type_changed)
        type_layout.addWidget(self.type_combo, 1)
        
        self.type_id_label = QLabel("ID: -")
        type_layout.addWidget(self.type_id_label)
        
        search_layout.addLayout(type_layout)
        

        
        search_group.setLayout(search_layout)
        layout.addWidget(search_group)
        
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self.start_search)
        layout.addWidget(search_btn)
        
        display_group = QGroupBox("Fields to Display")
        display_layout = QVBoxLayout()
        
        display_info = QLabel("Select fields to display in results (search fetches all fields):")
        display_layout.addWidget(display_info)
        
        self.fields_list = QListWidget()
        self.fields_list.setSelectionMode(QListWidget.MultiSelection)
        self.fields_list.itemChanged.connect(self.on_display_fields_changed)
        display_layout.addWidget(self.fields_list)
        
        display_group.setLayout(display_layout)
        layout.addWidget(display_group)
        
        constraint_group = QGroupBox("Field Constraints (Optional)")
        constraint_layout = QVBoxLayout()
        
        constraint_info = QLabel("Add constraints to filter results:")
        constraint_layout.addWidget(constraint_info)
        
        self.constraints_list = QListWidget()
        self.constraints_list.setMaximumHeight(100)
        constraint_layout.addWidget(self.constraints_list)
        
        add_constraint_layout = QHBoxLayout()
        add_constraint_layout.addWidget(QLabel("Field:"))
        self.constraint_field_combo = QComboBox()
        add_constraint_layout.addWidget(self.constraint_field_combo)
        
        add_constraint_layout.addWidget(QLabel("Contains:"))
        self.constraint_value_edit = QLineEdit()
        add_constraint_layout.addWidget(self.constraint_value_edit)
        
        add_btn = QPushButton("Add")
        add_btn.clicked.connect(self.add_constraint)
        add_constraint_layout.addWidget(add_btn)
        
        remove_btn = QPushButton("Remove Selected")
        remove_btn.clicked.connect(self.remove_constraint)
        add_constraint_layout.addWidget(remove_btn)
        
        constraint_layout.addLayout(add_constraint_layout)
        
        constraint_group.setLayout(constraint_layout)
        layout.addWidget(constraint_group)
        
        results_group = QGroupBox("Search Results")
        results_layout = QVBoxLayout()
        
        splitter = QSplitter(Qt.Vertical)
        
        self.file_tree = QTreeWidget()
        self.file_tree.setHeaderLabels(["File", "Instances Found"])
        self.file_tree.itemExpanded.connect(self.on_file_expanded)
        self.file_tree.itemClicked.connect(self.on_item_selected)
        splitter.addWidget(self.file_tree)
        
        self.details_tree = QTreeWidget()
        self.details_tree.setHeaderLabels(["Field", "Value"])
        splitter.addWidget(self.details_tree)
        
        splitter.setSizes([300, 200])
        results_layout.addWidget(splitter)
        
        results_group.setLayout(results_layout)
        layout.addWidget(results_group)
        
        self.status_bar = QLabel("Ready")
        self.status_bar.setStyleSheet("QLabel { color: gray; }")
        layout.addWidget(self.status_bar)
        
        button_box = QDialogButtonBox(QDialogButtonBox.Close)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)
        
    def browse_json_path(self):
        current_path = self.json_path_edit.text()

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select JSON Type Data File",
            os.path.dirname(current_path) if current_path else "",
            "JSON Files (*.json);;All Files (*.*)"
        )

        if file_path:
            self.json_path_edit.setText(file_path)
            self.load_type_registry()

    def browse_directory(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "Select Directory to Search",
            self.dir_edit.text()
        )
        if path:
            self.dir_edit.setText(path)
            
    def load_type_registry(self):
        json_path = self.json_path_edit.text()
        if not json_path:
            QMessageBox.warning(self, "Warning", "Please specify a JSON path")
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
                    QMessageBox.warning(self, "Warning", f"No JSON files found in {json_path}")
                    return
            json_path = json_file
        elif not os.path.exists(json_path):
            QMessageBox.warning(self, "Warning", f"Path does not exist: {json_path}")
            return
            
        try:
            self.type_registry = TypeRegistry(json_path)
            self.populate_type_combo()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load type registry: {str(e)}")
            
    def populate_type_combo(self):
        if not self.type_registry:
            return
            
        self.type_combo.clear()
        self.type_combo.setEnabled(False)
        
        QApplication.setOverrideCursor(Qt.WaitCursor)
        
        try:
            type_items = []
            total_items = len(self.type_registry.registry)
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
                self.type_id_label.setText(f"Loaded {len(type_items)} types")
        finally:
            QApplication.restoreOverrideCursor()
            self.type_combo.setEnabled(True)
            
    def on_type_changed(self, text):
        if not text or not self.type_registry:
            return

        type_id = self._resolve_type_id(text)

        if type_id is not None:
            self.type_id_label.setText(f"ID: 0x{type_id:08X}")
            self.populate_fields(type_id)
        else:
            self.type_id_label.setText("ID: -")

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
        
        constraint_text = f"{field} contains '{value}'"
        self.constraints_list.addItem(constraint_text)
        
        self.constraint_value_edit.clear()
        
        if self.all_results:
            self.apply_filters()
    
    def remove_constraint(self):
        current_item = self.constraints_list.currentItem()
        if current_item:
            row = self.constraints_list.row(current_item)
            self.constraints_list.takeItem(row)
            
            if self.all_results:
                self.apply_filters()
    
    def get_constraints(self):
        constraints = []
        for i in range(self.constraints_list.count()):
            text = self.constraints_list.item(i).text()
            if " contains '" in text:
                parts = text.split(" contains '")
                if len(parts) == 2:
                    field = parts[0]
                    value = parts[1].rstrip("'")
                    constraints.append((field, value))
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
            placeholder.setText(0, "Loading...")

        if constraints is None:
            constraints = self.get_constraints()
        if constraints:
            self.status_bar.setText(f"Filtered: {total_instances} instances in {len(self.results)} files")
        else:
            self.status_bar.setText(f"Total: {total_instances} instances in {len(self.results)} files")
        
    def start_search(self):
        if not self.dir_edit.text():
            QMessageBox.warning(self, "Warning", "Please select a directory to search")
            return
            
        if not self.type_combo.currentText():
            QMessageBox.warning(self, "Warning", "Please select a type")
            return
            
        text = self.type_combo.currentText()
        type_id = self._resolve_type_id(text) if text else None

        if type_id is None:
            QMessageBox.warning(self, "Warning", "Invalid type selection")
            return
            
        self.file_tree.clear()
        self.details_tree.clear()
        self.results.clear()
        self.all_results = {}
        self.last_selected_item = None
        
        self.search_thread = SearchWorkerThread(
            self.dir_edit.text(),
            type_id,
            self.type_registry,
            self.recursive_check.isChecked()
        )

        self.search_thread.progress_update.connect(self.update_progress)
        self.search_thread.file_found.connect(self.add_file_result)
        self.search_thread.search_complete.connect(self.search_complete)
        self.search_thread.error_occurred.connect(self.search_error)

        self._close_progress_dialog()
        self.progress_dialog = QProgressDialog("Searching files...", "Cancel", 0, 100, self)
        self.progress_dialog.setWindowTitle("Search Progress")
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
        dialog.setLabelText(f"Searching files... ({current}/{total})")
            
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
        placeholder.setText(0, "Loading...")
        
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
            instance_item.setText(0, f"Instance {instance_id}")
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
                        
        self.details_tree.resizeColumnToContents(0)
        
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

        constraints = self.get_constraints()
        result_source = self.results if constraints else self.all_results

        total_instances = 0
        for results in result_source.values():
            unique_instances = len(set(r[1] for r in results))
            total_instances += unique_instances

        total_files = len(result_source)

        if constraints:
            self.status_bar.setText(
                f"Search complete (filtered): {total_instances} instances in {total_files} files"
            )
        else:
            self.status_bar.setText(f"Search complete: {total_instances} instances in {total_files} files")

        if total_files == 0:
            if constraints and self.all_results:
                message = "No matches found for the current constraints."
            else:
                message = "No matching instances found."
            QMessageBox.information(self, "Search Complete", message)
        
    def search_error(self, error_msg):
        self._close_progress_dialog()

        QMessageBox.critical(self, "Search Error", f"An error occurred: {error_msg}")
