from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTreeWidget, QTreeWidgetItem, QGroupBox,
    QTextEdit, QFileDialog, QMessageBox, QTabWidget,
    QLineEdit, QFrame
)
from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from pathlib import Path
from typing import List, Optional
import os

from file_handlers.rsz.rsz_differ import RszDiffer, DiffResult


COMPARE_FILES_TEXT = QT_TRANSLATE_NOOP("RszDifferDialog", "Compare Files")


DROP_LABEL_DEFAULT_STYLE = """
    QLabel {
        background-color: #f0f0f0;
        padding: 10px;
        border: 2px dashed #ccc;
        border-radius: 5px;
        color: #333;
    }
    QLabel:hover {
        background-color: #e8e8e8;
        border-color: #999;
    }
"""

DROP_LABEL_ACCEPT_STYLE = """
    QLabel {
        background-color: #e0f0e0;
        padding: 10px;
        border: 2px solid #4CAF50;
        border-radius: 5px;
        color: #333;
    }
"""


RSZ_EXTENSIONS = {'.scn', '.pfb', '.user'}


def _is_rsz_file(file_path: str) -> bool:
    if not file_path:
        return False

    file_lower = file_path.lower()
    if any(file_lower.endswith(ext) for ext in RSZ_EXTENSIONS):
        return True

    parts = os.path.basename(file_lower).split('.')
    if len(parts) >= 2:
        ext = '.' + parts[-2]
        if ext in RSZ_EXTENSIONS:
            return True

    return False


def _collect_rsz_files(event) -> List[str]:
    if not event.mimeData().hasUrls():
        return []

    files: List[str] = []
    for url in event.mimeData().urls():
        if url.isLocalFile():
            file_path = url.toLocalFile()
            if _is_rsz_file(file_path):
                files.append(file_path)

    return files


class DropLabel(QLabel):
    file_dropped = Signal(str)

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setAcceptDrops(True)
        self.setStyleSheet(DROP_LABEL_DEFAULT_STYLE)

    def dragEnterEvent(self, event: QDragEnterEvent):
        files = _collect_rsz_files(event)
        if files:
            event.acceptProposedAction()
            self.setStyleSheet(DROP_LABEL_ACCEPT_STYLE)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self.setStyleSheet(DROP_LABEL_DEFAULT_STYLE)

    def dropEvent(self, event: QDropEvent):
        files = _collect_rsz_files(event)
        if files:
            event.acceptProposedAction()
            self.file_dropped.emit(files[0])
        else:
            event.ignore()
        self.setStyleSheet(DROP_LABEL_DEFAULT_STYLE)


class DiffWorker(QThread):
    finished = Signal(DiffResult)
    error = Signal(str)
    
    def __init__(
        self,
        file1_data: bytes,
        file2_data: bytes,
        file1_path: str,
        file2_path: str,
        game_version: str,
        json_path: Optional[str] = None,
        file1_json_path: Optional[str] = None,
        file2_json_path: Optional[str] = None,
    ):
        super().__init__()
        self.file1_data = file1_data
        self.file2_data = file2_data
        self.file1_path = file1_path
        self.file2_path = file2_path
        self.game_version = game_version
        self.json_path = json_path
        self.file1_json_path = file1_json_path
        self.file2_json_path = file2_json_path

    def run(self):
        try:
            differ = RszDiffer(self.json_path, self.file1_json_path, self.file2_json_path)
            differ.set_game_version(self.game_version)
            result = differ.compare(self.file1_data, self.file2_data, self.file1_path, self.file2_path)
            self.finished.emit(result)
        except Exception as e:
            import traceback
            error_msg = self.tr(
                "{error_type}: {error}\n\nTraceback:\n{traceback}"
            ).format(
                error_type=type(e).__name__, error=e, traceback=traceback.format_exc()
            )
            self.error.emit(error_msg)


