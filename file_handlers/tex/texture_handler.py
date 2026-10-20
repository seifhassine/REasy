from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler
lazy from .tex_viewer import TexViewer


class TextureViewerHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.raw_data: bytes | bytearray = b""

    def supports_editing(self) -> bool:
        return False

    def read(self, data: bytes):
        self.raw_data = data
        self.modified = False

    def rebuild(self) -> bytes:
        return bytes(self.raw_data)

    def create_viewer(self):
        viewer = TexViewer(self)
        viewer.modified_changed.connect(self.modified_changed.emit)
        return viewer

    def build_dds_bytes(self) -> bytes:
        return bytes(self.raw_data)
