from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler

lazy from .document import MotionDocument
lazy from .editor import MotionEditor

MAGICS = (b"mot ", b"mlst", b"mtre")


class MotionHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.document: MotionDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[4:8] in MAGICS

    def read(self, data: bytes) -> None:
        self.document = MotionDocument.read(data, self)
        self.document.modified_changed.connect(self._set_modified)
        self.modified = False

    def rebuild(self) -> bytes:
        data = self.document.write()
        self.document.mark_saved()
        self.modified = False
        return data

    def create_viewer(self):
        return MotionEditor(self)

    def _set_modified(self, modified: bool) -> None:
        self.modified = modified
