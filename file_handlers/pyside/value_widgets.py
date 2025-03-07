from PySide6.QtWidgets import (QColorDialog, QWidget, QHBoxLayout, QLineEdit, 
                              QGridLayout, QLabel, QComboBox, QPushButton, QCheckBox)
from PySide6.QtCore import Signal, Qt
from PySide6.QtGui import QDoubleValidator, QIntValidator, QColor
import uuid

class BaseValueWidget(QWidget):
    modified_changed = Signal(bool)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(0,0,0,0)
        self.layout.setSpacing(2)
        self.layout.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setMinimumHeight(24)
        self._data = None
        self._modified = False

    def mark_modified(self):
        """Mark data as modified and notify"""
        if not self._modified:
            self._modified = True
            self.modified_changed.emit(True)
        
    def setup_ui(self):
        pass
        
    def setValues(self, values):
        pass
        
    def getValues(self):
        pass

    def get_data(self):
        return self._data

    def set_data(self, data):
        #print(f"DEBUG: BaseValueWidget.set_data called with data: {data}, type: {type(data)}, dir: {dir(data)}")
        self._data = data
        self.update_display()

    def update_display(self):
        pass

class Vec3Input(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, coord in enumerate(['x', 'y', 'z']):
            line_edit = QLineEdit()
            line_edit.setValidator(QDoubleValidator())
            line_edit.setFixedWidth(100)
            line_edit.setProperty("coord", coord)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)
            
        # Add stretch at the end to push widgets left
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed) 

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y, self._data.z]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(f"{val:.8g}") 

    def setValues(self, values):
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(val))
        
    def getValues(self):
        return tuple(float(input_field.text() or "0") for input_field in self.inputs)
        
    def _on_value_changed(self):
        if not self._data:
            return
        
        try:
            new_values = []
            for input_field in self.inputs:
                text = input_field.text()
                if not text or text == '-': 
                    new_values.append(0.0)
                else:
                    new_values.append(float(text))
            
            new_values = tuple(new_values)
            old_values = (self._data.x, self._data.y, self._data.z)
            
            self._data.x = new_values[0]
            self._data.y = new_values[1]
            self._data.z = new_values[2]
            
            self.valueChanged.emit(new_values)
            self.mark_modified()
        except ValueError:
            pass  # Ignore invalid input during typing

class Vec4Input(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, coord in enumerate(['x', 'y', 'z', 'w']):
            line_edit = QLineEdit()
            line_edit.setValidator(QDoubleValidator())
            line_edit.setFixedWidth(100)
            line_edit.setProperty("coord", coord)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)
            
        # a stretch at the end to push widgets left
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed) 

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y, self._data.z, self._data.w]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(f"{val:.8g}")

    def setValues(self, values):
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(val))
            
    def getValues(self):
        return tuple(float(input_field.text() or "0") for input_field in self.inputs)
        
    def _on_value_changed(self):
        if not self._data:
            return
        
        try:
            new_values = []
            for input_field in self.inputs:
                text = input_field.text()
                if not text or text == '-':
                    new_values.append(0.0)
                else:
                    new_values.append(float(text))
            
            new_values = tuple(new_values)
            old_values = (self._data.x, self._data.y, self._data.z, self._data.w)
            
            self._data.x = new_values[0]
            self._data.y = new_values[1]
            self._data.z = new_values[2]
            self._data.w = new_values[3]
            
            self.valueChanged.emit(new_values)
            self.mark_modified()
        except ValueError:
            pass 

class GuidInput(BaseValueWidget):
    valueChanged = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setFixedWidth(230) 
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.line_edit.textChanged.connect(self._on_value_changed)

        self.gen_button = QPushButton("Generate")
        self.gen_button.setToolTip("Generate a random GUID")
        self.gen_button.setMaximumWidth(70)
        self.layout.addWidget(self.gen_button)
        self.gen_button.clicked.connect(self._generate_random_guid)

    def update_display(self):
        if not self._data:
            return
        self.line_edit.blockSignals(True)

        # Handle all GUID types
        if hasattr(self._data, 'guid_str'):
            self.line_edit.setText(self._data.guid_str)
        elif hasattr(self._data, 'guid'):  # For GameObjectRef array elements
            self.line_edit.setText(str(self._data.guid))
        elif hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))
        else:
            self.line_edit.setText(str(self._data))
        self.line_edit.blockSignals(False)

    def _on_value_changed(self, text):
        if not self._data:
            return
        # Handle all GUID types
        if hasattr(self._data, 'guid_str'):
            self._data.guid_str = text
        elif hasattr(self._data, 'guid'):  # For GameObjectRef array elements
            self._data.guid = text
        elif hasattr(self._data, 'value'):
            self._data.value = text
        self.valueChanged.emit(text)
        self.mark_modified()

    def _generate_random_guid(self):
        """Generate a random GUID and set it as the current value"""
        random_guid = str(uuid.uuid4())
        self.line_edit.setText(random_guid)
        self.valueChanged.emit(random_guid)

