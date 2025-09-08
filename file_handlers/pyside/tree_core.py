"""
Consolidated tree infrastructure that handles both eager and lazy-loading approaches.
"""

from PySide6.QtCore import Qt, QModelIndex, QAbstractItemModel
from PySide6.QtWidgets import QAbstractItemView, QStyledItemDelegate
from typing import Callable, Optional, Any
class DeferredChildBuilder:
    __slots__ = ('builder_func', 'context', '_built', '_children')
    
    def __init__(self, builder_func: Callable, context: Any = None):
        self.builder_func = builder_func
        self.context = context
        self._built = False
        self._children = None
    
    def build(self) -> list:
        if not self._built:
            self._children = self.builder_func(self.context) if self.context else self.builder_func()
            self._built = True
        return self._children or []
    
    def is_built(self) -> bool:
        return self._built
    
    def reset(self):
        self._built = False
        self._children = None


class TreeItem:
    __slots__ = ("raw", "data", "parent",
                 "_raw_children", "_children", "_children_list", "_child_to_row",
                 "_deferred_builder", "_children_built", "_expandable_hint")

    def __init__(self, data, parent=None):
        self.raw = data
        self.data = data["data"] if isinstance(data, dict) and "data" in data else data
        self.parent = parent

        self._deferred_builder = data.get("deferred_builder") if isinstance(data, dict) else None
        self._children_built = False
        self._expandable_hint = data.get("expandable", None) if isinstance(data, dict) else None
        
        if self._deferred_builder:
            self._raw_children = []
        else:
            self._raw_children = data.get("children", []) if isinstance(data, dict) else []
            self._children_built = True
            
        self._children: dict[int, "TreeItem"] = {}
        self._children_list = None
        self._child_to_row = {}

    def child_count(self) -> int:
        self._ensure_children_built()
        return len(self._raw_children)
    
    def has_children(self) -> bool:
        if self._deferred_builder and not self._children_built:
            if self._expandable_hint is not None:
                return self._expandable_hint
            return True
        return len(self._raw_children) > 0
    
    def _ensure_children_built(self):
        if self._deferred_builder and not self._children_built:
            self._raw_children = self._deferred_builder.build()
            self._children_built = True

    def child(self, row: int):
        self._ensure_children_built()
        if row < 0 or row >= len(self._raw_children):
            return None
        if row not in self._children:
            self._children[row] = TreeItem(self._raw_children[row], parent=self)
            self._child_to_row[id(self._children[row])] = row
        return self._children[row]

    def row(self) -> int:
        return 0 if self.parent is None else self.parent._row_of(self)

    @property
    def children(self):
        self._ensure_children_built()
        return [self.child(i) for i in range(self.child_count())]

    def _row_of(self, child):
        return self._child_to_row.get(id(child), 0)


class TreeModel(QAbstractItemModel):
    
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
        
        parent_item._ensure_children_built()
        return parent_item.child_count()
    
    def hasChildren(self, parent=QModelIndex()):
        if not parent.isValid():
            parent_item = self.rootItem
        else:
            parent_item = parent.internalPointer()
        return parent_item.has_children()

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
        if item is self.rootItem:
            return QModelIndex()
            
        if not item or not item.parent:
            return QModelIndex()
        
        row = item.row()
        return self.createIndex(row, 0, item)
    
    def addChildren(self, parent_item: TreeItem, children_raw: list):
        if parent_item is None or not children_raw:
            return False

        parent_item._ensure_children_built()
        
        start_row = parent_item.child_count()
        end_row   = start_row + len(children_raw) - 1

        parent_index = self.getIndexFromItem(parent_item)
        self.beginInsertRows(parent_index, start_row, end_row)

        for offset, raw in enumerate(children_raw):
            pos      = start_row + offset
            
            parent_item._raw_children.append(raw)

            child_it = TreeItem(raw, parent=parent_item)
            parent_item._children[pos] = child_it
            parent_item._child_to_row[id(child_it)] = pos
        if parent_item._children_list is not None:
            parent_item._children_list = [parent_item.child(i)
                                        for i in range(parent_item.child_count())]

        self.endInsertRows()
        return True
    
    def addChild(self, parent_item, child_raw):
        return self.addChildren(parent_item, [child_raw])


    def removeRow(self, row, parent=QModelIndex()):
        return self.removeRows(row, 1, parent)
        
    def removeRows(self, row, count, parent=QModelIndex()):
        if row < 0 or count <= 0:
            return False

        parent_item = self.rootItem if not parent.isValid() else parent.internalPointer()
        parent_item._ensure_children_built()
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
