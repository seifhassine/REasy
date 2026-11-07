"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

lazy from file_handlers.motion.preview import CharacterMotions

from ..rules import evaluated


class JcnsPreview(QWidget):
    names_changed = Signal()

    def __init__(self, handler, parent: QWidget | None = None):
        super().__init__(parent)
        self.handler = handler
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.note = QLabel(self.tr("Choose a prefab or character to preview the constraints on, then a motion."), self)
        self.note.setWordWrap(True)
        self.note.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.note)
        if not evaluated(handler.document.model.version):
            self.note.setText(self.tr("REasy does not evaluate version {0} yet: the preview poses the motion without "
                                      "these constraints.").format(handler.document.model.version))
        self.motions = CharacterMotions(handler, self, own=False)
        self.motions.preview.edited = (handler.filepath, handler.document.model)
        self.motions.preview.scene.renderables_changed.connect(self.names_changed)
        if evaluated(handler.document.model.version):
            self.motions.preview.picker.chosen.connect(self.note.hide)
        layout.addWidget(self.motions, 1)

    def names(self) -> dict[int, str]:
        poser = self.motions.preview.poser
        return {} if poser is None else {value: name for entry in poser.rigs
                                         for value, name in zip(entry.rig.hashes, entry.rig.names)}

    def refresh(self) -> None:
        self.motions.preview.rebind()

    def cleanup(self) -> None:
        self.motions.cleanup()

    def viewport(self):
        return self.motions.preview.scene.preview

    def constrained(self) -> tuple | None:
        """(poser, rig index, evaluation) of the rig the edited file constrains; None when none does."""
        poser = self.motions.preview.poser
        found = None if poser is None else poser.constrained(self.handler.document.model)
        return None if found is None else (poser, *found)

    def rig(self):
        poser = self.motions.preview.poser
        if poser is None:
            return None
        found = self.constrained()
        rigs = [entry.rig for entry in poser.rigs]
        return rigs[found[1]] if found else max(rigs, key=lambda rig: len(rig.hashes), default=None)