class NumberInput(BaseValueWidget):
    """Base class for numeric inputs"""
    valueChanged = Signal(float)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setFixedWidth(100)
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.line_edit.textChanged.connect(self._on_text_changed)

    def validate_and_convert(self, text):
        """Override in child classes to provide type-specific validation"""
        raise NotImplementedError()

    def _on_text_changed(self, text):
        if not self._data or not text:
            return
            
        try:
            value = self.validate_and_convert(text)
            if value is not None:
                old_value = self._data.value
                self._data.value = value
                if old_value != value:
                    self.valueChanged.emit(value)
                    self.mark_modified()
                self.line_edit.setStyleSheet("")
        except ValueError:
            self.line_edit.setStyleSheet("border: 1px solid red;")

class F32Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("Float32")
        self.line_edit.setFixedWidth(120)
        
    def validate_and_convert(self, text):
        value = float(text)
        if abs(value) > 3.402823e38:
            raise ValueError("Out of F32 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(f"{self._data.value:.8g}") 

class S32Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("Int32")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < -2147483648 or value > 2147483647:
            raise ValueError("Out of S32 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

class U32Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("UInt32")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < 0 or value > 4294967295:
            raise ValueError("Out of U32 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

class S8Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("Int8")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < -128 or value > 127:
            raise ValueError("Out of S8 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

class U8Input(NumberInput):
    valueChanged = Signal(int)  
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("UInt8")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < 0 or value > 255:
            raise ValueError("Out of U8 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

    def _on_text_changed(self, text):
        if not self._data or not text:
            return
            
        try:
            value = self.validate_and_convert(text)
            if value is not None:
                old_value = self._data.value
                self._data.value = value
                if old_value != value:
                    self.valueChanged.emit(value)
                    self.mark_modified()
                self.line_edit.setStyleSheet("")
        except ValueError:
            self.line_edit.setStyleSheet("border: 1px solid red;")

class OBBInput(BaseValueWidget):
    valueChanged = Signal(list)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        grid = QGridLayout()
        grid.setSpacing(2)
        grid.setAlignment(Qt.AlignLeft)
        self.layout.addLayout(grid)
        
        labels = ['AxisX', 'AxisY', 'AxisZ', 'Center', 'Extent']
        for i, label in enumerate(labels):
            grid.addWidget(QLabel(label), i, 0, alignment=Qt.AlignRight)
            
        self.inputs = []
        for row in range(5):
            row_inputs = []
            for col in range(4):
                line_edit = QLineEdit()
                line_edit.setValidator(QDoubleValidator())
                line_edit.setFixedWidth(100)
                line_edit.setAlignment(Qt.AlignLeft)
                grid.addWidget(line_edit, row, col + 1)  # +1 because column 0 has labels
                row_inputs.append(line_edit)
            self.inputs.append(row_inputs)
            
        self.layout.addStretch()
        
        if data:
            self.set_data(data)
            
        for row in self.inputs:
            for input_field in row:
                input_field.textEdited.connect(self._on_value_changed) 

    def update_display(self):
        if not self._data or not hasattr(self._data, 'values'):
            return
            
        values = self._data.values
        if len(values) != 20:
            return
            
        for row in range(5):
            for col in range(4):
                idx = row * 4 + col
                self.inputs[row][col].setText(f"{values[idx]:.8g}") 

    def setValues(self, values):
        flat_inputs = [input_field for row in self.inputs for input_field in row]
        for input_field, val in zip(flat_inputs, values):
            input_field.setText(str(val))
            
    def getValues(self):
        try:
            return [float(input_field.text() or "0") 
                   for row in self.inputs 
                   for input_field in row]
        except ValueError:
            return [0.0] * 20
        
    def _on_value_changed(self):
        if not self._data:
            return
        
        try:
            values = []
            for row in self.inputs:
                for input_field in row:
                    text = input_field.text()
                    if not text or text == '-':
                        values.append(0.0)
                    else:
                        values.append(float(text))
            
            self._data.values = values
            self.valueChanged.emit(values)
            self.mark_modified()
        except ValueError:
            pass 

class HexBytesInput(BaseValueWidget):
    valueChanged = Signal(bytes)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.hex_edit = QLineEdit()
        self.hex_edit.setAlignment(Qt.AlignLeft)
        self.hex_edit.setInputMask("HH "*32)
        self.layout.addWidget(self.hex_edit)
        
        if data:
            self.set_data(data)
            
        self.hex_edit.textEdited.connect(self._on_text_changed) 

    def update_display(self):
        if not self._data:
            return
        
        if hasattr(self._data, "raw_bytes"): 
            bytes_data = self._data.raw_bytes
        elif hasattr(self._data, 'bytes_raw'):
            bytes_data = self._data.bytes_raw
        elif hasattr(self._data, 'bytes_array'):
            bytes_data = self._data.bytes_array
        elif hasattr(self._data, 'value'):
            bytes_data = self._data.value
        else:
            bytes_data = self._data
        
        if isinstance(bytes_data, (bytes, bytearray)):
            hex_str = ' '.join([f'{b:02X}' for b in bytes_data])
            self.hex_edit.setText(hex_str)
        else:
            print(f"Warning: Unexpected data format in HexBytesInput: {type(bytes_data)}")

    def _on_text_changed(self):
        hex_str = self.hex_edit.text().strip()
        if not hex_str:
            return
        try:
            bytes_data = bytes([int(x, 16) for x in hex_str.split()])
            old_value = None
            
            if hasattr(self._data, 'raw_bytes'):
                old_value = self._data.raw_bytes
                self._data.raw_bytes = bytes_data
            elif hasattr(self._data, 'bytes_array'):
                old_value = self._data.bytes_array
                self._data.bytes_array = bytes_data
            elif hasattr(self._data, 'value'):
                old_value = self._data.value
                self._data.value = bytes_data
                
            if old_value != bytes_data:
                self.valueChanged.emit(bytes_data)
                self.mark_modified()
                
        except ValueError:
            pass

class StringInput(BaseValueWidget):
    """Widget for editing string values"""
    valueChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.line_edit.textChanged.connect(self._on_text_changed)

    def update_display(self):
        if self._data:
            self.line_edit.blockSignals(True)
            self.line_edit.setText(self._data.value.rstrip('\x00'))
            self.line_edit.blockSignals(False)

    def _on_text_changed(self, text):
        """Update data when text changes"""
        if self._data:
            self._data.value = text
            self.valueChanged.emit(text)
            self.mark_modified()

class UserDataInput(BaseValueWidget):
    valueChanged = Signal(str) 
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setReadOnly(True) 
        self.line_edit.setMinimumWidth(200) 
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.layout.addStretch()
        self.line_edit.textChanged.connect(self._on_value_changed)

    def update_display(self):
        if not self._data:
            return
        display_text = f"{self._data.value}"
        self.line_edit.setText(display_text)

    def _on_value_changed(self, text):
        if self._data:
            self._data.value = text
            self.valueChanged.emit(text)
            self.mark_modified()

class BoolInput(BaseValueWidget):
    valueChanged = Signal(bool)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        self.checkbox = QCheckBox()
        self.checkbox.setText("")
        self.checkbox.setFixedWidth(20)
        self.checkbox.setStyleSheet("""
            QCheckBox {
                padding: 2px;
            }
            QCheckBox::indicator {
                width: 16px;
                height: 16px;
            }
        """)
        self.layout.addWidget(self.checkbox)
        self.layout.addStretch()
        self.checkbox.stateChanged.connect(self._on_state_changed)

    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.checkbox.setChecked(bool(self._data.value))

    def _on_state_changed(self, state):
        if self._data:
            self._data.value = bool(state)
            self.valueChanged.emit(bool(state))
            self.mark_modified()

class RangeInput(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.layout.setSpacing(4)
        
        self.inputs = []
        for i, name in enumerate(['Min', 'Max']):
            container = QWidget()
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(2)
            
            label = QLabel(name)
            label.setFixedWidth(33)
            label.setStyleSheet("padding-right: 2px;")
            container_layout.addWidget(label)
            
            line_edit = QLineEdit()
            line_edit.setValidator(QDoubleValidator())
            line_edit.setFixedWidth(80)
            line_edit.setProperty("name", name.lower())
            line_edit.setAlignment(Qt.AlignLeft)
            line_edit.setStyleSheet("margin-left: 2px;")
            container_layout.addWidget(line_edit)
            
            if i == 0:
                container.setFixedWidth(120)
                container.setStyleSheet("margin-right: 6px;")
            
            self.layout.addWidget(container)
            self.inputs.append(line_edit)
        
        self.layout.addStretch(1)
        
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed)

    def update_display(self):
        if not self._data:
            return
        values = [self._data.min, self._data.max]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(f"{val:.8g}") 

    def setValues(self, values):
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(val))
            
    def getValues(self):
        try:
            return tuple(float(input_field.text() or "0") for input_field in self.inputs)
        except ValueError:
            return (0.0, 0.0)
        
    def _on_value_changed(self):
        if not self._data:
            return
            
        try:
            new_values = []
            for input_field in self.inputs:
                text = input_field.text()
                if not text or text == '-':
                    new_values.append(0.0)
                else:
                    new_values.append(float(text))
                
            new_values = tuple(new_values)
            self._data.min = new_values[0]
            self._data.max = new_values[1]
            
            self.valueChanged.emit(new_values)
            self.mark_modified()
        except ValueError:
            pass 

class RangeIInput(BaseValueWidget):
    """Widget for editing integer range values"""
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.layout.setSpacing(4)
        
        self.inputs = []
        for i, name in enumerate(['Min', 'Max']):
            container = QWidget()
            container_layout = QHBoxLayout(container)
            container_layout.setContentsMargins(0, 0, 0, 0)
            container_layout.setSpacing(2)
            
            label = QLabel(name)
            label.setFixedWidth(33)
            label.setStyleSheet("padding-right: 2px;")
            container_layout.addWidget(label)
            
            line_edit = QLineEdit()
            line_edit.setValidator(QIntValidator())
            line_edit.setFixedWidth(80)
            line_edit.setProperty("name", name.lower())
            line_edit.setAlignment(Qt.AlignLeft)
            line_edit.setStyleSheet("margin-left: 2px;")
            container_layout.addWidget(line_edit)
            
            if i == 0:
                container.setFixedWidth(120)
                container.setStyleSheet("margin-right: 6px;")
            
            self.layout.addWidget(container)
            self.inputs.append(line_edit)
        
        self.layout.addStretch(1)
        
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed)

    def update_display(self):
        if not self._data:
            return
        values = [self._data.min, self._data.max]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(val))

    def setValues(self, values):
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(val))
            
    def getValues(self):
        try:
            return tuple(int(input_field.text() or "0") for input_field in self.inputs)
        except ValueError:
            return (0, 0)
        
    def _on_value_changed(self):
        if not self._data:
            return
            
        try:
            new_values = []
            for input_field in self.inputs:
                text = input_field.text()
                if not text or text == '-':
                    new_values.append(0)
                else:
                    new_values.append(int(text))
            
            new_values = tuple(new_values)
            self._data.min = new_values[0]
            self._data.max = new_values[1]
            
            self.valueChanged.emit(new_values)
            self.mark_modified()
        except ValueError:
            pass 

class EnumInput(BaseValueWidget):
    """Widget for editing enum values with dropdown selection"""
    valueChanged = Signal(int)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.enum_values = []  
        
        self.line_edit = QLineEdit()
        self.line_edit.setFixedWidth(100)
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        
        self.combo_box = QComboBox()
        self.combo_box.setFixedWidth(150)
        self.combo_box.setMaxVisibleItems(15) 
        self.combo_box.view().setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded) 
        self.layout.addWidget(self.combo_box)
        
        # Connect signals
        self.line_edit.textChanged.connect(self._on_text_changed)
        self.combo_box.currentIndexChanged.connect(self._on_combo_changed)
        
    def set_enum_values(self, enum_values):
        """Set available enum values and populate dropdown"""
        self.enum_values = enum_values
        
        # Block signals during population to prevent auto-selection
        self.combo_box.blockSignals(True)
        self.combo_box.clear()
        
        # Populate dropdown with enum values
        for enum_value in enum_values:
            self.combo_box.addItem(f"{enum_value['name']} ({enum_value['value']})")
        
        # Restore signals
        self.combo_box.blockSignals(False)
        
        # Set combo selection to match current value if it exists
        if self._data and hasattr(self._data, 'value'):
            self.update_combo_selection()
            
    def update_combo_selection(self):
        """Set the combo box selection to match current value"""
        if not self._data or not self.enum_values:
            return
        
        current_value = self._data.value
        found_match = False
        
        # Block signals to prevent triggering _on_combo_changed
        self.combo_box.blockSignals(True)
        
        # Try to find matching enum value
        for i, enum_value in enumerate(self.enum_values):
            if enum_value['value'] == current_value:
                self.combo_box.setCurrentIndex(i)
                found_match = True
                break
                
        # If no match found, set combo to -1 (no selection)
        if not found_match:
            self.combo_box.setCurrentIndex(-1)
            
        self.combo_box.blockSignals(False)
                
    def update_display(self):
        """Update both the text input and dropdown to match data"""
        if not self._data:
            return
        
        # First update text field with actual value
        self.line_edit.blockSignals(True)
        self.line_edit.setText(str(self._data.value))
        self.line_edit.blockSignals(False)
        
        # Then update combo box selection to match value, if possible
        self.update_combo_selection()
        
    def _on_text_changed(self, text):
        """Update value when text is changed manually"""
        if not self._data or not text:
            return
            
        try:
            value = int(text)
            old_value = self._data.value
            
            if old_value != value:
                self._data.value = value
                self.valueChanged.emit(value)
                self._modified = True 
                self.modified_changed.emit(True)
                
                # Update dropdown to match new value, but don't trigger another change
                self.update_combo_selection()
                
            self.line_edit.setStyleSheet("")
        except ValueError:
            self.line_edit.setStyleSheet("border: 1px solid red;")
            
    def _on_combo_changed(self, index):
        """Update value when an enum option is selected"""
        if not self._data or index < 0 or index >= len(self.enum_values):
            return
            
        value = self.enum_values[index]['value']
        old_value = self._data.value
        
        if old_value != value:
            self._data.value = value
            
            self.line_edit.blockSignals(True)
            self.line_edit.setText(str(value))
            self.line_edit.blockSignals(False)
            
            self.valueChanged.emit(value)
            self._modified = True 
            self.modified_changed.emit(True)

