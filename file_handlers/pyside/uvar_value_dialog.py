"""Dialog for editing UVAR variable values with type-specific input fields"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QCheckBox, QSpinBox, QDoubleSpinBox,
    QDialogButtonBox, QWidget
)
from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt
from PySide6.QtGui import QDoubleValidator, QIntValidator

import uuid

from utils.number_format import format_float_sequence, format_full_float


VALUE_LABEL_TEXT = QT_TRANSLATE_NOOP(
    "UvarValueEditDialog", "Value:"
)


class UvarValueEditDialog(QDialog):
    """Dialog for editing UVAR variable values with type-specific input"""
    
    def __init__(self, var_name, var_type, current_value, var_flags=0, parent=None):
        super().__init__(parent)
        self.var_type = var_type
        self.current_value = current_value
        self.var_flags = var_flags
        self.new_value = None
        
        self.setWindowTitle(self.tr("Edit Value: {name}").format(name=var_name))
        self.setModal(True)
        self.setMinimumWidth(400)
        
        # Main layout
        layout = QVBoxLayout(self)
        
        # Type label
        type_label = QLabel(self.tr("Type: {type}").format(type=var_type.name))
        type_label.setStyleSheet("font-weight: bold; color: #4EC9B0;")
        layout.addWidget(type_label)
        
        # Create type-specific input widget
        self.input_widget = self._create_input_widget(var_type, current_value)
        layout.addWidget(self.input_widget)
        
        # Buttons
        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel,
            Qt.Horizontal, self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        
    def _create_input_widget(self, var_type, current_value):
        """Create type-specific input widget"""
        from file_handlers.uvar import TypeKind, UvarFlags
        
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(0, 10, 0, 10)
        
        # Check if this is a vector type based on flags
        is_vec3_flag = bool(self.var_flags & UvarFlags.IsVec3) if self.var_flags else False
        
        # Format current value for display
        current_str = self._format_current_value(current_value)
        
        # Handle special case: vector type based on flags
        if is_vec3_flag and var_type in (TypeKind.Single, TypeKind.Int32, TypeKind.Uint32, 
                                          TypeKind.Int8, TypeKind.Int16, TypeKind.Uint8, TypeKind.Uint16):
            self._create_flagged_vector_input(
                layout, var_type, current_value, TypeKind
            )
            
        elif var_type == TypeKind.Boolean:
            # Checkbox for boolean
            self.input = QCheckBox(self.tr("Value"))
            if current_value:
                self.input.setChecked(bool(current_value))
            layout.addWidget(self.input)
            
        elif var_type in (TypeKind.Int8, TypeKind.Int16, TypeKind.Int32, TypeKind.Int64, TypeKind.Enum):
            self._create_integer_input(
                layout, var_type, current_value, TypeKind, unsigned=False
            )
            
        elif var_type in (TypeKind.Uint8, TypeKind.Uint16, TypeKind.Uint32, TypeKind.Uint64):
            self._create_integer_input(
                layout, var_type, current_value, TypeKind, unsigned=True
            )
            
        elif var_type in (TypeKind.Single, TypeKind.Double):
            self.input = self._create_line_input(
                layout, current_str, validator=QDoubleValidator()
            )
            
        elif var_type in (TypeKind.C8, TypeKind.C16, TypeKind.String):
            self.input = self._create_line_input(
                layout, current_str, minimum_width=250
            )
            
        elif var_type == TypeKind.GUID:
            self.input = self._create_line_input(
                layout,
                str(current_value)
                if current_value
                else "00000000-0000-0000-0000-000000000000",
                label_text="GUID:",
                placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
                minimum_width=280,
            )
            
        elif var_type in (TypeKind.Vec2, TypeKind.Vec3, TypeKind.Vec4):
            self._create_vector_input(
                layout, var_type, current_value, TypeKind
            )
            
        else:
            self.input = self._create_line_input(layout, current_str)
            
        layout.addStretch()
        return container

    def _create_line_input(
        self,
        layout,
        text,
        *,
        validator=None,
        label_text=VALUE_LABEL_TEXT,
        placeholder=None,
        minimum_width=None,
    ):
        layout.addWidget(QLabel(self.tr(label_text)))
        input_field = QLineEdit()
        input_field.setText(text)
        if validator is not None:
            input_field.setValidator(validator)
        if placeholder is not None:
            input_field.setPlaceholderText(placeholder)
        layout.addWidget(input_field)
        if minimum_width is not None:
            input_field.setMinimumWidth(minimum_width)
        return input_field

    def _create_integer_input(
        self,
        layout,
        var_type,
        current_value,
        type_kind,
        *,
        unsigned,
    ):
        if unsigned:
            bounds = {
                type_kind.Uint8: (0, 255),
                type_kind.Uint16: (0, 65535),
                type_kind.Uint32: (0, 4294967295),
                type_kind.Uint64: (0, 9223372036854775807),
            }
        else:
            bounds = {
                type_kind.Int8: (-128, 127),
                type_kind.Int16: (-32768, 32767),
                type_kind.Int32: (-2147483648, 2147483647),
            }
        validator_bounds = bounds.get(var_type)
        validator = (
            QIntValidator(*validator_bounds)
            if validator_bounds is not None
            else QIntValidator()
        )
        self.input = self._create_line_input(
            layout,
            str(current_value) if current_value is not None else "0",
            validator=validator,
        )

    def _create_flagged_vector_input(
        self,
        layout,
        var_type,
        current_value,
        type_kind,
    ):
        if hasattr(current_value, 'x'):
            values = [current_value.x, current_value.y, current_value.z]
        elif isinstance(current_value, (list, tuple)) and len(current_value) >= 3:
            values = list(current_value[:3])
        else:
            values = [0, 0, 0]

        self.inputs = []
        for component, value in zip(('X', 'Y', 'Z'), values):
            layout.addWidget(QLabel(f"{component}:"))
            input_field = QLineEdit()
            if var_type == type_kind.Single:
                input_field.setValidator(QDoubleValidator())
                input_field.setText(format_full_float(value, 8))
            else:
                input_field.setValidator(QIntValidator())
                input_field.setText(str(int(value)))
            input_field.setMaximumWidth(100)
            layout.addWidget(input_field)
            self.inputs.append(input_field)
        self.input = self.inputs

    def _create_vector_input(
        self,
        layout,
        var_type,
        current_value,
        type_kind,
    ):
        num_components = {
            type_kind.Vec2: 2,
            type_kind.Vec3: 3,
            type_kind.Vec4: 4,
        }[var_type]
        if hasattr(current_value, 'x'):
            values = [current_value.x, current_value.y]
            if num_components >= 3:
                values.append(current_value.z)
            if num_components >= 4:
                values.append(current_value.w)
        elif (
            isinstance(current_value, (list, tuple))
            and len(current_value) >= num_components
        ):
            values = list(current_value[:num_components])
        else:
            values = [0.0] * num_components

        self.inputs = []
        for component, value in zip(('X', 'Y', 'Z', 'W'), values):
            layout.addWidget(QLabel(f"{component}:"))
            input_field = QLineEdit()
            input_field.setValidator(QDoubleValidator())
            input_field.setText(format_full_float(value, 8))
            input_field.setMaximumWidth(100)
            layout.addWidget(input_field)
            self.inputs.append(input_field)
        self.input = self.inputs
        
    def _format_current_value(self, value):
        """Format current value for display"""
        if value is None:
            return ""
        elif isinstance(value, bool):
            return "True" if value else "False"
        elif isinstance(value, float):
            return format_full_float(value, 8)
        elif isinstance(value, str):
            return value.strip('\x00')  # Remove null terminators
        elif hasattr(value, 'x') and hasattr(value, 'y'):
            # Vector types
            if hasattr(value, 'w'):
                return format_float_sequence((value.x, value.y, value.z, value.w), 8)
            elif hasattr(value, 'z'):
                return format_float_sequence((value.x, value.y, value.z), 8)
            else:
                return format_float_sequence((value.x, value.y), 8)
        elif isinstance(value, (list, tuple)):
            return format_float_sequence(value, 8)
        else:
            return str(value)
            
    def get_value(self):
        """Get the edited value"""
        from file_handlers.uvar import TypeKind, Vec3, Int3, Uint3, UvarFlags
        
        if not self.input:
            return None
            
        # Check if this is a vector type based on flags
        is_vec3_flag = bool(self.var_flags & UvarFlags.IsVec3) if self.var_flags else False
            
        try:
            # Handle special case: vector type based on flags
            if is_vec3_flag and self.var_type in (TypeKind.Single, TypeKind.Int32, TypeKind.Uint32,
                                                  TypeKind.Int8, TypeKind.Int16, TypeKind.Uint8, TypeKind.Uint16):
                return self._get_flagged_vector_value(
                    TypeKind, Vec3, Int3, Uint3
                )
            if self.var_type == TypeKind.Boolean:
                return self.input.isChecked()
                
            elif self.var_type in (TypeKind.Int8, TypeKind.Int16, TypeKind.Int32, TypeKind.Int64, TypeKind.Enum):
                text = self.input.text()
                return int(text) if text else 0
                
            elif self.var_type in (TypeKind.Uint8, TypeKind.Uint16, TypeKind.Uint32, TypeKind.Uint64):
                text = self.input.text()
                value = int(text) if text else 0
                masks = {
                    TypeKind.Uint8: 0xFF,
                    TypeKind.Uint16: 0xFFFF,
                    TypeKind.Uint32: 0xFFFFFFFF,
                    TypeKind.Uint64: 0xFFFFFFFFFFFFFFFF,
                }
                return value & masks[self.var_type]
                    
            elif self.var_type in (TypeKind.Single, TypeKind.Double):
                text = self.input.text()
                return float(text) if text else 0.0
                
            elif self.var_type in (TypeKind.C8, TypeKind.C16, TypeKind.String):
                return self.input.text()
                
            elif self.var_type == TypeKind.GUID:
                text = self.input.text().strip()
                try:
                    return uuid.UUID(text)
                except:
                    return uuid.UUID('00000000-0000-0000-0000-000000000000')
                    
            elif self.var_type in (TypeKind.Vec2, TypeKind.Vec3, TypeKind.Vec4):
                values = self._get_float_input_values()
                if self.var_type != TypeKind.Vec3:
                    return tuple(values)
                return Vec3(values[0], values[1], values[2])
                
            else:
                # Default: return text
                return self.input.text()
                
        except Exception as e:
            print(f"Error getting value: {e}")
            return None

    def _get_float_input_values(self):
        return [
            float(text) if text else 0.0
            for text in (input_field.text() for input_field in self.inputs)
        ]

    def _get_flagged_vector_value(
        self,
        type_kind,
        vec3_type,
        int3_type,
        uint3_type,
    ):
        converter = float if self.var_type == type_kind.Single else int
        default = 0.0 if converter is float else 0
        values = [
            converter(text) if text else default
            for text in (input_field.text() for input_field in self.inputs)
        ]
        vector_factory = {
            type_kind.Single: vec3_type,
            type_kind.Int32: int3_type,
            type_kind.Uint32: uint3_type,
        }.get(self.var_type)
        return vector_factory(*values) if vector_factory else values
