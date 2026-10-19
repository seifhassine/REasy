import os
from functools import wraps
from time import perf_counter

from PySide6.QtCore import QCoreApplication, QT_TRANSLATE_NOOP, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFileDialog, QTreeWidget, QTreeWidgetItem, QHeaderView,
    QMessageBox, QGroupBox, QFrame,
    QLineEdit, QInputDialog, QCheckBox, QFormLayout, QComboBox,
    QTabWidget, QWidget, QAbstractItemView, QSpinBox
)
from settings import app_settings, remember_setting
from ui.background_jobs import run_job
from ui.styles import get_color_scheme
from tools.file_list_search import format_duration
from tools.file_list_generator import (
    ExePathExtractor,
    ExtensionAnalyzer,
    ImproverMode,
    PathCollector,
    budget_applies,
    gpu_helps,
    numeric_mode,
    validate_game_executable,
    version_sort_key,
    read_list_paths,
)


PROCESSING_PROGRESS_TEXT = QT_TRANSLATE_NOOP(
    "FileListGeneratorDialog",
    "{message}\n\nProcessing {current} of {total}...",
)
EXPORT_ERROR_TITLE = QT_TRANSLATE_NOOP("FileListGeneratorDialog", "Export Error")


def _exclusive_operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self._busy:
            return
        self._busy = True
        self._update_actions()
        try:
            return method(self, *args, **kwargs)
        except Exception as error:
            self.status_label.setText(QCoreApplication.translate(
                "FileListGeneratorDialog", "Operation failed. Check the selected files and try again."))
            QMessageBox.critical(self, QCoreApplication.translate("FileListGeneratorDialog", "File List Generator Error"),
                                 str(error))
        finally:
            self._busy = False
            self._update_actions()
    return wrapped


class FileListGeneratorDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("File List Generator"))
        self.resize(920, 780)
        self.setMinimumSize(720, 680)
        self._busy = False
        self._analysis_ready = False
        settings = app_settings(parent) or {}
        self._colors = get_color_scheme(settings.get("tree_highlight_color"))
        
        self.analyzer = ExtensionAnalyzer()
        self.game_exe_path = None
        self.list_file_path = None
        self.path_collector = None
        self.exe_path_extractor = None
        self.path_prefix = "natives/stm/"
        self.include_variations = False
        self.include_streaming = False
        
        self._create_ui()
        self._search_preferences()
    
    def _button(self, text, callback, *, height=None, bold=False, tooltip=None):
        button = QPushButton(text)
        button.clicked.connect(callback)
        button.setAutoDefault(False)
        if height is not None:
            button.setMinimumHeight(height)
        if bold:
            font = button.font()
            font.setBold(True)
            button.setFont(font)
        if tooltip:
            button.setToolTip(tooltip)
        return button

    def _source_row(self, form, title, placeholder, accessible_name, callback, clear_button=None):
        row = QHBoxLayout()
        field = QLineEdit()
        field.setReadOnly(True)
        field.setPlaceholderText(placeholder)
        field.setAccessibleName(accessible_name)
        browse = QPushButton(self.tr("Browse..."))
        browse.clicked.connect(callback)
        row.addWidget(field, 1)
        row.addWidget(browse)
        if clear_button:
            row.addWidget(clear_button)
        form.addRow(title, row)
        return field, browse

    def _create_ui(self):
        layout = QVBoxLayout(self)
        self._create_info_section(layout)
        self._create_config_section(layout)
        layout.setContentsMargins(18, 18, 18, 14)
        layout.setSpacing(12)
        self._create_run_button(layout)
        self._create_results_section(layout)
        self._create_bottom_buttons(layout)
        self._update_actions()
    
    def _create_info_section(self, layout):
        info_frame = QFrame()
        info_frame.setFrameShape(QFrame.NoFrame)
        info_layout = QVBoxLayout(info_frame)
        
        info_label = QLabel(self.tr(
            "<b>Build and improve game file lists</b><br>"
            "Select a game executable, analyze its extensions, then discover paths or improve an existing list."
        ))
        info_label.setWordWrap(True)
        info_font = info_label.font()
        info_font.setPointSize(info_font.pointSize() + 1)
        info_label.setFont(info_font)
        info_layout.addWidget(info_label)
        
        layout.addWidget(info_frame)
    
    def _create_config_section(self, layout):
        self.config_group = QGroupBox(self.tr("1. Sources and path options"))
        config_layout = QVBoxLayout(self.config_group)
        source_form = QFormLayout()
        config_layout.addLayout(source_form)

        self.exe_label, self.browse_exe_btn = self._source_row(
            source_form, self.tr("Game executable:"), self.tr("Select the game's .exe file"),
            self.tr("Game executable path"), self._browse_game_exe,
        )
        self.clear_list_btn = QPushButton(self.tr("Clear"))
        self.clear_list_btn.clicked.connect(self._clear_list_file)
        self.list_label, self.browse_list_btn = self._source_row(
            source_form, self.tr("Existing list:"), self.tr("Optional — select a .list or .txt file"),
            self.tr("Existing list path"), self._browse_list_file, self.clear_list_btn,
        )

        ext_label = QLabel(self.tr("<b>Extensions to analyze:</b> {extensions}").format(
            extensions=", ".join(self.analyzer.target_extensions)
        ))
        ext_label.setWordWrap(True)
        ext_label.setStyleSheet(f"color: {self._colors['text_muted']};")
        config_layout.addWidget(ext_label)
        
        prefix_layout = QHBoxLayout()
        prefix_label = QLabel(self.tr("Path Prefix:"))
        self.prefix_input = QLineEdit(self.path_prefix)
        self.prefix_input.setPlaceholderText(
            self.tr("e.g., {prefix}").format(prefix="natives/stm/")
        )
        self.prefix_input.textChanged.connect(self._on_prefix_changed)
        prefix_layout.addWidget(prefix_label)
        prefix_layout.addWidget(self.prefix_input)
        config_layout.addLayout(prefix_layout)

        options_layout = QHBoxLayout()
        self.variation_checkbox = QCheckBox(self.tr("x64 / language variants"))
        self.variation_checkbox.setToolTip(self.tr(
            "Add extra x64/language variations when collecting or extracting paths"
        ))
        self.variation_checkbox.toggled.connect(self._on_variations_toggled)
        options_layout.addWidget(self.variation_checkbox)

        self.streaming_checkbox = QCheckBox(self.tr("streaming/ variants"))
        self.streaming_checkbox.setToolTip(self.tr(
            "Add streaming/ variants when collecting or extracting paths"
        ))
        self.streaming_checkbox.toggled.connect(self._on_streaming_toggled)
        options_layout.addWidget(self.streaming_checkbox)
        options_layout.addStretch()
        config_layout.addLayout(options_layout)
        
        layout.addWidget(self.config_group)
    
    def _create_run_button(self, layout):
        buttons_layout = QHBoxLayout()
        
        self.run_btn = self._button(self.tr("Run Analysis"), self._run_analysis, height=40, bold=True)
        buttons_layout.addWidget(self.run_btn)
        self.status_label = QLabel(self.tr("Select a game executable to begin."))
        self.status_label.setWordWrap(True)
        self.status_label.setTextFormat(Qt.PlainText)
        buttons_layout.addWidget(self.status_label, 1)
        layout.addLayout(buttons_layout)

        self.extract_exe_btn = self._button(
            self.tr("Extract from EXE"), self._extract_paths_from_exe, bold=True,
            tooltip=self.tr("Scan embedded file paths in the selected executable."),
        )
        self.extract_dump_btn = self._button(
            self.tr("Extract memory dump"), self._extract_paths_from_dump, bold=True,
            tooltip=self.tr("Scan file paths in a saved memory dump or binary file."),
        )
    
    def _create_results_section(self, layout):
        self.results_group = QGroupBox(self.tr("2. Extension analysis"))
        results_layout = QVBoxLayout(self.results_group)
        search_layout = QHBoxLayout()
        self.results_summary = QLabel(self.tr("No analysis yet"))
        search_layout.addWidget(self.results_summary, 1)
        self.extension_filter = QLineEdit()
        self.extension_filter.setPlaceholderText(self.tr("Filter extensions, versions or source…"))
        self.extension_filter.setClearButtonEnabled(True)
        self.extension_filter.setAccessibleName(self.tr("Filter extension results"))
        self.extension_filter.textChanged.connect(self._filter_results)
        search_layout.addWidget(self.extension_filter, 1)
        results_layout.addLayout(search_layout)
        
        self.results_tree = QTreeWidget()
        self.results_tree.setHeaderLabels([
            self.tr("Extension"), self.tr("Versions"), self.tr("Source")
        ])
        self.results_tree.setRootIsDecorated(False)
        self.results_tree.setUniformRowHeights(True)
        self.results_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.results_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.results_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.results_tree.itemDoubleClicked.connect(self._on_item_double_clicked)
        results_layout.addWidget(self.results_tree)
        
        self.results_hint = QLabel(self.tr("Run analysis to discover extensions. Double-click a Versions cell to edit it."))
        self.results_hint.setWordWrap(True)
        self.results_hint.setTextFormat(Qt.PlainText)
        self.results_hint.setStyleSheet(f"color: {self._colors['text_muted']};")
        results_layout.addWidget(self.results_hint)
        layout.addWidget(self.results_group, 1)

    def _create_bottom_buttons(self, layout):
        self.actions_tabs = QTabWidget()
        discovery = QWidget()
        discovery_layout = QVBoxLayout(discovery)
        discovery_hint = QLabel(self.tr(
            "Collect referenced paths from the game's PAK files, or scan an executable or memory dump. "
            "Variant options above apply to these discovery actions."
        ))
        discovery_hint.setWordWrap(True)
        discovery_layout.addWidget(discovery_hint)
        discovery_buttons = QHBoxLayout()
        self.collect_paths_btn = self._button(self.tr("Collect from PAKs"), self._collect_paths)
        for button in (self.collect_paths_btn, self.extract_exe_btn, self.extract_dump_btn):
            button.setMinimumHeight(36)
            discovery_buttons.addWidget(button)
        discovery_layout.addLayout(discovery_buttons)
        self.actions_tabs.addTab(discovery, self.tr("Discover paths"))

        improve = QWidget()
        improve_layout = QVBoxLayout(improve)
        mode_layout = QHBoxLayout()
        mode_label = QLabel(self.tr("Improvement mode:"))
        self.improver_combo = QComboBox()
        self.improver_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.improver_combo.setMinimumContentsLength(18)
        mode_label.setBuddy(self.improver_combo)
        self._improver_modes = (
            (self.tr("Numeric variants"), self.tr("Generate numeric path variants, validate against PAK hashes, and keep original paths."), ImproverMode.NUMERIC),
            (self.tr("Texture variants"), self.tr("Try texture suffix families without numeric expansion; keep other source paths."), ImproverMode.TEX_VARIANTS),
            (self.tr("Swap extensions"), self.tr("Try known extension/version combinations for each stem and validate against PAK hashes."), ImproverMode.EXTENSION_SWAPS),
            (self.tr("Texture / material / mesh siblings"), self.tr("Try sibling tex, mdf2 and mesh assets, including texture suffix families."), ImproverMode.TEX_MDF2_MESH_SWAPS),
            (self.tr("Stem suffix probes"), self.tr("Probe common stem suffixes, such as spaces, underscores, hyphens and numbered copies."), ImproverMode.STEM_SUFFIX_PROBES),
            (self.tr("Use another game's list"), self.tr("Choose another game's list, validate candidates against this game's PAKs, and keep your existing list."), ImproverMode.CROSS_GAME_VERSION_SWAPS),
            (self.tr("Combined variants"), self.tr("Combine numeric changes with texture suffixes and known "
                                                   "extension/version swaps, without needing intermediate matches. "
                                                   "Original paths are kept."), ImproverMode.COMBINED),
        )
        for name, description, _ in self._improver_modes:
            self.improver_combo.addItem(name)
            self.improver_combo.setItemData(self.improver_combo.count() - 1, description, Qt.ToolTipRole)
        self.improver_combo.currentIndexChanged.connect(self._update_improver_description)
        mode_layout.addWidget(mode_label)
        mode_layout.addWidget(self.improver_combo, 1)
        self.improve_list_btn = self._button(self.tr("Improve and export…"), self._run_selected_improver, height=36)
        mode_layout.addWidget(self.improve_list_btn)
        improve_layout.addLayout(mode_layout)
        compute_layout = QHBoxLayout()
        self.compute_label = QLabel(self.tr("Compute:"))
        compute_layout.addWidget(self.compute_label)
        self.compute_combo = QComboBox()
        self.compute_combo.addItem(self.tr("CPU"), 'cpu')
        self.compute_combo.addItem(self.tr("GPU"), 'gpu')
        self.compute_combo.setToolTip(self.tr("GPU performs exact PAK hashing. If unavailable, choose CPU; GPU failures never silently export incomplete results."))
        compute_layout.addWidget(self.compute_combo)
        self.advanced_numeric_check = QCheckBox(self.tr("Advanced numeric search"))
        self.advanced_numeric_check.setToolTip(self.tr("Probe wider ranges and simultaneous number changes, including synchronized and independent directory/filename IDs."))
        compute_layout.addWidget(self.advanced_numeric_check)
        compute_layout.addStretch()
        improve_layout.addLayout(compute_layout)
        self.numeric_options = QWidget()
        numeric_layout = QHBoxLayout(self.numeric_options)
        numeric_layout.setContentsMargins(0, 0, 0, 0)
        numeric_layout.setSpacing(4)
        self.numeric_field_widgets = []
        for name, label, maximum, default, tooltip in (
            ('numeric_max_spin', self.tr("Max:"), 999999, 9999,
             self.tr("Inclusive range: 0 to Max. Zero-padding is preserved; larger values can grow the field.")),
            ('numeric_depth_spin', self.tr("Fields:"), 3, 2,
             self.tr("Maximum numeric groups changed together. A synchronized ID counts as one group.")),
            ('candidate_budget_spin', self.tr("Budget (M):"), 100_000, 10,
             self.tr("Candidates to test at most, in millions (up to 100,000), standard probes included. The "
                     "search's time is estimated before it starts.")),
        ):
            spin, field_label = QSpinBox(), QLabel(label)
            spin.setRange(1, maximum)
            spin.setValue(default)
            spin.setButtonSymbols(QSpinBox.NoButtons)
            spin.setToolTip(tooltip)
            setattr(self, name, spin)
            numeric_layout.addWidget(field_label)
            numeric_layout.addWidget(spin)
            if name != 'candidate_budget_spin':
                self.numeric_field_widgets.extend((field_label, spin))
        improve_layout.addWidget(self.numeric_options)
        self.advanced_numeric_check.toggled.connect(self._update_search_options)
        self.compute_combo.currentIndexChanged.connect(self._update_search_options)
        self.improver_combo.currentIndexChanged.connect(self._update_search_options)
        self._update_search_options()
        self.improver_description = QLabel()
        self.improver_description.setWordWrap(True)
        improve_layout.addWidget(self.improver_description)
        self.improver_requirement = QLabel(self.tr("Requires analysis and an existing list. Results are exported to a file you choose."))
        self.improver_requirement.setWordWrap(True)
        self.improver_requirement.setStyleSheet(f"color: {self._colors['text_muted']};")
        improve_layout.addWidget(self.improver_requirement)
        self._update_improver_description(0)
        self.actions_tabs.addTab(improve, self.tr("Improve existing list"))
        layout.addWidget(self.actions_tabs)

        bottom_layout = QHBoxLayout()
        bottom_layout.addStretch()
        
        self.close_btn = QPushButton(self.tr("Close"))
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setMinimumWidth(120)
        bottom_layout.addWidget(self.close_btn)
        
        layout.addLayout(bottom_layout)
    
    def _browse_game_exe(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Game Executable"), "",
            "Executable Files (*.exe);;All Files (*.*)"
        )
        if file_path:
            valid, error = validate_game_executable(file_path)
            if not valid:
                QMessageBox.warning(self, self.tr("Invalid Executable"), error)
                return
            
            self.game_exe_path = file_path
            self.exe_label.setText(file_path)
            self._invalidate_analysis()
            self.exe_label.setToolTip(file_path)
    
    def _browse_list_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Existing List File (Optional)"), "", "List Files (*.list *.txt);;All Files (*.*)"
        )
        if file_path:
            self.list_file_path = file_path
            self.list_label.setText(file_path)
            self.list_label.setToolTip(file_path)
            self._invalidate_analysis()
    
    def _clear_list_file(self):
        self.list_file_path = None
        self.list_label.clear()
        self.list_label.setToolTip("")
        self._invalidate_analysis()
    
    def _update_actions(self):
        ready = self._analysis_ready and bool(self.analyzer.combined_extensions) and not self._busy
        self.run_btn.setEnabled(bool(self.game_exe_path) and not self._busy)
        self.clear_list_btn.setEnabled(bool(self.list_file_path) and not self._busy)
        for widget in (self.config_group, self.results_group, self.actions_tabs, self.close_btn):
            widget.setEnabled(not self._busy)
        for button in (self.collect_paths_btn, self.extract_exe_btn, self.extract_dump_btn):
            button.setEnabled(ready)
        self.improve_list_btn.setEnabled(ready and bool(self.list_file_path))

    def _invalidate_analysis(self):
        self._analysis_ready = False
        self.analyzer.clear()
        self.results_tree.clear()
        self.results_summary.setText(self.tr("No analysis yet"))
        self.results_hint.setText(self.tr("Run analysis to discover extensions. Double-click a Versions cell to edit it."))
        self.status_label.setText(self.tr("Ready to analyze.") if self.game_exe_path else self.tr("Select a game executable to begin."))
        self._update_actions()

    def _filter_results(self, text):
        query = text.strip().casefold()
        visible = 0
        total = self.results_tree.topLevelItemCount()
        for index in range(total):
            item = self.results_tree.topLevelItem(index)
            matches = not query or any(query in item.text(column).casefold() for column in range(3))
            item.setHidden(not matches)
            visible += int(matches)
        if self._analysis_ready:
            self.results_summary.setText(self.tr("{visible} / {total} extensions").format(visible=visible, total=total))

    def _update_search_options(self, *_):
        mode = self._improver_modes[self.improver_combo.currentIndex()][2]
        self.advanced_numeric_check.setVisible(numeric_mode(mode))
        self.advanced_numeric_check.setEnabled(numeric_mode(mode))
        advanced = self._advanced(mode)
        offered = gpu_helps(mode, advanced)
        self.compute_label.setVisible(offered)
        self.compute_combo.setVisible(offered)
        bounded = budget_applies(mode, self._compute_backend(mode), advanced)
        self.numeric_options.setVisible(bounded)
        self.numeric_options.setEnabled(bounded)
        for widget in self.numeric_field_widgets:
            widget.setVisible(advanced)
        self.numeric_max_spin.setEnabled(advanced)
        self.numeric_depth_spin.setEnabled(advanced)

    def _advanced(self, mode):
        return self.advanced_numeric_check.isChecked() and numeric_mode(mode)

    def _compute_backend(self, mode):
        return self.compute_combo.currentData() if gpu_helps(mode, self._advanced(mode)) else 'cpu'

    def _search_preferences(self, save=False):
        controls = (
            ('mode', self.improver_combo, 'currentIndex'),
            ('compute', self.compute_combo, 'currentIndex'),
            ('advanced', self.advanced_numeric_check, 'checked'),
            ('maximum', self.numeric_max_spin, 'value'),
            ('depth', self.numeric_depth_spin, 'value'),
            ('budget', self.candidate_budget_spin, 'value'),
        )
        if save:
            remember_setting(self.parent(), 'file_list_search',
                             {key: widget.property(prop) for key, widget, prop in controls})
            return
        preferences = (app_settings(self.parent()) or {}).get('file_list_search', {})
        if isinstance(preferences, dict):
            for key, widget, prop in controls:
                value = preferences.get(key)
                if type(value) is not type(widget.property(prop)):
                    continue
                if prop == 'value':
                    value = max(widget.minimum(), min(widget.maximum(), value))
                if prop != 'currentIndex' or 0 <= value < widget.count():
                    widget.setProperty(prop, value)

    def _update_improver_description(self, index):
        self.improver_description.setText(self._improver_modes[index][1])

    def _run_selected_improver(self):
        if self._busy:
            return
        name, description, mode = self._improver_modes[self.improver_combo.currentIndex()]
        if self._advanced(mode):
            description += self.tr("\n\nAdvanced search: values up to {maximum}, up to {depth} fields at once, {budget} million candidates maximum.").format(
                maximum=self.numeric_max_spin.value(), depth=self.numeric_depth_spin.value(), budget=self.candidate_budget_spin.value())
        if mode is ImproverMode.CROSS_GAME_VERSION_SWAPS:
            self._improve_list_from_other_game(name, description)
        else:
            self._run_list_improver(
                self.list_file_path,
                title=self.tr("List Improver — {mode}").format(mode=name),
                improver_mode=mode,
                mode_name=name,
                description=description,
            )

    def reject(self):
        if not self._busy:
            self._search_preferences(save=True)
            super().reject()

    def closeEvent(self, event):
        if self._busy:
            event.ignore()
        else:
            super().closeEvent(event)

    def _progress_label(self, message, current, total, *, detailed=True, started=None):
        label = self.tr(PROCESSING_PROGRESS_TEXT).format(message=message, current=current, total=total) if detailed else message
        if started is not None:
            seconds = int(perf_counter() - started)
            label += self.tr("\nElapsed: {minutes}:{seconds:02d}").format(minutes=seconds // 60, seconds=seconds % 60)
        return label

    def _job(self, title, status, work, *, stoppable=False, detailed=True, timed=False):
        started, latest, stopped = perf_counter() if timed else None, [status], []

        def operation(progress, cancelled):
            def update(message, current, total):
                latest[0] = message
                progress(current, total)
                if stoppable and cancelled():
                    stopped.append(True)
                    return True
                return False if stoppable else None
            return work(update)
        result = run_job(self, title, operation, status=status, cancellable=stoppable,
                         describe=lambda current, total: self._progress_label(latest[0], current, total,
                                                                              detailed=detailed, started=started))
        return result, bool(stopped)

    def _check_collection_inputs(self):
        if not self.analyzer.combined_extensions:
            QMessageBox.warning(
                self, self.tr("No Extensions"),
                self.tr("No extensions available. Please run the analysis first."),
            )
        elif not self.game_exe_path:
            QMessageBox.warning(
                self, self.tr("No Game Path"), self.tr("Game executable path is not available.")
            )
        else:
            return True
        return False

    def _export_paths(self, exporter, output_path, count, *args, improved=False):
        success, error = exporter.export_to_file(output_path, *args)
        if success:
            self.status_label.setText(self.tr("Exported {count} paths to {path}").format(count=count, path=output_path))
        else:
            message = self.tr("Failed to export improved list:\n\n{error}") if improved else self.tr("Failed to export paths:\n\n{error}")
            QMessageBox.critical(self, self.tr(EXPORT_ERROR_TITLE), message.format(error=error))
        return success

    def _on_prefix_changed(self, text):
        self.path_prefix = text.strip().replace('\\', '/')

    def _on_variations_toggled(self, checked):
        self.include_variations = checked

    def _on_streaming_toggled(self, checked):
        self.include_streaming = checked
    
    @_exclusive_operation
    def _run_analysis(self):
        if not self.game_exe_path:
            return
        self._invalidate_analysis()
        self.status_label.setText(self.tr("Analyzing game executable…"))
        exe_path = self.game_exe_path
        success, error = run_job(self, self.tr("Analyzing Game Executable"),
                                 lambda _progress, _cancelled: self.analyzer.run_extension_dumper(exe_path),
                                 status=self.tr("Running extension dumper..."))
        self.status_label.setText(self.tr("Analysis failed. Select valid inputs and try again."))
        
        if not success:
            QMessageBox.critical(
                self, self.tr("Extension Dumper Error"),
                self.tr("Failed to run extension dumper:\n\n{error}").format(error=error),
            )
            return
        
        if not self.analyzer.dumped_extensions:
            QMessageBox.information(
                self, self.tr("No Extensions Found"),
                self.tr("No extensions were found in the game executable."),
            )
            return
        
        if self.list_file_path:
            success, error = self.analyzer.parse_list_file(self.list_file_path)
            if not success:
                QMessageBox.warning(
                    self, self.tr("List File Parse Warning"),
                    self.tr(
                        "Failed to parse list file:\n{error}\n\nContinuing with dumper results only."
                    ).format(error=error),
                )
        
        self.analyzer.combine_extensions()
        self._display_results()
    
    def _display_results(self):
        self.results_tree.clear()
        source_labels = {
            "Dumper": self.tr("Dumper"),
            "List": self.tr("List"),
            "Dumper + List": self.tr("Dumper + List"),
            "Unknown": self.tr("Unknown"),
        }
        
        for ext, versions in self.analyzer.get_sorted_extensions():
            source = self.analyzer.get_extension_source(ext)
            
            versions_str = ", ".join(sorted(versions, key=version_sort_key))
            
            item = QTreeWidgetItem([ext, versions_str, source_labels.get(source, source)])
            item.setData(1, Qt.UserRole, versions)
            
            color_key = {"Dumper": "success", "List": "info"}.get(source, "text_muted")
            item.setForeground(2, QColor(self._colors[color_key]))
            for column in range(3):
                item.setToolTip(column, item.text(column))
            
            self.results_tree.addTopLevelItem(item)
        
        self._analysis_ready = bool(self.analyzer.combined_extensions)
        stats = self.analyzer.get_statistics()
        self.status_label.setText(self.tr(
            "Analysis complete · {total} extensions · {dumper_only} executable only · {list_only} list only · {both} shared"
        ).format(**stats))
        list_only = self.analyzer.get_list_only_extensions()
        if list_only:
            self.results_hint.setText(self.tr(
                "Review list-only extensions: {extensions}. They were not found in the executable and may be outdated. "
                "Double-click a Versions cell to edit."
            ).format(extensions=", ".join(list_only)))
        else:
            self.results_hint.setText(self.tr("Double-click a Versions cell to edit version identifiers before generating paths."))
        self._filter_results(self.extension_filter.text())
        self._update_actions()
    
    def _on_item_double_clicked(self, item, column):
        if column != 1:
            return
        
        extension = item.text(0)
        current_versions = item.data(1, Qt.UserRole)
        
        current_str = ", ".join(sorted(current_versions or (), key=version_sort_key))
        
        text, ok = QInputDialog.getText(
            self, self.tr("Edit Versions"),
            self.tr(
                "Edit version identifiers for extension '{extension}':\n"
                "(Enter comma-separated values, e.g., 3, 5.ja, 221)"
            ).format(extension=extension),
            QLineEdit.Normal, current_str
        )
        
        if ok:
            new_versions = self._parse_version_input(text)
            if new_versions is None:
                return
            
            versions_str = ", ".join(sorted(new_versions, key=version_sort_key))
            item.setText(1, versions_str)
            item.setData(1, Qt.UserRole, new_versions)
            item.setToolTip(1, versions_str)
            self.analyzer.update_extension_versions(extension, new_versions)
            self._filter_results(self.extension_filter.text())
    
    @_exclusive_operation
    def _collect_paths(self):
        if not self._check_collection_inputs():
            return
        extensions = list(self.analyzer.combined_extensions)
        
        pak_directory = os.path.dirname(self.game_exe_path)
        self.path_collector = PathCollector(
            extensions,
            extension_versions=self.analyzer.combined_extensions,
            path_prefix=self.path_prefix,
            include_variations=self.include_variations,
            include_streaming=self.include_streaming
        )
        
        collector = self.path_collector
        (success, error, _), collection_stopped = self._job(
            self.tr("Collecting Paths from PAK Files"), self.tr("Initializing..."),
            lambda update: collector.collect_from_pak_files(pak_directory, progress_callback=update), stoppable=True,
            detailed=False,
        )

        if collection_stopped:
            total_collected = self.path_collector.get_path_count()
            if total_collected == 0:
                QMessageBox.information(
                    self, self.tr("Collection Stopped"),
                    self.tr("Collection was stopped. No paths were collected."),
                )
                return
            
            QMessageBox.information(
                self, self.tr("Collection Stopped"),
                self.tr(
                    "Collection was stopped early.\n\nPaths collected so far: {count}"
                    "\n\nContinuing with partial results..."
                ).format(count=total_collected),
            )
        
        if not success:
            QMessageBox.critical(
                self, self.tr("PAK Collection Error"),
                self.tr("Failed to collect paths from PAK files:\n\n{error}").format(error=error),
            )
            return
        
        if self.list_file_path:
            success, error = self.path_collector.add_from_list_file(self.list_file_path)
            if not success:
                QMessageBox.warning(
                    self, self.tr("List File Warning"),
                    self.tr(
                        "Failed to add paths from list file:\n{error}\n\nContinuing with PAK paths only."
                    ).format(error=error),
                )
        
        total_collected = self.path_collector.get_path_count()
        validate_response = QMessageBox.question(
            self, self.tr("Validate Paths"),
            self.tr(
                "Collected {count} unique paths.\n\nValidate them against the PAK files so only existing paths are "
                "exported? This may take a few minutes."
            ).format(count=total_collected),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        
        validated_paths = None
        
        if validate_response == QMessageBox.Yes:
            (success, error, validated_paths), _stopped = self._job(
                self.tr("Validating Paths Against PAK Files"), self.tr("Validating paths..."),
                lambda update: collector.validate_paths_against_paks(pak_directory, progress_callback=update),
            )
            
            if not success:
                QMessageBox.critical(
                    self, self.tr("Validation Error"),
                    self.tr(
                        "Failed to validate paths:\n\n{error}\n\nContinuing with unvalidated paths."
                    ).format(error=error),
                )
                validated_paths = None
            else:
                valid_count = len(validated_paths)
                invalid_count = total_collected - valid_count
                QMessageBox.information(
                    self, self.tr("Validation Complete"),
                    self.tr(
                        "Validation complete!\n\nValid paths: {valid_count}\n"
                        "Invalid paths: {invalid_count}\n\nOnly valid paths will be exported."
                    ).format(valid_count=valid_count, invalid_count=invalid_count),
                )
        
        output_path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save Collected Paths"), "result.txt",
            "Text Files (*.txt);;List Files (*.list);;All Files (*.*)",
        )
        if not output_path:
            return
        
        final_count = len(validated_paths) if validated_paths is not None else total_collected
        if self._export_paths(self.path_collector, output_path, final_count, validated_paths):
            status = self.tr("validated") if validated_paths is not None else self.tr("collected")
            QMessageBox.information(
                self, self.tr("Export Complete"),
                self.tr(
                    "Successfully exported {count} {status} paths!\n\n"
                    "Output: lowercase, sorted\nSaved to: {path}"
                ).format(count=final_count, status=status, path=output_path),
            )

    def _confirm_improver_mode(self, mode_name, description, estimate):
        false = estimate.false_matches
        chance = (f"{false:,.2f}" if false >= 0.01 or not false
                  else self.tr("less than 1 in {odds}").format(odds=f"{int(1 / false):,}"))
        message = self.tr(
            "Run mode: {mode}\n\n{description}\n\n"
            "{candidates} candidates, about {duration} (measured on a sample of this search).\n"
            "Expected false matches: {chance} ({unfound} PAK entries the list does not name; paths match by 64-bit hash)."
            "\n\nContinue?").format(
            mode=mode_name, description=description, candidates=f"{estimate.candidates:,}",
            duration=format_duration(estimate.seconds), chance=chance, unfound=f"{estimate.unfound:,}",
        )
        return QMessageBox.question(
            self,
            self.tr("Confirm List Improver Mode"),
            message,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes
        ) == QMessageBox.Yes

    @_exclusive_operation
    def _run_list_improver(
        self,
        source_list_path,
        title,
        improver_mode,
        mode_name,
        description,
        append_from_list_path=None,
        keep_source_paths=True
    ):
        if not source_list_path:
            QMessageBox.warning(
                self, self.tr("No List File"), self.tr("Please select a source list file first.")
            )
            return

        if not self._check_collection_inputs():
            return
        extensions = list(self.analyzer.combined_extensions)
        self._search_preferences(save=True)

        pak_directory = os.path.dirname(self.game_exe_path)
        self.path_collector = PathCollector(
            extensions,
            extension_versions=self.analyzer.combined_extensions,
            path_prefix=self.path_prefix,
            improver_mode=improver_mode,
            compute_backend=self._compute_backend(improver_mode),
            advanced_numeric=self._advanced(improver_mode),
            numeric_maximum=self.numeric_max_spin.value(),
            numeric_depth=self.numeric_depth_spin.value(),
            candidate_budget=self.candidate_budget_spin.value() * 1_000_000,
        )

        collector = self.path_collector
        started = perf_counter()
        (search, result), _stopped = self._job(
            title, self.tr("Preparing the search..."),
            lambda update: collector.prepare_search(source_list_path, pak_directory, progress_callback=update),
            stoppable=True, detailed=False, timed=True,
        )
        searched = perf_counter() - started
        if search is not None:
            try:
                if not self._confirm_improver_mode(mode_name, description, search.estimate):
                    return
                started = perf_counter()
                result, _stopped = self._job(title, self.tr("Improving list entries..."), search.run,
                                             stoppable=True, detailed=False, timed=True)
                searched += perf_counter() - started
            finally:
                search.close()
        success, error, stats, validated_paths = result

        if not success:
            QMessageBox.critical(
                self, self.tr("List Improver Error"),
                self.tr("Failed to improve list:\n\n{error}").format(error=error),
            )
            return

        stats['elapsed_seconds'] = searched
        if stats.get('stopped') and not validated_paths:
            self.status_label.setText(self.tr("Search stopped before any new path was found."))
            return
        if stats.get('stopped') or stats.get('limited'):
            reason = self.tr("Search stopped.") if stats.get('stopped') else self.tr("Candidate budget reached.")
            response = QMessageBox.question(
                self, self.tr("Partial Results"),
                reason + self.tr("\n\nFound {count} validated paths. Export these partial results with the source list?").format(count=len(validated_paths)),
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if response != QMessageBox.Yes:
                return
        export_paths = set(validated_paths)
        source_paths = read_list_paths(source_list_path) if keep_source_paths else set()
        source_path_count = len(source_paths)
        export_paths.update(source_paths)

        base_paths = set()
        base_path_count = 0
        if append_from_list_path:
            base_paths = read_list_paths(append_from_list_path)
            base_path_count = len(base_paths)
            export_paths.update(base_paths)

        output_base = os.path.splitext(append_from_list_path or source_list_path)[0]
        output_path, _ = QFileDialog.getSaveFileName(
            self, self.tr("Save Improved List"), output_base + '_improved.list',
            "List Files (*.list);;Text Files (*.txt);;All Files (*.*)"
        )
        if not output_path:
            return

        if not self._export_paths(self.path_collector, output_path, len(export_paths), export_paths, improved=True):
            return
        summary_lines = []
        if stats.get('device'):
            summary_lines.append(self.tr("Compute: {device}").format(device=stats['device']))
        if 'elapsed_seconds' in stats:
            summary_lines.append(self.tr("Search time: {seconds:.1f} seconds").format(seconds=stats['elapsed_seconds']))
        if stats.get('stopped'):
            summary_lines.append(self.tr("Search stopped; partial results exported."))
        elif stats.get('limited'):
            summary_lines.append(self.tr("Candidate budget reached; partial results exported."))
        else:
            summary_lines.append(self.tr("Selected search finished. This does not mean all PAK paths are known."))
        known_paths = source_paths | base_paths
        summary_lines.append(self.tr("New validated paths: {count}").format(
            count=len(validated_paths - known_paths)))
        if keep_source_paths:
            summary_lines.append(
                self.tr("Source list paths kept: {count}").format(count=source_path_count)
            )
        if append_from_list_path:
            label = self.tr("Base list paths kept: {count}") if keep_source_paths else self.tr("Existing list paths kept: {count}")
            summary_lines.append(label.format(count=base_path_count))
        summary_lines.append(
            self.tr("Final exported paths: {count}").format(count=len(export_paths))
        )
        append_summary = "\n".join(summary_lines) + "\n\n"

        QMessageBox.information(
            self,
            self.tr("List Improver Complete"),
            self.tr(
                "List improver complete!\n\n"
                "Source list entries: {source_entries}\n"
                "Generated combinations: {generated_candidates}\n"
                "Validated paths: {validated_paths}\n"
                "{append_summary}Saved to: {path}"
            ).format(
                source_entries=stats["source_entries"], generated_candidates=stats["generated_candidates"],
                validated_paths=stats["validated_paths"], append_summary=append_summary, path=output_path,
            )
        )

    def _improve_list_from_other_game(self, mode_name, description):
        other_list_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select Source List from Another Game"),
            "",
            "List Files (*.list *.txt);;All Files (*.*)"
        )
        if not other_list_path:
            return

        if not self.list_file_path:
            QMessageBox.warning(
                self, self.tr("No Existing List"),
                self.tr("Please select your existing list file first."),
            )
            return

        self._run_list_improver(
            other_list_path,
            title=self.tr("Cross-Game List Improver"),
            improver_mode=ImproverMode.CROSS_GAME_VERSION_SWAPS,
            mode_name=mode_name,
            description=description,
            append_from_list_path=self.list_file_path,
            keep_source_paths=False
        )

    def _extract_paths_from_exe(self):
        if not self.game_exe_path:
            QMessageBox.warning(
                self, self.tr("Error"), self.tr("Please select a game executable first.")
            )
            return

        self._run_binary_extraction(
            self.game_exe_path,
            source_label=self.tr("executable"),
            default_filename="exe_paths.txt",
            save_title=self.tr("Save Extracted Paths")
        )

    @_exclusive_operation
    def _run_binary_extraction(self, input_path, source_label, default_filename, save_title):
        if not input_path or not self.analyzer.combined_extensions:
            QMessageBox.warning(
                self, self.tr("Error"), self.tr("Please run the analysis first.")
            )
            return

        self.exe_path_extractor = ExePathExtractor(
            list(self.analyzer.combined_extensions),
            self.analyzer.combined_extensions,
            self.path_prefix,
            include_variations=self.include_variations,
            include_streaming=self.include_streaming
        )

        extractor = self.exe_path_extractor
        (success, error, count), stopped = self._job(
            self.tr("Extracting File Paths"), self.tr("Extracting paths from {source}...").format(source=source_label),
            lambda update: extractor.extract_paths_from_binary_file(input_path, update, source_label=source_label),
            stoppable=True, detailed=False,
        )

        if stopped or not success or count == 0:
            if not stopped:
                msg = error or self.tr("No file paths were found in the {source}.").format(
                    source=source_label
                )
                QMessageBox.warning(self, self.tr("Extraction Failed"), msg)
            return

        output_path, _ = QFileDialog.getSaveFileName(
            self, save_title, default_filename,
            "Text Files (*.txt);;List Files (*.list);;All Files (*.*)"
        )
        if not output_path:
            return

        if self._export_paths(self.exe_path_extractor, output_path, count):
            stats = self.analyzer.get_statistics()
            QMessageBox.information(
                self, self.tr("Extraction Complete"),
                self.tr(
                    "Successfully extracted {count} unique file paths from {source}!\n\n"
                    "- {extension_count} extensions analyzed\n"
                    "- All version combinations included\n"
                    "- Prefix '{prefix}' applied\n\n"
                    "Saved to: {path}"
                ).format(
                    count=count, source=source_label,
                    extension_count=stats["total"], prefix=self.path_prefix,
                    path=output_path,
                )
            )

    def _extract_paths_from_dump(self):
        if not self.analyzer.combined_extensions:
            QMessageBox.warning(
                self, self.tr("Error"), self.tr("Please run the analysis first.")
            )
            return

        dump_path, _ = QFileDialog.getOpenFileName(
            self, self.tr("Select Memory Dump"), "",
            "Dump Files (*.dmp *.bin *.mdmp);;All Files (*.*)"
        )
        if not dump_path:
            return

        self._run_binary_extraction(
            dump_path,
            source_label=self.tr("memory dump"),
            default_filename="dump_paths.txt",
            save_title=self.tr("Save Extracted Paths from Memory Dump")
        )

    def _parse_version_input(self, text):
        new_versions = {part.strip().lower() for part in text.split(',') if part.strip()}
        
        if not new_versions:
            QMessageBox.warning(
                self, self.tr("Invalid Input"),
                self.tr("Please enter at least one valid version identifier."),
            )
            return None
        
        return new_versions
