import os
import shutil
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMessageBox, QProgressDialog, QApplication, QGroupBox, QFrame
)

from tools.outdated_files_detector import OutdatedFilesDetector, delete_files

class OutdatedFilesDialog(QDialog):
    def __init__(self, parent=None, registry_path=None):
        super().__init__(parent)
        self.setWindowTitle("Outdated Files Detector")
        self.resize(900, 700)
        
        self.detector = OutdatedFilesDetector(registry_path)
        self.scan_results = []
        
        self._create_ui()
        
    def _create_ui(self):
        layout = QVBoxLayout(self)
        
        info_frame = QFrame()
        info_frame.setStyleSheet("border-radius: 5px; padding: 10px;")
        info_layout = QVBoxLayout(info_frame)
        info_label = QLabel(
            "<b>Note:</b> This tool detects files that are incompatible with your selected RSZ template "
            "(.json file). Make sure to use the latest .json if you want to check for outdated mods."
        )
        info_label.setWordWrap(True)
        info_font = info_label.font()
        info_font.setPointSize(info_font.pointSize() + 1)
        info_label.setFont(info_font)
        info_layout.addWidget(info_label)
        layout.addWidget(info_frame)
        
        config_group = QGroupBox("Configuration")
        config_layout = QVBoxLayout(config_group)
        
        registry_layout = QHBoxLayout()
        self.registry_label = QLabel("RSZ JSON Path: Not set")
        self.browse_registry_btn = QPushButton("Browse...")
        self.browse_registry_btn.clicked.connect(self._browse_registry)
        registry_layout.addWidget(self.registry_label, 1)
        registry_layout.addWidget(self.browse_registry_btn)
        config_layout.addLayout(registry_layout)
        
        dir_layout = QHBoxLayout()
        self.dir_label = QLabel("Directory: Not selected")
        self.browse_dir_btn = QPushButton("Browse...")
        self.browse_dir_btn.clicked.connect(self._browse_directory)
        dir_layout.addWidget(self.dir_label, 1)
        dir_layout.addWidget(self.browse_dir_btn)
        config_layout.addLayout(dir_layout)
        
        layout.addWidget(config_group)
        
        self.scan_btn = QPushButton("Scan for Outdated Files")
        self.scan_btn.setMinimumHeight(40)
        font = self.scan_btn.font()
        font.setBold(True)
        self.scan_btn.setFont(font)
        self.scan_btn.clicked.connect(self._scan_directory)
        self.scan_btn.setEnabled(False)
        layout.addWidget(self.scan_btn)
        
        results_group = QGroupBox("Results")
        results_layout = QVBoxLayout(results_group)
        
        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(["File Path", "Mismatching Types Count"])
        self.results_tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.results_tree.header().setSectionsMovable(True)
        self.results_tree.header().setSectionsClickable(True)
        self.results_tree.header().setStretchLastSection(False)
        self.results_tree.setColumnWidth(0, 500)
        self.results_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        results_layout.addWidget(self.results_tree)
        
        action_layout = QHBoxLayout()
        self.select_all_btn = QPushButton("Select All")
        self.select_all_btn.clicked.connect(self._select_all)
        self.deselect_all_btn = QPushButton("Deselect All")
        self.deselect_all_btn.clicked.connect(self._deselect_all)
        
        self.move_btn = QPushButton("Move Selected to 'outdated' Folder")
        self.move_btn.clicked.connect(self._move_selected)
        self.move_btn.setEnabled(False)
        
        self.delete_btn = QPushButton("Delete Selected Files")
        self.delete_btn.clicked.connect(self._delete_selected)
        self.delete_btn.setEnabled(False)
        
        action_layout.addWidget(self.select_all_btn)
        action_layout.addWidget(self.deselect_all_btn)
        action_layout.addStretch()
        action_layout.addWidget(self.move_btn)
        action_layout.addWidget(self.delete_btn)
        
        results_layout.addLayout(action_layout)
        layout.addWidget(results_group)
        
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setMinimumWidth(120)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)
        
        if self.detector.type_registry:
            self.registry_label.setText(f"RSZ JSON Path: {self.detector.type_registry_path}")
        
    def _browse_registry(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, "Select Type Registry JSON", "", "JSON Files (*.json)"
        )
        
        if file_path:
            self.detector.set_type_registry_path(file_path)
            if self.detector.type_registry:
                self.registry_label.setText(f"RSZ JSON Path: {file_path}")
                if self.dir_label.text() != "Directory: Not selected":
                    self.scan_btn.setEnabled(True)
            else:
                QMessageBox.warning(
                    self, "Registry Load Error", 
                    "Failed to load the selected type registry file."
                )
    
    def _browse_directory(self):
        directory = QFileDialog.getExistingDirectory(
            self, "Select Directory to Scan", ""
        )
        
        if directory:
            self.dir_label.setText(f"Directory: {directory}")
            if self.detector.type_registry:
                self.scan_btn.setEnabled(True)
    
    def _scan_directory(self):
        directory = self.dir_label.text().replace("Directory: ", "")
        
        if not os.path.isdir(directory):
            QMessageBox.warning(
                self, "Invalid Directory", 
                "The selected directory does not exist."
            )
            return
        
        self.results_tree.clear()
        self.scan_results = []
        self.delete_btn.setEnabled(False)
        self.move_btn.setEnabled(False)
        
        rsz_files = []
        
        count_progress = QProgressDialog("Finding RSZ files...", "Cancel", 0, 0, self)
        count_progress.setWindowTitle("Finding Files")
        count_progress.setWindowModality(Qt.WindowModal)
        count_progress.setMinimumDuration(0)
        count_progress.setValue(0)
        count_progress.show()
        
        for root, _, files in os.walk(directory):
            if count_progress.wasCanceled():
                return
            
            QApplication.processEvents()
            
            for file in files:
                if self.detector._is_rsz_file(file):
                    file_path = os.path.join(root, file)
                    rsz_files.append(file_path)
                    
        total_files = len(rsz_files)
        if total_files == 0:
            count_progress.close()
            QMessageBox.information(self, "No Files Found", "No RSZ files found in the selected directory.")
            return
            
        progress = QProgressDialog("Scanning for outdated files...", "Cancel", 0, total_files, self)
        progress.setWindowTitle("Scanning Files")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        count_progress.close()
        
        results = []
        for i, file_path in enumerate(rsz_files):
            if progress.wasCanceled():
                break
                
            progress.setValue(i)
            progress.setLabelText(f"Scanning file {i+1} of {total_files}:\n{os.path.basename(file_path)}")
            QApplication.processEvents()
            
            try:
                mismatched_types = self.detector.check_file_for_outdated_types(file_path)
                if mismatched_types:
                    results.append((file_path, mismatched_types))
            except Exception as e:
                print(f"Error checking file {file_path}: {str(e)}")
        
        progress.setValue(total_files)
        self.scan_results = results
        
        if len(results) > 500:
            self._populate_tree_in_batches(results)
        else:
            self._populate_tree(results)
        
        self.results_tree.itemChanged.connect(self._on_item_checked)
        
        QMessageBox.information(
            self, 
            "Scan Complete",
            f"Found {len(results)} outdated files with mismatching type CRCs."
        )
    
    def _populate_tree(self, results):
        """Populate tree with results - used for smaller result sets"""
        for file_path, mismatches in results:
            item = QTreeWidgetItem(self.results_tree)
            item.setText(0, file_path)
            item.setText(1, str(len(mismatches)))
            item.setCheckState(0, Qt.Unchecked)
            
            for mismatch in mismatches:
                child = QTreeWidgetItem(item)
                type_name = mismatch.get("name", "Unknown")
                file_crc = mismatch.get("file_crc", 0)
                registry_crc = mismatch.get("registry_crc", None)
                
                crc_text = f"File CRC: 0x{file_crc:08X}"
                if registry_crc is not None:
                    crc_text += f", Registry CRC: 0x{registry_crc:08X}"
                else:
                    crc_text += ", Not found in registry"
                
                child.setText(0, f"{type_name} - {crc_text}")
            
            item.setExpanded(False)
    
    def _populate_tree_in_batches(self, results):
        """Populate tree with results in batches to avoid UI freezing"""
        total_items = len(results)
        batch_size = 100
        
        self.results_tree.setSortingEnabled(False)
        self.results_tree.setUpdatesEnabled(False)
        
        progress = QProgressDialog("Populating results...", "Cancel", 0, total_items, self)
        progress.setWindowTitle("Loading Results")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        
        for i in range(0, total_items, batch_size):
            if progress.wasCanceled():
                break
                
            batch = results[i:min(i+batch_size, total_items)]
            for file_path, mismatches in batch:
                item = QTreeWidgetItem(self.results_tree)
                item.setText(0, file_path)
                item.setText(1, str(len(mismatches)))
                item.setCheckState(0, Qt.Unchecked)
                
                for mismatch in mismatches[:20]:
                    child = QTreeWidgetItem(item)
                    type_name = mismatch.get("name", "Unknown")
                    file_crc = mismatch.get("file_crc", 0)
                    registry_crc = mismatch.get("registry_crc", None)
                    
                    crc_text = f"File CRC: 0x{file_crc:08X}"
                    if registry_crc is not None:
                        crc_text += f", Registry CRC: 0x{registry_crc:08X}"
                    else:
                        crc_text += ", Not found in registry"
                    
                    child.setText(0, f"{type_name} - {crc_text}")
                
                if len(mismatches) > 20:
                    more_item = QTreeWidgetItem(item)
                    more_item.setText(0, f"... and {len(mismatches) - 20} more mismatches (double-click to load)")
                    more_item.setData(0, Qt.UserRole, mismatches[20:])
            
            progress.setValue(min(i + batch_size, total_items))
            QApplication.processEvents()
        
        self.results_tree.setUpdatesEnabled(True)
        progress.close()

    def _on_item_checked(self, item, column):
        any_checked = False
        for i in range(self.results_tree.topLevelItemCount()):
            top_item = self.results_tree.topLevelItem(i)
            if top_item.checkState(0) == Qt.Checked:
                any_checked = True
                break
        
        self.delete_btn.setEnabled(any_checked)
        self.move_btn.setEnabled(any_checked)
    
    def _select_all(self):
        for i in range(self.results_tree.topLevelItemCount()):
            item = self.results_tree.topLevelItem(i)
            item.setCheckState(0, Qt.Checked)
    
    def _deselect_all(self):
        for i in range(self.results_tree.topLevelItemCount()):
            item = self.results_tree.topLevelItem(i)
            item.setCheckState(0, Qt.Unchecked)
    
    def _get_selected_files(self):
        files_to_process = []
        
        for i in range(self.results_tree.topLevelItemCount()):
            item = self.results_tree.topLevelItem(i)
            if item.checkState(0) == Qt.Checked:
                files_to_process.append(item.text(0))
        
        return files_to_process
    
    def _move_selected(self):
        files_to_move = self._get_selected_files()
        
        if not files_to_move:
            return
        
        base_dir = os.path.dirname(files_to_move[0])
        outdated_dir = os.path.join(base_dir, "outdated")
        
        if not os.path.exists(outdated_dir):
            try:
                os.makedirs(outdated_dir)
            except Exception as e:
                QMessageBox.warning(
                    self, "Error Creating Directory",
                    f"Could not create 'outdated' directory:\n{str(e)}"
                )
                return
        
        confirm = QMessageBox.question(
            self, "Confirm Move",
            f"Are you sure you want to move {len(files_to_move)} files to the 'outdated' folder?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if confirm == QMessageBox.Yes:
            success = []
            errors = []
            
            for file_path in files_to_move:
                try:
                    filename = os.path.basename(file_path)
                    dest_path = os.path.join(outdated_dir, filename)
                    
                    counter = 1
                    while os.path.exists(dest_path):
                        base_name, ext = os.path.splitext(filename)
                        dest_path = os.path.join(outdated_dir, f"{base_name}_{counter}{ext}")
                        counter += 1
                    
                    shutil.move(file_path, dest_path)
                    success.append(file_path)
                except Exception as e:
                    errors.append((file_path, str(e)))
            
            for file_path in success:
                for i in range(self.results_tree.topLevelItemCount()):
                    item = self.results_tree.topLevelItem(i)
                    if item.text(0) == file_path:
                        self.results_tree.takeTopLevelItem(i)
                        break
            
            if errors:
                error_msg = "\n".join([f"{file_path}: {error}" for file_path, error in errors])
                QMessageBox.warning(
                    self, "Move Errors",
                    f"Moved {len(success)} files successfully to '{outdated_dir}'.\n\n"
                    f"Failed to move {len(errors)} files:\n{error_msg}"
                )
            else:
                QMessageBox.information(
                    self, "Move Complete",
                    f"Successfully moved {len(success)} files to '{outdated_dir}'."
                )
    
    def _delete_selected(self):
        files_to_delete = self._get_selected_files()
        
        if not files_to_delete:
            return
        
        confirm = QMessageBox.question(
            self, "Confirm Deletion",
            f"Are you sure you want to delete {len(files_to_delete)} files?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if confirm == QMessageBox.Yes:
            success, errors = delete_files(files_to_delete)
            
            for file_path in success:
                for i in range(self.results_tree.topLevelItemCount()):
                    item = self.results_tree.topLevelItem(i)
                    if item.text(0) == file_path:
                        self.results_tree.takeTopLevelItem(i)
                        break
            
            if errors:
                error_msg = "\n".join([f"{file_path}: {error}" for file_path, error in errors])
                QMessageBox.warning(
                    self, "Deletion Errors",
                    f"Deleted {len(success)} files successfully.\n\n"
                    f"Failed to delete {len(errors)} files:\n{error_msg}"
                )
            else:
                QMessageBox.information(
                    self, "Deletion Complete",
                    f"Successfully deleted {len(success)} files."
                )
        confirm = QMessageBox.question(
            self, "Confirm Deletion",
            f"Are you sure you want to delete {len(files_to_delete)} files?",
            QMessageBox.Yes | QMessageBox.No
        )
        
        if confirm == QMessageBox.Yes:
            success, errors = delete_files(files_to_delete)
            
            for file_path in success:
                for i in range(self.results_tree.topLevelItemCount()):
                    item = self.results_tree.topLevelItem(i)
                    if item.text(0) == file_path:
                        self.results_tree.takeTopLevelItem(i)
                        break
                for i in range(self.results_tree.topLevelItemCount()):
                    item = self.results_tree.topLevelItem(i)
                    if item.text(0) == file_path:
                        self.results_tree.takeTopLevelItem(i)
                        break
