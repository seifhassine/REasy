"""See docs/internals/motion-editor.md."""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from file_handlers.document_commands import Change, EditDocument
from utils.hash_util import murmur3_hash_utf16le

lazy from file_handlers.clip.document import ClipDocument

from .decode import read_file
from .encode import write_file
from .model import JointChannels, Motion, MotionList, MotionListEntry, MotionTree, Sequence, TreeNode


def children(item: object) -> list:
    if isinstance(item, MotionList):
        return list(item.entries)
    if isinstance(item, MotionListEntry):
        return [*children(item.item), *item.overrides]
    if isinstance(item, Motion):
        return [*item.channels, *item.properties, *item.sequences]
    if isinstance(item, MotionTree):
        return [*item.nodes, *item.links]
    if isinstance(item, TreeNode):
        return list(item.params or ())
    return []


class MotionDocument(EditDocument):
    modified_changed = Signal(bool)
    clip_changed = Signal()

    def __init__(self, root: Motion | MotionList | MotionTree, parent: QObject | None = None):
        super().__init__(parent)
        self.root = root
        self._parents: dict[int, object] | None = None
        self._names: dict[int, str] | None = None
        self._clips: dict[int, ClipDocument] = {}
        self.changed.connect(self._forget)
        self.undo_stack.cleanChanged.connect(self._clean_changed)

    @classmethod
    def read(cls, data: bytes, parent: QObject | None = None) -> MotionDocument:
        return cls(read_file(data), parent)

    def write(self) -> bytes:
        return write_file(self.root)

    @property
    def modified(self) -> bool:
        return not self.undo_stack.isClean() or any(clip.modified for clip in self._clips.values())

    def mark_saved(self) -> None:
        super().mark_saved()
        for clip in self._clips.values():
            clip.mark_saved()

    def clip_document(self, sequence: Sequence) -> ClipDocument:
        if (found := self._clips.get(id(sequence.clip))) is None:
            found = self._clips[id(sequence.clip)] = ClipDocument(sequence.clip, self)
            found.undo_stack.cleanChanged.connect(self._clean_changed)
            found.changed.connect(self.clip_changed)
        return found

    def parent(self, item: object) -> object | None:
        if self._parents is None:
            self._parents, stack = {}, [self.root]
            while stack:
                owner = stack.pop()
                for child in children(owner):
                    self._parents[id(child)] = owner
                    stack.append(child)
        return self._parents.get(id(item))

    def motion_of(self, item: object) -> Motion | None:
        while item is not None and not isinstance(item, Motion):
            item = item.item if isinstance(item, MotionListEntry) and isinstance(item.item, Motion) else \
                self.parent(item)
        return item

    def motions(self) -> list[Motion]:
        root = self.root
        if isinstance(root, MotionList):
            return [entry.item for entry in root.entries if isinstance(entry.item, Motion)]
        return [root] if isinstance(root, Motion) else []

    def joint_name(self, channel: JointChannels) -> str:
        if self._names is None:
            self._names = {murmur3_hash_utf16le(joint.name): joint.name for motion in self.motions()
                           if motion.skeleton is not None for joint in motion.skeleton.joints}
        return self._names.get(channel.name_hash, f"0x{channel.name_hash:08X}")

    def _clean_changed(self, _clean: bool) -> None:
        self.modified_changed.emit(self.modified)

    def _forget(self, change: Change) -> None:
        if change.structural:
            self._parents = self._names = None
