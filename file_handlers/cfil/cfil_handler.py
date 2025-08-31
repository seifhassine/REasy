import struct
from typing import Optional, Dict, Any

from file_handlers.base_handler import BaseFileHandler
from .cfil_file import CfilFile, CFIL_MAGIC


class CfilHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.cfil: Optional[CfilFile] = None

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
        if not f.read(data):
            raise ValueError("Failed to parse CFIL")
        self.cfil = f
        self.modified = False

    def rebuild(self) -> bytes:
        if not self.cfil:
            return b""
        result = self.cfil.write()
        self.modified = False
        return result

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        if hasattr(tree, '__class__') and tree.__class__.__name__ == 'QTreeView':
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
        try:
            from .cfil_viewer import CfilViewer
            v = CfilViewer(self)
            v.modified_changed.connect(self.modified_changed.emit)
            return v
        except Exception:
            return None

