"""
Consolidated tree infrastructure that handles both eager and lazy-loading approaches.
"""

from PySide6.QtCore import Qt, QModelIndex, QAbstractItemModel
from PySide6.QtWidgets import QAbstractItemView, QStyledItemDelegate

class TreeItem:
    """
    Tree node that references a dict of the form:
       {
         "data": ["Title", "Value"],
         "children": [ {...}, {...} ]
       }
    """
    def __init__(self, data, parent=None, lazy_loading=True):
        self.raw = data
        if isinstance(data, dict) and "data" in data:
            self.data = data["data"]
        else:
            self.data = data
        self.parent = parent
        self.lazy_loading = lazy_loading
        
        if lazy_loading:
            self._children = None  # Lazy loading - load children on demand
        else:
            self.children = []     # Eager loading - load children immediately
            self._load_children()

    def child_count(self):
        if self.lazy_loading:
            if self._children is None:
                self._load_children()
            return len(self._children)
        else:
            return len(self.children)

    def child(self, row):
        if self.lazy_loading:
            if self._children is None:
                self._load_children()
            return self._children[row] if 0 <= row < len(self._children) else None
        else:
            return self.children[row] if 0 <= row < len(self.children) else None

    def row(self):
        if self.parent:
            if self.lazy_loading:
                return self.parent._children.index(self) if self in self.parent._children else 0
            else:
                return self.parent.children.index(self) if self in self.parent.children else 0
        return 0

    def _load_children(self):
        """Load children from the raw data dict"""
        children_list = [] if self.lazy_loading else self.children
        
        if isinstance(self.raw, dict) and "children" in self.raw:
            for child_data in self.raw["children"]:
                children_list.append(TreeItem(child_data, parent=self, lazy_loading=self.lazy_loading))
                
        if self.lazy_loading:
            self._children = children_list


class TreeModel(QAbstractItemModel):
    """Unified tree model implementation with optional lazy loading"""
    
    def __init__(self, root_data, parent=None, lazy_loading=False):
        super().__init__(parent)
        self.rootItem = TreeItem(root_data, lazy_loading=lazy_loading)
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
        if parent.column() > 0:
            return 0
        if not parent.isValid():
            parent_item = self.rootItem
        else:
            parent_item = parent.internalPointer()
        return parent_item.child_count()

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
    
    def getIndexFromItem(self, item):
        """Get the model index for a tree item"""
        if item is self.rootItem:
            return QModelIndex()
            
        if not item or not item.parent:
            return QModelIndex()
        
        # Find this item's row in its parent
        row = item.row()
        return self.createIndex(row, 0, item)
    
    def addChild(self, parent_item, child_data):
        """Add a new child to an existing item"""
        if not parent_item or not child_data:
            return False
            
        # Begin row insertion - tell views that we're adding a new item
        parent_index = self.getIndexFromItem(parent_item)
        position = parent_item.child_count()
        self.beginInsertRows(parent_index, position, position)
        
        # Create the new tree item and add it as a child
        new_item = TreeItem(child_data, parent_item, lazy_loading=parent_item.lazy_loading)
        if parent_item.lazy_loading:
            if parent_item._children is None:
                parent_item._load_children()
            parent_item._children.append(new_item)
        else:
            parent_item.children.append(new_item)
        
        # End row insertion - tell views we're done
        self.endInsertRows()
        return True

    def removeRow(self, row, parent=QModelIndex()):
        """Remove a row from the model"""
        return self.removeRows(row, 1, parent)
        
    def removeRows(self, row, count, parent=QModelIndex()):
        """Remove multiple rows from the model"""
        if row < 0 or count <= 0:
            return False
            
        if not parent.isValid():
            parent_item = self.rootItem
        else:
            parent_item = parent.internalPointer()
            
        if row + count > len(parent_item.children):
            return False
            
        self.beginRemoveRows(parent, row, row + count - 1)
        
        for _ in range(count):
            del parent_item.children[row]
            
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
