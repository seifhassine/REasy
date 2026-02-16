import os
import struct
from typing import Optional

from file_handlers.base_handler import BaseFileHandler

from .uvs_file import UVS_MAGIC, UvsFile


class UvsHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.uvs: Optional[UvsFile] = None
        self.filepath: str | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == UVS_MAGIC

    def supports_editing(self) -> bool:
        return True

    def _detect_version(self) -> int:
        if self.filepath:
            suffix = os.path.splitext(self.filepath.lower())[1][1:]
            if suffix.isdigit():
                return int(suffix)
        return 7

    def read(self, data: bytes):
        self.uvs = UvsFile()
        if not self.uvs.read(data, version=self._detect_version()):
            raise ValueError("Failed to parse UVS")
        self.modified = False

    def rebuild(self) -> bytes:
        if not self.uvs:
            return b""
        self.modified = False
        return self.uvs.write()

    def create_viewer(self):
        try:
            from .uvs_viewer import UvsViewer
            viewer = UvsViewer(self)
            viewer.modified_changed.connect(self.modified_changed.emit)
            return viewer
        except Exception:
            return None
