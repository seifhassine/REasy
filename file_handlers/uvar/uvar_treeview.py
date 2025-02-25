from PySide6.QtCore import Qt, QModelIndex, QAbstractItemModel, QSize 
from PySide6.QtWidgets import QCheckBox, QVBoxLayout, QTreeView, QStyledItemDelegate, QWidget, QLineEdit, QHBoxLayout, QComboBox 

# Sample tree item class
class LazyTreeItem:
    def __init__(self, raw_data, parent=None):
        self.raw = raw_data
        self.parent_item = parent
        self.child_items = []
        self.data = raw_data.get('data', [])
        
        # Pre-create children from raw data
        for child_data in raw_data.get('children', []):
            self.child_items.append(LazyTreeItem(child_data, self))

    def child(self, row):
        if 0 <= row < len(self.child_items):
            return self.child_items[row]
        return None

    def childCount(self):
        return len(self.child_items)

    def columnCount(self):
        return len(self.data)

    def row(self):
        if self.parent_item:
            return self.parent_item.child_items.index(self)
        return 0

    def parent(self):
        return self.parent_item

# Custom QAbstractItemModel for lazy loading tree data
class LazyTreeModel(QAbstractItemModel):
    def __init__(self, root_data):
        super().__init__()
        self.root_item = LazyTreeItem(root_data)

    def columnCount(self, parent=QModelIndex()):
        if parent.isValid():
            return parent.internalPointer().columnCount()
        return len(self.root_item.data)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None

        item = index.internalPointer()

        if role == Qt.DisplayRole:
            if 0 <= index.column() < len(item.data):
                return str(item.data[index.column()])
            return None
        
        elif role == Qt.UserRole:
            # Return metadata if available
            if hasattr(item, 'raw') and 'meta' in item.raw:
                return item.raw['meta']
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            if hasattr(self.root_item, 'raw'):
                columns = self.root_item.raw.get('columns', [])
                if 0 <= section < len(columns):
                    return columns[section]
        return None

    def index(self, row, column, parent=QModelIndex()):
        if not self.hasIndex(row, column, parent):
            return QModelIndex()

        if not parent.isValid():
            parent_item = self.root_item
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
        parent_item = child_item.parent()

        if parent_item == self.root_item:
            return QModelIndex()

        return self.createIndex(parent_item.row(), 0, parent_item)

    def rowCount(self, parent=QModelIndex()):
        if parent.column() > 0:
            return 0

        if not parent.isValid():
            parent_item = self.root_item
        else:
            parent_item = parent.internalPointer()

        return parent_item.childCount()

    def setData(self, index, value, role=Qt.EditRole):
        if role == Qt.EditRole:
            item = index.internalPointer()
            if item and index.column() < len(item.data):
                item.data[index.column()] = value
                self.dataChanged.emit(index, index)
                return True
        return False

# Custom delegate for advanced appearance and embedded widgets
class AdvancedTreeDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QWidget(parent)
        layout = QHBoxLayout(editor)
        layout.setSpacing(5)
        layout.setContentsMargins(0, 0, 0, 0)
        editor.line1 = QLineEdit(editor)
        editor.line2 = QLineEdit(editor)
        layout.addWidget(editor.line1)
        layout.addWidget(editor.line2)
        editor.setLayout(layout)
        return editor

    def setEditorData(self, editor, index):
        value = index.model().data(index, Qt.EditRole)
        editor.line1.setText(str(value))
        editor.line2.setText(str(value))

    def setModelData(self, editor, model, index):
        new_value = editor.line1.text()
        model.setData(index, new_value, Qt.EditRole)

    def paint(self, painter, option, index):
        super().paint(painter, option, index)

    def sizeHint(self, option, index):
        return QSize(100, 40)

class AdvancedTreeView(QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setUniformRowHeights(False) 
        self.setItemDelegate(AdvancedTreeDelegate())
        self.setExpandsOnDoubleClick(True)

    def setModelData(self, rootData):
        model = LazyTreeModel(rootData)
        self.setModel(model)
        header = self.header()
        header.setSectionResizeMode(0, header.ResizeToContents)
        header.setSectionResizeMode(1, header.Stretch)
        self.embed_forms() 

    def embed_forms(self):
        """Embed composite widgets in the value (column 1) cells.
        """
        model = self.model()
        def embed_recursive(parent_index):
            rows = model.rowCount(parent_index)
            for row in range(rows):
                # Get index for column 1 (value field)
                index = model.index(row, 1, parent_index)
                if not index.isValid():
                    continue
                # Determine if  node is a branch (has children) or leaf.
                child_index = model.index(row, 0, parent_index)
                if model.rowCount(child_index) > 0:
                    widget = QWidget()
                    h_layout = QHBoxLayout(widget)
                    h_layout.setContentsMargins(0, 0, 0, 0)
                    combo = QComboBox()
                    combo.addItems(["Branch Option A", "Branch Option B"])
                    line = QLineEdit("Branch note")
                    h_layout.addWidget(combo)
                    h_layout.addWidget(line)
                    widget.setLayout(h_layout)
                else:
                    widget = QWidget()
                    v_layout = QVBoxLayout(widget)
                    v_layout.setContentsMargins(0, 0, 0, 0)
                    line_edit = QLineEdit("Leaf text")
                    combo = QComboBox()
                    combo.addItems(["Leaf Option 1", "Leaf Option 2", "Leaf Option 3"])
                    check = QCheckBox("Confirm")
                    v_layout.addWidget(line_edit)
                    v_layout.addWidget(combo)
                    v_layout.addWidget(check)
                    widget.setLayout(v_layout)
                self.setIndexWidget(index, widget)
                # Recurse for children in column 0
                child_parent = model.index(row, 0, parent_index)
                if model.rowCount(child_parent) > 0:
                    embed_recursive(child_parent)
        top_count = model.rowCount(QModelIndex())
        for row in range(top_count):
            parent_index = model.index(row, 0, QModelIndex())
            embed_recursive(parent_index)
