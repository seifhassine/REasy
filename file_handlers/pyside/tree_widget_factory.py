from file_handlers.rsz.rsz_data_types import (
    StructData, U32Data, S32Data
)
from file_handlers.pyside.value_widgets import (
    Vec2Input, Vec3Input, Vec4Input, F32Input, S32Input, U32Input, S16Input, U16Input, U64Input, S64Input, S8Input, U8Input,
    GuidInput, OBBInput, AABBInput, AreaInput, Mat4Input, HexBytesInput, StringInput, BoolInput, UserDataInput, RangeInput,
    RangeIInput, ColorInput, Vec3ColorInput, CapsuleInput, Int3Input, EnumInput, F64Input
)
from utils.enum_manager import EnumManager

from PySide6.QtGui import QIcon
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget,
    QHBoxLayout,
    QLabel,
)

class TreeWidgetFactory:
    """Factory class for creating tree node widgets"""
    
    # Centralized widget type mapping - used by RszViewer currently
    WIDGET_TYPES = {
        "Vec2Data": Vec2Input,
        "Vec4Data": Vec4Input,
        "Float4Data": Vec4Input,
        "QuaternionData": Vec4Input,
        "Vec3Data": Vec3Input,
        "Float2Data": Vec2Input,
        "Float3Data": Vec3Input,
        "PositionData": Vec3Input,
        "GameObjectRefData": GuidInput,
        "GuidData": GuidInput,
        "OBBData": OBBInput,
        "AABBData": AABBInput,
        "AreaData": AreaInput,
        "Mat4Data": Mat4Input,
        "RawBytesData": HexBytesInput,
        "StringData": StringInput,
        "ResourceData": StringInput,
        "RuntimeTypeData": StringInput,
        "BoolData": BoolInput,
        "F32Data": F32Input,
        "S32Data": S32Input,
        "U32Data": U32Input,
        "S16Data": S16Input,
        "U16Data": U16Input,
        "U64Data": U64Input,
        "S64Data": S64Input,
        "S8Data": S8Input,
        "UserDataData": UserDataInput,
        "U8Data": U8Input, 
        "RangeData": RangeInput,
        "RangeIData": RangeIInput,
        "ColorData": ColorInput,
        "Vec3ColorData": Vec3ColorInput,
        "CapsuleData": CapsuleInput,
        "Int3Data": Int3Input,
        "F64Data": F64Input, 
        "SizeData": Vec2Input,
        "PointData": Vec3Input,
    }

    @staticmethod
    def create_widget(node_type, data_obj, name_text, widget_parent, on_modified=None):
        """Create appropriate widget based on node type"""
        widget = QWidget(widget_parent)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(4)
        
        min_label_width = 150
        if hasattr(widget_parent, 'label_width'):
            min_label_width = widget_parent.label_width
        
        if data_obj and hasattr(data_obj, '__class__') and data_obj.__class__.__name__ == 'ArrayData':
            label = QLabel()
            label.setText(f"{name_text} <span style='color: #666;'>(Array: {len(data_obj.values)} items)</span>")
            label.setTextFormat(Qt.RichText)
            layout.addWidget(label)
            
            layout.addStretch(1)
            
            return widget
            
        if data_obj and isinstance(data_obj, StructData):

            label = QLabel()
            label.setText(f"{name_text} <span style='color: #666;'>(Struct: {len(data_obj.values)} items)</span>")
            label.setTextFormat(Qt.RichText)
            layout.addWidget(label)
            
            return widget
        
        is_enum = False
        enum_type = None
        
        if data_obj and hasattr(data_obj, 'orig_type') and data_obj.orig_type:
            enum_type = data_obj.orig_type
            enum_values = EnumManager.instance().get_enum_values(enum_type)
            if enum_values:
                is_enum = True
                enum_value_list = [val["value"] for val in enum_values]
                if data_obj.value not in enum_value_list:
                    if isinstance(data_obj, U32Data):
                        original_value = data_obj.value
                        converted_value = original_value - 0x100000000 if original_value > 0x7FFFFFFF else original_value
                        if converted_value in enum_value_list:
                            new_data = S32Data()
                            new_data.__dict__.update(data_obj.__dict__)
                            new_data.value = converted_value
                            data_obj = new_data
                    elif isinstance(data_obj, S32Data):
                        original_value = data_obj.value
                        converted_value = original_value + 0x100000000 if original_value < 0 else original_value
                        if converted_value in enum_value_list:
                            new_data = U32Data()
                            new_data.__dict__.update(data_obj.__dict__)
                            new_data.value = converted_value
                            data_obj = new_data
        if is_enum:
            label = QLabel(name_text)
            label.setMinimumWidth(min_label_width)
            layout.addWidget(label)
            
            input_widget = EnumInput(parent=widget)
            input_widget.set_data(data_obj)
            input_widget.set_enum_values(EnumManager.instance().get_enum_values(enum_type))
            layout.addWidget(input_widget)
            
            if on_modified:
                input_widget.modified_changed.connect(on_modified)
        
        elif node_type in TreeWidgetFactory.WIDGET_TYPES and data_obj:
            label = QLabel(name_text)
            label.setMinimumWidth(min_label_width)
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
        elif node_type in ("gameobject", "folder", "template"):
            icon_name = node_type
            
            icon = QIcon(f"resources/icons/{icon_name}.png")
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
            label = QLabel(name_text)
            label.setMinimumWidth(min_label_width)
            layout.addWidget(label)
            widget.setFixedHeight(24)
            
        return widget

    @staticmethod
    def should_create_widget(item):
        """Check if we should create a widget for this item"""
        if not isinstance(item.raw, dict):
            return True
            
        # Skip array nodes and array elements
        if item.raw.get("type") == "array" or item.raw.get("type") == "struct":
            return False
            
        parent = item.parent
        if parent and isinstance(parent.raw, dict) and parent.raw.get("type") in ("array", "struct"):
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

        # Skip artificial group nodes used for chunked array loading
        if item.raw.get("type") == "array_group":
            return True
            
        # Don't skip array element widgets if they have a supported type
        return False
