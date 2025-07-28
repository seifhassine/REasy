"""
Consolidated tree infrastructure that handles both eager and lazy-loading approaches.
"""

from PySide6.QtCore import Qt, QModelIndex, QAbstractItemModel
from PySide6.QtWidgets import QAbstractItemView, QStyledItemDelegate
class TreeItem:
    __slots__ = ("raw", "data", "parent",
                 "_raw_children", "_children", "_children_list", "_child_to_row")

    def __init__(self, data, parent=None):
        self.raw = data
        self.data = data["data"] if isinstance(data, dict) and "data" in data else data
        self.parent = parent

        self._raw_children = data.get("children", []) if isinstance(data, dict) else []
        self._children: dict[int, "TreeItem"] = {}
        self._children_list = None
        self._child_to_row = {}

    def child_count(self) -> int:
        return len(self._raw_children)

    def child(self, row: int):
        if row < 0 or row >= len(self._raw_children):
            return None
        if row not in self._children:
            self._children[row] = TreeItem(self._raw_children[row], parent=self)
            self._child_to_row[id(self._children[row])] = row  # 🔹 keep reverse map
        return self._children[row]

    def row(self) -> int:
        return 0 if self.parent is None else self.parent._row_of(self)

    # ---------- legacy compatibility ----------
    @property
    def children(self):
        return [self.child(i) for i in range(self.child_count())]

    def _row_of(self, child):
        return self._child_to_row.get(id(child), 0)


class TreeModel(QAbstractItemModel):
    """Unified tree model implementation with optional lazy loading"""
    
    def __init__(self, root_data, parent=None):
        super().__init__(parent)
        self.rootItem = TreeItem(root_data)
        self.row_height = 24 

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()
        if not parent.isValid():
            parent_item = self.rootItem
        else:
            parent_item = parent.internalPointer()
        child_item = parent_item.child(row)
        if child_item:
            return self.createIndex(row, column, child_item)
        return QModelIndex()

    def parent(self, index):
        if not index.isValid():
            return QModelIndex()
        child_item = index.internalPointer()
        parent_item = child_item.parent
        if parent_item == self.rootItem or parent_item is None:
            return QModelIndex()
        return self.createIndex(parent_item.row(), 0, parent_item)

    def rowCount(self, parent=QModelIndex()):
        if not parent.isValid():
            parent_item = self.rootItem
        else:
            parent_item = parent.internalPointer()
        return parent_item.child_count()

    def columnCount(self, parent=QModelIndex()):
        return 1

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
            
        item = index.internalPointer()
        if role == Qt.DisplayRole:
            txt = item.data[0] if isinstance(item.data, (list, tuple)) else item.data
            return str(txt)
        if role == Qt.UserRole:
            val = item.data[1] if isinstance(item.data, (list, tuple)) and len(item.data) > 1 else ""
            return str(val)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable
    
    def getIndexFromItem(self, item):
        """Get the model index for a tree item"""
        if item is self.rootItem:
            return QModelIndex()
            
        if not item or not item.parent:
            return QModelIndex()
        
        # Find this item's row in its parent
        row = item.row()
        return self.createIndex(row, 0, item)
    
    def addChildren(self, parent_item: TreeItem, children_raw: list):
        if parent_item is None or not children_raw:
            return False

        start_row = parent_item.child_count()
        end_row   = start_row + len(children_raw) - 1

        parent_index = self.getIndexFromItem(parent_item)
        self.beginInsertRows(parent_index, start_row, end_row)

        for offset, raw in enumerate(children_raw):
            pos      = start_row + offset
            
            parent_item._raw_children.insert(pos, raw)

            child_it = TreeItem(raw, parent=parent_item)
            parent_item._children[pos]          = child_it
            parent_item._child_to_row[id(child_it)] = pos

        # 3) legacy list (todo remove list)
        if parent_item._children_list is not None:
            parent_item._children_list = [parent_item.child(i)
                                        for i in range(parent_item.child_count())]

        self.endInsertRows()
        return True
    
    def addChild(self, parent_item, child_raw):
        return self.addChildren(parent_item, [child_raw])


    def removeRow(self, row, parent=QModelIndex()):
        """Remove a row from the model"""
        return self.removeRows(row, 1, parent)
        
    def removeRows(self, row, count, parent=QModelIndex()):
        """Remove `count` rows starting at `row` from `parent`."""
        if row < 0 or count <= 0:
            return False

        parent_item = self.rootItem if not parent.isValid() else parent.internalPointer()
        if row + count > parent_item.child_count():
            return False

        self.beginRemoveRows(parent, row, row + count - 1)

        del parent_item._raw_children[row: row + count]

        shift_map = {}   

        for old_row in sorted(parent_item._children):
            if old_row < row:               
                continue
            if old_row >= row + count:           
                new_row = old_row - count
                shift_map[old_row] = new_row

        for r in range(row, row + count):
            child = parent_item._children.pop(r, None)
            if child:
                parent_item._child_to_row.pop(id(child), None)

        for old_row, new_row in shift_map.items():
            child = parent_item._children.pop(old_row)
            parent_item._children[new_row] = child
            parent_item._child_to_row[id(child)] = new_row

        #  legacy list becomes stale, rebuild lazily next time it’s accessed
        parent_item._children_list = None

        self.endRemoveRows()
        return True

class TreeStyleDelegate(QStyledItemDelegate):
    """Style delegate for tree items"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.default_row_height = 24 
        
    def sizeHint(self, option, index):
        """Ensure consistent row height for all items"""
        size = super().sizeHint(option, index)
        
        # Get the tree view if available
        tree_view = self.parent()
        if tree_view and hasattr(tree_view, 'default_row_height'):
            self.default_row_height = tree_view.default_row_height
            
        size.setHeight(self.default_row_height)
        return size


class AdvancedTreeDelegate(QAbstractItemView):
    """Base delegate for more advanced tree rendering"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = None

    def sizeHintForRow(self, row):
        return 24
