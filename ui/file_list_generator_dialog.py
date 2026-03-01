import os
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMessageBox, QProgressDialog, QApplication, QGroupBox, QFrame,
    QLineEdit, QInputDialog, QCheckBox
)
from tools.file_list_generator import ExtensionAnalyzer, validate_game_executable, PathCollector, ExePathExtractor


class ExtensionDumperThread(QThread):
    finished = Signal(bool, str)
    
    def __init__(self, analyzer, exe_path):
        super().__init__()
        self.analyzer = analyzer
        self.exe_path = exe_path
    
    def run(self):
        success, error = self.analyzer.run_extension_dumper(self.exe_path)
        self.finished.emit(success, error or "")


class FileListGeneratorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("File List Generator")
        self.resize(900, 700)
        
        self.analyzer = ExtensionAnalyzer()
        self.game_exe_path = None
        self.list_file_path = None
        self.path_collector = None
        self.exe_path_extractor = None
        self.path_prefix = "natives/stm/"
        self.include_variations = False
        self.include_streaming = False
        
        self._create_ui()
    
    def _create_ui(self):
        layout = QVBoxLayout(self)
        self._create_info_section(layout)
        self._create_config_section(layout)
        self._create_run_button(layout)
        self._create_results_section(layout)
        self._create_bottom_buttons(layout)
    
    def _create_info_section(self, layout):
        info_frame = QFrame()
        info_frame.setStyleSheet("border-radius: 5px; padding: 10px;")
        info_layout = QVBoxLayout(info_frame)
        
        info_label = QLabel(
            "<b>File List Generator</b><br>"
            "This tool helps improve existing PAK file path lists or generate new ones.<br>"
            "It analyzes the game executable to discover file extensions and their version numbers."
        )
        info_label.setWordWrap(True)
        info_font = info_label.font()
        info_font.setPointSize(info_font.pointSize() + 1)
        info_label.setFont(info_font)
        info_layout.addWidget(info_label)
        
        layout.addWidget(info_frame)
    
    def _create_config_section(self, layout):
        config_group = QGroupBox("Configuration")
        config_layout = QVBoxLayout(config_group)
        
        exe_layout = QHBoxLayout()
        self.exe_label = QLabel("Game Executable: Not selected")
        self.browse_exe_btn = QPushButton("Browse...")
        self.browse_exe_btn.clicked.connect(self._browse_game_exe)
        exe_layout.addWidget(self.exe_label, 1)
        exe_layout.addWidget(self.browse_exe_btn)
        config_layout.addLayout(exe_layout)
        
        list_layout = QHBoxLayout()
        self.list_label = QLabel("Existing List File: None (optional)")
        self.browse_list_btn = QPushButton("Browse...")
        self.browse_list_btn.clicked.connect(self._browse_list_file)
        self.clear_list_btn = QPushButton("Clear")
        self.clear_list_btn.clicked.connect(self._clear_list_file)
        self.clear_list_btn.setEnabled(False)
        list_layout.addWidget(self.list_label, 1)
        list_layout.addWidget(self.browse_list_btn)
        list_layout.addWidget(self.clear_list_btn)
        config_layout.addLayout(list_layout)
        
        ext_label = QLabel(f"<b>Extensions to analyze:</b> {', '.join(self.analyzer.target_extensions)}")
        config_layout.addWidget(ext_label)
        
        prefix_layout = QHBoxLayout()
        prefix_label = QLabel("Path Prefix:")
        self.prefix_input = QLineEdit(self.path_prefix)
        self.prefix_input.setPlaceholderText("e.g., natives/stm/")
        self.prefix_input.textChanged.connect(self._on_prefix_changed)
        prefix_layout.addWidget(prefix_label)
        prefix_layout.addWidget(self.prefix_input)
        config_layout.addLayout(prefix_layout)

        self.variation_checkbox = QCheckBox("Add extra x64/language variations to each entry")
        self.variation_checkbox.toggled.connect(self._on_variations_toggled)
        config_layout.addWidget(self.variation_checkbox)

        self.streaming_checkbox = QCheckBox("Add streaming/ variant after the prefix for each entry")
        self.streaming_checkbox.toggled.connect(self._on_streaming_toggled)
        config_layout.addWidget(self.streaming_checkbox)
        
        layout.addWidget(config_group)
    
    def _create_run_button(self, layout):
        buttons_layout = QHBoxLayout()
        
        self.run_btn = QPushButton("Run Analysis")
        self.run_btn.setMinimumHeight(40)
        font = self.run_btn.font()
        font.setBold(True)
        self.run_btn.setFont(font)
        self.run_btn.clicked.connect(self._run_analysis)
        self.run_btn.setEnabled(False)
        buttons_layout.addWidget(self.run_btn)
        
        self.extract_exe_btn = QPushButton("Extract Paths from EXE")
        self.extract_exe_btn.setMinimumHeight(40)
        extract_font = self.extract_exe_btn.font()
        extract_font.setBold(True)
        self.extract_exe_btn.setFont(extract_font)
        self.extract_exe_btn.clicked.connect(self._extract_paths_from_exe)
        self.extract_exe_btn.setEnabled(False)
        self.extract_exe_btn.setStyleSheet("QPushButton { background-color: #2E7D32; color: white; } QPushButton:disabled { background-color: #BDBDBD; color: #666; }")
        buttons_layout.addWidget(self.extract_exe_btn)

        self.extract_dump_btn = QPushButton("Extract Paths from Memory Dump")
        self.extract_dump_btn.setMinimumHeight(40)
        dump_font = self.extract_dump_btn.font()
        dump_font.setBold(True)
        self.extract_dump_btn.setFont(dump_font)
        self.extract_dump_btn.clicked.connect(self._extract_paths_from_dump)
        self.extract_dump_btn.setEnabled(False)
        self.extract_dump_btn.setStyleSheet("QPushButton { background-color: #1565C0; color: white; } QPushButton:disabled { background-color: #BDBDBD; color: #666; }")
        buttons_layout.addWidget(self.extract_dump_btn)
        
        layout.addLayout(buttons_layout)
    
    def _create_results_section(self, layout):
        results_group = QGroupBox("Extension Analysis Results")
        results_layout = QVBoxLayout(results_group)
        
        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels(["Extension", "Versions", "Source"])
        self.results_tree.header().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.results_tree.setColumnWidth(0, 200)
        self.results_tree.setColumnWidth(1, 300)
        self.results_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        results_layout.addWidget(self.results_tree)
        
        layout.addWidget(results_group)
    
    def _create_bottom_buttons(self, layout):
        bottom_layout = QHBoxLayout()
        
        self.collect_paths_btn = QPushButton("Collect Paths from PAK Files")
        self.collect_paths_btn.clicked.connect(self._collect_paths)
        self.collect_paths_btn.setVisible(False)
        self.collect_paths_btn.setMinimumHeight(35)
        font = self.collect_paths_btn.font()
        font.setBold(True)
        self.collect_paths_btn.setFont(font)
        bottom_layout.addWidget(self.collect_paths_btn)

        self.improve_list_btn = QPushButton("Improve Existing List")
        self.improve_list_btn.clicked.connect(self._improve_list)
        self.improve_list_btn.setVisible(False)
        self.improve_list_btn.setMinimumHeight(35)
        improve_font = self.improve_list_btn.font()
        improve_font.setBold(True)
        self.improve_list_btn.setFont(improve_font)
        bottom_layout.addWidget(self.improve_list_btn)
        
        bottom_layout.addStretch()
        
        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setMinimumWidth(120)
        bottom_layout.addWidget(self.close_btn)
        
        layout.addLayout(bottom_layout)
    
    def _browse_game_exe(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Game Executable", "", "Executable Files (*.exe);;All Files (*.*)")
        if file_path:
            valid, error = validate_game_executable(file_path)
            if not valid:
                QMessageBox.warning(self, "Invalid Executable", error)
                return
            
            self.game_exe_path = file_path
            self.exe_label.setText(f"Game Executable: {os.path.basename(file_path)}")
            self.exe_label.setToolTip(file_path)
            self._update_run_button()
    
    def _browse_list_file(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Select Existing List File (Optional)", "", "List Files (*.list);;All Files (*.*)")
        if file_path:
            self.list_file_path = file_path
            self.list_label.setText(f"Existing List File: {os.path.basename(file_path)}")
            self.list_label.setToolTip(file_path)
            self.clear_list_btn.setEnabled(True)
            if hasattr(self, 'improve_list_btn'):
                self.improve_list_btn.setEnabled(True)
    
    def _clear_list_file(self):
        self.list_file_path = None
        self.list_label.setText("Existing List File: None (optional)")
        self.list_label.setToolTip("")
        self.clear_list_btn.setEnabled(False)
        if hasattr(self, 'improve_list_btn'):
            self.improve_list_btn.setEnabled(False)
    
    def _update_run_button(self):
        has_exe = self.game_exe_path is not None
        self.run_btn.setEnabled(has_exe)
        # Extract button is only enabled after analysis
        # (will be enabled in _display_results)
    
    def _on_prefix_changed(self, text):
        self.path_prefix = text

    def _on_variations_toggled(self, checked):
        self.include_variations = checked

    def _on_streaming_toggled(self, checked):
        self.include_streaming = checked
    
    def _run_analysis(self):
        if not self.game_exe_path:
            QMessageBox.warning(self, "Error", "Please select a game executable first.")
            return
        
        progress = QProgressDialog("Running extension dumper...", None, 0, 0, self)
        progress.setWindowTitle("Analyzing Game Executable")
        progress.setWindowModality(Qt.WindowModal)
        progress.setCancelButton(None)
        progress.show()
        QApplication.processEvents()
        
        self.dumper_thread = ExtensionDumperThread(self.analyzer, self.game_exe_path)
        self.dumper_thread.finished.connect(lambda success, error: self._on_dumper_finished(success, error, progress))
        self.dumper_thread.start()
    
    def _on_dumper_finished(self, success, error, progress):
        progress.close()
        
        if not success:
            QMessageBox.critical(self, "Extension Dumper Error", f"Failed to run extension dumper:\n\n{error}")
            return
        
        if not self.analyzer.dumped_extensions:
            QMessageBox.information(self, "No Extensions Found", "No extensions were found in the game executable.")
            return
        
        if self.list_file_path:
            success, error = self.analyzer.parse_list_file(self.list_file_path)
            if not success:
                QMessageBox.warning(self, "List File Parse Warning", f"Failed to parse list file:\n{error}\n\nContinuing with dumper results only.")
        
        self.analyzer.combine_extensions()
        self._display_results()
    
    def _display_results(self):
        self.results_tree.clear()
        
        for ext, versions in self.analyzer.get_sorted_extensions():
            source = self.analyzer.get_extension_source(ext)
            
            def version_sort_key(v):
                try:
                    return (0, int(v))
                except ValueError:
                    return (1, v)
            
            sorted_versions = sorted(versions, key=version_sort_key)
            versions_str = ", ".join(sorted_versions)
            
            item = QTreeWidgetItem([ext, versions_str, source])
            item.setData(1, Qt.UserRole, versions)
            
            if source == "Dumper":
                item.setForeground(2, Qt.darkGreen)
            elif source == "List":
                item.setForeground(2, Qt.darkBlue)
            else:
                item.setForeground(2, Qt.darkMagenta)
            
            self.results_tree.addTopLevelItem(item)
        
        list_only_extensions = self.analyzer.get_list_only_extensions()
        stats = self.analyzer.get_statistics()
        summary_msg = (
            f"Extension analysis complete!\n\n"
            f"Total extensions: {stats['total']}\n"
            f"From dumper only: {stats['dumper_only']}\n"
            f"From list only: {stats['list_only']}\n"
            f"From both sources: {stats['both']}\n\n"
            f"Results are displayed in the table below.\n\n"
            f"Tip: Double-click on a Versions cell to edit the version numbers."
        )
        
        if list_only_extensions:
            warning_msg = (
                f"⚠️ Warning: Found {len(list_only_extensions)} extension(s) in the list file "
                f"that were NOT found by the dumper:\n\n"
                f"{', '.join(list_only_extensions)}\n\n"
                f"This might indicate:\n"
                f"• Extensions that were not included in the analysis\n"
                f"• Outdated or incorrect entries in the list file\n"
                f"• Extensions that need to be added to the target extensions list\n\n"
                f"Do you want to continue?"
            )
            
            response = QMessageBox.warning(self, "List-Only Extensions Detected", warning_msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
            if response == QMessageBox.No:
                return
        
        QMessageBox.information(self, "Analysis Complete", summary_msg)
        self.collect_paths_btn.setVisible(True)
        self.improve_list_btn.setVisible(True)
        self.improve_list_btn.setEnabled(bool(self.list_file_path))
        self.extract_exe_btn.setEnabled(True)
        self.extract_dump_btn.setEnabled(True)
    
    def _on_item_double_clicked(self, item, column):
        if column != 1:
            return
        
        extension = item.text(0)
        current_versions = item.data(1, Qt.UserRole)
        
        def version_sort_key(v):
            try:
                return (0, int(v))
            except ValueError:
                return (1, v)
        
        sorted_versions = sorted(current_versions, key=version_sort_key)
        current_str = ", ".join(sorted_versions)
        
        text, ok = QInputDialog.getText(
            self, "Edit Versions",
            f"Edit version identifiers for extension '{extension}':\n(Enter comma-separated values, e.g., 3, 5.ja, 221)",
            QLineEdit.Normal, current_str
        )
        
        if ok and text:
            new_versions = self._parse_version_input(text)
            if new_versions is None:
                return
            
            sorted_versions = sorted(new_versions, key=version_sort_key)
            versions_str = ", ".join(sorted_versions)
            item.setText(1, versions_str)
            item.setData(1, Qt.UserRole, new_versions)
            self.analyzer.update_extension_versions(extension, new_versions)
    
    def _collect_paths(self):
        extensions = list(self.analyzer.combined_extensions.keys())
        
        if not extensions:
            QMessageBox.warning(self, "No Extensions", "No extensions available. Please run the analysis first.")
            return
        
        if not self.game_exe_path:
            QMessageBox.warning(self, "No Game Path", "Game executable path is not available.")
            return
        
        pak_directory = os.path.dirname(self.game_exe_path)
        self.path_collector = PathCollector(
            extensions,
            extension_versions=self.analyzer.combined_extensions,
            path_prefix=self.path_prefix,
            include_variations=self.include_variations,
            include_streaming=self.include_streaming
        )
        
        progress = QProgressDialog("Initializing...", "Stop", 0, 100, self)
        progress.setWindowTitle("Collecting Paths from PAK Files")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)
        
        collection_stopped = False
        
        def update_progress(message, current, total):
            nonlocal collection_stopped
            if progress.wasCanceled():
                collection_stopped = True
                return True
            
            if total > 0:
                percentage = int((current / total) * 100)
                progress.setValue(percentage)
            
            progress.setLabelText(f"{message}\n\nProcessing {current} of {total}...")
            QApplication.processEvents()
            return False
        
        success, error, paths_found = self.path_collector.collect_from_pak_files(pak_directory, progress_callback=update_progress)
        progress.close()
        
        if collection_stopped:
            total_collected = self.path_collector.get_path_count()
            if total_collected == 0:
                QMessageBox.information(self, "Collection Stopped", "Collection was stopped. No paths were collected.")
                return
            
            QMessageBox.information(self, "Collection Stopped", f"Collection was stopped early.\n\nPaths collected so far: {total_collected}\n\nContinuing with partial results...")
        
        if not success:
            QMessageBox.critical(self, "PAK Collection Error", f"Failed to collect paths from PAK files:\n\n{error}")
            return
        
        if self.list_file_path:
            success, error = self.path_collector.add_from_list_file(self.list_file_path)
            if not success:
                QMessageBox.warning(self, "List File Warning", f"Failed to add paths from list file:\n{error}\n\nContinuing with PAK paths only.")
        
        total_collected = self.path_collector.get_path_count()
        validate_response = QMessageBox.question(
            self, "Validate Paths",
            f"Successfully collected {total_collected} unique paths!\n\n"
            f"Do you want to validate these paths against the PAK files?\n"
            f"(This will ensure only existing paths are exported)\n\n"
            f"Note: Validation may take a few minutes.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        
        validated_paths = None
        
        if validate_response == QMessageBox.Yes:
            progress = QProgressDialog("Validating paths...", None, 0, 100, self)
            progress.setWindowTitle("Validating Paths Against PAK Files")
            progress.setWindowModality(Qt.WindowModal)
            progress.setCancelButton(None)
            progress.setMinimumDuration(0)
            progress.setValue(0)
            
            def update_validation_progress(message, current, total):
                if total > 0:
                    percentage = int((current / total) * 100)
                    progress.setValue(percentage)
                progress.setLabelText(f"{message}\n\nProcessing {current} of {total}...")
                QApplication.processEvents()
            
            success, error, validated_paths = self.path_collector.validate_paths_against_paks(pak_directory, progress_callback=update_validation_progress)
            progress.close()
            
            if not success:
                QMessageBox.critical(self, "Validation Error", f"Failed to validate paths:\n\n{error}\n\nContinuing with unvalidated paths.")
                validated_paths = None
            else:
                valid_count = len(validated_paths)
                invalid_count = total_collected - valid_count
                QMessageBox.information(self, "Validation Complete", f"Validation complete!\n\nValid paths: {valid_count}\nInvalid paths: {invalid_count}\n\nOnly valid paths will be exported.")
        
        output_path, _ = QFileDialog.getSaveFileName(self, "Save Collected Paths", "result.txt", "Text Files (*.txt);;List Files (*.list);;All Files (*.*)")
        if not output_path:
            return
        
        success, error = self.path_collector.export_to_file(output_path, validated_paths)
        
        if success:
            final_count = len(validated_paths) if validated_paths else total_collected
            status = "validated" if validated_paths else "collected"
            QMessageBox.information(self, "Export Complete", f"Successfully exported {final_count} {status} paths!\n\nOutput: lowercase, sorted\nSaved to: {output_path}")
        else:
            QMessageBox.critical(self, "Export Error", f"Failed to export paths:\n\n{error}")
    
    def _improve_list(self):
        if not self.list_file_path:
            QMessageBox.warning(self, "No List File", "Please select an existing list file first.")
            return

        extensions = list(self.analyzer.combined_extensions.keys())
        if not extensions:
            QMessageBox.warning(self, "No Extensions", "No extensions available. Please run the analysis first.")
            return

        if not self.game_exe_path:
            QMessageBox.warning(self, "No Game Path", "Game executable path is not available.")
            return

        pak_directory = os.path.dirname(self.game_exe_path)
        self.path_collector = PathCollector(
            extensions,
            extension_versions=self.analyzer.combined_extensions,
            path_prefix=self.path_prefix,
            include_variations=self.include_variations,
            include_streaming=self.include_streaming
        )

        progress = QProgressDialog("Improving list entries...", None, 0, 100, self)
        progress.setWindowTitle("List Improver")
        progress.setWindowModality(Qt.WindowModal)
        progress.setCancelButton(None)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        def update_progress(message, current, total):
            if total > 0:
                progress.setValue(int((current / total) * 100))
            progress.setLabelText(f"{message}\n\nProcessing {current} of {total}...")
            QApplication.processEvents()

        success, error, stats, validated_paths = self.path_collector.improve_list_with_chunked_validation(
            self.list_file_path,
            pak_directory,
            progress_callback=update_progress
        )
        progress.close()

        if not success:
            QMessageBox.critical(self, "List Improver Error", f"Failed to improve list:\n\n{error}")
            return

        output_path, _ = QFileDialog.getSaveFileName(
            self, "Save Improved List", "improved_result.list",
            "List Files (*.list);;Text Files (*.txt);;All Files (*.*)"
        )
        if not output_path:
            return

        success, error = self.path_collector.export_to_file(output_path, validated_paths)
        if not success:
            QMessageBox.critical(self, "Export Error", f"Failed to export improved list:\n\n{error}")
            return

        QMessageBox.information(
            self,
            "List Improver Complete",
            f"List improver complete!\n\n"
            f"Source list entries: {stats['source_entries']}\n"
            f"Generated combinations: {stats['generated_candidates']}\n"
            f"Validated paths: {stats['validated_paths']}\n\n"
            f"Saved to: {output_path}"
        )

    def _extract_paths_from_exe(self):
        if not self.game_exe_path:
            QMessageBox.warning(self, "Error", "Please select a game executable first.")
            return

        self._run_binary_extraction(
            self.game_exe_path,
            source_label="executable",
            default_filename="exe_paths.txt",
            save_title="Save Extracted Paths"
        )

    def _run_binary_extraction(self, input_path, source_label, default_filename, save_title):
        if not input_path or not self.analyzer.combined_extensions:
            QMessageBox.warning(self, "Error", "Please run the analysis first.")
            return

        self.exe_path_extractor = ExePathExtractor(
            list(self.analyzer.combined_extensions.keys()),
            self.analyzer.combined_extensions,
            self.path_prefix,
            include_variations=self.include_variations,
            include_streaming=self.include_streaming
        )

        progress = QProgressDialog(f"Extracting paths from {source_label}...", "Stop", 0, 100, self)
        progress.setWindowTitle("Extracting File Paths")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setValue(0)

        stopped = False

        def update_progress(msg, cur, total):
            nonlocal stopped
            if progress.wasCanceled():
                stopped = True
                return True
            if total > 0:
                progress.setValue(int((cur / total) * 100))
            progress.setLabelText(msg)
            QApplication.processEvents()
            return False

        success, error, count = self.exe_path_extractor.extract_paths_from_binary_file(
            input_path,
            update_progress,
            source_label=source_label
        )
        progress.close()

        if stopped or not success or count == 0:
            if not stopped:
                msg = error or f"No file paths were found in the {source_label}."
                QMessageBox.warning(self, "Extraction Failed", msg)
            return

        output_path, _ = QFileDialog.getSaveFileName(
            self, save_title, default_filename,
            "Text Files (*.txt);;List Files (*.list);;All Files (*.*)"
        )
        if not output_path:
            return

        success, error = self.exe_path_extractor.export_to_file(output_path)

        if success:
            stats = self.analyzer.get_statistics()
            QMessageBox.information(
                self, "Extraction Complete",
                f"Successfully extracted {count} unique file paths from {source_label}!\n\n"
                f"- {stats['total']} extensions analyzed\n"
                f"- All version combinations included\n"
                f"- Prefix '{self.path_prefix}' applied\n\n"
                f"Saved to: {output_path}"
            )
        else:
            QMessageBox.critical(self, "Export Error", f"Failed to export paths:\n\n{error}")

    def _extract_paths_from_dump(self):
        if not self.analyzer.combined_extensions:
            QMessageBox.warning(self, "Error", "Please run the analysis first.")
            return

        dump_path, _ = QFileDialog.getOpenFileName(
            self, "Select Memory Dump", "",
            "Dump Files (*.dmp *.bin *.mdmp);;All Files (*.*)"
        )
        if not dump_path:
            return

        self._run_binary_extraction(
            dump_path,
            source_label="memory dump",
            default_filename="dump_paths.txt",
            save_title="Save Extracted Paths from Memory Dump"
        )

    def _parse_version_input(self, text):
        new_versions = set()
        for part in text.split(','):
            part = part.strip()
            if part:
                new_versions.add(part.lower())
        
        if not new_versions:
            QMessageBox.warning(self, "Invalid Input", "Please enter at least one valid version identifier.")
            return None
        
        return new_versions