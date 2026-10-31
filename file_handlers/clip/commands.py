from __future__ import annotations

import copy

from PySide6.QtGui import QUndoCommand

from .document import Change, ClipDocument

_SET_FIELDS = 1


class SetFields(QUndoCommand):
    def __init__(self, document: ClipDocument, edits: list[tuple[object, dict]], text: str, merge_key=None):
        super().__init__(text)
        self.document, self.merge_key = document, merge_key
        self.edits = [(target, {name: copy.copy(getattr(target, name)) for name in values}, dict(values))
                      for target, values in edits]

    def id(self) -> int:
        return _SET_FIELDS if self.merge_key is not None else -1

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, SetFields) or other.merge_key != self.merge_key or \
                [id(target) for target, _, _ in other.edits] != [id(target) for target, _, _ in self.edits]:
            return False
        self.edits = [(target, before, after) for (target, before, _), (_, _, after) in zip(self.edits, other.edits)]
        return True

    def redo(self) -> None:
        self._apply(2)

    def undo(self) -> None:
        self._apply(1)

    def _apply(self, side: int) -> None:
        for edit in self.edits:
            target, values = edit[0], edit[side]
            for name, value in values.items():
                if isinstance(value, list):
                    getattr(target, name)[:] = value
                else:
                    setattr(target, name, value)
        for edit in self.edits:
            for name in edit[side]:
                self.document.changed.emit(Change(edit[0], name))


class ReplaceContents(QUndoCommand):
    def __init__(self, document: ClipDocument, owner: object, field: str, contents, text: str):
        super().__init__(text)
        self.document, self.owner, self.field = document, owner, field
        current = getattr(owner, field)
        self.before = dict(current) if isinstance(current, dict) else list(current)
        self.after = dict(contents) if isinstance(current, dict) else list(contents)

    def redo(self) -> None:
        self._apply(self.after)

    def undo(self) -> None:
        self._apply(self.before)

    def _apply(self, contents) -> None:
        container = getattr(self.owner, self.field)
        if isinstance(container, dict):
            container.clear()
            container.update(contents)
        else:
            container[:] = contents
        self.document.changed.emit(Change(self.owner, self.field, True))

