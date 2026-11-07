from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler

lazy from .rtex_file import RtexFile, is_rtex, read_rtex
lazy from .rtex_tree import build_rtex_tree_model


class RtexHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.rtex: RtexFile | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return is_rtex(data)

    def supports_editing(self) -> bool:
        return False

    def read(self, data: bytes):
        self.rtex = read_rtex(data)
        self.modified = False

    def rebuild(self) -> bytes:
        raise NotImplementedError("REasy does not write render textures")

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        if self.rtex is None:
            return
        game = self.app.settings.get("game_version", "") if self.app is not None else ""
        tree.setModel(build_rtex_tree_model(self.rtex, game))
        tree.header().setStretchLastSection(True)
        tree.setColumnWidth(0, 260)

    def create_viewer(self):
        return None
