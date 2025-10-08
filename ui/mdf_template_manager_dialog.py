from __future__ import annotations

from typing import Dict, Optional

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QLabel,
    QLineEdit,
    QTextEdit,
    QPushButton,
    QWidget,
    QSplitter,
    QMessageBox,
    QComboBox,
)

from file_handlers.mdf.mdf_template_manager import MdfTemplateManager


class MdfTemplateManagerDialog(QDialog):
    template_imported = Signal(object, dict)

    def __init__(self, parent=None, viewer=None):
        super().__init__(parent)
        self.setWindowTitle("MDF Template Manager")
        self.resize(820, 540)

        self.viewer = viewer
        self._templates: Dict[str, Dict] = {}

        self._create_ui()
        self._connect_signals()

        self._updating_fields = False
        self._loaded_metadata = {}
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.setSingleShot(True)
        self._auto_save_timer.timeout.connect(self._commit_metadata_changes)
        self._ignore_selection_change = False

        self._load_tags()
        self._refresh_templates()
        self._prefill_from_selection()

    def _create_ui(self) -> None:
        main_layout = QVBoxLayout(self)

        splitter = QSplitter(Qt.Horizontal)
        main_layout.addWidget(splitter, 1)

        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)

        filter_layout = QHBoxLayout()
        self.tag_combo = QComboBox()
        self.tag_combo.addItem("All Tags", None)
        filter_layout.addWidget(QLabel("Tag:"))
        filter_layout.addWidget(self.tag_combo, 1)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search templates...")
        filter_layout.addWidget(self.search_edit, 2)

        left_layout.addLayout(filter_layout)

        self.template_list = QListWidget()
        self.template_list.setSelectionMode(QListWidget.SingleSelection)
        left_layout.addWidget(self.template_list, 1)

        splitter.addWidget(left_panel)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)

        name_layout = QHBoxLayout()
        name_layout.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit()
        name_layout.addWidget(self.name_edit, 1)
        right_layout.addLayout(name_layout)

        tags_layout = QHBoxLayout()
        tags_layout.addWidget(QLabel("Tags:"))
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Comma separated tags")
        tags_layout.addWidget(self.tags_edit, 1)
        right_layout.addLayout(tags_layout)

        right_layout.addWidget(QLabel("Description:"))
        self.description_edit = QTextEdit()
        right_layout.addWidget(self.description_edit, 1)

        right_layout.addWidget(QLabel("Preview:"))
        self.preview_label = QLabel("Select a template to preview its details.")
        self.preview_label.setWordWrap(True)
        self.preview_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        right_layout.addWidget(self.preview_label)

        self.source_label = QLabel("Source File: -")
        right_layout.addWidget(self.source_label)
        self.version_label = QLabel("Source Version: -")
        right_layout.addWidget(self.version_label)
        self.created_label = QLabel("Created: -")
        right_layout.addWidget(self.created_label)
        self.modified_label = QLabel("Modified: -")
        right_layout.addWidget(self.modified_label)

        actions_row = QHBoxLayout()
        self.import_button = QPushButton("Import Template")
        actions_row.addWidget(self.import_button)
        self.delete_button = QPushButton("Delete Template")
        actions_row.addWidget(self.delete_button)
        right_layout.addLayout(actions_row)

        right_layout.addStretch(1)

        close_row = QHBoxLayout()
        close_row.addStretch(1)
        self.close_button = QPushButton("Close")
        close_row.addWidget(self.close_button)
        right_layout.addLayout(close_row)

        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)

        if self.viewer is None:
            self.import_button.setEnabled(False)

    def _connect_signals(self) -> None:
        self.tag_combo.currentIndexChanged.connect(lambda _: self._refresh_templates())
        self.search_edit.textChanged.connect(lambda _: self._refresh_templates())
        self.template_list.currentItemChanged.connect(self._on_current_template_changed)
        self.import_button.clicked.connect(self._import_selected_template)
        self.delete_button.clicked.connect(self._delete_selected_template)
        self.close_button.clicked.connect(self.close)
        self.name_edit.editingFinished.connect(self._schedule_metadata_save)
        self.tags_edit.editingFinished.connect(self._schedule_metadata_save)
        self.description_edit.textChanged.connect(self._schedule_metadata_save)

    def _current_template_id(self) -> Optional[str]:
        item = self.template_list.currentItem()
        if not item:
            return None
        return item.data(Qt.UserRole)

    def _refresh_templates(self, select_id: Optional[str] = None) -> None:
        if select_id is None:
            select_id = self._current_template_id()
        templates = MdfTemplateManager.get_template_list()
        tag_filter = self.tag_combo.currentData()
        search_text = (self.search_edit.text() or "").lower().strip()

        self._templates = {tpl["id"]: tpl for tpl in templates}

        self.template_list.blockSignals(True)
        self.template_list.clear()
        chosen_row = -1

        for tpl in templates:
            if tag_filter and tag_filter not in tpl.get("tags", []):
                continue
            if search_text:
                haystack = "\n".join([
                    tpl.get("name", ""),
                    tpl.get("description", ""),
                    ",".join(tpl.get("tags", [])),
                ]).lower()
                if search_text not in haystack:
                    continue
            item = QListWidgetItem(tpl.get("name", "Unnamed"))
            item.setData(Qt.UserRole, tpl.get("id"))
            tooltip_lines = []
            if tpl.get("description"):
                tooltip_lines.append(tpl["description"])
            if tpl.get("tags"):
                tooltip_lines.append("Tags: " + ", ".join(tpl["tags"]))
            if tpl.get("source_file_name"):
                tooltip_lines.append(f"Source: {tpl['source_file_name']}")
            if tpl.get("source_version"):
                tooltip_lines.append(f"Version: {tpl['source_version']}")
            if tooltip_lines:
                item.setToolTip("\n".join(tooltip_lines))
            row = self.template_list.count()
            self.template_list.addItem(item)
            if select_id and tpl.get("id") == select_id:
                chosen_row = row

        self.template_list.blockSignals(False)

        if chosen_row != -1:
            self.template_list.setCurrentRow(chosen_row)
        elif self.template_list.count() > 0:
            self.template_list.setCurrentRow(0)
        else:
            self._show_template_details(None)

    def _load_tags(self) -> None:
        current_tag = self.tag_combo.currentData()
        tags = MdfTemplateManager.get_all_tags()

        self.tag_combo.blockSignals(True)
        self.tag_combo.clear()
        self.tag_combo.addItem("All Tags", None)
        selected_index = 0
        for tag in tags:
            idx = self.tag_combo.count()
            self.tag_combo.addItem(tag, tag)
            if current_tag == tag:
                selected_index = idx
        self.tag_combo.setCurrentIndex(selected_index)
        self.tag_combo.blockSignals(False)

    def _on_current_template_changed(self, current: Optional[QListWidgetItem], previous: Optional[QListWidgetItem]) -> None:
        if self._ignore_selection_change:
            return
        prev_id = previous.data(Qt.UserRole) if previous else None
        current_id = current.data(Qt.UserRole) if current else None
        self._auto_save_timer.stop()
        if prev_id and not self._updating_fields:
            self._commit_metadata_changes(prev_id, select_after=current_id)
        info = self._templates.get(current_id) if current_id else None
        self._show_template_details(info)

    def _show_template_details(self, info: Optional[Dict]) -> None:
        self._auto_save_timer.stop()
        self._updating_fields = True
        if info:
            self.name_edit.setText(info.get("name", ""))
            self.tags_edit.setText(", ".join(info.get("tags", [])))
            self.description_edit.setPlainText(info.get("description", ""))
            source_file = info.get("source_file_name") or "-"
            self.source_label.setText(f"Source File: {source_file}")
            source_version = info.get("source_version")
            version_str = str(source_version) if source_version else "-"
            self.version_label.setText(f"Source Version: {version_str}")
            created = info.get("created") or "-"
            modified = info.get("modified") or "-"
            self.created_label.setText(f"Created: {created}")
            self.modified_label.setText(f"Modified: {modified}")
        else:
            self.name_edit.clear()
            self.tags_edit.clear()
            self.description_edit.clear()
            self.source_label.setText("Source File: -")
            self.version_label.setText("Source Version: -")
            self.created_label.setText("Created: -")
            self.modified_label.setText("Modified: -")
            self._prefill_from_selection()
        self._updating_fields = False
        template_id = info.get("id") if info else self._current_template_id()
        if info:
            self._loaded_metadata = self._collect_metadata()
        else:
            self._loaded_metadata = {}
        self._update_preview(template_id)
        has_selection = info is not None
        self.import_button.setEnabled(has_selection and self.viewer is not None)
        self.delete_button.setEnabled(has_selection)

    def _prefill_from_selection(self) -> None:
        if not self.viewer:
            return
        material, _, _ = self.viewer.get_material_export_context()
        if material and not self.name_edit.text():
            name = material.header.mat_name
            if name:
                self.name_edit.setText(name)

    def _import_selected_template(self) -> None:
        if not self.viewer:
            QMessageBox.warning(self, "Import Template", "The MDF viewer is unavailable.")
            return
        template_id = self._current_template_id()
        if not template_id:
            QMessageBox.warning(self, "Import Template", "Select a template to import.")
            return
        target_version = self.viewer.get_target_version()
        result = MdfTemplateManager.import_template(template_id, target_version)
        if not result.get("success"):
            QMessageBox.warning(self, "Import Template", result.get("message", "Failed to import template."))
            return

        self.template_imported.emit(result.get("material"), result.get("metadata", {}))

    def _delete_selected_template(self) -> None:
        template_id = self._current_template_id()
        if not template_id:
            QMessageBox.warning(self, "Delete Template", "Select a template to delete.")
            return
        confirm = QMessageBox.question(
            self,
            "Delete Template",
            "Are you sure you want to delete this template?",
        )
        if confirm != QMessageBox.Yes:
            return
        if not MdfTemplateManager.delete_template(template_id):
            QMessageBox.warning(self, "Delete Template", "Failed to delete template.")
            return
        self._load_tags()
        self._refresh_templates()

    def _collect_metadata(self) -> Dict[str, object]:
        name = (self.name_edit.text() or "").strip()
        tags = [t.strip() for t in (self.tags_edit.text() or "").split(",") if t.strip()]
        description = (self.description_edit.toPlainText() or "").strip()
        return {"name": name, "tags": tags, "description": description}

    def _schedule_metadata_save(self) -> None:
        if self._updating_fields:
            return
        if not self._current_template_id():
            return
        if self._auto_save_timer.isActive():
            self._auto_save_timer.stop()
        self._auto_save_timer.start(400)

    def _commit_metadata_changes(self, template_id: Optional[str] = None, select_after: Optional[str] = None) -> None:
        if template_id is None:
            template_id = self._current_template_id()
        if not template_id:
            return
        data = self._collect_metadata()
        if data == self._loaded_metadata:
            return
        result = MdfTemplateManager.update_template_metadata(
            template_id,
            name=data["name"],
            tags=data["tags"],
            description=data["description"],
        )
        if not result.get("success"):
            QMessageBox.warning(
                self,
                "Update Template",
                result.get("message", "Failed to update template."),
            )
            self._show_template_details(self._templates.get(template_id))
            return
        new_id = result.get("template_id", template_id)
        tags_changed = data["tags"] != self._loaded_metadata.get("tags")
        self._loaded_metadata = data
        if tags_changed:
            self._load_tags()
        target_id = select_after or new_id
        self._ignore_selection_change = True
        try:
            self._refresh_templates(target_id)
        finally:
            self._ignore_selection_change = False
        if target_id:
            info = self._templates.get(target_id)
            self._show_template_details(info)
        else:
            self._show_template_details(None)

    def _update_preview(self, template_id: Optional[str]) -> None:
        if not template_id:
            self.preview_label.setText("Select a template to preview its details.")
            return
        preview = MdfTemplateManager.get_template_preview(template_id)
        if not preview:
            self.preview_label.setText("Preview unavailable for this template.")
            return
        lines = []
        if preview.get("material_name"):
            lines.append(f"Material: {preview['material_name']}")
        if preview.get("mmtr_path"):
            lines.append(f"mmtrPath: {preview['mmtr_path']}")
        shader = preview.get("shader_type")
        textures = preview.get("texture_count", 0)
        params = preview.get("parameter_count", 0)
        lines.append(f"Shader: {shader if shader is not None else '-'} | Parameters: {params} | Textures: {textures}")
        tex_types = preview.get("texture_types", [])
        if tex_types:
            lines.append("Texture Types: " + ", ".join(tex_types))
        gpu_buffers = preview.get("gpu_buffer_count", 0)
        shader_lod_redirects = preview.get("tex_id_count", 0)
        extra_bits = []
        if gpu_buffers:
            extra_bits.append(f"GPU Buffers: {gpu_buffers}")
        if shader_lod_redirects:
            extra_bits.append(f"Shader LOD Redirects: {shader_lod_redirects}")
        if extra_bits:
            lines.append(" | ".join(extra_bits))
        self.preview_label.setText("\n".join(lines))
