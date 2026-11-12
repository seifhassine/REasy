"""See docs/internals/clip-editor.md."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand, QUndoStack

_SET_FIELDS = 1


@dataclass(frozen=True, slots=True)
class Change:
    target: object
    field: str = ""
    structural: bool = False


class EditDocument(QObject):
    changed = Signal(object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.undo_stack = QUndoStack(self)

    @property
    def modified(self) -> bool:
        return not self.undo_stack.isClean()

    def mark_saved(self) -> None:
        self.undo_stack.setClean()

    def push(self, command: QUndoCommand) -> None:
        self.undo_stack.push(command)

    @contextmanager
    def macro(self, text: str):
        stack = self.undo_stack
        stack.beginMacro(text)
        try:
            yield
        except BaseException:
            stack.endMacro()
            if stack.canUndo():
                (dropped := stack.command(stack.index() - 1)).undo()
                dropped.setObsolete(True)
                stack.undo()
            raise
        stack.endMacro()


class SetFields(QUndoCommand):
    def __init__(self, document, edits: list[tuple[object, dict]], text: str, merge_key=None):
        super().__init__(text)
        self.document, self.merge_key = document, merge_key
        self.edits = [(target, {name: list(value) if isinstance(value := getattr(target, name), list) else value
                                for name in values}, dict(values)) for target, values in edits]

    def id(self) -> int:
        return _SET_FIELDS if self.merge_key is not None else -1

    def mergeWith(self, other: QUndoCommand) -> bool:
        if not isinstance(other, SetFields) or other.merge_key != self.merge_key or \
                [id(target) for target, _, _ in other.edits] != [id(target) for target, _, _ in self.edits]:
            return False
        self.edits = [(target, {**new_before, **before}, {**after, **new_after})
                      for (target, before, after), (_, new_before, new_after) in zip(self.edits, other.edits)]
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
    def __init__(self, document, owner: object, field: str, contents, text: str):
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
