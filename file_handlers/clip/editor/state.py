from __future__ import annotations

import math

from PySide6.QtCore import QObject, Signal

from ..document import Change, ClipDocument
from ..model import Key, SpeedPoint


class EditorState(QObject):
    selection_changed = Signal()
    frame_changed = Signal(float)
    view_changed = Signal()

    def __init__(self, document: ClipDocument, parent: QObject | None = None):
        super().__init__(parent)
        self.document = document
        self.selection: list = []
        self.frame = 0.0
        self.view_start = 0.0
        self.view_scale = 4.0
        self.snap = True
        document.changed.connect(self._on_changed)

    @property
    def current(self) -> object:
        return self.selection[-1] if self.selection else self.document.timeline

    def keys(self) -> list:
        return [item for item in self.selection if isinstance(item, (Key, SpeedPoint))]

    def select(self, items: list, mode: str = "replace") -> None:
        items = [item for item in items if self.document.parent(item) is not None]
        if mode == "replace":
            selection = list(dict.fromkeys(items))
        elif mode == "add":
            selection = list(dict.fromkeys([*self.selection, *items]))
        else:
            selection = list(self.selection)
            for item in items:
                if item in selection:
                    selection.remove(item)
                else:
                    selection.append(item)
        if [id(item) for item in selection] != [id(item) for item in self.selection]:
            self.selection = selection
            self.selection_changed.emit()

    def set_frame(self, frame: float) -> None:
        frame = max(0.0, float(frame))
        if math.isfinite(frame) and frame != self.frame:
            self.frame = frame
            self.frame_changed.emit(frame)

    def set_view(self, start: float, scale: float) -> None:
        scale = min(400.0, max(0.02, scale))
        if math.isfinite(start) and (start, scale) != (self.view_start, self.view_scale):
            self.view_start, self.view_scale = start, scale
            self.view_changed.emit()

    def snapped(self, frame: float) -> float:
        return float(round(frame)) if self.snap else frame

    def _on_changed(self, change: Change) -> None:
        if not change.structural:
            return
        live = [item for item in self.selection if self.document.parent(item) is not None]
        if len(live) != len(self.selection):
            self.selection = live
            self.selection_changed.emit()

