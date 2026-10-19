from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout

from file_handlers.motion.preview.renderer import MotionPreviewRenderer
from .controls import StrandsControls


class MotionStrandsDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.setWindowTitle(self.tr('Strands · motion preview'))
        self.setWindowModality(Qt.NonModal)
        self.resize(420, 780)
        self.owner = owner
        self.viewport = owner.viewport
        self.handler = owner._target.handler
        self._shown = None
        self.viewport.strands_source = self._source
        self.controls = StrandsControls(self)
        QVBoxLayout(self).addWidget(self.controls)

    def _source(self):
        return None if self._shown is None else (MotionPreviewRenderer.TARGET_KEY, self.handler, self._shown, {})

    def strands_changed(self):
        shown = self.controls.shown()
        if shown != self._shown:
            self._shown = shown
            self.owner.strands_changed()
        self.viewport.update()

    def cleanup(self):
        self.viewport.strands_source = None
        if self._shown is not None:
            self._shown = None
            self.owner.strands_changed()
        self.controls.cleanup()
        self.hide()
        self.deleteLater()
