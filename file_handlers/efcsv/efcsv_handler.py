"""See docs/internals/efcsv-format.md."""
from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

lazy from .document import EfcsvDocument
lazy from .editor import EfcsvEditor


class EfcsvHandler(DocumentHandler):
    def __init__(self):
        super().__init__()
        self.document: EfcsvDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return False

    def read(self, data: bytes) -> None:
        self.document = EfcsvDocument.read(data, self)
        self.modified = False

    def create_viewer(self):
        editor = EfcsvEditor(self.document)
        editor.modified_changed.connect(self._set_modified)
        return editor
