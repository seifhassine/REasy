from __future__ import annotations

from typing import List

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QMessageBox,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
)

lazy from ui.editor_widgets import styled_label


class MdfTemplateExportDialog(QDialog):
    """Prompt for MDF template export metadata."""

    def __init__(self, parent=None, default_name: str = "", mmtr_path: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Export MDF Template"))
        self.resize(440, 280)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(10)

        self._path_label = None
        if mmtr_path:
            self._path_text = self.tr("MMTR Path: {path}").format(path=mmtr_path)
            self._path_label = styled_label(self._path_text, tone="muted")
            self._path_label.setToolTip(mmtr_path)
            self._path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self._path_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            layout.addWidget(self._path_label)

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit(default_name)
        self.name_edit.setPlaceholderText(self.tr("Template name"))
        form.addRow(self.tr("Name"), self.name_edit)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText(self.tr("Comma separated tags"))
        form.addRow(self.tr("Tags"), self.tags_edit)

        self.description_edit = QTextEdit()
        self.description_edit.setPlaceholderText(
            self.tr("Add an optional description for this template")
        )
        self.description_edit.setAcceptRichText(False)
        form.addRow(self.tr("Description"), self.description_edit)

        layout.addLayout(form, 1)

        button_box = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._path_label is not None:
            self._path_label.setText(self._path_label.fontMetrics().elidedText(
                self._path_text, Qt.ElideMiddle, self._path_label.width()
            ))

    def _on_accept(self) -> None:
        name = (self.name_edit.text() or "").strip()
        if not name:
            QMessageBox.warning(
                self, self.tr("Export Template"), self.tr("Template name is required.")
            )
            return
        self.accept()

    def export_data(self) -> dict:
        name = (self.name_edit.text() or "").strip()
        tags = self._parse_tags(self.tags_edit.text())
        description = (self.description_edit.toPlainText() or "").strip()
        return {"name": name, "tags": tags, "description": description}

    @staticmethod
    def _parse_tags(raw: str) -> List[str]:
        if not raw:
            return []
        parts = [part.strip() for part in raw.split(",")]
        return [part for part in parts if part]
