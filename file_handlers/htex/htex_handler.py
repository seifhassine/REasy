"""See docs/internals/htex-format.md."""
from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

from .htex_file import MAGIC
lazy from .document import HtexDocument
lazy from .editor import HtexEditor


class HtexHandler(DocumentHandler):
    def __init__(self):
        super().__init__()
        self.document: HtexDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[:4] == MAGIC

    def read(self, data: bytes) -> None:
        self.document = HtexDocument.read(data, self)
        self.modified = False

    def create_viewer(self):
        editor = HtexEditor(self.document, self)
        editor.modified_changed.connect(self._set_modified)
        return editor
