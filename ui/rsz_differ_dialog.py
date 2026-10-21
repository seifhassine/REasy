from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QTreeWidget, QTreeWidgetItem, QWidget,
    QTextEdit, QFileDialog, QMessageBox, QTabWidget,
    QLineEdit
)
from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, QThread, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent
from pathlib import Path
from typing import List, Optional
import os

from file_handlers.rsz.rsz_differ import RszDiffer, DiffResult
lazy from ui.editor_widgets import editor_strip, styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role


COMPARE_FILES_TEXT = QT_TRANSLATE_NOOP("RszDifferDialog", "Compare Files")
_STATUS_MARKS = {
    "added": ("add", "success"),
    "removed": ("remove", "danger"),
    "modified": ("edit", "warning"),
    "changed": ("edit", "warning"),
}


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
        self.setAlignment(Qt.AlignCenter)
        self.setProperty("dropZone", True)

    def dragEnterEvent(self, event: QDragEnterEvent):
        files = _collect_rsz_files(event)
        if files:
            event.acceptProposedAction()
            set_style_role(self, dragActive=True)
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        set_style_role(self, dragActive=False)

    def dropEvent(self, event: QDropEvent):
        files = _collect_rsz_files(event)
        if files:
            event.acceptProposedAction()
            self.file_dropped.emit(files[0])
        else:
            event.ignore()
        set_style_role(self, dragActive=False)


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
        self.setMinimumSize(760, 520)
        self.resize(1100, 760)

        self.setAcceptDrops(True)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self.create_json_bar())

        layout = QVBoxLayout()
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        root.addLayout(layout, 1)

        file_section = self.create_file_section()
        layout.addWidget(file_section)

        self.compare_button = QPushButton(self.tr(COMPARE_FILES_TEXT))
        self.compare_button.clicked.connect(self.compare_files)
        self.compare_button.setEnabled(False)
        self.compare_button.setProperty("variant", "primary")
        compare_row = QHBoxLayout()
        instructions = styled_label(self.tr(
            "Tip: You can drag and drop 2 RSZ files (SCN/PFB/USER) at once onto this dialog"
        ), tone="muted")
        instructions.setWordWrap(True)
        compare_row.addWidget(instructions, 1)
        compare_row.addWidget(self.compare_button)
        layout.addLayout(compare_row)

        self.result_tabs = QTabWidget()

        self.summary_widget = self.create_summary_widget()
        self.show_initial_summary()
        self.result_tabs.addTab(self.summary_widget, line_icon("info"), self.tr("Summary"))

        self.gameobject_tree = self.create_diff_tree()
        self.show_initial_tree_message(self.gameobject_tree)
        self.result_tabs.addTab(self.gameobject_tree, line_icon("gameobject"), self.tr("GameObjects"))

        self.folder_tree = self.create_diff_tree()
        self.show_initial_tree_message(self.folder_tree)
        self.result_tabs.addTab(self.folder_tree, line_icon("folder"), self.tr("Folders"))

        layout.addWidget(self.result_tabs, 1)

    def create_json_bar(self) -> QWidget:
        bar, json_layout = editor_strip("toolbar", (8, 6, 8, 6), 6)
        json_layout.addWidget(QLabel(self.tr("JSON File (Default):")))
        self.json_path_input = QLineEdit()
        self.json_path_input.setText(self.json_path or "")
        self.json_path_input.setPlaceholderText(self.tr("Path to type definitions JSON file..."))
        self.json_path_input.textChanged.connect(self.on_json_path_changed)
        json_layout.addWidget(self.json_path_input, 1)
        self.json_browse_button = QPushButton(self.tr("Browse..."))
        self.json_browse_button.clicked.connect(self.browse_json_path)
        json_layout.addWidget(self.json_browse_button)
        return bar

    def create_file_section(self) -> QWidget:
        section = QWidget()
        layout = QVBoxLayout(section)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        warning = styled_label(self.tr(
            "This diff viewer is still highly EXPERIMENTAL. Results might not be accurate."
        ), banner="warning")
        warning.setWordWrap(True)
        layout.addWidget(warning)

        files = QHBoxLayout()
        files.setSpacing(12)
        files.addLayout(self._add_file_controls(1), 1)
        files.addLayout(self._add_file_controls(2), 1)
        layout.addLayout(files)
        return section

    def _add_file_controls(self, file_number: int) -> QVBoxLayout:
        prefix = f"file{file_number}"
        file_caption = (self.tr("File 1:"), self.tr("File 2:"))[file_number - 1]
        override_caption = (self.tr("File 1 JSON Override:"), self.tr("File 2 JSON Override:"))[file_number - 1]
        override_placeholder = (self.tr("Optional JSON file for File 1..."), self.tr("Optional JSON file for File 2..."))[file_number - 1]
        column = QVBoxLayout()
        column.setSpacing(6)

        header = QHBoxLayout()
        caption = styled_label(file_caption, heading="panel")
        header.addWidget(caption)
        header.addStretch()
        file_button = QPushButton(line_icon("folder_open"), self.tr("Browse..."))
        file_button.clicked.connect(lambda _checked=False: self.select_file(file_number))
        header.addWidget(file_button)
        column.addLayout(header)
        setattr(self, f"{prefix}_button", file_button)

        file_label = DropLabel(self.tr("Drop RSZ file here or click Browse..."))
        file_label.setMinimumHeight(48)
        file_label.file_dropped.connect(lambda path: self.load_file(file_number, path))
        setattr(self, f"{prefix}_label", file_label)
        column.addWidget(file_label)

        row = QHBoxLayout()
        override_label = styled_label(override_caption, tone="muted")
        row.addWidget(override_label)
        json_input = QLineEdit()
        json_input.setPlaceholderText(override_placeholder)
        json_input.textChanged.connect(lambda text: self.on_file_json_path_changed(file_number, text))
        setattr(self, f"{prefix}_json_input", json_input)
        row.addWidget(json_input, 1)
        json_button = QPushButton(self.tr("Browse..."))
        json_button.clicked.connect(lambda _checked=False: self.browse_file_json_path(file_number))
        row.addWidget(json_button)
        setattr(self, f"{prefix}_json_button", json_button)
        column.addLayout(row)
        return column

    def create_summary_widget(self) -> QTextEdit:
        widget = QTextEdit()
        widget.setReadOnly(True)
        c = current_colors()
        widget.document().setDefaultStyleSheet(
            f"h2, h3 {{ color: {c['text_bright']}; font-size: medium; font-weight: 600; }}"
            f"h2 {{ margin-bottom: 6px; }}"
            f"h3 {{ color: {c['text_muted']}; margin-top: 12px; margin-bottom: 2px; }}"
            f".muted {{ color: {c['text_muted']}; }}"
            f".added {{ color: {c['success']}; }}"
            f".removed {{ color: {c['danger']}; }}"
            f".modified {{ color: {c['warning']}; }}"
        )
        return widget

    def create_diff_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setAlternatingRowColors(False)
        tree.setUniformRowHeights(True)
        tree.setProperty("mono", True)
        tree.setHeaderLabels([self.tr("Object/Field"), self.tr("Change"), self.tr("Value")])
        tree.setColumnWidth(0, 380)
        tree.setColumnWidth(1, 110)
        tree.setSortingEnabled(False)

        return tree

    @staticmethod
    def _set_tree_message(tree: QTreeWidget, text: str):
        tree.clear()
        item = QTreeWidgetItem([text, "", ""])
        item.setFlags(Qt.NoItemFlags)
        item.setForeground(0, QColor(current_colors()["text_subtle"]))
        tree.addTopLevelItem(item)

    def _mark_status(self, item: QTreeWidgetItem, status: str, *, icon: bool = True):
        mark = _STATUS_MARKS.get(status)
        if mark is None:
            return
        color = current_colors()[mark[1]]
        item.setForeground(1, QColor(color))
        if icon:
            item.setIcon(0, line_icon(mark[0], color))

    @staticmethod
    def _group_item(text: str, icon: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem([text, "", ""])
        item.setIcon(0, line_icon(icon))
        font = item.font(0)
        font.setBold(True)
        item.setFont(0, font)
        return item

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
        <h3>{heading}</h3>
        <p class="muted">{instructions}</p>
        <p class="muted">
            {supported}<br>
            {scn_type}<br>
            {pfb_type}<br>
            {user_type}
        </p>
        """
        self.summary_widget.setHtml(html)

    def show_initial_tree_message(self, tree: QTreeWidget):
        self._set_tree_message(tree, self.tr("Select files and click Compare to see differences"))
    
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
                self.file1_label.setToolTip(file_path)
            else:
                self.file2_data = data
                self.file2_path = file_path
                self.file2_label.setText(file_name)
                self.file2_label.setToolTip(file_path)
                
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
<li><span class="added">{gameobjects_added}</span></li>
<li><span class="removed">{gameobjects_removed}</span></li>
<li><span class="modified">{gameobjects_modified}</span></li>
</ul>

<h3>{folders_heading}</h3>
<ul>
<li><span class="added">{folders_added}</span></li>
<li><span class="removed">{folders_removed}</span></li>
<li><span class="modified">{folders_modified}</span></li>
</ul>
"""
        self.summary_widget.setHtml(text)
        
    def update_gameobject_tree(self):
        self.gameobject_tree.clear()
        
        gameobjects_root = self._group_item(self.tr("GameObjects"), "gameobject")
        embedded_root = self._group_item(self.tr("Embedded RSZ"), "layers")
        status_labels = {
            "added": self.tr("added"),
            "removed": self.tr("removed"),
            "modified": self.tr("modified"),
        }

        go_changes = 0
        embedded_changes = 0

        for diff in self.diff_result.gameobject_diffs:
            if diff.guid and diff.guid.startswith("embedded"):
                item = QTreeWidgetItem([diff.name, "", ""])

                if diff.details and "\n" in diff.details:
                    for change_line in diff.details.split("\n"):
                        if change_line.strip():
                            clean_change = change_line.replace("• ", "").strip()
                            if ":" in clean_change:
                                field, value = clean_change.split(":", 1)
                                child = QTreeWidgetItem([
                                    field, self.tr("modified"), value.strip()
                                ])
                                self._mark_status(child, "modified", icon=False)
                            else:
                                child = QTreeWidgetItem([clean_change, "", ""])
                            item.addChild(child)
                else:
                    item.setText(2, diff.details or "")

                embedded_root.addChild(item)
                embedded_changes += 1

            else:
                item = QTreeWidgetItem([
                    diff.name, status_labels.get(diff.status, diff.status), ""
                ])
                self._mark_status(item, diff.status)
                item.setData(0, Qt.UserRole, diff.guid)

                if diff.details:
                    if "\n" in diff.details:
                        for change_line in diff.details.split("\n"):
                            if change_line.strip():
                                clean_change = change_line.replace("• ", "").strip()
                                if ":" in clean_change:
                                    field, value = clean_change.split(":", 1)
                                    child = QTreeWidgetItem([
                                        field, self.tr("changed"), value.strip()
                                    ])
                                    self._mark_status(child, "changed", icon=False)
                                else:
                                    child = QTreeWidgetItem([clean_change, "", ""])
                                item.addChild(child)
                    else:
                        item.setText(2, diff.details)

                gameobjects_root.addChild(item)
                go_changes += 1

        if go_changes > 0:
            gameobjects_root.setText(0, self.tr(
                "GameObjects ({count} changes)"
            ).format(count=go_changes))
            gameobjects_root.setExpanded(True)
            self.gameobject_tree.addTopLevelItem(gameobjects_root)

        if embedded_changes > 0:
            embedded_root.setText(0, self.tr(
                "Embedded RSZ ({count} changes)"
            ).format(count=embedded_changes))
            embedded_root.setExpanded(True)
            self.gameobject_tree.addTopLevelItem(embedded_root)

        if not go_changes and not embedded_changes:
            self._set_tree_message(self.gameobject_tree, self.tr("No GameObject changes detected"))

    def update_folder_tree(self):
        self.folder_tree.clear()

        if not self.diff_result.folder_diffs:
            self._set_tree_message(self.folder_tree, self.tr("No folder changes detected"))
            return

        folders_root = self._group_item(
            self.tr("Folder Changes ({count})").format(
                count=len(self.diff_result.folder_diffs)
            ), "folder"
        )
        status_labels = {
            "added": self.tr("added"),
            "removed": self.tr("removed"),
            "modified": self.tr("modified"),
        }

        for diff in self.diff_result.folder_diffs:
            item = QTreeWidgetItem([
                diff.path,
                status_labels.get(diff.status, diff.status),
                diff.details or "",
            ])
            self._mark_status(item, diff.status)
            if diff.status not in _STATUS_MARKS:
                item.setIcon(0, line_icon("folder"))

            folders_root.addChild(item)
        
        folders_root.setExpanded(True)
        self.folder_tree.addTopLevelItem(folders_root)
            
