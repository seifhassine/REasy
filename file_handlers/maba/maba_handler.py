"""See docs/internals/maba-format.md."""
from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

from .maba_file import MAGIC
lazy from .document import MabaDocument
lazy from .editor import MabaEditor


class MabaHandler(DocumentHandler):
    def __init__(self):
        super().__init__()
        self.document: MabaDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[4:8] == MAGIC

    def read(self, data: bytes) -> None:
        self.document = MabaDocument.read(data, self)
        self.modified = False

    def create_viewer(self):
        editor = MabaEditor(self.document, self)
        editor.modified_changed.connect(self._set_modified)
        return editor
