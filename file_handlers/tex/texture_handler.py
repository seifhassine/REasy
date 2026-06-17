from __future__ import annotations

from typing import Any

from file_handlers.base_handler import BaseFileHandler


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

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        return

    def get_context_menu(self, tree, item, meta: dict):
        return None

    def handle_edit(self, meta: dict[str, Any], new_val, old_val, item, *args):
        pass

    def add_variables(self, target, prefix: str, count: int):
        pass

    def update_strings(self):
        pass

    def create_viewer(self):
        from .tex_viewer import TexViewer

        viewer = TexViewer(self)
        viewer.modified_changed.connect(self.modified_changed.emit)
        return viewer

    def build_dds_bytes(self, image_index: int = 0) -> bytes:
        return bytes(self.raw_data)
