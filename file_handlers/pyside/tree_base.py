"""
Core tree infrastructure classes that handle the lazy-loading tree model.

contains:
- LazyTreeItem: Individual tree nodes that load children on demand
- LazyTreeModel: Qt model implementation for managing the tree data structure
- AdvancedTreeDelegate: Base delegate for custom tree item display
- AdvancedStyledDelegate: Style customization for tree items
"""

from PySide6.QtCore import Qt, QModelIndex, QAbstractItemModel
from PySide6.QtWidgets import QAbstractItemView, QStyledItemDelegate

class LazyTreeItem:
    """
    Each item in the lazy tree references a dict of the form:
       {
         "data": ["Title", "Value"],
         "children": [ {...}, {...} ]
       }
    """
    def __init__(self, data, parent=None):
        self.raw = data
        if isinstance(data, dict) and "data" in data:
            self.data = data["data"]
        else:
            self.data = data
        self.parent = parent
        self._children = None

    def childCount(self):
        if self._children is None:
            self._loadChildren()
        return len(self._children)

    def child(self, row):
        if self._children is None:
            self._loadChildren()
        return self._children[row]

    def row(self):
        if self.parent:
            return self.parent._children.index(self)
        return 0

    def _loadChildren(self):
        self._children = []
        if isinstance(self.raw, dict) and "children" in self.raw:
            for child_data in self.raw["children"]:
                self._children.append(LazyTreeItem(child_data, parent=self))


class LazyTreeModel(QAbstractItemModel):
    def __init__(self, rootData, parent=None):
        super().__init__(parent)
        self.rootItem = LazyTreeItem(rootData)

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            parentItem = self.rootItem
        else:
            parentItem = parent.internalPointer()
        childItem = parentItem.child(row)
        if childItem:
            return self.createIndex(row, column, childItem)
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        childItem = index.internalPointer()
        parentItem = childItem.parent
        if parentItem == self.rootItem or parentItem is None:
            return QModelIndex()
        return self.createIndex(parentItem.row(), 0, parentItem)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0
        if not parent.isValid():
            parentItem = self.rootItem
        else:
            parentItem = parent.internalPointer()
        return parentItem.childCount()

    def columnCount(self, parent=QModelIndex()):
        return 1

    def itemFromIndex(self, index):
        """Get item from model index - needed for search"""
        if not index.isValid():
            return None
        return index.internalPointer()

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
            
        item = index.internalPointer()
        if not item:
            return None
            
        if role == Qt.DisplayRole:
            if isinstance(item.data, (list, tuple)) and len(item.data) > 0:
                return str(item.data[0])
            return str(item.data)
            
        elif role == Qt.UserRole:  # For search to find values
            if isinstance(item.data, (list, tuple)) and len(item.data) > 1:
                return str(item.data[1])
            return ""
            
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable


class AdvancedTreeDelegate(QAbstractItemView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None

    def sizeHintForRow(self, row):
        return 24


class AdvancedStyledDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
