lazy from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QVBoxLayout

lazy from utils.resource_file_utils import resource_key
lazy from .controls import StrandsControls


class MotionStrandsDialog(QDialog):
    def __init__(self, preview, handler):
        super().__init__(preview)
        self.setWindowTitle(self.tr('Strands · motion preview'))
        self.setWindowModality(Qt.NonModal)
        self.resize(420, 780)
        self.preview, self.handler = preview, handler
        self.viewport = preview.scene.preview
        self._shown = None
        self.viewport.strands_source = self._source
        self.controls = StrandsControls(self)
        QVBoxLayout(self).addWidget(self.controls)

    def _source(self):
        loaded = self.controls.loaded
        if self._shown is None or loaded is None:
            return None
        bound = resource_key(loaded.binding_mesh)
        held = next((mesh for mesh in self.preview.scene.loaded_meshes()
                     if resource_key(mesh.renderable.mesh_path) == bound), None)
        return None if held is None else (held.key, held.handler, self._shown, {})

    def strands_changed(self):
        shown = self.controls.shown()
        if shown != self._shown:
            self._shown = shown
            self.preview.scene.strands_changed()
        self.viewport.update()

    def cleanup(self):
        self.viewport.strands_source = None
        if self._shown is not None:
            self._shown = None
            self.preview.scene.strands_changed()
        self.controls.cleanup()
        self.hide()
        self.deleteLater()
