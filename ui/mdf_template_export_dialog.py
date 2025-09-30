from __future__ import annotations

from typing import List

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTextEdit,
    QVBoxLayout,
)


class MdfTemplateExportDialog(QDialog):
    """Prompt for MDF template export metadata."""

    def __init__(self, parent=None, default_name: str = "", mmtr_path: str = "") -> None:
        super().__init__(parent)
        self.setWindowTitle("Export MDF Template")
        self.resize(420, 260)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        if mmtr_path:
            layout.addWidget(QLabel(f"MMTR Path: {mmtr_path}"))

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit(default_name)
        self.name_edit.setPlaceholderText("Template name")
        form.addRow("Name", self.name_edit)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Comma separated tags")
        form.addRow("Tags", self.tags_edit)

        self.description_edit = QTextEdit()
        self.description_edit.setPlaceholderText("Add an optional description for this template")
        self.description_edit.setAcceptRichText(False)
        form.addRow("Description", self.description_edit)

        layout.addLayout(form)

        button_box = QDialogButtonBox(QDialogButtonBox.Cancel | QDialogButtonBox.Save)
        button_box.accepted.connect(self._on_accept)
        button_box.rejected.connect(self.reject)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch(1)
        buttons_layout.addWidget(button_box)
        layout.addLayout(buttons_layout)

    def _on_accept(self) -> None:
        name = (self.name_edit.text() or "").strip()
        if not name:
            QMessageBox.warning(self, "Export Template", "Template name is required.")
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
