import struct
from typing import Optional, Dict, Any

from file_handlers.base_handler import BaseFileHandler
from .mdf_file import MdfFile, MDF_MAGIC


class MdfHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.mdf: Optional[MdfFile] = None
        self.raw_data: bytes | bytearray = b""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic == MDF_MAGIC

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        self.raw_data = data
        f = MdfFile()
        file_path = getattr(self, 'filepath', '') if hasattr(self, 'filepath') else ''
        if not f.read(data, file_path):
            raise ValueError("Failed to parse MDF file")
        self.mdf = f
        self.modified = False

    def rebuild(self) -> bytes:
        if not self.mdf:
            return b""
        result = self.mdf.write()
        self.modified = False
        return result

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        return

    def get_context_menu(self, tree, item, meta: dict):
        return None

    def handle_edit(self, meta: Dict[str, Any], new_val, old_val, item):
        pass

    def add_variables(self, target, prefix: str, count: int):
        pass

    def update_strings(self):
        pass

    def create_viewer(self):
        from .mdf_viewer import MdfViewer
        v = MdfViewer(self)
        v.modified_changed.connect(self.modified_changed.emit)
        v.modified_changed.connect(lambda val: setattr(self, 'modified', bool(val)))
        return v

