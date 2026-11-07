"""See docs/internals/effect-lifting.md."""
from __future__ import annotations

from collections import Counter, defaultdict
from os.path import commonprefix
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from file_handlers.sdf.shader_graph import ShaderGraphError
lazy from file_handlers.mdf.mdf_file import MdfFile
lazy from ui.background_jobs import run_job
lazy from ui.styles import set_style_role
lazy from utils.resource_file_utils import (
    find_local_resource, natives_split, normalize_resource_path, resource_version_from_path,
)
lazy from .effect_lift import insert_effect, lift_effect
lazy from .material_channels import reach_summary
lazy from .material_context import MaterialInstanceContext
lazy from .material_family import bind_drawn_master, parsed_master
lazy from .material_project import new_material_project


def _game_files(loader, mdf_path: str):
    split = natives_split(mdf_path) if Path(mdf_path).is_file() else None

    def read(path):
        found = loader(path) if loader is not None else None
        return found if found is not None or split is None else find_local_resource(
            split[0] / normalize_resource_path(path))
    return read


def _source(material, read, game):
    def master(path, data):
        return parsed_master(data, game, resource_version_from_path(path, "mmtr"), path)

    document = bind_drawn_master(MaterialInstanceContext.from_material(material), material.header.mmtr_path, read,
                                 master)
    project = new_material_project(document, defer_shader_recovery=True)
    return document, next(node for node in project.graph.nodes if node.kind == "re_engine_recovered_master_material")


