"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from file_handlers.document_commands import Change, EditDocument

from .decode import read_jcns
from .encode import write_jcns
from .model import JointConstraints, Record
from .rules import rules_for


@dataclass(eq=False, slots=True)
class Table:
    owner: Record
    name: str
    kind: str


class JcnsDocument(EditDocument):
    modified_changed = Signal(bool)

    def __init__(self, model: JointConstraints, parent: QObject | None = None):
        super().__init__(parent)
        self.model = model
        self.root = model.root
        self._tables: dict[tuple[int, str], Table] = {}
        self._parents: dict[int, Table] | None = None
        self.changed.connect(self._forget)
        self.undo_stack.cleanChanged.connect(self._clean_changed)

    @classmethod
    def read(cls, data: bytes, parent: QObject | None = None) -> JcnsDocument:
        return cls(read_jcns(data), parent)

    def write(self) -> bytes:
        return write_jcns(self.model)

    def layout(self, record: Record):
        return rules_for(self.model.version).records[record.kind]

    def children(self, item) -> list:
        if isinstance(item, Record):
            return [self.table(item, link.name, link.record) for link in self.layout(item).links if link.record]
        if isinstance(item, Table):
            return list(item.owner.links.get(item.name) or ())
        return []

    def table(self, owner: Record, name: str, kind: str = "") -> Table:
        key = (id(owner), name)
        if key not in self._tables:
            self._tables[key] = Table(owner, name, kind or next(
                link.record for link in self.layout(owner).links if link.name == name))
        return self._tables[key]

    def parent(self, item) -> object | None:
        if isinstance(item, Table):
            return item.owner
        if self._parents is None:
            self._parents, stack = {}, [self.root]
            while stack:
                for table in self.children(stack.pop()):
                    for record in self.children(table):
                        self._parents[id(record)] = table
                        stack.append(record)
        return self._parents.get(id(item))

    def _forget(self, change: Change) -> None:
        if change.structural:
            self._parents = None

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)
