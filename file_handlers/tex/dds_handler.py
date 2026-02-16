from __future__ import annotations

import struct
from typing import Dict, Any

from file_handlers.base_handler import BaseFileHandler

from .dds import DDS_MAGIC


class DdsHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.raw_data: bytes | bytearray = b""

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 4:
            return False
        magic = struct.unpack_from('<I', data, 0)[0]
        return magic == DDS_MAGIC

    def supports_editing(self) -> bool:
        return False

    def read(self, data: bytes):
        self.raw_data = data
        self.modified = False

    def rebuild(self) -> bytes:
        return bytes(self.raw_data)

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
        from .tex_viewer import TexViewer
        v = TexViewer(self)
        v.modified_changed.connect(self.modified_changed.emit)
        return v

