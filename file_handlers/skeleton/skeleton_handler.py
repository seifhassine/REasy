"""See docs/internals/skeleton-format.md."""
from __future__ import annotations

from file_handlers.base_handler import DocumentHandler

from .skeleton_file import MAGIC
lazy from .document import SkeletonDocument
lazy from .editor import SkeletonEditor


class SkeletonHandler(DocumentHandler):
    def __init__(self):
        super().__init__()
        self.document: SkeletonDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[4:8] == MAGIC

    def read(self, data: bytes) -> None:
        self.document = SkeletonDocument.read(data, self)
        self.modified = False

    def create_viewer(self):
        editor = SkeletonEditor(self)
        editor.modified_changed.connect(self._set_modified)
        return editor
