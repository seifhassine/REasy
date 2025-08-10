"""Dialog for editing UVAR variable values with type-specific input fields"""

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, 
    QPushButton, QCheckBox, QSpinBox, QDoubleSpinBox,
    QDialogButtonBox, QWidget
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QDoubleValidator, QIntValidator

import uuid


class UvarValueEditDialog(QDialog):
    """Dialog for editing UVAR variable values with type-specific input"""
    
    def __init__(self, var_name, var_type, current_value, var_flags=0, parent=None):
        super().__init__(parent)
        self.var_type = var_type
        self.current_value = current_value
        self.var_flags = var_flags
        self.new_value = None
        
        self.setWindowTitle(f"Edit Value: {var_name}")
        self.setModal(True)
        self.setMinimumWidth(400)
        
        # Main layout
        layout = QVBoxLayout(self)
        
        # Type label
        type_label = QLabel(f"Type: {var_type.name}")
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
            # This is actually a Vec3/Int3/Uint3 stored with special flags
            self.inputs = []
            components = ['X', 'Y', 'Z']
            
            # Parse current value
            if hasattr(current_value, 'x'):
                values = [current_value.x, current_value.y, current_value.z]
            elif isinstance(current_value, (list, tuple)) and len(current_value) >= 3:
                values = list(current_value[:3])
            else:
                values = [0, 0, 0]
            
            for i in range(3):
                label = QLabel(f"{components[i]}:")
                layout.addWidget(label)
                
                input_field = QLineEdit()
                if var_type == TypeKind.Single:
                    input_field.setValidator(QDoubleValidator())
                    input_field.setText(f"{float(values[i]):.8g}")
                else:
                    # Integer types
                    input_field.setValidator(QIntValidator())
                    input_field.setText(str(int(values[i])))
                input_field.setMaximumWidth(100)
                layout.addWidget(input_field)
                self.inputs.append(input_field)
                
            # Store reference for multi-input
            self.input = self.inputs
            
        elif var_type == TypeKind.Boolean:
            # Checkbox for boolean
            self.input = QCheckBox("Value")
            if current_value:
                self.input.setChecked(bool(current_value))
            layout.addWidget(self.input)
            
        elif var_type in (TypeKind.Int8, TypeKind.Int16, TypeKind.Int32, TypeKind.Int64, TypeKind.Enum):
            # Integer input with appropriate range
            label = QLabel("Value:")
            layout.addWidget(label)
            
            self.input = QLineEdit()
            self.input.setText(str(current_value) if current_value is not None else "0")
            
            # Set validator based on type
            if var_type == TypeKind.Int8:
                validator = QIntValidator(-128, 127)
            elif var_type == TypeKind.Int16:
                validator = QIntValidator(-32768, 32767)
            elif var_type == TypeKind.Int32:
                validator = QIntValidator(-2147483648, 2147483647)
            else:  # Int64 or Enum
                validator = QIntValidator()
            
            self.input.setValidator(validator)
            layout.addWidget(self.input)
            
        elif var_type in (TypeKind.Uint8, TypeKind.Uint16, TypeKind.Uint32, TypeKind.Uint64):
            # Unsigned integer input
            label = QLabel("Value:")
            layout.addWidget(label)
            
            self.input = QLineEdit()
            self.input.setText(str(current_value) if current_value is not None else "0")
            
            # Set validator based on type
            if var_type == TypeKind.Uint8:
                validator = QIntValidator(0, 255)
            elif var_type == TypeKind.Uint16:
                validator = QIntValidator(0, 65535)
            elif var_type == TypeKind.Uint32:
                validator = QIntValidator(0, 4294967295)
            else:  # Uint64
                validator = QIntValidator(0, 9223372036854775807)  # Max for QIntValidator
            
            self.input.setValidator(validator)
            layout.addWidget(self.input)
            
        elif var_type in (TypeKind.Single, TypeKind.Double):
            # Float/Double input
            label = QLabel("Value:")
            layout.addWidget(label)
            
            self.input = QLineEdit()
            self.input.setText(current_str)
            self.input.setValidator(QDoubleValidator())
            layout.addWidget(self.input)
            
        elif var_type in (TypeKind.C8, TypeKind.C16, TypeKind.String):
            # String input - no validation needed
            label = QLabel("Value:")
            layout.addWidget(label)
            
            self.input = QLineEdit()
            self.input.setText(current_str)
            layout.addWidget(self.input)
            self.input.setMinimumWidth(250)
            
        elif var_type == TypeKind.GUID:
            # GUID input with format validation
            label = QLabel("GUID:")
            layout.addWidget(label)
            
            self.input = QLineEdit()
            self.input.setText(str(current_value) if current_value else "00000000-0000-0000-0000-000000000000")
            self.input.setPlaceholderText("xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx")
            layout.addWidget(self.input)
            self.input.setMinimumWidth(280)
            
        elif var_type in (TypeKind.Vec2, TypeKind.Vec3, TypeKind.Vec4):
            # Vector inputs
            self.inputs = []
            components = ['X', 'Y', 'Z', 'W']
            
            # Determine number of components
            if var_type == TypeKind.Vec2:
                num_components = 2
            elif var_type == TypeKind.Vec3:
                num_components = 3
            else:  # Vec4
                num_components = 4
            
            # Parse current value
            if hasattr(current_value, 'x'):
                values = [current_value.x, current_value.y]
                if num_components >= 3:
                    values.append(current_value.z)
                if num_components >= 4:
                    values.append(current_value.w)
            elif isinstance(current_value, (list, tuple)) and len(current_value) >= num_components:
                values = list(current_value[:num_components])
            else:
                values = [0.0] * num_components
            
            for i in range(num_components):
                label = QLabel(f"{components[i]}:")
                layout.addWidget(label)
                
                input_field = QLineEdit()
                input_field.setValidator(QDoubleValidator())
                input_field.setText(f"{values[i]:.8g}" if i < len(values) else "0.0")
                input_field.setMaximumWidth(100)
                layout.addWidget(input_field)
                self.inputs.append(input_field)
                
            # Store reference for multi-input
            self.input = self.inputs
            
        else:
            # Default text input for unknown types
            label = QLabel("Value:")
            layout.addWidget(label)
            
            self.input = QLineEdit()
            self.input.setText(current_str)
            layout.addWidget(self.input)
            
        layout.addStretch()
        return container
        
    def _format_current_value(self, value):
        """Format current value for display"""
        if value is None:
            return ""
        elif isinstance(value, bool):
            return "True" if value else "False"
        elif isinstance(value, float):
            return f"{value:.8g}"
        elif isinstance(value, str):
            return value.strip('\x00')  # Remove null terminators
        elif hasattr(value, 'x') and hasattr(value, 'y'):
            # Vector types
            if hasattr(value, 'w'):
                return f"{value.x:.8g}, {value.y:.8g}, {value.z:.8g}, {value.w:.8g}"
            elif hasattr(value, 'z'):
                return f"{value.x:.8g}, {value.y:.8g}, {value.z:.8g}"
            else:
                return f"{value.x:.8g}, {value.y:.8g}"
        elif isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
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
                values = []
                for inp in self.inputs:
                    text = inp.text()
                    if self.var_type == TypeKind.Single:
                        values.append(float(text) if text else 0.0)
                    else:
                        values.append(int(text) if text else 0)
                
                # Return appropriate vector type
                if self.var_type == TypeKind.Single:
                    return Vec3(values[0], values[1], values[2])
                elif self.var_type == TypeKind.Int32:
                    return Int3(values[0], values[1], values[2])
                elif self.var_type == TypeKind.Uint32:
                    return Uint3(values[0], values[1], values[2])
                else:
                    # For smaller int types, return as list
                    return values
            if self.var_type == TypeKind.Boolean:
                return self.input.isChecked()
                
            elif self.var_type in (TypeKind.Int8, TypeKind.Int16, TypeKind.Int32, TypeKind.Int64, TypeKind.Enum):
                text = self.input.text()
                return int(text) if text else 0
                
            elif self.var_type in (TypeKind.Uint8, TypeKind.Uint16, TypeKind.Uint32, TypeKind.Uint64):
                text = self.input.text()
                value = int(text) if text else 0
                # Apply unsigned mask
                if self.var_type == TypeKind.Uint8:
                    return value & 0xFF
                elif self.var_type == TypeKind.Uint16:
                    return value & 0xFFFF
                elif self.var_type == TypeKind.Uint32:
                    return value & 0xFFFFFFFF
                else:
                    return value & 0xFFFFFFFFFFFFFFFF
                    
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
                    
            elif self.var_type == TypeKind.Vec2:
                values = []
                for inp in self.inputs:
                    text = inp.text()
                    values.append(float(text) if text else 0.0)
                return tuple(values)
                
            elif self.var_type == TypeKind.Vec3:
                values = []
                for inp in self.inputs:
                    text = inp.text()
                    values.append(float(text) if text else 0.0)
                return Vec3(values[0], values[1], values[2])
                
            elif self.var_type == TypeKind.Vec4:
                values = []
                for inp in self.inputs:
                    text = inp.text()
                    values.append(float(text) if text else 0.0)
                return tuple(values)
                
            else:
                # Default: return text
                return self.input.text()
                
        except Exception as e:
            print(f"Error getting value: {e}")
            return None