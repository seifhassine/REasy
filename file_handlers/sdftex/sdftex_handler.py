lazy from PySide6.QtCore import QCoreApplication
lazy from PySide6.QtWidgets import QLabel

from file_handlers.base_handler import BaseFileHandler
lazy from utils.resource_file_utils import resource_version_from_path

lazy from .sdftex_file import SdfTexFile
lazy from .viewer import DistanceFieldViewer


class SdfTexHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.sdftex = None

    @classmethod
    def can_handle(cls, data):
        return data[:4] == b'SDFT'

    def read(self, data):
        version = resource_version_from_path(self.filepath, 'sdftex')
        if version is None:
            raise ValueError('SDFTEX filename needs its numeric resource version')
        self.sdftex = SdfTexFile.from_bytes(data, resource_version=version)
        self.modified = False

    def rebuild(self):
        data = self.sdftex.to_bytes()
        self.modified = False
        return data

    def create_viewer(self):
        viewer = DistanceFieldViewer(self.sdftex)
        note = QLabel(QCoreApplication.translate(
            'DistanceFieldViewer',
            'Standalone grid is shown centered at zero. Open its MESH to see native placement or bake geometry.'))
        note.setWordWrap(True)
        viewer.layout().insertWidget(0, note)
        return viewer
