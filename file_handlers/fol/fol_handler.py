from __future__ import annotations

import struct

from file_handlers.base_handler import BaseFileHandler

from .fol_file import FOL_MAGIC, FolFile
from .fol_tree import build_fol_tree_model


class FolHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.filepath: str = ""
        self.fol: FolFile | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return len(data) >= 4 and struct.unpack_from("<I", data)[0] == FOL_MAGIC

    def supports_editing(self) -> bool:
        return False

    def read(self, data: bytes):
        fol = FolFile()
        if not fol.read(data):
            raise ValueError("Failed to parse FOL file")
        self.fol = fol
        self.modified = False

    def rebuild(self) -> bytes:
        raise NotImplementedError("FOL serialization is not implemented")

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        if self.fol is None:
            return

        tree.setModel(build_fol_tree_model(self.fol))
        tree.setUniformRowHeights(True)
        tree.header().setStretchLastSection(True)
        tree.setColumnWidth(0, 320)
        tree.expandToDepth(1)

    def create_viewer(self):
        return None
