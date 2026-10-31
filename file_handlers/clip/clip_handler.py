from __future__ import annotations

from file_handlers.base_handler import BaseFileHandler

from .decode import MAGIC
from .model import ResourceKind
lazy from .document import ClipDocument
lazy from .editor import ClipEditor


def resource_kind(path: str) -> ResourceKind | None:
    name = path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    return next((kind for kind in ResourceKind if f".{kind.value}." in name or name.endswith(f".{kind.value}")), None)


class ClipHandler(BaseFileHandler):
    def __init__(self):
        super().__init__()
        self.document: ClipDocument | None = None

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        return data[:4] == MAGIC

    def read(self, data: bytes) -> None:
        self.document = ClipDocument.read(data, resource_kind(self.filepath), self)
        self.modified = False

    def rebuild(self) -> bytes:
        data = self.document.write()
        self.document.mark_saved()
        self.modified = False
        return data

    def create_viewer(self):
        editor = ClipEditor(self.document)
        editor.modified_changed.connect(self._set_modified)
        return editor

    def _set_modified(self, modified: bool) -> None:
        self.modified = modified
