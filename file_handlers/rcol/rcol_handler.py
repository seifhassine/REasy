import os
import struct
from typing import Any

from file_handlers.base_handler import BaseFileHandler
from utils.registry_manager import RegistryManager

from .rcol_file import RcolFile
from .rcol_structures import RCOL_MAGIC


class RcolHandler(BaseFileHandler):

    def __init__(self):
        super().__init__()
        self.rcol: RcolFile | None = None
        self.filepath = ""
        self.file_version = 25

    @staticmethod
    def needs_json_path() -> bool:
        return True

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return len(data) >= 4 and struct.unpack_from("<I", data, 0)[0] == RCOL_MAGIC

    def init_type_registry(self):
        if hasattr(self, "app") and self.app:
            json_path = self.app.settings.get("rcol_json_path")
            if json_path:
                self.type_registry = RegistryManager.instance().get_registry(json_path)

    def _infer_file_version(self) -> int:
        if not self.filepath:
            return 25

        ext = os.path.splitext(self.filepath)[1]
        if ext and len(ext) > 1 and ext[1:].isdigit():
            return int(ext[1:])
        return 25

    def supports_editing(self) -> bool:
        return True

    def read(self, data: bytes):
        self.init_type_registry()
        self.file_version = self._infer_file_version()

        rcol = RcolFile()
        rcol.type_registry = self.type_registry
        if not rcol.read(data, file_version=self.file_version, file_path=self.filepath or ""):
            raise ValueError("Failed to parse RCOL file")

        self.rcol = rcol
        self.modified = False

    def rebuild(self) -> bytes:
        if not self.rcol:
            raise ValueError("No RCOL file loaded")
        result = self.rcol.write(file_version=self.file_version)
        self.modified = False
        return result

    def populate_treeview(self, tree, parent_item, metadata_map: dict):
        if hasattr(tree, "__class__") and tree.__class__.__name__ == "QTreeView":
            return

    def get_context_menu(self, tree, item, meta: dict):
        return None

    def handle_edit(self, meta: dict[str, Any], new_val, old_val, item):
        pass

    def add_variables(self, target, prefix: str, count: int):
        pass

    def update_strings(self):
        pass

    def create_viewer(self):
        try:
            from .rcol_viewer import RcolViewer

            viewer = RcolViewer(self)
            viewer.modified_changed.connect(self.modified_changed.emit)
            return viewer
        except Exception as exc:
            print(f"Failed to create RCOL viewer: {exc}")
            return None
