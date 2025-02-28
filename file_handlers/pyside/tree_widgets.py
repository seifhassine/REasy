from PySide6.QtWidgets import (QWidget, QHBoxLayout, QLabel, QTreeView, 
                               QHeaderView, QSpinBox, QMenu, QDoubleSpinBox, QMessageBox, QStyledItemDelegate)
from PySide6.QtGui import QIcon, QCursor
from PySide6.QtCore import Qt

from file_handlers.rsz.rsz_data_types import ArrayData
from .tree_core import TreeModel

from .value_widgets import *
from utils.enum_manager import EnumManager


class AdvancedStyledDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.default_row_height = 24 
        
    def sizeHint(self, option, index):
        """Ensure consistent row height for all items"""
        size = super().sizeHint(option, index)
        
        tree_view = self.parent()
        if tree_view and hasattr(tree_view, 'default_row_height'):
            self.default_row_height = tree_view.default_row_height
            
        size.setHeight(self.default_row_height)
        return size

class AdvancedTreeView(QTreeView):
    """
    A QTreeView that uses an advanced delegate or a simpler delegate,
    and supports expansions.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setItemDelegate(AdvancedStyledDelegate()) 
        self.setUniformRowHeights(False)
        self.setExpandsOnDoubleClick(True)
        self.setHeaderHidden(True)  
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self.show_context_menu)
        self.parent_modified_callback = None

    def setModelData(self, rootData):
        """
        Helper to build a TreeModel from the nested dict.
        """
        model = TreeModel(rootData)
        self.setModel(model)
        header = self.header()
        header.setSectionResizeMode(0, QHeaderView.Stretch) 

    def embed_forms(self, parent_modified_callback=None):
        """Use TreeWidgetFactory for embedding widgets consistently"""
        self.parent_modified_callback = parent_modified_callback
        model = self.model()
        if not model:
            return

        def embed_children_on_expand(parent_index):
            self.expand(parent_index)
            self.create_widgets_for_children(parent_index)

        # Only connect the signal
        self.expanded.connect(embed_children_on_expand)

    def create_widgets_for_children(self, parent_index):
        """Create widgets for all children of the given parent index"""
        model = self.model()
        if not model:
            return

        rows = model.rowCount(parent_index)
        for row in range(rows):
            index0 = model.index(row, 0, parent_index)
            if not index0.isValid():
                continue
                
            item = index0.internalPointer()
            if not item or TreeWidgetFactory.should_skip_widget(item):
                continue

            name_text = item.data[0] if item.data else ""
            node_type = item.raw.get("type", "") if isinstance(item.raw, dict) else ""
            data_obj = item.raw.get("obj", None) if isinstance(item.raw, dict) else None
            
            widget = TreeWidgetFactory.create_widget(
                node_type, data_obj, name_text, self, self.parent_modified_callback
            )
            if widget:
                self.setIndexWidget(index0, widget)

    def show_context_menu(self, position):
        """Show context menu for tree items"""
        index = self.indexAt(position)
        if not index.isValid():
            return
            
        item = index.internalPointer()
        if not item:
            return
        
        is_array = False
        array_type = ""
        data_obj = None
        
        if hasattr(item, 'raw') and isinstance(item.raw, dict):
            is_array = item.raw.get("type") == "array"
            data_obj = item.raw.get("obj")
            if is_array and data_obj:
                if hasattr(data_obj, 'orig_type') and data_obj.orig_type:
                    array_type = data_obj.orig_type
      

        if array_type:
            print(f"Showing menu for array: {array_type}")
            menu = QMenu(self)
            add_action = menu.addAction("Add New Element")
            action = menu.exec_(QCursor.pos())
            
            if action == add_action:
                self.add_array_element(index, array_type, data_obj, item)
        else:
            print(f"No array detected. is_array={is_array}, array_type={array_type}")

    def add_array_element(self, index, array_type, data_obj, array_item):
        """Add a new element to an array"""
        if not array_type.endswith("[]"):
            QMessageBox.warning(self, "Error", f"Invalid array type format: {array_type}")
            return
            
        element_type = array_type[:-2]  # Remove the [] suffix
        
        # Get the parent widget/handler that can create elements
        parent = self.parent()
        if not hasattr(parent, "create_array_element"):
            QMessageBox.warning(self, "Error", "Cannot find element creator")
            return
            
        try:
            new_element = parent.create_array_element(element_type, data_obj, direct_update=True, array_item=array_item)
            if new_element:
                if not self.isExpanded(index):
                    self.expand(index)

                model = self.model()
                array_item = index.internalPointer()
                if hasattr(array_item, 'children') and array_item.children:
                    last_child_idx = model.index(len(array_item.children) - 1, 0, index)
                    if last_child_idx.isValid():
                        item = last_child_idx.internalPointer()
                        if item and not TreeWidgetFactory.should_skip_widget(item):
                            name_text = item.data[0] if item.data else ""
                            node_type = item.raw.get("type", "") if isinstance(item.raw, dict) else ""
                            data_obj = item.raw.get("obj", None) if isinstance(item.raw, dict) else None
                            
                            widget = TreeWidgetFactory.create_widget(
                                node_type, data_obj, name_text, self, self.parent_modified_callback
                            )
                            if widget:
                                self.setIndexWidget(last_child_idx, widget)

                        self.scrollTo(last_child_idx)
                        
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to add element: {str(e)}")

    def get_value_at_index(self, index):
        """Get value from either embedded widget or model data"""
        if not index.isValid():
            return ""
            
        widget = self.indexWidget(index)
        if widget:
            input_widget = None
            for child in widget.findChildren(QWidget):
                if isinstance(child, (QLineEdit, QDoubleSpinBox, QSpinBox, QCheckBox)):
                    input_widget = child
                    break
                    
            if input_widget:
                if isinstance(input_widget, QLineEdit):
                    return input_widget.text()
                elif isinstance(input_widget, (QSpinBox, QDoubleSpinBox)):
                    return str(input_widget.value())
                elif isinstance(input_widget, QCheckBox):
                    return str(input_widget.isChecked())
                    
        # Fallback to model data
        item = index.internalPointer()
        if item and hasattr(item, 'data') and len(item.data) > 1:
            return str(item.data[1])
            
        return str(index.data(Qt.UserRole) or "")

class TreeWidgetFactory:
    """Factory class for creating tree node widgets"""
    
    # Centralized widget type mapping - used by RszViewer currently
    WIDGET_TYPES = {
        "Vec4Data": Vec4Input,
        "QuaternionData": Vec4Input,
        "Vec3Data": Vec3Input,
        "GameObjectRefData": GuidInput,
        "GuidData": GuidInput,
        "OBBData": OBBInput,
        "Mat4Data": Mat4Input,
        "RawBytesData": HexBytesInput,
        "StringData": StringInput,
        "BoolData": BoolInput,
        "F32Data": F32Input,
        "S32Data": S32Input,
        "U32Data": U32Input,
        "S8Data": S8Input,
        "UserDataData": UserDataInput,
        "U8Data": U8Input, 
        "RangeData": RangeInput,
        "RangeIData": RangeIInput,
        "EnumInput": EnumInput, 
    }

    @staticmethod
    def create_widget(node_type, data_obj, name_text, widget_parent, on_modified=None):
        """Create appropriate widget based on node type"""
        widget = QWidget(widget_parent)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        
        is_enum = False
        enum_type = None
        
        if data_obj and hasattr(data_obj, 'orig_type') and data_obj.orig_type:
            enum_type = data_obj.orig_type
            enum_values = EnumManager.instance().get_enum_values(enum_type)
            if enum_values:
                is_enum = True
        
        if is_enum:
            label = QLabel(name_text)
            layout.addWidget(label)
            
            input_widget = EnumInput(parent=widget)
            input_widget.set_data(data_obj)
            input_widget.set_enum_values(EnumManager.instance().get_enum_values(enum_type))
            layout.addWidget(input_widget)
            
            if on_modified:
                input_widget.modified_changed.connect(on_modified)
        
        elif node_type in TreeWidgetFactory.WIDGET_TYPES and data_obj:
            label = QLabel(name_text)
            layout.addWidget(label)
            
            input_class = TreeWidgetFactory.WIDGET_TYPES[node_type]
            input_widget = input_class(parent=widget)
            input_widget.set_data(data_obj)
            layout.addWidget(input_widget)
            
            if on_modified:
                input_widget.modified_changed.connect(on_modified)
                
            if node_type == "OBBData" or node_type == "Mat4Data":
                widget.setFixedHeight(150)
                
        # Icon widgets
        elif node_type in ("gameobject", "folder"):
            icon = QIcon(f"resources/icons/{node_type}.png")
            icon_label = QLabel()
            icon_label.setFixedWidth(16)
            icon_label.setPixmap(icon.pixmap(16, 16))
            
            text_label = QLabel(name_text)
            text_label.setStyleSheet("padding-left: 2px;")
            
            layout.addWidget(icon_label)
            layout.addWidget(text_label, 1)
            widget.setFixedHeight(30)
            
        # Default text widgets
        else:
            layout.addWidget(QLabel(name_text))
            widget.setFixedHeight(24)
            
        return widget

    @staticmethod
    def should_create_widget(item):
        """Check if we should create a widget for this item"""
        if not isinstance(item.raw, dict):
            return True
            
        # Skip array nodes and array elements
        if item.raw.get("type") == "array":
            return False
            
        parent = item.parent
        if parent and isinstance(parent.raw, dict) and parent.raw.get("type") == "array":
            return False
            
        return True

    @staticmethod
    def should_skip_widget(item):
        """Only create widgets for editable data objects"""
        if not isinstance(item.raw, dict):
            return True

        # Trash, needs rework
        name = item.raw.get("data", [""])[0]
        if name in ("Header", "GameObjects", "RSZHeader", "Folder Infos", "Object Table", "Instance Infos", "RSZUserData Infos"):
            return True
            
        # Don't skip array element widgets if they have a supported type
        node_type = item.raw.get("type")
        if node_type in TreeWidgetFactory.WIDGET_TYPES:
            return False
   
        return False
