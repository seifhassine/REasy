"""See docs/internals/maba-format.md, Sampling."""
from __future__ import annotations

from PySide6.QtWidgets import (QButtonGroup, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QRadioButton,
                               QVBoxLayout, QWidget)

lazy from ui.editor_widgets import styled_label


def _path_box(text: str, hint: str, tip: str) -> QLineEdit:
    box = QLineEdit(text)
    box.setMinimumWidth(420)
    box.setPlaceholderText(hint)
    box.setToolTip(tip)
    return box


class SampleDialog(QDialog):
    def __init__(self, scopes: list[tuple[str, list]], chosen: int, paths: tuple[str, str], kept, parent: QWidget):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Sample from Motions"))
        self.scopes, self.kept = scopes, kept
        self.skeleton = _path_box(paths[0], self.tr("The character's skeleton (.fbxskel)"), self.tr(
            "The skeleton the motions play on: its joints' rest lengths, and its joint pairs for mirrored motions."))
        self.joint_map = _path_box(paths[1], self.tr("Optional: the character Motion's joint map (.jmap)"), self.tr(
            "Its mirror flags turn the joints of mirrored motions. Without one, mirrored motions mirror as on a "
            "character whose joint map has none."))
        form = QFormLayout()
        form.addRow(self.tr("Skeleton"), self.skeleton)
        form.addRow(self.tr("Joint map"), self.joint_map)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        self.group = QButtonGroup(self)
        for index, (text, items) in enumerate(scopes):
            button = QRadioButton(text)
            button.setEnabled(bool(items))
            self.group.addButton(button, index)
            layout.addWidget(button)
        self.group.button(chosen).setChecked(True)
        self.note = styled_label(tone="muted")
        self.note.setWordWrap(True)
        layout.addWidget(self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(self.tr("Sample"))
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.group.idToggled.connect(self._show_kept)
        self._show_kept()

    def chosen(self) -> list:
        return self.scopes[self.group.checkedId()][1]

    def _show_kept(self, *_) -> None:
        kinds = self.kept(self.chosen())
        self.note.setText(self.tr("Sampled values replace those in the file. Keys of a kind REasy has no rule for "
                                  "keep theirs: {0}.").format(", ".join(kinds)) if kinds else
                          self.tr("Sampled values replace those in the file."))
