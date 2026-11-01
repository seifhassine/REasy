"""See docs/internals/clip-editor.md."""

from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QModelIndex, QObject, Qt


class OutlineRow:
    __slots__ = ("item", "parent", "row", "children")

    def __init__(self, item, parent: OutlineRow | None, row: int):
        self.item, self.parent, self.row = item, parent, row
        self.children: list[OutlineRow] | None = None


class OutlineModel(QAbstractItemModel):

    def __init__(self, root: object, parent: QObject | None = None):
        super().__init__(parent)
        self._root = OutlineRow(root, None, 0)

    def children_of(self, item) -> list:
        raise NotImplementedError

    def owner_of(self, item) -> object | None:
        raise NotImplementedError

    def display(self, item, row: int, column: int, role: int):
        return None

    def rename(self, item, name: str) -> bool:
        return False

    def item(self, index: QModelIndex) -> object:
        return self._row(index).item

    def index_of(self, item) -> QModelIndex:
        path = []
        while item is not None and item is not self._root.item:
            path.append(item)
            item = self.owner_of(item)
        if item is None:
            return QModelIndex()
        row, index = self._root, QModelIndex()
        for target in reversed(path):
            match = next((child for child in self._children(row) if child.item is target), None)
            if match is None:
                return QModelIndex()
            row, index = match, self.createIndex(match.row, 0, match)
        return index

    def reset(self, root: object) -> None:
        self.beginResetModel()
        self._root = OutlineRow(root, None, 0)
        self.endResetModel()

    def refresh(self, item) -> None:
        index = self.index_of(item)
        if index.isValid():
            self.dataChanged.emit(index, index.siblingAtColumn(1))

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
        return bool(self.children_of(self._row(parent).item))

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
        return flags | Qt.ItemIsEditable if index.column() == 0 and self.rename(self.item(index), None) else flags

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        return self.display(self.item(index), index.row(), index.column(), role) if index.isValid() else None

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole) -> bool:
        if role != Qt.EditRole or index.column() != 0 or not isinstance(value, str) or "\0" in value:
            return False
        return self.rename(self.item(index), value)

    def _row(self, index: QModelIndex) -> OutlineRow:
        return index.internalPointer() if index.isValid() else self._root

    def _children(self, row: OutlineRow) -> list[OutlineRow]:
        if row.children is None:
            row.children = [OutlineRow(item, row, position) for position, item in enumerate(self.children_of(row.item))]
        return row.children
