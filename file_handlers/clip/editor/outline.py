from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt

lazy from ui.icons import line_icon

from ..document import Change, ClipDocument
from ..model import Clip, Node, Property, Track
from ..rules import NodeType, display_names, property_type
from .lanes import tone

_NODE_ICONS = {NodeType.GAME_OBJECT: "gameobject", NodeType.COMPONENT: "component", NodeType.FOLDER: "folder"}


class _Row:
    __slots__ = ("item", "parent", "row", "children")

    def __init__(self, item, parent: _Row | None, row: int):
        self.item, self.parent, self.row = item, parent, row
        self.children: list[_Row] | None = None


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


class OutlineModel(QAbstractItemModel):
    def __init__(self, document: ClipDocument, parent: QObject | None = None):
        super().__init__(parent)
        self.document = document
        self._root = _Row(document.timeline, None, 0)
        document.changed.connect(self._on_changed)

    def item(self, index: QModelIndex) -> object:
        return self._row(index).item

    def index_of(self, item) -> QModelIndex:
        path = []
        while (found := self.document.parent(item)) is not None and item is not self.document.timeline:
            path.append(item)
            item = found[0]
        row, index = self._root, QModelIndex()
        for target in reversed(path):
            children = self._children(row)
            match = next((child for child in children if child.item is target), None)
            if match is None:
                return QModelIndex()
            row, index = match, self.createIndex(match.row, 0, match)
        return index

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:
        children = self._children(self._row(parent))
        if not 0 <= row < len(children) or not 0 <= column < 2:
            return QModelIndex()
        return self.createIndex(row, column, children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        parent = index.internalPointer().parent
        if parent is None or parent is self._root:
            return QModelIndex()
        return self.createIndex(parent.row, 0, parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid() and parent.column() != 0:
            return 0
        return len(self._children(self._row(parent)))

    def hasChildren(self, parent: QModelIndex = QModelIndex()) -> bool:
        return bool(children_of(self._row(parent).item))

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 2

    def headerData(self, section: int, orientation, role: int = Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return (self.tr("Name"), self.tr("Type"))[section]
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable
        return flags | Qt.ItemIsEditable if index.column() == 0 else flags

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        item = self.item(index)
        if role == Qt.DisplayRole:
            return self._name(index, item) if index.column() == 0 else self._kind(item)
        if role == Qt.EditRole and index.column() == 0:
            return item.name
        if role == Qt.DecorationRole and index.column() == 0:
            return line_icon(self._icon(item))
        if role == Qt.ForegroundRole and isinstance(item, Track) and not item.enabled:
            return tone("text_disabled")
        if role == Qt.ToolTipRole:
            messages = [issue.message for issue in self.document.issues() if issue.target is item]
            return "\n".join(messages) or None
        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole) -> bool:
        if role != Qt.EditRole or index.column() != 0 or not isinstance(value, str):
            return False
        from ..edits import rename

        item = self.item(index)
        if value != item.name and "\0" not in value:
            rename(self.document, item, value)
        return True

    def _row(self, index: QModelIndex) -> _Row:
        return index.internalPointer() if index.isValid() else self._root

    def _children(self, row: _Row) -> list[_Row]:
        if row.children is None:
            row.children = [_Row(item, row, position) for position, item in enumerate(children_of(row.item))]
        return row.children

    def _name(self, index: QModelIndex, item) -> str:
        if isinstance(item, Clip) and not item.name:
            return self.tr("Clip {0}").format(index.row() + 1)
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
            self.beginResetModel()
            self._root = _Row(self.document.timeline, None, 0)
            self.endResetModel()
        elif change.field in ("name", "group", "enabled", "node_type", "type", "frame_in", "frame_out"):
            index = self.index_of(change.target)
            if index.isValid():
                self.dataChanged.emit(index, index.siblingAtColumn(1))
