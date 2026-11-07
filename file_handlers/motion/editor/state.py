from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Signal

from file_handlers.clip.editor.transport import Playback
from file_handlers.document_commands import Change

from ..document import ENTRIES, MotionDocument
from ..model import JointChannels, Motion, MotionListEntry, MotionTree, PropertyTrack, TreeNode
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
        self.played: Motion | MotionTree | None = self.motion or (
            document.root if isinstance(document.root, MotionTree) else None)
        self.end_frame = 0.0 if self.motion is None else self.motion.end_frame
        self.inherited: Callable[[MotionListEntry], Motion | None] | None = None
        self.mirrored_of: Callable[[MotionListEntry], bool] | None = None
        self.sequences_of: Callable[[MotionListEntry], list] | None = None
        self.stop_of: Callable[[], MotionListEntry | None] = lambda: None
        self.mirrored, self.sequences, self.mask = False, None, None
        self.variables_of: Callable[[TreeNode], dict[bytes, tuple[str, object]]] = lambda _node: {}
        self._inherited_entry: MotionListEntry | None = None
        document.changed.connect(self._on_changed)

    @property
    def current(self) -> object:
        return self.selection[-1] if self.selection else self.document.root

    def select(self, items: list) -> None:
        items = [item for item in dict.fromkeys(items)
                 if item is self.document.root or self.document.parent(item) is not None]
        current = items[-1] if items else self.document.root
        motion, entry = self.tree_of(current) or self.document.motion_of(current), None
        if motion is None and isinstance(current, MotionListEntry) and self.inherited is not None:
            motion, entry = self.inherited(current), current
        row = current
        while row is not None and not isinstance(row, MotionListEntry):
            row = self.document.parent(row)
        listed = isinstance(motion, Motion) and row is not None
        mirrored = listed and self.mirrored_of is not None and self.mirrored_of(row)
        sequences = self.sequences_of(row) if listed and self.sequences_of is not None else None
        mask = (row.joint_mask, getattr(self.document.parent(row), "joint_mask_mode", 0)) if listed else None
        if motion is not None and (motion is not self.played or (mirrored, sequences, mask) != (
                self.mirrored, self.sequences, self.mask)):
            self._show_motion(motion, entry, mirrored, sequences, mask)
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

    def tree_of(self, item: object) -> MotionTree | None:
        while item is not None and not isinstance(item, MotionTree):
            item = item.item if isinstance(item, ENTRIES) and isinstance(item.item, MotionTree) else \
                None if isinstance(item, ENTRIES) else self.document.parent(item)
        return item

    def _show_motion(self, motion: Motion | MotionTree | None, entry: MotionListEntry | None = None,
                     mirrored: bool = False, sequences: list | None = None, mask: tuple | None = None) -> None:
        """A tree's frames run to its root's end, which the preview reports once it plays it (``set_end_frame``)."""
        self.played, self._inherited_entry, self.mirrored, self.sequences = motion, entry, mirrored, sequences
        self.mask = mask
        self.motion = motion if isinstance(motion, Motion) else None
        self.set_end_frame(motion.end_frame if isinstance(motion, Motion) else 0.0)
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
        root = self.document.root
        if isinstance(self.played, MotionTree) and self.played in (
                [entry.item for entry in root.entries] if hasattr(root, "entries") else [root]):
            return
        if self.motion not in motions and self.document.parent(self._inherited_entry) is None:
            self._show_motion(motions[0] if motions else None)
