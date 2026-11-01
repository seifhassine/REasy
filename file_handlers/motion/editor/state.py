from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from file_handlers.clip.editor.transport import Playback
from file_handlers.document_commands import Change

from ..document import MotionDocument
from ..model import JointChannels, Motion, MotionListEntry, PropertyTrack
from ..pose import TRANSFORMS


class EditorState(Playback):
    selection_changed = Signal()
    motion_changed = Signal()

    def __init__(self, document: MotionDocument, parent: QObject | None = None):
        super().__init__(parent)
        self.document = document
        self.selection: list = []
        motions = document.motions()
        self.motion: Motion | None = motions[0] if motions else None
        self.end_frame = 0.0 if self.motion is None else self.motion.end_frame
        self.inherited: Callable[[MotionListEntry], Motion | None] | None = None
        self._inherited_entry: MotionListEntry | None = None
        document.changed.connect(self._on_changed)

    @property
    def current(self) -> object:
        return self.selection[-1] if self.selection else self.document.root

    def select(self, items: list) -> None:
        items = [item for item in dict.fromkeys(items)
                 if item is self.document.root or self.document.parent(item) is not None]
        current = items[-1] if items else self.document.root
        motion, entry = self.document.motion_of(current), None
        if motion is None and isinstance(current, MotionListEntry) and self.inherited is not None:
            motion, entry = self.inherited(current), current
        if motion is not None and motion is not self.motion:
            self._show_motion(motion, entry)
        elif motion is not None:
            self._inherited_entry = entry
        if [id(item) for item in items] != [id(item) for item in self.selection]:
            self.selection = items
            self.selection_changed.emit()

    def tracks(self) -> list[tuple[JointChannels | PropertyTrack, str]]:
        found = []
        for item in self.selection:
            if isinstance(item, JointChannels):
                found += [(item, name) for name in TRANSFORMS if getattr(item, name) is not None]
            elif isinstance(item, PropertyTrack):
                found.append((item, "track"))
        return found

    def _show_motion(self, motion: Motion | None, entry: MotionListEntry | None = None) -> None:
        self.motion, self._inherited_entry = motion, entry
        self.set_end_frame(0.0 if motion is None else motion.end_frame)
        self.motion_changed.emit()

    def _on_changed(self, change: Change) -> None:
        if change.target is self.motion and change.field == "end_frame":
            self.set_end_frame(self.motion.end_frame)
        if not change.structural:
            return
        live = [item for item in self.selection if item is self.document.root or self.document.parent(item) is not None]
        if len(live) != len(self.selection):
            self.selection = live
            self.selection_changed.emit()
        motions = self.document.motions()
        if self.motion not in motions and self.document.parent(self._inherited_entry) is None:
            self._show_motion(motions[0] if motions else None)
