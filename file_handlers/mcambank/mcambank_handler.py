import struct
from typing import Any, Dict, Optional

from file_handlers.base_handler import BaseFileHandler
from .mcambank_file import McambankFile, MCAMBANK_MAGIC


class McambankHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.mcambank: Optional[McambankFile] = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 8:
            return False
        magic = struct.unpack_from('<I', data, 4)[0]
        return magic == MCAMBANK_MAGIC

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        bank = McambankFile()
        if not bank.read(data):
            raise ValueError("Failed to parse MCAMBANK file")
        self.mcambank = bank
        self.modified = False

    def rebuild(self) -> bytes:
        if not self.mcambank:
            return b""
        result = self.mcambank.write()
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
        try:
            from .mcambank_viewer import McambankViewer

            viewer = McambankViewer(self)
            viewer.modified_changed.connect(self.modified_changed.emit)
            viewer.modified_changed.connect(lambda val: setattr(self, "modified", bool(val)))
            return viewer
        except Exception:
            return None
