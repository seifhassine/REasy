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
        self.setWindowTitle(self.tr("Outdated Files Detector"))
        self.resize(900, 700)
        
        self.detector = OutdatedFilesDetector(registry_path)
        self.scan_results = []
        self.selected_directory = ""
        
        self._create_ui()
        
    def _create_ui(self):
        layout = QVBoxLayout(self)
        
        info_frame = QFrame()
        info_frame.setStyleSheet("border-radius: 5px; padding: 10px;")
        info_layout = QVBoxLayout(info_frame)
        info_label = QLabel(self.tr(
            "<b>Note:</b> This tool detects files that are incompatible with your selected RSZ template "
            "(.json file). Make sure to use the latest .json if you want to check for outdated mods."
        ))
        info_label.setWordWrap(True)
        info_font = info_label.font()
        info_font.setPointSize(info_font.pointSize() + 1)
        info_label.setFont(info_font)
        info_layout.addWidget(info_label)
        layout.addWidget(info_frame)
        
        config_group = QGroupBox(self.tr("Configuration"))
        config_layout = QVBoxLayout(config_group)
        
        registry_layout = QHBoxLayout()
        self.registry_label = QLabel(self.tr("RSZ JSON Path: Not set"))
        self.browse_registry_btn = QPushButton(self.tr("Browse..."))
        self.browse_registry_btn.clicked.connect(self._browse_registry)
        registry_layout.addWidget(self.registry_label, 1)
        registry_layout.addWidget(self.browse_registry_btn)
        config_layout.addLayout(registry_layout)
        
        dir_layout = QHBoxLayout()
        self.dir_label = QLabel(self.tr("Directory: Not selected"))
        self.browse_dir_btn = QPushButton(self.tr("Browse..."))
        self.browse_dir_btn.clicked.connect(self._browse_directory)
        dir_layout.addWidget(self.dir_label, 1)
        dir_layout.addWidget(self.browse_dir_btn)
        config_layout.addLayout(dir_layout)
        
        layout.addWidget(config_group)
        
        self.scan_btn = QPushButton(self.tr("Scan for Outdated Files"))
        self.scan_btn.setMinimumHeight(40)
        font = self.scan_btn.font()
        font.setBold(True)
        self.scan_btn.setFont(font)
        self.scan_btn.clicked.connect(self._scan_directory)
        self.scan_btn.setEnabled(False)
        layout.addWidget(self.scan_btn)
        
        results_group = QGroupBox(self.tr("Results"))
        results_layout = QVBoxLayout(results_group)
        
        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels([
            self.tr("File Path"), self.tr("Mismatching Types Count")
        ])
        self.results_tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.results_tree.header().setSectionsMovable(True)
        self.results_tree.header().setSectionsClickable(True)
        self.results_tree.header().setStretchLastSection(False)
        self.results_tree.setColumnWidth(0, 500)
        self.results_tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        results_layout.addWidget(self.results_tree)
        
        action_layout = QHBoxLayout()
        self.select_all_btn = QPushButton(self.tr("Select All"))
        self.select_all_btn.clicked.connect(self._select_all)
        self.deselect_all_btn = QPushButton(self.tr("Deselect All"))
        self.deselect_all_btn.clicked.connect(self._deselect_all)
        
        self.move_btn = QPushButton(self.tr("Move Selected to 'outdated' Folder"))
        self.move_btn.clicked.connect(self._move_selected)
        self.move_btn.setEnabled(False)
        
        self.delete_btn = QPushButton(self.tr("Delete Selected Files"))
        self.delete_btn.clicked.connect(self._delete_selected)
        self.delete_btn.setEnabled(False)
        self.results_tree.itemChanged.connect(self._on_item_checked)
        
        action_layout.addWidget(self.select_all_btn)
        action_layout.addWidget(self.deselect_all_btn)
        action_layout.addStretch()
        action_layout.addWidget(self.move_btn)
        action_layout.addWidget(self.delete_btn)
        
        results_layout.addLayout(action_layout)
        layout.addWidget(results_group)
        
        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        self.close_btn = QPushButton(self.tr("Close"))
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setMinimumWidth(120)
        bottom_layout.addWidget(self.close_btn)
        layout.addLayout(bottom_layout)
        
        if self.detector.type_registry:
            self.registry_label.setText(
                self.tr("RSZ JSON Path: {path}").format(path=self.detector.type_registry_path)
            )
        
    def _browse_registry(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Type Registry JSON"), "", "JSON Files (*.json)"
        )
        
        if file_path:
            self.detector.set_type_registry_path(file_path)
            if self.detector.type_registry:
                self.registry_label.setText(self.tr("RSZ JSON Path: {path}").format(path=file_path))
                self.scan_btn.setEnabled(bool(self.selected_directory))
            else:
                QMessageBox.warning(
                    self, self.tr("Registry Load Error"),
                    self.tr("Failed to load the selected type registry file.")
                )
    
    def _browse_directory(self):
        directory = QFileDialog.getExistingDirectory(
            self, self.tr("Select Directory to Scan"), ""
        )
        
        if directory:
            self.selected_directory = directory
            self.dir_label.setText(self.tr("Directory: {path}").format(path=directory))
            if self.detector.type_registry:
                self.scan_btn.setEnabled(True)
    
    def _scan_directory(self):
        directory = self.selected_directory
        
        if not os.path.isdir(directory):
            QMessageBox.warning(
                self, self.tr("Invalid Directory"),
                self.tr("The selected directory does not exist.")
            )
            return
        
        self.results_tree.clear()
        self.scan_results = []
        self.delete_btn.setEnabled(False)
        self.move_btn.setEnabled(False)
        
        rsz_files = []
        
        count_progress = QProgressDialog(
            self.tr("Finding RSZ files..."), self.tr("Cancel"), 0, 0, self
        )
        count_progress.setWindowTitle(self.tr("Finding Files"))
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
            QMessageBox.information(
                self,
                self.tr("No Files Found"),
                self.tr("No RSZ files found in the selected directory."),
            )
            return
            
        progress = QProgressDialog(
            self.tr("Scanning for outdated files..."), self.tr("Cancel"), 0, total_files, self
        )
        progress.setWindowTitle(self.tr("Scanning Files"))
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        count_progress.close()
        
        results = []
        for i, file_path in enumerate(rsz_files):
            if progress.wasCanceled():
                break
                
            progress.setValue(i)
            progress.setLabelText(self.tr(
                "Scanning file {current} of {total}:\n{file_name}"
            ).format(current=i + 1, total=total_files, file_name=os.path.basename(file_path)))
            QApplication.processEvents()
            
            try:
                mismatched_types = self.detector.check_file_for_outdated_types(file_path)
                if mismatched_types:
                    results.append((file_path, mismatched_types))
            except Exception as e:
                print(f"Error checking file {file_path}: {str(e)}")
        
        progress.setValue(total_files)
        self.scan_results = results
        
        signals_were_blocked = self.results_tree.blockSignals(True)
        try:
            if len(results) > 500:
                self._populate_tree_in_batches(results)
            else:
                self._populate_tree(results)
        finally:
            self.results_tree.blockSignals(signals_were_blocked)
        
        QMessageBox.information(
            self, 
            self.tr("Scan Complete"),
            self.tr("Found {count} outdated files with mismatching type CRCs.").format(
                count=len(results)
            )
        )
    
    def _populate_tree(self, results):
        """Populate tree with results - used for smaller result sets"""
        for file_path, mismatches in results:
            self._add_result_item(file_path, mismatches)

    def _add_result_item(self, file_path, mismatches, detail_limit=None):
        item = QTreeWidgetItem(self.results_tree)
        item.setText(0, file_path)
        item.setText(1, str(len(mismatches)))
        item.setCheckState(0, Qt.Unchecked)

        displayed = mismatches if detail_limit is None else mismatches[:detail_limit]
        for mismatch in displayed:
            type_name = mismatch.get("name", self.tr("Unknown"))
            file_crc = mismatch.get("file_crc", 0)
            registry_crc = mismatch.get("registry_crc")
            if registry_crc is None:
                crc_text = self.tr(
                    "File CRC: 0x{file_crc:08X}, Not found in registry"
                ).format(file_crc=file_crc)
            else:
                crc_text = self.tr(
                    "File CRC: 0x{file_crc:08X}, Registry CRC: 0x{registry_crc:08X}"
                ).format(file_crc=file_crc, registry_crc=registry_crc)
            QTreeWidgetItem(item).setText(0, f"{type_name} - {crc_text}")

        if detail_limit is not None and len(mismatches) > detail_limit:
            more_item = QTreeWidgetItem(item)
            more_item.setText(0, self.tr(
                "... and {count} more mismatches (double-click to load)"
            ).format(count=len(mismatches) - detail_limit))
            more_item.setData(0, Qt.UserRole, mismatches[detail_limit:])

        item.setExpanded(False)
    
    def _populate_tree_in_batches(self, results):
        """Populate tree with results in batches to avoid UI freezing"""
        total_items = len(results)
        batch_size = 100
        
        self.results_tree.setSortingEnabled(False)
        self.results_tree.setUpdatesEnabled(False)
        
        progress = QProgressDialog(
            self.tr("Populating results..."), self.tr("Cancel"), 0, total_items, self
        )
        progress.setWindowTitle(self.tr("Loading Results"))
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        progress.show()
        
        for i in range(0, total_items, batch_size):
            if progress.wasCanceled():
                break
                
            batch = results[i:min(i+batch_size, total_items)]
            for file_path, mismatches in batch:
                self._add_result_item(file_path, mismatches, detail_limit=20)
            
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
                    self, self.tr("Error Creating Directory"),
                    self.tr("Could not create '{folder}' directory:\n{error}").format(
                        folder="outdated", error=e
                    )
                )
                return
        
        confirm = QMessageBox.question(
            self, self.tr("Confirm Move"),
            self.tr("Are you sure you want to move {count} files to the '{folder}' folder?").format(
                count=len(files_to_move), folder="outdated"
            ),
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
                    self, self.tr("Move Errors"),
                    self.tr(
                        "Moved {success_count} files successfully to '{directory}'.\n\n"
                        "Failed to move {error_count} files:\n{errors}"
                    ).format(
                        success_count=len(success), directory=outdated_dir,
                        error_count=len(errors), errors=error_msg,
                    )
                )
            else:
                QMessageBox.information(
                    self, self.tr("Move Complete"),
                    self.tr("Successfully moved {count} files to '{directory}'.").format(
                        count=len(success), directory=outdated_dir
                    )
                )
    
    def _delete_selected(self):
        files_to_delete = self._get_selected_files()
        
        if not files_to_delete:
            return
        
        confirm = QMessageBox.question(
            self, self.tr("Confirm Deletion"),
            self.tr("Are you sure you want to delete {count} files?").format(
                count=len(files_to_delete)
            ),
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
                    self, self.tr("Deletion Errors"),
                    self.tr(
                        "Deleted {success_count} files successfully.\n\n"
                        "Failed to delete {error_count} files:\n{errors}"
                    ).format(
                        success_count=len(success), error_count=len(errors), errors=error_msg
                    )
                )
            else:
                QMessageBox.information(
                    self, self.tr("Deletion Complete"),
                    self.tr("Successfully deleted {count} files.").format(count=len(success))
                )
