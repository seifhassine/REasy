from PySide6.QtWidgets import (QWidget, QHBoxLayout, QLineEdit, 
                              QDoubleSpinBox, QCheckBox, QGridLayout, QLabel)
from PySide6.QtCore import Signal, Qt

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
        
        self.spinboxes = []
        for i, coord in enumerate(['x', 'y', 'z']):
            spin = QDoubleSpinBox()
            spin.setRange(-999999, 999999)
            spin.setDecimals(6)
            spin.setFixedWidth(100)
            spin.setProperty("coord", coord)
            spin.setAlignment(Qt.AlignLeft)  # Add left alignment
            self.layout.addWidget(spin)
            self.spinboxes.append(spin)
            
        # Add stretch at the end to push widgets left
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for spin in self.spinboxes:
            spin.valueChanged.connect(self._on_value_changed)

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y, self._data.z]
        for spin, val in zip(self.spinboxes, values):
            spin.setValue(val)

    def setValues(self, values):
        self.x.setValue(values[0])
        self.y.setValue(values[1])
        self.z.setValue(values[2])
        
    def getValues(self):
        return (self.x.value(), self.y.value(), self.z.value())
        
    def _on_value_changed(self):
        if not self._data:
            return
            
        old_values = (self._data.x, self._data.y, self._data.z)
        new_values = tuple(spin.value() for spin in self.spinboxes)
        
        # Update data
        self._data.x = new_values[0]
        self._data.y = new_values[1]
        self._data.z = new_values[2]
        
        # Only emit if values actually changed
        if old_values != new_values:
            self.valueChanged.emit(new_values)
            self.mark_modified()

class Vec4Input(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.spinboxes = []
        for i, coord in enumerate(['x', 'y', 'z', 'w']):
            spin = QDoubleSpinBox()
            spin.setRange(-999999, 999999)
            spin.setDecimals(6)
            spin.setFixedWidth(100)
            spin.setProperty("coord", coord)
            spin.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(spin)
            self.spinboxes.append(spin)
            
        # Add stretch at the end to push widgets left
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for spin in self.spinboxes:
            spin.valueChanged.connect(self._on_value_changed)

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y, self._data.z, self._data.w]
        for spin, val in zip(self.spinboxes, values):
            spin.setValue(val)

    def setValues(self, values):
        for spin, val in zip(self.spinboxes, values):
            spin.setValue(val)
            
    def getValues(self):
        return tuple(spin.value() for spin in self.spinboxes)
        
    def _on_value_changed(self):
        if not self._data:
            return
        
        old_values = (self._data.x, self._data.y, self._data.z, self._data.w)
        new_values = tuple(spin.value() for spin in self.spinboxes)
        
        # Update data
        self._data.x = new_values[0]
        self._data.y = new_values[1]
        self._data.z = new_values[2]
        self._data.w = new_values[3]
        
        # Only emit if values actually changed
        if old_values != new_values:
            self.valueChanged.emit(new_values)
            self.mark_modified()

class GuidInput(BaseValueWidget):
    valueChanged = Signal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setMinimumWidth(250) 
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.line_edit.textChanged.connect(self._on_value_changed)

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
                if old_value != value:  # Only emit if value actually changed
                    self.valueChanged.emit(value)
                    self.mark_modified()
                self.line_edit.setStyleSheet("")
        except ValueError:
            self.line_edit.setStyleSheet("border: 1px solid red;")

class F32Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("Float32")
        
    def validate_and_convert(self, text):
        value = float(text)
        if abs(value) > 3.402823e38:
            raise ValueError("Out of F32 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

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
            
        self.spinboxes = []
        for row in range(5):
            row_spins = []
            for col in range(4):
                spin = QDoubleSpinBox()
                spin.setRange(-999999, 999999)
                spin.setDecimals(6)
                spin.setFixedWidth(100)
                spin.setAlignment(Qt.AlignLeft)
                grid.addWidget(spin, row, col + 1)  # +1 because column 0 has labels
                row_spins.append(spin)
            self.spinboxes.append(row_spins)
            
        self.layout.addStretch()
        
        if data:
            self.set_data(data)
            
        for row in self.spinboxes:
            for spin in row:
                spin.valueChanged.connect(self._on_value_changed)

    def update_display(self):
        if not self._data or not hasattr(self._data, 'values'):
            return
            
        values = self._data.values
        if len(values) != 20:
            return
            
        for row in range(5):
            for col in range(4):
                idx = row * 4 + col
                self.spinboxes[row][col].setValue(values[idx])

    def setValues(self, values):
        for spin, val in zip(self.spins, values):
            spin.setValue(val)
            
    def getValues(self):
        return [spin.value() for spin in self.spins]
        
    def _on_value_changed(self):
        if not self._data:
            return
        values = []
        for row in self.spinboxes:
            values.extend([spin.value() for spin in row])
        old_values = self._data.values
        self._data.values = values
        if old_values != values:
            self.valueChanged.emit(values)
            self.mark_modified()

class HexBytesInput(BaseValueWidget):
    valueChanged = Signal(bytes)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.hex_edit = QLineEdit()
        self.hex_edit.setAlignment(Qt.AlignLeft)
        # Only allow hex digits and spaces
        self.hex_edit.setInputMask("HH "*32)  # Support up to 32 bytes
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
        display_text = f"{self._data.value} (Index: {self._data.index})"
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
