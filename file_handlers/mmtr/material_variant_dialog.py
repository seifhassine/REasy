from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

lazy from file_handlers.mdf.mdf_file import MdfFile
lazy from utils.resource_file_utils import normalize_resource_path

lazy from .material_variant import (
    MaterialVariantError,
    mmtr_resource_path_from_file,
    resolve_project_output_path,
    suggest_variant_file,
    validate_variant_targets,
)
lazy from .material_variant_sources import (
    MdfVariantSourceChoice,
    make_mdf_source_choice,
)


_ORIGIN_LABELS = {
    "project": QT_TRANSLATE_NOOP("MaterialVariantDialog", "Project"),
    "pak": QT_TRANSLATE_NOOP("MaterialVariantDialog", "PAK"),
    "system": QT_TRANSLATE_NOOP("MaterialVariantDialog", "System"),
    "disk": QT_TRANSLATE_NOOP("MaterialVariantDialog", "Disk"),
}


class MaterialVariantDialog(QDialog):
    def __init__(
        self,
        *,
        project_dir: str,
        source_mmtr_resource_path: str,
        source_mmtr_relative_path: str = "",
        mdf_sources: tuple[MdfVariantSourceChoice, ...] = (),
        pak_loader=None,
        preferred_source: MdfVariantSourceChoice | None = None,
        preferred_materials: tuple[int, ...] = (),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(self.tr("Create Material Variant"))
        self.resize(860, 720)
        self._project_dir = os.path.abspath(project_dir) if project_dir else ""
        self._source_resource = str(source_mmtr_resource_path or "")
        self._source_relative = normalize_resource_path(source_mmtr_relative_path)
        self._source_choices = list(mdf_sources)
        self._active_source: MdfVariantSourceChoice | None = None
        self._preferred_source, self._preferred_materials = preferred_source, tuple(preferred_materials)
        self._pak_loader = pak_loader

        root = QVBoxLayout(self)
        intro = QLabel(
            self.tr(
                "REasy compiles the graph, creates the MMTR and assigned MDF in the open project, and validates both "
                "after save/reload."
            )
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(
            "QLabel { background:#243b2d; color:#d9f1df; border-radius:6px; padding:9px 11px; }"
        )
        root.addWidget(intro)

        project = QLabel(
            self.tr("Output project: {path}").format(
                path=self._project_dir or self.tr("No project open")
            )
        )
        project.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(project)

        source_title = QLabel(
            self.tr("MDFs that currently use {path}").format(
                path=self._source_resource
            )
        )
        source_title.setWordWrap(True)
        root.addWidget(source_title)

        source_tools = QHBoxLayout()
        self.source_search = QLineEdit()
        self.source_search.setPlaceholderText(
            self.tr("Search Project, PAK, and System MDF results…")
        )
        self.source_search.textChanged.connect(self._filter_sources)
        source_tools.addWidget(self.source_search, 1)
        disk_button = QPushButton(self.tr("Add disk MDF…"))
        disk_button.clicked.connect(self._add_disk_source)
        source_tools.addWidget(disk_button)
        self.pak_button = QPushButton(self.tr("Add PAK path…"))
        self.pak_button.setEnabled(callable(self._pak_loader))
        self.pak_button.clicked.connect(self._add_pak_source)
        source_tools.addWidget(self.pak_button)
        root.addLayout(source_tools)

        self.source_results = QListWidget()
        self.source_results.currentItemChanged.connect(self._source_selected)
        root.addWidget(self.source_results, 1)
        self.source_status = QLabel()
        self.source_status.setWordWrap(True)
        root.addWidget(self.source_status)

        form = QFormLayout()
        default_mmtr = suggest_variant_file(
            self._source_relative or "natives/x64/material_variant.mmtr"
        ).replace("\\", "/")
        self.mmtr_output = QLineEdit(default_mmtr)
        self.mmtr_output.setPlaceholderText(
            self.tr("natives/x64/.../new_material.mmtr.<version>")
        )
        self.mmtr_output.textChanged.connect(self._mmtr_output_changed)
        form.addRow(self.tr("New MMTR project path"), self.mmtr_output)

        self.resource_path = QLineEdit()
        self.resource_path.setReadOnly(True)
        self.resource_path.setToolTip(
            self.tr("Derived automatically and written into the selected MDF material rows.")
        )
        form.addRow(self.tr("MDF assignment (automatic)"), self.resource_path)

        self.mdf_output = QLineEdit()
        self.mdf_output.setPlaceholderText(
            self.tr("natives/x64/.../material.mdf2.<version>")
        )
        form.addRow(self.tr("MDF project path"), self.mdf_output)
        root.addLayout(form)
        self._mmtr_output_changed(self.mmtr_output.text())

        root.addWidget(QLabel(self.tr("Materials to assign in the selected MDF")))
        self.materials = QListWidget()
        root.addWidget(self.materials, 1)
        note = QLabel(
            self.tr(
                "Existing values and texture paths are kept by name. New inputs use graph defaults; removed inputs "
                "are deleted."
            )
        )
        note.setWordWrap(True)
        root.addWidget(note)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Save).setText(
            self.tr("Create in Project")
        )
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._populate_sources(preferred_source)

    def _source_text(self, choice: MdfVariantSourceChoice) -> str:
        origin = self.tr(_ORIGIN_LABELS.get(choice.origin, choice.origin.title()))
        count = len(choice.matching_material_indices)
        return self.tr("{origin}  ·  {path}  ·  {count} matching material(s)").format(
            origin=origin,
            path=choice.suggested_output_path or choice.label,
            count=count,
        )

    def _populate_sources(self, select_choice: MdfVariantSourceChoice | None = None) -> None:
        self.source_results.clear()
        selected_item = None
        for choice in self._source_choices:
            item = QListWidgetItem(self._source_text(choice))
            item.setData(Qt.ItemDataRole.UserRole, choice)
            item.setToolTip(choice.path)
            self.source_results.addItem(item)
            if choice is select_choice:
                selected_item = item
        count = self.source_results.count()
        self.source_status.setText(
            self.tr("Found {count} MDF consumer(s) across the active asset sources.").format(
                count=count
            )
            if count
            else self.tr(
                "No matching MDF was found in the active Project, PAK, or System sources. Add one by disk or PAK path."
            )
        )
        if selected_item is not None:
            self.source_results.setCurrentItem(selected_item)
        elif count:
            self.source_results.setCurrentRow(0)

    def _filter_sources(self, value: str) -> None:
        needle = str(value or "").strip().casefold()
        for row in range(self.source_results.count()):
            item = self.source_results.item(row)
            item.setHidden(bool(needle and needle not in item.text().casefold()))

    def _source_selected(self, item, _previous=None) -> None:
        choice = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
        if isinstance(choice, MdfVariantSourceChoice):
            self._set_source(choice)

    def _set_source(self, choice: MdfVariantSourceChoice) -> None:
        try:
            mdf = MdfFile()
            mdf.read(choice.data, choice.path)
        except Exception as exc:
            QMessageBox.critical(self, self.tr("MDF could not be opened"), str(exc))
            return
        self._active_source = choice
        if choice.suggested_output_path:
            self.mdf_output.setText(choice.suggested_output_path)
        self.materials.clear()
        source = normalize_resource_path(self._source_resource).casefold()
        for index, material in enumerate(mdf.materials):
            current_path = str(material.header.mmtr_path or "")
            item = QListWidgetItem(
                f"{material.header.mat_name or self.tr('Unnamed material')}  ·  "
                f"{current_path or self.tr('No MMTR')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            is_match = (index in self._preferred_materials if choice is self._preferred_source
                        and self._preferred_materials else normalize_resource_path(current_path).casefold() == source)
            item.setCheckState(
                Qt.CheckState.Checked if is_match else Qt.CheckState.Unchecked
            )
            self.materials.addItem(item)
        if not choice.matching_material_indices and self.materials.count() == 1:
            self.materials.item(0).setCheckState(Qt.CheckState.Checked)

    def _append_source(self, choice: MdfVariantSourceChoice) -> None:
        key = (choice.origin, choice.suggested_output_path.casefold())
        for existing in self._source_choices:
            if (existing.origin, existing.suggested_output_path.casefold()) == key:
                self._populate_sources(existing)
                return
        self._source_choices.append(choice)
        self._populate_sources(choice)
        self._filter_sources(self.source_search.text())

    def _add_disk_source(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Add source MDF"),
            "",
            self.tr("Material definitions (*.mdf2.*);;All files (*)"),
        )
        if not filename:
            return
        try:
            data = Path(filename).read_bytes()
        except OSError as exc:
            QMessageBox.critical(self, self.tr("MDF could not be opened"), str(exc))
            return
        relative = Path(filename).name
        origin = "disk"
        if self._project_dir:
            try:
                relative = Path(filename).resolve().relative_to(
                    Path(self._project_dir).resolve()
                ).as_posix()
                origin = "project"
            except ValueError:
                pass
        try:
            choice = make_mdf_source_choice(
                data=data,
                path=filename,
                mmtr_resource_path=self._source_resource,
                origin=origin,
                relative_path=relative,
                label=filename,
                require_match=False,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                self.tr("Not an MDF"),
                self.tr("The selected file could not be parsed as an MDF resource: {error}").format(error=exc),
            )
            return
        self._append_source(choice)

    def _add_pak_source(self) -> None:
        if not callable(self._pak_loader):
            return
        path, accepted = QInputDialog.getText(
            self,
            self.tr("Add MDF from PAK"),
            self.tr("In-game PAK path"),
            text="natives/x64/",
        )
        if not accepted or not path.strip():
            return
        relative = normalize_resource_path(path)
        try:
            data = self._pak_loader(relative)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, self.tr("PAK read failed"), str(exc))
            return
        if not data:
            QMessageBox.warning(
                self,
                self.tr("PAK path not found"),
                self.tr("No file was found at that PAK path."),
            )
            return
        try:
            choice = make_mdf_source_choice(
                data=data,
                path=relative,
                mmtr_resource_path=self._source_resource,
                origin="pak",
                relative_path=relative,
                label=relative,
                require_match=False,
            )
        except ValueError as exc:
            QMessageBox.warning(
                self,
                self.tr("Not an MDF"),
                self.tr("The PAK resource could not be parsed as an MDF: {error}").format(error=exc),
            )
            return
        self._append_source(choice)

    def _mmtr_output_changed(self, value: str) -> None:
        self.resource_path.setText(mmtr_resource_path_from_file(value))

    def material_indices(self) -> tuple[int, ...]:
        return tuple(
            int(item.data(Qt.ItemDataRole.UserRole))
            for index in range(self.materials.count())
            for item in (self.materials.item(index),)
            if item.checkState() == Qt.CheckState.Checked
        )

    @property
    def source_choice(self) -> MdfVariantSourceChoice | None:
        return self._active_source

    @property
    def mmtr_output_path(self) -> str:
        return resolve_project_output_path(self._project_dir, self.mmtr_output.text())

    @property
    def mdf_output_path(self) -> str:
        return resolve_project_output_path(self._project_dir, self.mdf_output.text())

    def _accept(self) -> None:
        if self._active_source is None:
            QMessageBox.warning(
                self,
                self.tr("Source MDF required"),
                self.tr("Choose an MDF material instance for this variant."),
            )
            return
        try:
            for text in (self.mmtr_output.text(), self.mdf_output.text()):
                resolve_project_output_path(self._project_dir, text)
            validate_variant_targets(self._source_relative or self._source_resource, self._active_source.path,
                                     self.mmtr_output.text(), self.mdf_output.text())
        except MaterialVariantError as exc:
            QMessageBox.warning(self, self.tr("Invalid project path"), str(exc))
            return
        if not self.material_indices():
            QMessageBox.warning(
                self,
                self.tr("Material required"),
                self.tr("Select at least one MDF material to assign."),
            )
            return
        self.accept()


__all__ = ["MaterialVariantDialog"]
