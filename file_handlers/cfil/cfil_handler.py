import struct
from typing import Optional

from file_handlers.base_handler import BaseFileHandler
from utils.resource_file_utils import resource_version_from_path

from .cfil_file import CfilFile, CFIL_MAGIC


class CfilHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.cfil: Optional[CfilFile] = None
        self.filepath = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        sig = struct.unpack_from('<I', data, 0)[0]
        return sig == CFIL_MAGIC

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        f = CfilFile()
        version = resource_version_from_path(self.filepath or "", "cfil") or 0
        if not f.read(data, version):
            raise ValueError("Failed to parse CFIL")
        self.cfil = f
        self.modified = False

    def rebuild(self) -> bytes:
        if not self.cfil:
            return b""
        result = self.cfil.write()
        self.modified = False
        return result

    def create_viewer(self):
        try:
            from .cfil_viewer import CfilViewer
            v = CfilViewer(self)
            v.modified_changed.connect(self.modified_changed.emit)
            return v
        except Exception:
            return None

