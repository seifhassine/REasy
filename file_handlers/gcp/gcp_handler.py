"""See docs/internals/gcp-format.md."""
from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

from .gcp_file import MAGIC
lazy from .document import GcpDocument
lazy from .editor import GcpEditor


class GcpHandler(DocumentHandler):
    def __init__(self):
        super().__init__()
        self.document: GcpDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[4:8] == MAGIC

    def read(self, data: bytes) -> None:
        self.document = GcpDocument.read(data, self)
        self.modified = False

    def create_viewer(self):
        editor = GcpEditor(self.document)
        editor.modified_changed.connect(self._set_modified)
        return editor
