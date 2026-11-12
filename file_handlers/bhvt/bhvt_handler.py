"""See docs/internals/bhvt-editor.md."""

from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

from .files import MAGICS
lazy from utils.registry_manager import RegistryManager
lazy from .document import BhvtDocument
lazy from .editor import BhvtEditor
lazy from .rsz_blocks import RszBlocks


class BhvtHandler(DocumentHandler):
    @staticmethod
    def needs_json_path() -> bool:
        return True

    def __init__(self):
        super().__init__()
        self.document: BhvtDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return any(data[at:at + 4] == magic for at, magic in MAGICS)

    def read(self, data: bytes) -> None:
        settings = self.app.settings
        registry = RegistryManager.instance().get_registry(settings.get("rcol_json_path"))
        blocks = RszBlocks(registry, settings.get("game_version", "RE4"), self.filepath)
        self.document = BhvtDocument.read(data, self.filepath, blocks, self)
        self.modified = False

    def create_viewer(self):
        editor = BhvtEditor(self.document, self)
        editor.modified_changed.connect(self._set_modified)
        return editor