class EffectFromMaterialDialog(QDialog):
    def __init__(self, project, document, loader, parent=None):
        super().__init__(parent)
        self.project, self.document, self.loader = project, document, loader
        self.mdf = self.source = self.found = None
        self.function_node = ""
        self.setWindowTitle(self.tr("Effect from Material"))
        self.resize(820, 700)
        root = QVBoxLayout(self)
        intro = QLabel(self.tr(
            "Copies an effect another material draws, such as a rim light or a dissolve, into this one. Choose that "
            "material and tick the parameters that drive the effect: REasy finds the code they change in its "
            "shaders and adds the same code here, reading this material's own values where the other read its own."))
        intro.setWordWrap(True)
        set_style_role(intro, tone="muted")
        root.addWidget(intro)
        form = QFormLayout()
        row = QHBoxLayout()
        self.mdf_path = QLineEdit()
        self.mdf_path.setPlaceholderText(self.tr("An MDF file, or its path in the game's files"))
        self.mdf_path.editingFinished.connect(self._read_mdf)
        browse = QPushButton(self.tr("Browse…"))
        browse.clicked.connect(self._browse)
        row.addWidget(self.mdf_path, 1)
        row.addWidget(browse)
        form.addRow(self.tr("Material file"), row)
        self.material = QComboBox()
        self.material.setPlaceholderText(self.tr("Choose a material"))
        self.material.currentIndexChanged.connect(self._read_material)
        form.addRow(self.tr("Material"), self.material)
        self.name = QLineEdit()
        self.name.setMaxLength(128)
        form.addRow(self.tr("Effect name"), self.name)
        root.addLayout(form)
        self.parameters = QTreeWidget()
        self.parameters.setHeaderLabels((self.tr("Parameter"), self.tr("Value"), self.tr("Changes")))
        self.parameters.setRootIsDecorated(False)
        self.parameters.itemChanged.connect(self._ticked)
        root.addWidget(self.parameters, 2)
        self.find_button = QPushButton(self.tr("Find the Effect"))
        self.find_button.setEnabled(False)
        self.find_button.clicked.connect(self._find)
        root.addWidget(self.find_button, 0, Qt.AlignmentFlag.AlignLeft)
        self.coverage = QTreeWidget()
        self.coverage.setHeaderLabels((self.tr("This material gets"), self.tr("Passes")))
        root.addWidget(self.coverage, 3)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.insert_button = buttons.addButton(self.tr("Insert"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.insert_button.setEnabled(False)
        buttons.accepted.connect(self._insert)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(self, self.tr("Choose a material file"), self.mdf_path.text(),
                                                    self.tr("Material files (*.mdf2.*);;All files (*)"))
        if path:
            self.mdf_path.setText(path)
            self._read_mdf()

    def _read_mdf(self) -> None:
        text = self.mdf_path.text().strip()
        if not text or self.mdf is not None and self.mdf[0] == text:
            return
        found = (text, Path(text).read_bytes()) if Path(text).is_file() else self.loader(text) if self.loader else None
        mdf = MdfFile()
        try:
            if found is None:
                raise ValueError(self.tr("REasy found no file there, in the project, the unpacked files or the "
                                         "game's PAKs."))
            mdf.read(found[1], found[0])
        except ValueError as exc:
            QMessageBox.warning(self, self.tr("Material file not read"), str(exc))
            return
        self.mdf = text, mdf
        self.material.blockSignals(True)
        self.material.clear()
        self.material.addItems([material.header.mat_name for material in mdf.materials])
        self.material.setCurrentIndex(-1)
        self.material.blockSignals(False)
        self._read_material(-1)

    def _read_material(self, index: int) -> None:
        self.source = None
        self.parameters.clear()
        self.find_button.setEnabled(False)
        self._forget()
        if index < 0:
            return
        material = self.mdf[1].materials[index]
        read, game = _game_files(self.loader, self.mdf[0]), self.document.profile.game
        try:
            document, master = run_job(self, self.tr("Reading the material"),
                                       lambda _progress, _cancelled: _source(material, read, game),
                                       status=self.tr("Reading {name}…").format(name=material.header.mat_name))
        except (OSError, ValueError, ShaderGraphError) as exc:
            QMessageBox.warning(self, self.tr("Material not read"), str(exc))
            return
        self.source = document, material
        values = {item.name: item.parameter for item in material.parameters}
        self.parameters.blockSignals(True)
        for parameter in document.material_interface.parameters:
            reach = reach_summary(master, "parameter", parameter.name)
            item = QTreeWidgetItem((parameter.name, ", ".join(
                f"{value:g}" for value in tuple(values.get(parameter.name, ()))[:parameter.byte_size // 4]),
                reach.splitlines()[0]))
            item.setToolTip(2, reach)
            item.setCheckState(0, Qt.CheckState.Unchecked)
            self.parameters.addTopLevelItem(item)
        self.parameters.blockSignals(False)
        self.parameters.resizeColumnToContents(0)

    def _effect(self) -> list[str]:
        return [item.text(0) for item in map(self.parameters.topLevelItem, range(self.parameters.topLevelItemCount()))
                if item.checkState(0) == Qt.CheckState.Checked]

    def _ticked(self, _item, _column) -> None:
        names = self._effect()
        self.find_button.setEnabled(bool(names))
        self._forget()
        if not self.name.isModified():
            prefix = commonprefix(names).strip("_ ") if names else ""
            self.name.setText(f"{prefix or self.tr('Effect')} ({self.source[1].header.mat_name})" if names else "")

    def _forget(self) -> None:
        self.found = None
        self.coverage.clear()
        self.insert_button.setEnabled(False)

    def _find(self) -> None:
        (document, material), effect = self.source, self._effect()
        project, target, name = self.project, self.document, self.name.text().strip()
        try:
            self.found = run_job(self, self.tr("Finding the effect"), lambda _progress, _cancelled: lift_effect(
                project, target, document, material, effect, name=name),
                status=self.tr("Finding what {names} change…").format(names=", ".join(effect)))
        except ShaderGraphError as exc:
            QMessageBox.warning(self, self.tr("Effect not found"), str(exc))
            return
        self._show(self.found)

    def _show(self, lift) -> None:
        self.coverage.clear()

        def group(title, rows=()):
            item = QTreeWidgetItem(self.coverage, (title, ""))
            for row in rows:
                QTreeWidgetItem(item, row)
            item.setExpanded(True)
            return item

        pins = (lift.function.inputs, lift.function.outputs) if lift.function is not None else ((), ())
        names = {pin.pin_id: pin.name for pin in pins[1]}
        written = defaultdict(set)
        for output, (_pin, programs) in lift.outputs.items():
            written[names[output]].update(programs)
        group(self.tr("Writes"), [(label, str(len(programs))) for label, programs in sorted(written.items())])
        group(self.tr("Adds"), [
            *((self.tr("Parameter {name} = {value}").format(name=name, value=", ".join(f"{item:g}" for item in values)),
               "") for name, (_type, values) in lift.parameters.items()),
            *((self.tr("Texture {name} ({path})").format(name=name, path=path or self.tr("no default")), "")
              for name, (_dimension, path) in lift.textures.items())])
        group(self.tr("Reads"), [(pin.name, "") for pin in pins[0]]).setExpanded(False)
        left = defaultdict(Counter)
        for label, program, reason in lift.left_out:
            left[reason][f"{label} · {program}" if label else program] += 1
        missing = group(self.tr("Left out"))
        for reason, rows in sorted(left.items()):
            QTreeWidgetItem(missing, (reason, str(rows.total()))).addChildren(
                [QTreeWidgetItem((row, str(count))) for row, count in rows.items()])
        self.coverage.resizeColumnToContents(0)
        self.insert_button.setEnabled(bool(lift.outputs))

    def _insert(self) -> None:
        if self.found is not None:
            self.function_node = insert_effect(self.project, self.found)
            self.accept()
