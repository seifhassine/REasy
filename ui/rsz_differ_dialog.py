from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTreeWidget, QTreeWidgetItem, QGroupBox,
    QTextEdit, QFileDialog, QMessageBox, QTabWidget,
    QLineEdit, QFrame
)
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from pathlib import Path
from typing import List, Optional
import os

from file_handlers.rsz.rsz_differ import RszDiffer, DiffResult


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
            error_msg = f"{type(e).__name__}: {str(e)}\n\nTraceback:\n{traceback.format_exc()}"
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
                self.setStatusTip(f"Drop to load {min(2, len(files))} files")
            else:
                self.setStatusTip("Drop to load file")

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
                    "Multiple Files",
                    f"Loaded first 2 files out of {len(files)} dropped files."
                )

        event.acceptProposedAction()
        
    def setup_ui(self):
        self.setWindowTitle("RSZ File Diff")
        self.setMinimumSize(1200, 800)
        
        self.setAcceptDrops(True)
        
        layout = QVBoxLayout(self)
        
        file_section = self.create_file_section()
        layout.addWidget(file_section)
        
        self.compare_button = QPushButton("Compare Files")
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
        self.result_tabs.addTab(self.summary_widget, "Summary")
        
        self.gameobject_tree = self.create_diff_tree()
        self.show_initial_tree_message(self.gameobject_tree)
        self.result_tabs.addTab(self.gameobject_tree, "GameObjects")
        
        self.folder_tree = self.create_diff_tree()
        self.show_initial_tree_message(self.folder_tree)
        self.result_tabs.addTab(self.folder_tree, "Folders")
        
        layout.addWidget(self.result_tabs)
        
    def create_file_section(self) -> QGroupBox:
        group = QGroupBox("This diff viewer is still highly EXPERIMENTAL. Results might not be accurate.")
        layout = QVBoxLayout()
        
        instructions = QLabel("Tip: You can drag and drop 2 RSZ files (SCN/PFB/USER) at once onto this dialog")
        instructions.setWordWrap(True)
        instructions.setStyleSheet("QLabel { font-style: italic; padding: 5px; }")
        layout.addWidget(instructions)
        
        json_layout = QHBoxLayout()
        json_layout.addWidget(QLabel("JSON File (Default):"))
        self.json_path_input = QLineEdit()
        self.json_path_input.setText(self.json_path or "")
        self.json_path_input.setPlaceholderText("Path to type definitions JSON file...")
        self.json_path_input.textChanged.connect(self.on_json_path_changed)
        json_layout.addWidget(self.json_path_input, 1)
        self.json_browse_button = QPushButton("Browse...")
        self.json_browse_button.clicked.connect(self.browse_json_path)
        json_layout.addWidget(self.json_browse_button)
        layout.addLayout(json_layout)
        
        separator = QFrame()
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addWidget(separator)
        
        file1_layout = QHBoxLayout()
        file1_layout.addWidget(QLabel("File 1:"))
        self.file1_label = DropLabel("Drop RSZ file here or click Browse...")
        self.file1_label.file_dropped.connect(lambda path: self.load_file(1, path))
        file1_layout.addWidget(self.file1_label, 1)
        self.file1_button = QPushButton("Browse...")
        self.file1_button.clicked.connect(lambda: self.select_file(1))
        file1_layout.addWidget(self.file1_button)
        layout.addLayout(file1_layout)

        file1_json_layout = QHBoxLayout()
        file1_json_layout.addWidget(QLabel("File 1 JSON Override:"))
        self.file1_json_input = QLineEdit()
        self.file1_json_input.setPlaceholderText("Optional JSON file for File 1...")
        self.file1_json_input.textChanged.connect(lambda text: self.on_file_json_path_changed(1, text))
        file1_json_layout.addWidget(self.file1_json_input, 1)
        self.file1_json_button = QPushButton("Browse...")
        self.file1_json_button.clicked.connect(lambda: self.browse_file_json_path(1))
        file1_json_layout.addWidget(self.file1_json_button)
        layout.addLayout(file1_json_layout)

        file2_layout = QHBoxLayout()
        file2_layout.addWidget(QLabel("File 2:"))
        self.file2_label = DropLabel("Drop RSZ file here or click Browse...")
        self.file2_label.file_dropped.connect(lambda path: self.load_file(2, path))
        file2_layout.addWidget(self.file2_label, 1)
        self.file2_button = QPushButton("Browse...")
        self.file2_button.clicked.connect(lambda: self.select_file(2))
        file2_layout.addWidget(self.file2_button)
        layout.addLayout(file2_layout)

        file2_json_layout = QHBoxLayout()
        file2_json_layout.addWidget(QLabel("File 2 JSON Override:"))
        self.file2_json_input = QLineEdit()
        self.file2_json_input.setPlaceholderText("Optional JSON file for File 2...")
        self.file2_json_input.textChanged.connect(lambda text: self.on_file_json_path_changed(2, text))
        file2_json_layout.addWidget(self.file2_json_input, 1)
        self.file2_json_button = QPushButton("Browse...")
        self.file2_json_button.clicked.connect(lambda: self.browse_file_json_path(2))
        file2_json_layout.addWidget(self.file2_json_button)
        layout.addLayout(file2_json_layout)

        group.setLayout(layout)
        return group
        
    def create_summary_widget(self) -> QTextEdit:
        widget = QTextEdit()
        widget.setReadOnly(True)
        return widget
        
    def create_diff_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setAlternatingRowColors(False) 
        tree.setHeaderLabels(["Object/Field", "Change", "Value"])
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
        html = """
        <html>
        <body style="font-family: 'Segoe UI', Arial, sans-serif;">
            <h3 style="color: #666;">No Comparison Performed</h3>
            <p style="color: #888;">Select two RSZ files (SCN, PFB, or USER) and click "Compare Files" to begin.</p>
            <hr style="border: 1px solid #e0e0e0;">
            <p style="color: #888; font-size: 10pt;">
                Supported file types:<br>
                • SCN files (Scene)<br>
                • PFB files (Prefab)<br>
                • USER files (User data)<br>
            </p>
        </body>
        </html>
        """
        self.summary_widget.setHtml(html)
    
    def show_initial_tree_message(self, tree: QTreeWidget):
        item = QTreeWidgetItem(["Select files and click Compare to see differences", "", ""])
        tree.addTopLevelItem(item)
    
    def on_json_path_changed(self, text: str):
        self.json_path = text if text else None
        if self.file1_data and self.file2_data:
            self.diff_result = None
            self.update_results()

    def on_file_json_path_changed(self, file_number: int, text: str):
        path = text if text else None
        if file_number == 1:
            self.file1_json_path = path
        else:
            self.file2_json_path = path

        if self.file1_data and self.file2_data:
            self.diff_result = None
            self.update_results()

    def browse_json_path(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select JSON Type Definitions File",
            self.json_path or "",
            "JSON Files (*.json);;All Files (*.*)"
        )
        if file_path:
            self.json_path_input.setText(file_path)
            self.json_path = file_path

    def browse_file_json_path(self, file_number: int):
        current_path = self.file1_json_path if file_number == 1 else self.file2_json_path
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            f"Select JSON Override for File {file_number}",
            current_path or "",
            "JSON Files (*.json);;All Files (*.*)"
        )

        if file_path:
            if file_number == 1:
                self.file1_json_input.setText(file_path)
                self.file1_json_path = file_path
            else:
                self.file2_json_input.setText(file_path)
                self.file2_json_path = file_path

    def select_file(self, file_number: int):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            f"Select RSZ File {file_number}",
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
                    QMessageBox.warning(self, "Invalid File", 
                                       "The file does not appear to be a valid RSZ file.\nExpected SCN, PFB, or USR header.")
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
            QMessageBox.critical(self, "Error", f"Failed to read file: {e}")
                
    def compare_files(self):
        if not self.file1_data or not self.file2_data:
            return
            
        self.compare_button.setEnabled(False)
        self.compare_button.setText("Comparing...")
        
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
        self.compare_button.setText("Compare Files")
        
    def on_comparison_error(self, error: str):
        QMessageBox.critical(self, "Comparison Error", f"Failed to compare files: {error}")
        self.compare_button.setEnabled(True)
        self.compare_button.setText("Compare Files")
        
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
            embedded_section = f"""
<h3>Embedded RSZ Data:</h3>
<ul>
<li>File 1: {"Yes" if has_embedded1 else "No"} ({embedded_count1} entries)</li>
<li>File 2: {"Yes" if has_embedded2 else "No"} ({embedded_count2} entries)</li>
</ul>
"""
        
        text = f"""<h2>Comparison Summary</h2>
        
<h3>Files Exported:</h3>
<ul>
<li>File 1: {summary['export_path1']}</li>
<li>File 2: {summary['export_path2']}</li>
</ul>

<h3>Total Instances:</h3>
<ul>
<li>File 1: {total_instances1} instances</li>
<li>File 2: {total_instances2} instances</li>
</ul>
{embedded_section}
<h3>GameObject Changes:</h3>
<ul>
<li><span style="color: green;">Added: {summary['gameobjects_added']}</span></li>
<li><span style="color: red;">Removed: {summary['gameobjects_removed']}</span></li>
<li><span style="color: orange;">Modified: {summary['gameobjects_modified']}</span></li>
</ul>

<h3>Folder Changes:</h3>
<ul>
<li><span style="color: green;">Added: {summary['folders_added']}</span></li>
<li><span style="color: red;">Removed: {summary['folders_removed']}</span></li>
<li><span style="color: orange;">Modified: {summary['folders_modified']}</span></li>
</ul>
"""
        self.summary_widget.setHtml(text)
        
    def update_gameobject_tree(self):
        self.gameobject_tree.clear()
        
        gameobjects_root = QTreeWidgetItem(["🎮 GameObjects", "", ""])
        embedded_root = QTreeWidgetItem(["📦 Embedded RSZ", "", ""])
        
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
                                child = QTreeWidgetItem([f"    {field}", "modified", value.strip()])
                            else:
                                child = QTreeWidgetItem([f"    {clean_change}", "", ""])
                            item.addChild(child)
                else:
                    item.setText(2, diff.details or "")
                
                embedded_root.addChild(item)
                embedded_changes += 1
                
            else:
                status_icon = {"added": "➕", "removed": "➖", "modified": "✏️"}.get(diff.status, "•")
                item = QTreeWidgetItem([f"{status_icon} {diff.name}", diff.status, ""])
                item.setData(0, Qt.UserRole, diff.guid)
                
                if diff.details:
                    if "\n" in diff.details:
                        for change_line in diff.details.split("\n"):
                            if change_line.strip():
                                clean_change = change_line.replace("• ", "").strip()
                                if ":" in clean_change:
                                    field, value = clean_change.split(":", 1)
                                    child = QTreeWidgetItem([f"  {field}", "changed", value.strip()])
                                else:
                                    child = QTreeWidgetItem([f"  {clean_change}", "", ""])
                                item.addChild(child)
                    else:
                        item.setText(2, diff.details)
                
                gameobjects_root.addChild(item)
                go_changes += 1
        
        if go_changes > 0:
            gameobjects_root.setText(0, f"🎮 GameObjects ({go_changes} changes)")
            gameobjects_root.setExpanded(True)
            self.gameobject_tree.addTopLevelItem(gameobjects_root)
        
        if embedded_changes > 0:
            embedded_root.setText(0, f"📦 Embedded RSZ ({embedded_changes} changes)")
            embedded_root.setExpanded(True)
            self.gameobject_tree.addTopLevelItem(embedded_root)
            
    def update_folder_tree(self):
        self.folder_tree.clear()
        
        if not self.diff_result.folder_diffs:
            no_changes = QTreeWidgetItem(["No folder changes detected", "", ""])
            self.folder_tree.addTopLevelItem(no_changes)
            return
        
        folders_root = QTreeWidgetItem([f"📁 Folder Changes ({len(self.diff_result.folder_diffs)})", "", ""])
        
        for diff in self.diff_result.folder_diffs:
            status_icon = {"added": "➕", "removed": "➖", "modified": "✏️"}.get(diff.status, "📁")
            
            item = QTreeWidgetItem([f"{status_icon} {diff.path}", diff.status, diff.details or ""])
            
            folders_root.addChild(item)
        
        folders_root.setExpanded(True)
        self.folder_tree.addTopLevelItem(folders_root)
            
