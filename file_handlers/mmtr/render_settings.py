from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QVBoxLayout,
)

lazy from file_handlers.sdf.state_editor import SdfStateEditor

lazy from .pass_purpose import PURPOSES, pass_purpose, purpose_title
lazy from .pipeline_settings import (
    PLAIN_SETTINGS, STATE_KINDS, PipelineSettingError, changed_fields, effective_program, pass_key,
    plain_setting_state, setting_members, state_values, with_plain_setting,
)


class RenderSettingsDialog(QDialog):
    def __init__(self, document, states, parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Render Settings"))
        self.resize(760, 820)
        self.document = document
        self.states = deepcopy(dict(states))
        profile = document.profile.shader_graph_profile
        self._passes = [(lods, member, target.index, program.program_index,
                         program.name if lods == (None,) else self.tr("LOD {lods} · {pass_name}").format(
                             lods=", ".join(map(str, lods)), pass_name=program.name),
                         pass_purpose(member, profile, target.index, program.program_index))
                        for lods, member in setting_members(document)
                        for target in member.sdf.targets for program in target.programs
                        if any(getattr(program, attribute) is not None for attribute, _method in STATE_KINDS.values())]
        root = QVBoxLayout(self)

        plain = QGroupBox(self.tr("In plain terms"))
        plain_layout = QVBoxLayout(plain)
        self._plain: list[tuple[object, QCheckBox, QLabel]] = []
        for setting in PLAIN_SETTINGS:
            row = QHBoxLayout()
            checkbox = QCheckBox(setting.display_title)
            checkbox.setToolTip(setting.display_description)
            checkbox.clicked.connect(lambda _checked, item=setting: self._toggle(item))
            count = QLabel()
            row.addWidget(checkbox, 1)
            row.addWidget(count)
            plain_layout.addLayout(row)
            self._plain.append((setting, checkbox, count))
        root.addWidget(plain)

        passes = QGroupBox(self.tr("Each pass"))
        pass_layout = QVBoxLayout(passes)
        note = QLabel(self.tr("Every field of the selected pass's pipeline state. Changes are saved into a copy of "
                              "the state private to that pass."))
        note.setWordWrap(True)
        pass_layout.addWidget(note)
        selector = QHBoxLayout()
        self.purpose_filter = QComboBox()
        self.purpose_filter.setToolTip(self.tr("List only the passes drawing for one purpose."))
        self.purpose_filter.addItem(self.tr("Every pass ({count})").format(count=len(self._passes)), None)
        for purpose in PURPOSES:
            count = sum(item[5] == purpose for item in self._passes)
            if count:
                self.purpose_filter.addItem(self.tr("{purpose} ({count})").format(
                    purpose=purpose_title(purpose), count=count), purpose)
        self.purpose_filter.currentIndexChanged.connect(lambda _index: self._refresh())
        selector.addWidget(self.purpose_filter)
        self.pass_list = QComboBox()
        self.pass_list.currentIndexChanged.connect(lambda _index: self._show_pass())
        selector.addWidget(self.pass_list, 1)
        self.restore = QPushButton(self.tr("Restore Pass"))
        self.restore.setToolTip(self.tr("Give the selected pass its original pipeline state again."))
        self.restore.clicked.connect(self._restore_pass)
        selector.addWidget(self.restore)
        pass_layout.addLayout(selector)
        self.state_editor = SdfStateEditor(scoped=False)
        self.state_editor.state_edit_requested.connect(self._state_edited)
        pass_layout.addWidget(self.state_editor, 1)
        root.addWidget(passes, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._refresh()

    def _refresh(self) -> None:
        for setting, checkbox, count in self._plain:
            total, holding = plain_setting_state(self.document, self.states, setting)
            checkbox.setEnabled(bool(total))
            checkbox.setCheckState(Qt.CheckState.Checked if total and holding == total else
                                   Qt.CheckState.PartiallyChecked if holding else Qt.CheckState.Unchecked)
            count.setText(self.tr("{count} of {total} passes").format(count=holding, total=total) if total
                          else self.tr("No pass of this material can have it"))
        current = self.pass_list.currentData()
        shown = self.purpose_filter.currentData()
        self.pass_list.blockSignals(True)
        self.pass_list.clear()
        for index, (lods, _member, target_index, program_index, name, purpose) in enumerate(self._passes):
            if shown is not None and purpose != shown:
                continue
            text = f"{name} · {purpose_title(purpose)}"
            if pass_key(target_index, program_index, lods[0]) in self.states:
                text = self.tr("{pass_name} (changed)").format(pass_name=text)
            self.pass_list.addItem(text, index)
        self.pass_list.setCurrentIndex(max(self.pass_list.findData(current), 0))
        self.pass_list.blockSignals(False)
        self._show_pass()

    def _selected(self):
        index = self.pass_list.currentData()
        if index is None:
            return (), None, None
        lods, member, target_index, program_index, _name, _purpose = self._passes[index]
        return ([pass_key(target_index, program_index, lod) for lod in lods],
                member.sdf.targets[target_index].programs[program_index], member)

    def _show_pass(self) -> None:
        keys, program, member = self._selected()
        self.state_editor.setEnabled(program is not None)
        self.restore.setEnabled(bool(keys) and keys[0] in self.states)
        if program is not None:
            self.state_editor.set_program(effective_program(program, self.states.get(keys[0], {})),
                                          member.sdf.profile, {})

    def _toggle(self, setting) -> None:
        total, holding = plain_setting_state(self.document, self.states, setting)
        try:
            self.states = with_plain_setting(self.document, self.states, setting, holding != total)
        except PipelineSettingError as exc:
            QMessageBox.warning(self, setting.display_title, str(exc))
        self._refresh()

    def _state_edited(self, kind: str, values, _isolate: bool) -> None:
        keys, program, _member = self._selected()
        if program is None:
            return
        change = changed_fields(state_values(program, kind), values)
        for key in keys:
            changes = self.states.setdefault(key, {})
            if change:
                changes[kind] = deepcopy(change)
            else:
                changes.pop(kind, None)
            if not changes:
                del self.states[key]
        self._refresh()

    def _restore_pass(self) -> None:
        keys, _program, _member = self._selected()
        for key in keys:
            self.states.pop(key, None)
        self._refresh()


__all__ = ["RenderSettingsDialog"]
