from __future__ import annotations

from PySide6.QtCore import QObject, Qt

lazy from ui.icons import line_icon

from file_handlers.document_commands import Change
from ui.outline_model import OutlineModel as TreeOutline

from ..document import ClipDocument
from ..model import Clip, Node, Property, Track
from ..rules import NodeType, display_names, property_type
from .lanes import tone

_NODE_ICONS = {NodeType.GAME_OBJECT: "gameobject", NodeType.COMPONENT: "component", NodeType.FOLDER: "folder"}


def children_of(item) -> list:
    if isinstance(item, Track):
        return [*item.clips, *item.nodes]
    if isinstance(item, Clip):
        return item.nodes
    if isinstance(item, Node):
        return [*item.children, *item.properties]
    if isinstance(item, Property):
        return item.children
    return item.tracks


class OutlineModel(TreeOutline):
    def __init__(self, document: ClipDocument, parent: QObject | None = None):
        super().__init__(document.timeline, parent)
        self.document = document
        document.changed.connect(self._on_changed)

    def children_of(self, item) -> list:
        return children_of(item)

    def owner_of(self, item) -> object | None:
        found = self.document.parent(item)
        return None if found is None else found[0]

    def display(self, item, row: int, column: int, role: int):
        if role == Qt.DisplayRole:
            return self._name(row, item) if column == 0 else self._kind(item)
        if role == Qt.EditRole and column == 0:
            return item.name
        if role == Qt.DecorationRole and column == 0:
            return line_icon(self._icon(item))
        if role == Qt.ForegroundRole and isinstance(item, Track) and not item.enabled:
            return tone("text_disabled")
        if role == Qt.ToolTipRole:
            messages = [issue.message for issue in self.document.issues() if issue.target is item]
            return "\n".join(messages) or None
        return None

    def rename(self, item, name: str | None) -> bool:
        from ..edits import rename

        if name is not None and name != item.name:
            rename(self.document, item, name)
        return True

    def _name(self, row: int, item) -> str:
        if isinstance(item, Clip) and not item.name:
            return self.tr("Clip {0}").format(row + 1)
        return item.name

    def _kind(self, item) -> str:
        if isinstance(item, Track):
            return item.group
        if isinstance(item, Clip):
            return f"{item.frame_in:g} - {item.frame_out:g}"
        if isinstance(item, Node):
            return display_names("NodeType", self.document.timeline.version).get(item.node_type, str(item.node_type))
        return display_names("PropertyType", self.document.timeline.version).get(item.type, f"0x{item.type:X}")

    @staticmethod
    def _icon(item) -> str:
        if isinstance(item, Track):
            return "layers"
        if isinstance(item, Clip):
            return "crop"
        if isinstance(item, Node):
            return _NODE_ICONS.get(item.node_type, "box")
        info = property_type(item.type)
        return "list" if item.children or (info is not None and info.keys_since is None) else "variable"

    def _on_changed(self, change: Change) -> None:
        if change.structural:
            self.reset(self.document.timeline)
        elif change.field in ("name", "group", "enabled", "node_type", "type", "frame_in", "frame_out"):
            self.refresh(change.target)
