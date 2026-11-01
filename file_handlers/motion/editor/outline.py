from __future__ import annotations

from PySide6.QtCore import QObject, Qt

lazy from ui.icons import line_icon

from file_handlers.document_commands import Change
from ui.outline_model import OutlineModel

from ..document import MotionDocument, children
from ..model import (
    JointChannels,
    Motion,
    MotionList,
    MotionListEntry,
    MotionTree,
    PropertyTrack,
    ReferencePath,
    Sequence,
    TreeLink,
    TreeNode,
    TreeParam,
)
from ..pose import TRANSFORMS
from ..rules import display_names

_ICONS = {Motion: "layers", MotionTree: "graph", ReferencePath: "link", JointChannels: "bone",
          PropertyTrack: "variable", Sequence: "clock", TreeNode: "box", TreeLink: "link", TreeParam: "variable"}
_SHOWN_FIELDS = {"name", "motion_id", "name_hash", "category", "key", "type", "end_frame", "path", "track",
                 "translation", "rotation", "scale"}


def _node_name(node: TreeNode) -> str:
    return node.name or node.class_name


class MotionOutline(OutlineModel):
    def __init__(self, document: MotionDocument, parent: QObject | None = None):
        super().__init__(document.root, parent)
        self.document = document
        document.changed.connect(self._on_changed)

    def children_of(self, item) -> list:
        return children(item)

    def owner_of(self, item) -> object | None:
        return self.document.parent(item)

    def display(self, item, row: int, column: int, role: int):
        if role == Qt.DisplayRole:
            return self._name(item) if column == 0 else self._kind(item)
        if role == Qt.DecorationRole and column == 0:
            held = item.item if isinstance(item, MotionListEntry) else item
            return line_icon(_ICONS.get(type(held), "box"))
        return None

    def _name(self, item) -> str:
        if isinstance(item, MotionListEntry):
            held = item.item
            return f"{item.motion_id}  {held.path if isinstance(held, ReferencePath) else getattr(held, 'name', '')}"
        if isinstance(item, JointChannels):
            return self.document.joint_name(item)
        if isinstance(item, PropertyTrack):
            return f"0x{item.name_hash:08X}"
        if isinstance(item, Sequence):
            return display_names("SequenceCategory").get(item.category, str(item.category))
        if isinstance(item, TreeNode):
            return _node_name(item)
        if isinstance(item, TreeLink):
            return f"{_node_name(item.parent)} > {_node_name(item.child)}"
        return item.name or f"0x{item.key:08X}"

    def _kind(self, item) -> str:
        if isinstance(item, MotionListEntry):
            return self._kind(item.item)
        if isinstance(item, Motion):
            return self.tr("{0:g} frames").format(item.end_frame)
        if isinstance(item, MotionTree):
            return f"v{item.version}"
        if isinstance(item, ReferencePath):
            return self.tr("Reference")
        if isinstance(item, JointChannels):
            return " ".join(name[0].upper() for name in TRANSFORMS if getattr(item, name) is not None)
        if isinstance(item, PropertyTrack):
            return self.tr("{0} keys").format(item.track.key_count)
        if isinstance(item, Sequence):
            return self.tr("Sequence")
        if isinstance(item, TreeNode):
            return item.class_name
        if isinstance(item, TreeParam):
            return display_names("TreeParamType").get(item.type, str(item.type))
        if isinstance(item, TreeLink):
            return display_names("TreeLinkType").get(item.type, str(item.type))
        return ""

    def _on_changed(self, change: Change) -> None:
        if change.structural:
            self.reset(self.document.root)
        elif change.field in _SHOWN_FIELDS:
            target, root = change.target, self.document.root
            if isinstance(root, MotionList):
                target = next((entry for entry in root.entries if entry.item is target), target)
            self.refresh(target)
            if isinstance(target, TreeNode):
                for link in children(self.document.parent(target)):
                    if isinstance(link, TreeLink) and (link.parent is target or link.child is target):
                        self.refresh(link)
