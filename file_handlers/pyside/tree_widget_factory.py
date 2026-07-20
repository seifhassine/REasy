from file_handlers.rsz.rsz_data_types import (
    StructData, U32Data, S32Data
)
import uuid
from file_handlers.pyside.value_widgets import (
    Vec2Input, Vec3Input, Vec4Input, F32Input, S32Input, U32Input, S16Input, U16Input, U64Input, S64Input, S8Input, U8Input,
    GuidInput, OBBInput, AABBInput, AreaInput, Mat4Input, HexBytesInput, StringInput, BoolInput, UserDataInput, RangeInput,
    RangeIInput, ColorInput, Vec3ColorInput, CapsuleInput, Int3Input, Uint3Input, Uint2Input, Int2Input, Int4Input, Int4ColorInput, EnumInput, F64Input, 
    SizeInput, RectInput
)
from utils.enum_manager import EnumManager

from PySide6.QtGui import QIcon
from PySide6.QtCore import QCoreApplication, Qt
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
        "RectData": RectInput,
        "AreaData": AreaInput,
        "AreaDataOld": AreaInput,
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
        "Uint3Data": Uint3Input,
        "Uint2Data": Uint2Input,
        "Int2Data": Int2Input,
        "Int4Data": Int4Input,
        "Int4ColorData": Int4ColorInput,
        "F64Data": F64Input, 
        "SizeData": SizeInput,
        "PointData": Vec3Input,
    }

    @staticmethod
    def tr(text):
        return QCoreApplication.translate("TreeWidgetFactory", text)

    @staticmethod
    def _add_collection_summary(
        layout,
        name_text,
        data_obj,
        collection_name,
        *,
        add_stretch=False,
    ):
        label = QLabel()
        if collection_name == "Array":
            summary_text = TreeWidgetFactory.tr(
                "{name} <span style='color: #666;'>"
                "(Array: {count} items)</span>"
            )
        else:
            summary_text = TreeWidgetFactory.tr(
                "{name} <span style='color: #666;'>"
                "(Struct: {count} items)</span>"
            )
        label.setText(
            summary_text.format(
                name=name_text, count=len(data_obj.values)
            )
        )
        label.setTextFormat(Qt.RichText)
        layout.addWidget(label)
        if add_stretch:
            layout.addStretch(1)

    @staticmethod
    def _normalize_enum_data(data_obj):
        enum_type = getattr(data_obj, "orig_type", None) if data_obj else None
        if not enum_type:
            return data_obj, None, None

        enum_values = EnumManager.instance().get_enum_values(enum_type)
        if not enum_values:
            return data_obj, enum_type, None

        enum_value_list = [value["value"] for value in enum_values]
        if data_obj.value in enum_value_list:
            return data_obj, enum_type, enum_values

        if isinstance(data_obj, U32Data):
            converted_value = (
                data_obj.value - 0x100000000
                if data_obj.value > 0x7FFFFFFF
                else data_obj.value
            )
            converted_class = S32Data
        elif isinstance(data_obj, S32Data):
            converted_value = (
                data_obj.value + 0x100000000
                if data_obj.value < 0
                else data_obj.value
            )
            converted_class = U32Data
        else:
            return data_obj, enum_type, enum_values

        if converted_value not in enum_value_list:
            return data_obj, enum_type, enum_values

        converted_data = converted_class()
        converted_data.__dict__.update(data_obj.__dict__)
        converted_data.value = converted_value
        return converted_data, enum_type, enum_values

    @staticmethod
    def _add_input_widget(
        layout,
        widget,
        name_text,
        min_label_width,
        data_obj,
        input_class,
        on_modified,
        enum_values=None,
    ):
        label = QLabel(name_text)
        label.setMinimumWidth(min_label_width)
        layout.addWidget(label)

        input_widget = input_class(parent=widget)
        input_widget.set_data(data_obj)
        if enum_values is not None:
            input_widget.set_enum_values(enum_values)
        layout.addWidget(input_widget)

        if on_modified:
            input_widget.modified_changed.connect(
                lambda _=False, obj=data_obj: on_modified(obj)
            )
        return input_widget

    @staticmethod
    def _connect_gameobject_guid(input_widget, data_obj):
        if not (
            hasattr(data_obj, "gameobject")
            and hasattr(input_widget, "valueChanged")
        ):
            return

        def _on_guid_changed(new_guid: str, target=data_obj):
            target.guid_str = new_guid
            target.gameobject.guid = uuid.UUID(new_guid).bytes_le

        input_widget.valueChanged.connect(_on_guid_changed)

    @staticmethod
    def _add_icon_widget(layout, widget, node_type, name_text):
        icon = QIcon(f"resources/icons/{node_type}.png")
        icon_label = QLabel()
        icon_label.setFixedWidth(16)
        icon_label.setPixmap(icon.pixmap(16, 16))

        text_label = QLabel(name_text)
        text_label.setStyleSheet("padding-left: 2px;")

        layout.addWidget(icon_label)
        layout.addWidget(text_label, 1)
        widget.setFixedHeight(30)

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
            TreeWidgetFactory._add_collection_summary(
                layout,
                name_text,
                data_obj,
                "Array",
                add_stretch=True,
            )
            return widget
            
        if data_obj and isinstance(data_obj, StructData):
            TreeWidgetFactory._add_collection_summary(
                layout,
                name_text,
                data_obj,
                "Struct",
            )
            return widget
        
        data_obj, enum_type, enum_values = (
            TreeWidgetFactory._normalize_enum_data(data_obj)
        )
        if enum_values:
            TreeWidgetFactory._add_input_widget(
                layout,
                widget,
                name_text,
                min_label_width,
                data_obj,
                EnumInput,
                on_modified,
                enum_values,
            )
        
        elif node_type in TreeWidgetFactory.WIDGET_TYPES and data_obj:
            input_class = TreeWidgetFactory.WIDGET_TYPES[node_type]
            input_widget = TreeWidgetFactory._add_input_widget(
                layout,
                widget,
                name_text,
                min_label_width,
                data_obj,
                input_class,
                on_modified,
            )
            if node_type == "GuidData":
                TreeWidgetFactory._connect_gameobject_guid(
                    input_widget, data_obj
                )
                
            if node_type in ("OBBData", "Mat4Data"):
                widget.setFixedHeight(150)
                
        # Icon widgets
        elif node_type in ("gameobject", "folder", "template"):
            TreeWidgetFactory._add_icon_widget(
                layout, widget, node_type, name_text
            )
            
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