class RszDifferDialog(QDialog):
    def __init__(self, parent=None, game_version="RE4", json_path=None):
        super().__init__(parent)
        self.game_version = game_version
        self.json_path = json_path
        self.file1_data = None
        self.file2_data = None
        self.file1_path = None
        self.file2_path = None
        self.file1_json_path = None
        self.file2_json_path = None
        self.diff_result = None
        self.worker = None
        
        self.setup_ui()
    
    def dragEnterEvent(self, event):
        files = _collect_rsz_files(event)
        if files:
            event.acceptProposedAction()
            if len(files) >= 2:
                self.setStatusTip(self.tr("Drop to load {count} files").format(
                    count=min(2, len(files))
                ))
            else:
                self.setStatusTip(self.tr("Drop to load file"))

    def dropEvent(self, event):
        files = _collect_rsz_files(event)
        if not files:
            return

        if len(files) == 1:
            if not self.file1_path:
                self.load_file(1, files[0])
            elif not self.file2_path:
                self.load_file(2, files[0])
            else:
                self.load_file(1, files[0])

        elif len(files) >= 2:
            self.load_file(1, files[0])
            self.load_file(2, files[1])

            if len(files) > 2:
                QMessageBox.information(
                    self,
                    self.tr("Multiple Files"),
                    self.tr("Loaded first 2 files out of {count} dropped files.").format(
                        count=len(files)
                    )
                )

        event.acceptProposedAction()
        
    def setup_ui(self):
        self.setWindowTitle(self.tr("RSZ File Diff"))
        self.setMinimumSize(1200, 800)
        
        self.setAcceptDrops(True)
        
        layout = QVBoxLayout(self)
        
        file_section = self.create_file_section()
        layout.addWidget(file_section)
        
        self.compare_button = QPushButton(self.tr(COMPARE_FILES_TEXT))
        self.compare_button.clicked.connect(self.compare_files)
        self.compare_button.setEnabled(False)
        self.compare_button.setStyleSheet("""
            QPushButton {
                padding: 10px 20px;
                font-weight: bold;
                font-size: 11pt;
                margin: 10px;
            }
        """)
        layout.addWidget(self.compare_button)
        
        self.result_tabs = QTabWidget()
        
        self.summary_widget = self.create_summary_widget()
        self.show_initial_summary()
        self.result_tabs.addTab(self.summary_widget, self.tr("Summary"))
        
        self.gameobject_tree = self.create_diff_tree()
        self.show_initial_tree_message(self.gameobject_tree)
        self.result_tabs.addTab(self.gameobject_tree, self.tr("GameObjects"))
        
        self.folder_tree = self.create_diff_tree()
        self.show_initial_tree_message(self.folder_tree)
        self.result_tabs.addTab(self.folder_tree, self.tr("Folders"))
        
        layout.addWidget(self.result_tabs)
        
    def create_file_section(self) -> QGroupBox:
        group = QGroupBox(self.tr(
            "This diff viewer is still highly EXPERIMENTAL. Results might not be accurate."
        ))
        layout = QVBoxLayout()
        
        instructions = QLabel(self.tr(
            "Tip: You can drag and drop 2 RSZ files (SCN/PFB/USER) at once onto this dialog"
        ))
        instructions.setWordWrap(True)
        instructions.setStyleSheet("QLabel { font-style: italic; padding: 5px; }")
        layout.addWidget(instructions)
        
        json_layout = QHBoxLayout()
        json_layout.addWidget(QLabel(self.tr("JSON File (Default):")))
        self.json_path_input = QLineEdit()
        self.json_path_input.setText(self.json_path or "")
        self.json_path_input.setPlaceholderText(self.tr("Path to type definitions JSON file..."))
        self.json_path_input.textChanged.connect(self.on_json_path_changed)
        json_layout.addWidget(self.json_path_input, 1)
        self.json_browse_button = QPushButton(self.tr("Browse..."))
        self.json_browse_button.clicked.connect(self.browse_json_path)
        json_layout.addWidget(self.json_browse_button)
        layout.addLayout(json_layout)
        
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addWidget(separator)
        
        self._add_file_controls(layout, 1)
        self._add_file_controls(layout, 2)

        group.setLayout(layout)
        return group

    def _add_file_controls(self, layout, file_number: int):
        prefix = f"file{file_number}"
        file_caption = (self.tr("File 1:"), self.tr("File 2:"))[file_number - 1]
        override_caption = (self.tr("File 1 JSON Override:"), self.tr("File 2 JSON Override:"))[file_number - 1]
        override_placeholder = (self.tr("Optional JSON file for File 1..."), self.tr("Optional JSON file for File 2..."))[file_number - 1]

        def add_browse_row(caption, widget, callback):
            row = QHBoxLayout()
            row.addWidget(QLabel(caption))
            row.addWidget(widget, 1)
            button = QPushButton(self.tr("Browse..."))
            button.clicked.connect(callback)
            row.addWidget(button)
            layout.addLayout(row)
            return button

        file_label = DropLabel(self.tr("Drop RSZ file here or click Browse..."))
        file_label.file_dropped.connect(lambda path: self.load_file(file_number, path))
        setattr(self, f"{prefix}_label", file_label)
        file_button = add_browse_row(
            file_caption, file_label, lambda _checked=False: self.select_file(file_number)
        )
        setattr(self, f"{prefix}_button", file_button)

        json_input = QLineEdit()
        json_input.setPlaceholderText(override_placeholder)
        json_input.textChanged.connect(lambda text: self.on_file_json_path_changed(file_number, text))
        setattr(self, f"{prefix}_json_input", json_input)
        json_button = add_browse_row(
            override_caption, json_input,
            lambda _checked=False: self.browse_file_json_path(file_number),
        )
        setattr(self, f"{prefix}_json_button", json_button)
        
    def create_summary_widget(self) -> QTextEdit:
        widget = QTextEdit()
        widget.setReadOnly(True)
        return widget
        
    def create_diff_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setAlternatingRowColors(False) 
        tree.setHeaderLabels([self.tr("Object/Field"), self.tr("Change"), self.tr("Value")])
        tree.setColumnWidth(0, 450)
        tree.setColumnWidth(1, 120)
        tree.setColumnWidth(2, 500)
        
        tree.setStyleSheet("""
            QTreeWidget {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 10pt;
                outline: none;
            }
            QTreeWidget::item {
                padding: 3px 5px;
            }
            QHeaderView::section {
                padding: 6px;
                font-weight: 600;
                font-size: 10pt;
            }
        """)
        
        tree.setSortingEnabled(False)
        
        return tree
    
    def show_initial_summary(self):
        heading = self.tr("No Comparison Performed")
        instructions = self.tr(
            'Select two RSZ files (SCN, PFB, or USER) and click "Compare Files" to begin.'
        )
        supported = self.tr("Supported file types:")
        scn_type = self.tr("• SCN files (Scene)")
        pfb_type = self.tr("• PFB files (Prefab)")
        user_type = self.tr("• USER files (User data)")
        html = f"""
        <html>
        <body style="font-family: 'Segoe UI', Arial, sans-serif;">
            <h3 style="color: #666;">{heading}</h3>
            <p style="color: #888;">{instructions}</p>
            <hr style="border: 1px solid #e0e0e0;">
            <p style="color: #888; font-size: 10pt;">
                {supported}<br>
                {scn_type}<br>
                {pfb_type}<br>
                {user_type}<br>
            </p>
        </body>
        </html>
        """
        self.summary_widget.setHtml(html)
    
    def show_initial_tree_message(self, tree: QTreeWidget):
        item = QTreeWidgetItem([
            self.tr("Select files and click Compare to see differences"), "", ""
        ])
        tree.addTopLevelItem(item)
    
    def on_json_path_changed(self, text: str):
        self.json_path = text if text else None
        if self.file1_data and self.file2_data:
            self.diff_result = None
            self.update_results()

    def on_file_json_path_changed(self, file_number: int, text: str):
        prefix = "file1" if file_number == 1 else "file2"
        setattr(self, f"{prefix}_json_path", text or None)

        if self.file1_data and self.file2_data:
            self.diff_result = None
            self.update_results()

    def browse_json_path(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select JSON Type Definitions File"),
            self.json_path or "",
            "JSON Files (*.json);;All Files (*.*)"
        )
        if file_path:
            self.json_path_input.setText(file_path)
            self.json_path = file_path

    def browse_file_json_path(self, file_number: int):
        prefix = "file1" if file_number == 1 else "file2"
        current_path = getattr(self, f"{prefix}_json_path")
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select JSON Override for File {file_number}").format(
                file_number=file_number
            ),
            current_path or "",
            "JSON Files (*.json);;All Files (*.*)"
        )

        if file_path:
            getattr(self, f"{prefix}_json_input").setText(file_path)
            setattr(self, f"{prefix}_json_path", file_path)

    def select_file(self, file_number: int):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select RSZ File {file_number}").format(file_number=file_number),
            "",
            "RSZ Files (*.scn *.scn.* *.pfb *.pfb.* *.user *.user.*);;All Files (*.*)"
        )
        
        if file_path:
            self.load_file(file_number, file_path)
            
    def load_file(self, file_number: int, file_path: str):
        try:
            with open(file_path, 'rb') as f:
                data = f.read()
                
            if len(data) >= 4:
                header = data[:4]
                if header not in [b"SCN\x00", b"PFB\x00", b"USR\x00"]:
                    QMessageBox.warning(
                        self, self.tr("Invalid File"),
                        self.tr(
                            "The file does not appear to be a valid RSZ file.\n"
                            "Expected SCN, PFB, or USR header."
                        ),
                    )
                    return
                    
            file_name = Path(file_path).name
            
            if file_number == 1:
                self.file1_data = data
                self.file1_path = file_path
                self.file1_label.setText(file_name)
            else:
                self.file2_data = data
                self.file2_path = file_path
                self.file2_label.setText(file_name)
                
            if self.file1_data and self.file2_data:
                self.compare_button.setEnabled(True)
                
        except Exception as e:
            QMessageBox.critical(
                self, self.tr("Error"), self.tr("Failed to read file: {error}").format(error=e)
            )
                
    def compare_files(self):
        if not self.file1_data or not self.file2_data:
            return
            
        self.compare_button.setEnabled(False)
        self.compare_button.setText(self.tr("Comparing..."))
        
        self.worker = DiffWorker(
            self.file1_data,
            self.file2_data,
            self.file1_path,
            self.file2_path,
            self.game_version,
            self.json_path,
            self.file1_json_path,
            self.file2_json_path,
        )
        self.worker.finished.connect(self.on_comparison_finished)
        self.worker.error.connect(self.on_comparison_error)
        self.worker.start()
        
    def on_comparison_finished(self, result: DiffResult):
        self.diff_result = result
        self.update_results()
        self.compare_button.setEnabled(True)
        self.compare_button.setText(self.tr(COMPARE_FILES_TEXT))
        
    def on_comparison_error(self, error: str):
        QMessageBox.critical(
            self, self.tr("Comparison Error"),
            self.tr("Failed to compare files: {error}").format(error=error),
        )
        self.compare_button.setEnabled(True)
        self.compare_button.setText(self.tr(COMPARE_FILES_TEXT))
        
    def update_results(self):
        if not self.diff_result:
            return
            
        self.update_summary()
        self.update_gameobject_tree()
        self.update_folder_tree()
        
    def update_summary(self):
        summary = self.diff_result.summary
        
        total_instances1 = summary.get('total_instances1', 0)
        total_instances2 = summary.get('total_instances2', 0)
        has_embedded1 = summary.get('has_embedded1', False)
        has_embedded2 = summary.get('has_embedded2', False)
        embedded_count1 = summary.get('embedded_count1', 0)
        embedded_count2 = summary.get('embedded_count2', 0)
        
        embedded_section = ""
        if has_embedded1 or has_embedded2:
            embedded_heading = self.tr("Embedded RSZ Data:")
            file1_embedded = self.tr(
                "File 1: {availability} ({count} entries)"
            ).format(
                availability=self.tr("Yes") if has_embedded1 else self.tr("No"),
                count=embedded_count1,
            )
            file2_embedded = self.tr(
                "File 2: {availability} ({count} entries)"
            ).format(
                availability=self.tr("Yes") if has_embedded2 else self.tr("No"),
                count=embedded_count2,
            )
            embedded_section = f"""
<h3>{embedded_heading}</h3>
<ul>
<li>{file1_embedded}</li>
<li>{file2_embedded}</li>
</ul>
"""

        comparison_heading = self.tr("Comparison Summary")
        files_heading = self.tr("Files Exported:")
        file1_path = self.tr("File 1: {path}").format(path=summary['export_path1'])
        file2_path = self.tr("File 2: {path}").format(path=summary['export_path2'])
        instances_heading = self.tr("Total Instances:")
        file1_instances = self.tr("File 1: {count} instances").format(count=total_instances1)
        file2_instances = self.tr("File 2: {count} instances").format(count=total_instances2)
        gameobjects_heading = self.tr("GameObject Changes:")
        gameobjects_added = self.tr("Added: {count}").format(count=summary['gameobjects_added'])
        gameobjects_removed = self.tr("Removed: {count}").format(count=summary['gameobjects_removed'])
        gameobjects_modified = self.tr("Modified: {count}").format(count=summary['gameobjects_modified'])
        folders_heading = self.tr("Folder Changes:")
        folders_added = self.tr("Added: {count}").format(count=summary['folders_added'])
        folders_removed = self.tr("Removed: {count}").format(count=summary['folders_removed'])
        folders_modified = self.tr("Modified: {count}").format(count=summary['folders_modified'])

        text = f"""<h2>{comparison_heading}</h2>

<h3>{files_heading}</h3>
<ul>
<li>{file1_path}</li>
<li>{file2_path}</li>
</ul>

<h3>{instances_heading}</h3>
<ul>
<li>{file1_instances}</li>
<li>{file2_instances}</li>
</ul>
{embedded_section}
<h3>{gameobjects_heading}</h3>
<ul>
<li><span style="color: green;">{gameobjects_added}</span></li>
<li><span style="color: red;">{gameobjects_removed}</span></li>
<li><span style="color: orange;">{gameobjects_modified}</span></li>
</ul>

<h3>{folders_heading}</h3>
<ul>
<li><span style="color: green;">{folders_added}</span></li>
<li><span style="color: red;">{folders_removed}</span></li>
<li><span style="color: orange;">{folders_modified}</span></li>
</ul>
"""
        self.summary_widget.setHtml(text)
        
    def update_gameobject_tree(self):
        self.gameobject_tree.clear()
        
        gameobjects_root = QTreeWidgetItem([self.tr("🎮 GameObjects"), "", ""])
        embedded_root = QTreeWidgetItem([self.tr("📦 Embedded RSZ"), "", ""])
        status_labels = {
            "added": self.tr("added"),
            "removed": self.tr("removed"),
            "modified": self.tr("modified"),
        }
        
        go_changes = 0
        embedded_changes = 0
        
        for diff in self.diff_result.gameobject_diffs:
            if diff.guid and diff.guid.startswith("embedded"):
                item = QTreeWidgetItem([f"  {diff.name}", "", ""])
                
                if diff.details and "\n" in diff.details:
                    for change_line in diff.details.split("\n"):
                        if change_line.strip():
                            clean_change = change_line.replace("• ", "").strip()
                            if ":" in clean_change:
                                field, value = clean_change.split(":", 1)
                                child = QTreeWidgetItem([
                                    f"    {field}", self.tr("modified"), value.strip()
                                ])
                            else:
                                child = QTreeWidgetItem([f"    {clean_change}", "", ""])
                            item.addChild(child)
                else:
                    item.setText(2, diff.details or "")
                
                embedded_root.addChild(item)
                embedded_changes += 1
                
            else:
                status_icon = {"added": "➕", "removed": "➖", "modified": "✏️"}.get(diff.status, "•")
                item = QTreeWidgetItem([
                    f"{status_icon} {diff.name}", status_labels.get(diff.status, diff.status), ""
                ])
                item.setData(0, Qt.UserRole, diff.guid)
                
                if diff.details:
                    if "\n" in diff.details:
                        for change_line in diff.details.split("\n"):
                            if change_line.strip():
                                clean_change = change_line.replace("• ", "").strip()
                                if ":" in clean_change:
                                    field, value = clean_change.split(":", 1)
                                    child = QTreeWidgetItem([
                                        f"  {field}", self.tr("changed"), value.strip()
                                    ])
                                else:
                                    child = QTreeWidgetItem([f"  {clean_change}", "", ""])
                                item.addChild(child)
                    else:
                        item.setText(2, diff.details)
                
                gameobjects_root.addChild(item)
                go_changes += 1
        
        if go_changes > 0:
            gameobjects_root.setText(0, self.tr(
                "🎮 GameObjects ({count} changes)"
            ).format(count=go_changes))
            gameobjects_root.setExpanded(True)
            self.gameobject_tree.addTopLevelItem(gameobjects_root)
        
        if embedded_changes > 0:
            embedded_root.setText(0, self.tr(
                "📦 Embedded RSZ ({count} changes)"
            ).format(count=embedded_changes))
            embedded_root.setExpanded(True)
            self.gameobject_tree.addTopLevelItem(embedded_root)
            
    def update_folder_tree(self):
        self.folder_tree.clear()
        
        if not self.diff_result.folder_diffs:
            no_changes = QTreeWidgetItem([self.tr("No folder changes detected"), "", ""])
            self.folder_tree.addTopLevelItem(no_changes)
            return
        
        folders_root = QTreeWidgetItem([
            self.tr("📁 Folder Changes ({count})").format(
                count=len(self.diff_result.folder_diffs)
            ), "", ""
        ])
        status_labels = {
            "added": self.tr("added"),
            "removed": self.tr("removed"),
            "modified": self.tr("modified"),
        }
        
        for diff in self.diff_result.folder_diffs:
            status_icon = {"added": "➕", "removed": "➖", "modified": "✏️"}.get(diff.status, "📁")
            
            item = QTreeWidgetItem([
                f"{status_icon} {diff.path}",
                status_labels.get(diff.status, diff.status),
                diff.details or "",
            ])
            
            folders_root.addChild(item)
        
        folders_root.setExpanded(True)
        self.folder_tree.addTopLevelItem(folders_root)
            
