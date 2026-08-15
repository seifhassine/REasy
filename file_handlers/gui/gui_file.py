"""Stateful semantic GUI document with command-based history."""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .codec import parse_gui
from .errors import GuiFormatError
from .model import GUI_MAGIC, GuiAnimation, GuiDocument, GuiObject, GuiSymbol
from .profiles import GuiFormatProfile, gui_profile_from_data
from .serializer import serialize_gui


def _snapshot(value: Any) -> Any:
    if isinstance(value, (GuiObject, GuiAnimation, GuiSymbol)):
        return value
    if isinstance(value, list) and all(
        isinstance(item, (GuiObject, GuiAnimation, GuiSymbol)) for item in value
    ):
        return list(value)
    return copy.deepcopy(value)


@dataclass(slots=True)
class _Change:
    target: object
    attribute: str
    before: Any
    after: Any

    def apply(self, forward: bool) -> None:
        setattr(self.target, self.attribute, _snapshot(self.after if forward else self.before))


@dataclass(slots=True)
class _Command:
    label: str
    changes: list[_Change]
    before_state: int
    after_state: int


class SemanticEdit:

    def __init__(self) -> None:
        self.changes: list[_Change] = []
        self._indices: dict[tuple[int, str], int] = {}

    def set(self, target: object, attribute: str, value: Any) -> None:
        key = id(target), attribute
        current = getattr(target, attribute)
        if key in self._indices:
            change = self.changes[self._indices[key]]
            change.after = _snapshot(value)
            setattr(target, attribute, _snapshot(value))
            return
        if current == value:
            return
        self._indices[key] = len(self.changes)
        change = _Change(target, attribute, _snapshot(current), _snapshot(value))
        self.changes.append(change)
        setattr(target, attribute, _snapshot(value))

    def rollback(self) -> None:
        for change in reversed(self.changes):
            change.apply(False)


GuiEdit = Callable[[GuiDocument, SemanticEdit], None]


class GuiFile:
    HISTORY_LIMIT = 128

    def __init__(self) -> None:
        self.source = "<bytes>"
        self.profile: GuiFormatProfile | None = None
        self.document: GuiDocument | None = None
        self._saved_bytes = b""
        self._state = 0
        self._next_state = 1
        self._saved_state = 0
        self._undo: list[_Command] = []
        self._redo: list[_Command] = []

    @classmethod
    def can_handle(cls, data: bytes) -> bool:
        if len(data) < 8 or data[4:8] != GUI_MAGIC:
            return False
        try:
            gui_profile_from_data(data)
        except ValueError:
            return False
        return True

    @classmethod
    def from_bytes(cls, data: bytes, source: str = "<bytes>") -> "GuiFile":
        result = cls()
        result.read(data, source)
        return result

    def require_document(self) -> GuiDocument:
        if self.document is None:
            raise GuiFormatError("no GUI document is loaded")
        return self.document

    def read(self, data: bytes, source: str = "<bytes>") -> None:
        payload = bytes(data)
        try:
            profile = gui_profile_from_data(payload)
        except ValueError as exc:
            raise GuiFormatError(f"{source}: {exc}") from exc
        self.document = parse_gui(payload, source, profile=profile)
        self.source = source
        self.profile = profile
        self._saved_bytes = payload
        self._state = self._saved_state = 0
        self._next_state = 1
        self._undo.clear()
        self._redo.clear()

    @property
    def modified(self) -> bool:
        return self._state != self._saved_state

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str:
        return self._undo[-1].label if self._undo else ""

    @property
    def redo_label(self) -> str:
        return self._redo[-1].label if self._redo else ""

    @property
    def data(self) -> bytes:
        return self.serialize()

    def edit(self, label: str, operation: GuiEdit) -> bool:
        document = self.require_document()
        edit = SemanticEdit()
        try:
            operation(document, edit)
            document.validate_relationships()
        except Exception:
            edit.rollback()
            raise
        edit.changes = [item for item in edit.changes if item.before != item.after]
        if not edit.changes:
            return False
        before = self._state
        after = self._next_state
        self._next_state += 1
        self._state = after
        self._undo.append(_Command(str(label), edit.changes, before, after))
        if len(self._undo) > self.HISTORY_LIMIT:
            del self._undo[0]
        self._redo.clear()
        return True


    def undo(self) -> bool:
        if not self._undo:
            return False
        command = self._undo.pop()
        for change in reversed(command.changes):
            change.apply(False)
        self._state = command.before_state
        self._redo.append(command)
        return True

    def redo(self) -> bool:
        if not self._redo:
            return False
        command = self._redo.pop()
        for change in command.changes:
            change.apply(True)
        self._state = command.after_state
        self._undo.append(command)
        return True

    def reset(self) -> bool:
        if not self.modified:
            return False
        assert self.profile is not None
        self.document = parse_gui(self._saved_bytes, self.source, profile=self.profile)
        self._state = self._saved_state = 0
        self._next_state = 1
        self._undo.clear()
        self._redo.clear()
        return True

    def serialize(self) -> bytes:
        if self.profile is None:
            raise GuiFormatError("no GUI profile is loaded")
        return serialize_gui(self.require_document(), profile=self.profile)

    def write(self) -> bytes:
        result = self.serialize()
        self._saved_bytes = result
        self._saved_state = self._state
        return result
