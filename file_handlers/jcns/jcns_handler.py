from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

lazy from .document import JcnsDocument
lazy from .editor import JcnsEditor


class JcnsHandler(DocumentHandler):
    def __init__(self):
        super().__init__()
        self.document: JcnsDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[4:8] == b"jcns"

    def read(self, data: bytes) -> None:
        self.document = JcnsDocument.read(data, self)
        self.document.modified_changed.connect(self._set_modified)
        self.modified = False

    def create_viewer(self):
        return JcnsEditor(self)