class Mat4Input(BaseValueWidget):
    """Widget for editing 4x4 matrix values"""
    valueChanged = Signal(list)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        grid = QGridLayout()
        grid.setSpacing(2)
        grid.setAlignment(Qt.AlignLeft)
        self.layout.addLayout(grid)
        
        self.inputs = []
        for row in range(4):
            row_inputs = []
            for col in range(4):
                line_edit = QLineEdit()
                line_edit.setValidator(QDoubleValidator())
                line_edit.setFixedWidth(75) 
                line_edit.setFixedHeight(24)
                line_edit.setAlignment(Qt.AlignLeft)
                grid.addWidget(line_edit, row, col)
                row_inputs.append(line_edit)
                line_edit.textEdited.connect(self._on_value_changed)
            self.inputs.append(row_inputs)
        
        self.layout.addStretch()
        
        if data:
            self.set_data(data)

    def update_display(self):
        """Update input fields from the data"""
        if not self._data or not hasattr(self._data, 'values'):
            return
        
        values = self._data.values
        if len(values) != 16: 
            return
            
        for row in range(4):
            for col in range(4):
                idx = row * 4 + col  
                self.inputs[row][col].setText(f"{values[idx]:.8g}")

    def _on_value_changed(self):
        """Handle input changes and update the data model"""
        if not self._data:
            return
        
        try:
            values = []
            for row in self.inputs:
                for input_field in row:
                    text = input_field.text()
                    if not text or text == '-':
                        values.append(0.0)
                    else:
                        values.append(float(text))
            
            if hasattr(self._data, 'values') and len(values) == 16:
                self._data.values = values
                self.valueChanged.emit(values)
                self.mark_modified()
        except ValueError:
            pass 

    def setValues(self, values):
        """Set all input values at once"""
        if len(values) != 16:
            return
            
        for row in range(4):
            for col in range(4):
                idx = row * 4 + col
                self.inputs[row][col].setText(str(values[idx]))
    
    def getValues(self):
        """Get all values as a flat list"""
        try:
            return [float(input_field.text() or "0") 
                   for row in self.inputs 
                   for input_field in row]
        except ValueError:
            return [0.0] * 16

