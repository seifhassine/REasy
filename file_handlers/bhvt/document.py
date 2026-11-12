"""See docs/internals/bhvt-editor.md, Document and edits."""

from __future__ import annotations

from PySide6.QtCore import QObject

from file_handlers.document_commands import EditDocument

from .errors import BhvtFormatError
from .files import Resource, read_resource, write_resource
from .rsz_blocks import RszBlocks
from .validation import errors


class BhvtDocument(EditDocument):
    def __init__(self, resource: Resource, rsz: RszBlocks, parent: QObject | None = None):
        super().__init__(parent)
        self.resource, self.rsz = resource, rsz
        self.embedded_modified = False
        self.owner_variables: dict[str, object] = {}

    @classmethod
    def read(cls, data: bytes, path: str, rsz: RszBlocks, parent: QObject | None = None) -> BhvtDocument:
        return cls(read_resource(data, path, rsz), rsz, parent)

    def write(self) -> bytes:
        if found := errors(self.resource):
            more = self.tr(" (and {0} more)").format(len(found) - 3) if len(found) > 3 else ""
            raise BhvtFormatError("; ".join(item.text for item in found[:3]) + more)
        return write_resource(self.resource, self.rsz)

    @property
    def modified(self) -> bool:
        return not self.undo_stack.isClean() or self.embedded_modified

    def mark_saved(self) -> None:
        self.embedded_modified = False
        super().mark_saved()
