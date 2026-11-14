"""See docs/internals/gui-editor.md, Document and edits."""

from __future__ import annotations

from PySide6.QtCore import QObject

from file_handlers.clip.document import ClipDocument
from file_handlers.document_commands import Change, EditDocument

from .authoring import GuiAuthoring, gui_authoring
from .codec import parse_gui
from .errors import GuiWriteError
from .model import GuiAnimation, GuiDocument
from .serializer import serialize_gui
from .states import shape
from .structure import Structure
from .validation import errors


class StateClip(ClipDocument):
    def __init__(self, animation: GuiAnimation, parent: GuiEditDocument):
        super().__init__(animation.clip, parent, parent.undo_stack)
        self.animation = animation


class GuiEditDocument(EditDocument):
    def __init__(self, gui: GuiDocument, parent: QObject | None = None):
        super().__init__(parent)
        self.gui = gui
        self.authoring: GuiAuthoring = gui_authoring(gui.version)
        self.workspace = None
        self._structure: Structure | None = None
        self._clips: dict[int, StateClip] = {}
        self._touched: dict[int, GuiAnimation] = {}
        self.changed.connect(self._forget)
        self.undo_stack.indexChanged.connect(self._settle)

    @classmethod
    def read(cls, data: bytes, path: str, parent: QObject | None = None) -> GuiEditDocument:
        return cls(parse_gui(data, path), parent)

    def write(self) -> bytes:
        if found := errors(self):
            more = self.tr(" (and {0} more)").format(len(found) - 3) if len(found) > 3 else ""
            raise GuiWriteError("; ".join(item.text for item in found[:3]) + more)
        return serialize_gui(self.gui)

    @property
    def structure(self) -> Structure:
        if self._structure is None:
            self._structure = Structure(self.gui)
        return self._structure

    def clip(self, animation: GuiAnimation) -> StateClip:
        if (found := self._clips.get(id(animation))) is None:
            found = self._clips[id(animation)] = StateClip(animation, self)
            found.changed.connect(self._clip_changed)
        return found

    def _clip_changed(self, change: Change) -> None:
        animation = self.sender().animation
        self._touched[id(animation)] = animation
        self.changed.emit(change)

    def _forget(self, change: Change) -> None:
        if change.structural or change.field in ("name", "prototype_guid"):
            self._structure = None

    def _settle(self, _index: int) -> None:
        touched, self._touched = self._touched, {}
        for animation in touched.values():
            clip = self.clip(animation)
            for target, values in shape(animation):
                for name, value in values.items():
                    setattr(target, name, value)
                    clip.changed.emit(Change(target, name))
        self._touched = {}