class ColorInput(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.color_button = QPushButton()
        self.color_button.setFixedSize(24, 24)
        self.color_button.clicked.connect(self._show_color_dialog)
        
        grid = QGridLayout()
        grid.setSpacing(2) 
        grid.setContentsMargins(0, 0, 0, 0)
        
        grid.addWidget(self.color_button, 0, 0)
        grid.setColumnMinimumWidth(1, 6) 

        self.inputs = []
        for i, comp in enumerate(['R', 'G', 'B', 'A']):
            label = QLabel(comp)
            label.setFixedWidth(8)
            label.setAlignment(Qt.AlignCenter)
            label.setStyleSheet("""
                QLabel {
                    margin: 0;
                    padding: 0;
                    border: none;
                }
            """)
            
            line_edit = QLineEdit()
            validator = QIntValidator(0, 255)
            line_edit.setValidator(validator)
            line_edit.setFixedWidth(28) 
            line_edit.setFixedHeight(20)
            line_edit.setProperty("component", comp.lower())
            line_edit.setStyleSheet("""
                QLineEdit {
                    margin: 0;
                    padding: 1px 2px;
                    border: 1px solid #888888;
                }
                QLineEdit:focus {
                    border: 1px solid #aaaaaa;
                }
                QLineEdit[invalid="true"] {
                    border: 1px solid red;
                }
            """)
            
            col_offset = (i * 3) + 2
            grid.addWidget(label, 0, col_offset)
            grid.addWidget(line_edit, 0, col_offset + 1)
            
            if i < 3: 
                grid.setColumnMinimumWidth(col_offset + 2, 3)
                
            self.inputs.append(line_edit)
        
        self.layout.addLayout(grid)
        self.layout.addStretch()
        
        if data:
            self.set_data(data)
        
        for input_field in self.inputs:
            input_field.textEdited.connect(self._validate_and_update)

    def _validate_and_update(self):
        """Validate input and update values"""
        if not self._data:
            return
            
        try:
            values = []
            valid = True
            
            for input_field in self.inputs:
                text = input_field.text().strip()
                
                try:
                    if text == '' or text == '-':
                        value = 0
                    else:
                        value = int(text)
                        if value < 0 or value > 255:
                            valid = False
                            value = max(0, min(255, value))  # Clamp value
                except ValueError:
                    valid = False
                    value = 0
                
                values.append(value)
                
                input_field.setProperty("invalid", not valid)
                input_field.style().unpolish(input_field)
                input_field.style().polish(input_field)
                
                if not valid:
                    input_field.setText(str(value))
            
            self._data.r = values[0]
            self._data.g = values[1]
            self._data.b = values[2]
            self._data.a = values[3]
            
            self._update_color_button()
            
            if valid:
                self.valueChanged.emit(tuple(values))
                self.mark_modified()
                
        except ValueError:
            pass

    def update_display(self):
        if not self._data:
            return
            
        values = [self._data.r, self._data.g, self._data.b, self._data.a]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(val))
            
        self._update_color_button()
    
    def _update_color_button(self):
        """Update the color button appearance based on current RGBA values"""
        if not self._data:
            return
            
        r = int(self._data.r)
        g = int(self._data.g)
        b = int(self._data.b)
        a = int(self._data.a)
        
        alpha_normalized = a / 255.0
        
        self.color_button.setStyleSheet(
            f"background-color: rgba({r}, {g}, {b}, {alpha_normalized}); border: 1px solid #888888;"
        )
    
    def _show_color_dialog(self):
        """Open color picker dialog and update values if user selects a color"""
        if not self._data:
            return
            
        r = int(self._data.r)
        g = int(self._data.g)
        b = int(self._data.b)
        a = int(self._data.a)
        
        
        initial_color = QColor(r, g, b, a)
        
        color = QColorDialog.getColor(initial_color, self, "Select Color", QColorDialog.ShowAlphaChannel)
        
        if color.isValid():
            self._data.r = color.red()
            self._data.g = color.green()
            self._data.b = color.blue()
            self._data.a = color.alpha()
            
            self.update_display()
            
            self.valueChanged.emit((self._data.r, self._data.g, self._data.b, self._data.a))
            self.mark_modified()
    
    def setValues(self, values):
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(val))
        self._update_color_button()
    
    def getValues(self):
        try:
            return tuple(int(input_field.text() or "0") for input_field in self.inputs)
        except ValueError:
            return (0, 0, 0, 255) 
    
    def _on_value_changed(self):
        """Handle changes from manual input of RGBA values"""
        if not self._data:
            return
            
        try:
            values = []
            for input_field in self.inputs:
                text = input_field.text()
                if not text or text == '-':
                    values.append(0)
                else:
                    values.append(max(0, min(255, int(text))))
            
            self._data.r = values[0]
            self._data.g = values[1]
            self._data.b = values[2]
            self._data.a = values[3]
            
            self._update_color_button()
            
            self.valueChanged.emit(tuple(values))
            self.mark_modified()
        except ValueError:
            pass

