from PySide6.QtWidgets import (QColorDialog, QWidget, QHBoxLayout, QLineEdit,
                              QGridLayout, QLabel, QComboBox, QPushButton, QCheckBox, QSizePolicy,
                              QTreeView, QApplication, QSlider, QToolButton, QInputDialog, QMessageBox)
from PySide6.QtCore import Signal, Qt, QRegularExpression, QTimer
from PySide6.QtGui import (
    QDoubleValidator,
    QRegularExpressionValidator,
    QIntValidator,
    QColor,
    QPalette,
    QFontMetrics,
    QKeySequence,
)
import uuid
import re

from file_handlers.rsz.rsz_data_types import RawBytesData, ResourceData
from file_handlers.pyside.component_selector import ComponentSelectorDialog
from ui.widgets_utils import ColorPreviewButton

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

    def get_data(self):
        return self._data

    def set_data(self, data):
        self._data = data
        self.update_display()


class ClipboardContentError(ValueError):
    """Error raised when clipboard content cannot be parsed for vector inputs."""

    def __init__(self, reason, actual_count=None):
        super().__init__(reason)
        self.reason = reason
        self.actual_count = actual_count


class VectorClipboardMixin:
    """Mixin that adds copy/paste helpers for vector-style inputs."""

    clipboard_precision = 8

    clipboard_button_width = 44

    def _create_clipboard_button(self, text, tooltip, handler):
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedWidth(self.clipboard_button_width)
        button.clicked.connect(handler)
        self.layout.addWidget(button)
        return button

    def _setup_clipboard_buttons(self):
        self.copy_button = self._create_clipboard_button(
            self.tr("Copy"), self.tr("Copy values to clipboard"), self._copy_values_to_clipboard
        )
        self.paste_button = self._create_clipboard_button(
            self.tr("Paste"), self.tr("Paste values from clipboard"), self._paste_values_from_clipboard
        )

    def _format_clipboard_value(self, value):
        if isinstance(value, float):
            return f"{value:.{self.clipboard_precision}g}"
        return str(value)

    def _convert_clipboard_token(self, token):
        return float(token)

    def _copy_values_to_clipboard(self):
        values = self.getValues()
        if not values:
            return

        text = ", ".join(self._format_clipboard_value(value) for value in values)
        QApplication.clipboard().setText(text)

    def _parse_clipboard_values(self, text, expected_count):
        if not text or not text.strip():
            raise ClipboardContentError("empty")

        tokens = [t for t in re.split(r"[\s,;]+", text.strip()) if t]
        if len(tokens) != expected_count:
            raise ClipboardContentError("length", len(tokens))

        values = []
        for token in tokens:
            try:
                values.append(self._convert_clipboard_token(token))
            except ValueError:
                raise ClipboardContentError("invalid", len(tokens))
        return values

    def _show_clipboard_error(self, error, expected_count):
        if error.reason == "empty":
            message = self.tr("Clipboard is empty. Copy values before pasting.")
        elif error.reason == "length":
            message = (
                f"Incompatible clipboard data length: expected {expected_count} "
                f"values but found {error.actual_count or 0}."
            )
        else:
            message = (
                f"Incompatible clipboard data: expected {expected_count} numeric "
                f"values but found non-numeric content among {error.actual_count or 0} "
                "values."
            )

        QMessageBox.warning(self, self.tr("Paste Error"), message)

    def _paste_values_from_clipboard(self):
        expected = len(self.inputs)
        if expected == 0:
            return

        clipboard_text = QApplication.clipboard().text()
        try:
            values = self._parse_clipboard_values(clipboard_text, expected)
        except ClipboardContentError as error:
            self._show_clipboard_error(error, expected)
            return

        self.setValues(values)
        self._on_value_changed()

class SizeInput(VectorClipboardMixin, BaseValueWidget):
    valueChanged = Signal(tuple)

    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, coord in enumerate(['width', 'height']):
            line_edit = QLineEdit()
            line_edit.setValidator(QDoubleValidator())
            line_edit.setFixedWidth(100)
            line_edit.setProperty("coord", coord)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)

        self._setup_clipboard_buttons()
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed) 

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y]
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
            
            self._data.width = new_values[0]
            self._data.height = new_values[1]
            
            self.valueChanged.emit(new_values)
            self.mark_modified()
        except ValueError:
            pass

class Vec2Input(VectorClipboardMixin, BaseValueWidget):
    valueChanged = Signal(tuple)

    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, coord in enumerate(['x', 'y']):
            line_edit = QLineEdit()
            line_edit.setValidator(QDoubleValidator())
            line_edit.setFixedWidth(100)
            line_edit.setProperty("coord", coord)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)

        self._setup_clipboard_buttons()
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed) 

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y]
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
            
            self._data.x = new_values[0]
            self._data.y = new_values[1]
            
            self.valueChanged.emit(new_values)
            self.mark_modified()
        except ValueError:
            pass

class Vec3Input(VectorClipboardMixin, BaseValueWidget):
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

        # Add clipboard helpers and stretch to keep widgets left-aligned
        self._setup_clipboard_buttons()
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
            
            self._data.x = new_values[0]
            self._data.y = new_values[1]
            self._data.z = new_values[2]
            
            self.valueChanged.emit(new_values)
            self.mark_modified()
        except ValueError:
            pass  # Ignore invalid input during typing

class Vec4Input(VectorClipboardMixin, BaseValueWidget):
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
        self._setup_clipboard_buttons()
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
        self.line_edit = OverwriteGuidLineEdit(self)
        self.line_edit.setFixedWidth(238)
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.line_edit.textEdited.connect(self._on_text_edited)

        self._PENDING_ATTR = "__pending_guid_state"
        self._history_anchor = None

        self.gen_button = QToolButton()
        self.gen_button.setText(self.tr("Generate"))
        self.gen_button.setToolTip(self.tr("Generate a random GUID"))
        self.gen_button.setFixedWidth(60)
        self.layout.addWidget(self.gen_button)
        self.gen_button.clicked.connect(self._generate_guid)
        
        self.reset_button = QToolButton()
        self.reset_button.setText(self.tr("Reset"))
        self.reset_button.setToolTip(self.tr("Reset to null GUID (all zeros)"))
        self.reset_button.setFixedWidth(40)
        self.layout.addWidget(self.reset_button)
        self.reset_button.clicked.connect(self._reset_guid)

    def _generate_guid(self):
        """Generate a new random GUID"""
        if not self._data:
            return

        new_guid = str(uuid.uuid4())
        self._apply_programmatic_guid(new_guid)

    def _reset_guid(self):
        """Reset to null GUID (all zeros)"""
        if not self._data:
            return

        null_guid = "00000000-0000-0000-0000-000000000000"
        self._apply_programmatic_guid(null_guid)

    def _apply_programmatic_guid(self, text):
        if not self._data:
            return

        self.line_edit.apply_text(text)
        self._data.guid_str = text
        self.valueChanged.emit(text)
        self.mark_modified()
        self.line_edit.setStyleSheet("")
        self._clear_pending_guid()
        self._history_anchor = ("valid", text)

    def _pending_state(self):
        if not self._data:
            return None
        return getattr(self._data, self._PENDING_ATTR, None)

    def _remember_pending_guid(self, text, cursor):
        if not self._data:
            return

        if text is None:
            self._clear_pending_guid()
            return

        base_guid = getattr(self._data, "guid_str", "") or ""
        pending_state = {
            "text": text,
            "base": base_guid,
            "cursor": cursor,
        }
        setattr(self._data, self._PENDING_ATTR, pending_state)
        self._history_anchor = ("pending", base_guid, text)

    def _clear_pending_guid(self):
        if not self._data:
            return
        if hasattr(self._data, self._PENDING_ATTR):
            delattr(self._data, self._PENDING_ATTR)
        base_guid = getattr(self._data, "guid_str", "") or ""
        self._history_anchor = ("valid", base_guid)

    def _format_guid(self, text):
        """Format text as UUID, removing invalid chars and adding hyphens"""
        hex_only = ''.join(c for c in text.lower() if c in '0123456789abcdef')
        hex_only = hex_only[:32]
        
        if not hex_only:
            return ''
            
        parts = [
            hex_only[:8],
            hex_only[8:12],
            hex_only[12:16],
            hex_only[16:20],
            hex_only[20:32]
        ]
        return '-'.join(part for part in parts if part)

    def _cursor_for_hex_count(self, formatted_text, hex_count):
        if hex_count <= 0:
            return 0

        seen = 0
        for index, char in enumerate(formatted_text):
            if char == '-':
                continue
            seen += 1
            if seen >= hex_count:
                return min(index + 1, len(formatted_text))
        return len(formatted_text)

    def _on_text_edited(self, text, *, from_history=False):
        if not self._data:
            return

        cursor_pos = self.line_edit.cursorPosition()
        hex_before_cursor = sum(1 for c in text[:cursor_pos] if c in '0123456789abcdefABCDEF')

        formatted = self._format_guid(text)

        if formatted != text:
            new_pos = self._cursor_for_hex_count(formatted, hex_before_cursor)
            self.line_edit.apply_text(
                formatted,
                cursor=new_pos,
                record_history=not from_history,
            )
        elif not from_history:
            self.line_edit.finalize_user_edit()

        is_valid = False

        if len(formatted) == 36:
            try:
                uuid.UUID(formatted)
                is_valid = True
            except ValueError:
                is_valid = False

        if is_valid:
            self._data.guid_str = formatted
            self.valueChanged.emit(formatted)
            self.mark_modified()
            self.line_edit.setStyleSheet("")
            self._clear_pending_guid()
            self._history_anchor = ("valid", formatted)
        else:
            cursor_snapshot = self.line_edit.cursorPosition()
            self._remember_pending_guid(formatted, cursor_snapshot)
            self.line_edit.setStyleSheet("border: 1px solid red;")

    def update_display(self):
        if not self._data:
            return
        pending_state = self._pending_state()
        base_guid = getattr(self._data, "guid_str", "") or ""

        if pending_state and pending_state.get("base") == base_guid:
            display_text = pending_state.get("text", "")
            cursor_pos = pending_state.get("cursor", len(display_text))
            is_valid = False
            anchor = ("pending", base_guid, display_text)
        else:
            if pending_state and pending_state.get("base") != base_guid:
                self._clear_pending_guid()
            display_text = base_guid
            cursor_pos = len(display_text)
            is_valid = True
            anchor = ("valid", base_guid)

        if self._history_anchor != anchor:
            self.line_edit.reset_history(display_text, cursor_pos)
            self._history_anchor = anchor
        else:
            self.line_edit.apply_text(display_text, cursor=cursor_pos, record_history=False)

        if is_valid:
            self.line_edit.setStyleSheet("")
        else:
            self.line_edit.setStyleSheet("border: 1px solid red;")

class OverwriteGuidLineEdit(QLineEdit):
    def __init__(self, guid_widget, parent=None):
        super().__init__(parent)
        self.guid_widget = guid_widget
        self._history = []
        self._history_index = -1
        self._suspend_history = False
        self._restoring_history = False

    def reset_history(self, text, cursor=None):
        if cursor is None:
            cursor = len(text)

        self._history[:] = [(text, cursor)]
        self._history_index = 0
        self.apply_text(text, cursor=cursor, record_history=False)

    def apply_text(self, text, cursor=None, record_history=True):
        if cursor is None:
            cursor = len(text)

        cursor = max(0, min(cursor, len(text)))

        self._suspend_history = True
        try:
            super().setText(text)
            self.setCursorPosition(cursor)
        finally:
            self._suspend_history = False

        if record_history:
            self._record_history_entry(text, cursor)

    def finalize_user_edit(self):
        if not (self._suspend_history or self._restoring_history):
            self._record_history_entry()

    def _record_history_entry(self, text=None, cursor=None):
        if text is None:
            text = self.text()
        if cursor is None:
            cursor = self.cursorPosition()

        state = (text, cursor)
        if self._history_index >= 0 and self._history[self._history_index] == state:
            return

        del self._history[self._history_index + 1 :]
        self._history.append(state)
        self._history_index = len(self._history) - 1

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Cut):
            self.copy()
            return

        if event.key() in (Qt.Key_Backspace, Qt.Key_Delete):
            return

        if event.modifiers() & Qt.ControlModifier:
            if event.key() == Qt.Key_Z:
                self._step_history(-1)
                return
            if event.key() == Qt.Key_Y:
                self._step_history(1)
                return

        super().keyPressEvent(event)

    def cut(self):
        self.copy()

    def _step_history(self, offset):
        new_index = self._history_index + offset
        if not (0 <= new_index < len(self._history)):
            return

        self._history_index = new_index
        text, cursor = self._history[self._history_index]
        self._restoring_history = True
        try:
            self.apply_text(text, cursor=cursor, record_history=False)
        finally:
            self._restoring_history = False

        self.guid_widget._on_text_edited(text, from_history=True)

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
        if abs(value) > 3.4028235e38:
            raise ValueError("Out of F32 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(f"{self._data.value:.8g}") 

class F64Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("Float64")
        self.line_edit.setFixedWidth(140)
    
    def validate_and_convert(self, text):
        value = float(text)
        if abs(value) > 1.7976931348623157e308:
            raise ValueError("Out of F64 range")
        return value

    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(f"{self._data.value:.17g}")

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


class Int3Input(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, _ in enumerate(['x', 'y', 'z']):
            line_edit = QLineEdit()
            validator = QIntValidator()
            line_edit.setValidator(validator)
            line_edit.setMaxLength(12)
            line_edit.setFixedWidth(100)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)
            
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
            input_field.setText(str(int(val))) 

    def _on_value_changed(self):
        if not self._data:
            return
        
        try:
            new_values = []
            valid_input = True
            
            for i, input_field in enumerate(self.inputs):
                text = input_field.text()
                try:
                    if not text or text == '-': 
                        value = 0
                    else:
                        value = int(text)
                        
                    if value > 2147483647:
                        raise ValueError("Out of Int32 range")
                    input_field.setStyleSheet("")
                        
                except ValueError:
                    input_field.setStyleSheet("border: 1px solid red;")
                    valid_input = False
                    value = 0
                    
                new_values.append(value)
            
            if valid_input:
                self._data.x = new_values[0]
                self._data.y = new_values[1]
                self._data.z = new_values[2]
                
                self.valueChanged.emit(tuple(new_values))
                self.mark_modified()

        except Exception as e:
            print(f"Error in Int3Input._on_value_changed: {e}")

class Int2Input(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, _ in enumerate(['x', 'y']):
            line_edit = QLineEdit()
            validator = QIntValidator()
            line_edit.setValidator(validator)
            line_edit.setMaxLength(12)
            line_edit.setFixedWidth(100)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)
            
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed) 

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(int(val))) 

    def _on_value_changed(self):
        if not self._data:
            return
        
        try:
            new_values = []
            valid_input = True
            
            for i, input_field in enumerate(self.inputs):
                text = input_field.text()
                try:
                    if not text or text == '-': 
                        value = 0
                    else:
                        value = int(text)
                        
                    if value > 2147483647:
                        raise ValueError("Out of Int32 range")
                    input_field.setStyleSheet("")
                        
                except ValueError:
                    input_field.setStyleSheet("border: 1px solid red;")
                    valid_input = False
                    value = 0
                    
                new_values.append(value)
            
            if valid_input:
                self._data.x = new_values[0]
                self._data.y = new_values[1]
                
                self.valueChanged.emit(tuple(new_values))
                self.mark_modified()

        except Exception as e:
            print(f"Error in Int2Input._on_value_changed: {e}")

class Uint2Input(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, _ in enumerate(['x', 'y']):
            line_edit = QLineEdit()
            validator = QIntValidator()
            line_edit.setValidator(validator)
            line_edit.setMaxLength(12)
            line_edit.setFixedWidth(100)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)
            
        self.layout.addStretch()
            
        if data:
            self.set_data(data)
            
        for input_field in self.inputs:
            input_field.textEdited.connect(self._on_value_changed) 

    def update_display(self):
        if not self._data:
            return
        values = [self._data.x, self._data.y]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(str(int(val))) 

    def _on_value_changed(self):
        if not self._data:
            return
        
        try:
            new_values = []
            valid_input = True
            
            for i, input_field in enumerate(self.inputs):
                text = input_field.text()
                try:
                    if not text or text == '-': 
                        value = 0
                    else:
                        value = int(text)
                        
                    if value < 0 or value > 4294967295:
                        raise ValueError("Out of U32 range")
                    input_field.setStyleSheet("")
                        
                except ValueError:
                    input_field.setStyleSheet("border: 1px solid red;")
                    valid_input = False
                    value = 0
                    
                new_values.append(value)
            
            if valid_input:
                self._data.x = new_values[0]
                self._data.y = new_values[1]
                
                self.valueChanged.emit(tuple(new_values))
                self.mark_modified()

        except Exception as e:
            print(f"Error in Uint2Input._on_value_changed: {e}")


class Uint3Input(BaseValueWidget):
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        self.inputs = []
        for i, _ in enumerate(['x', 'y', 'z']):
            line_edit = QLineEdit()
            validator = QIntValidator()
            line_edit.setValidator(validator)
            line_edit.setMaxLength(12)
            line_edit.setFixedWidth(100)
            line_edit.setAlignment(Qt.AlignLeft)
            self.layout.addWidget(line_edit)
            self.inputs.append(line_edit)
            
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
            input_field.setText(str(int(val))) 

    def _on_value_changed(self):
        if not self._data:
            return
        
        try:
            new_values = []
            valid_input = True
            
            for i, input_field in enumerate(self.inputs):
                text = input_field.text()
                try:
                    if not text or text == '-': 
                        value = 0
                    else:
                        value = int(text)
                        
                    if value < 0 or value > 4294967295:
                        raise ValueError("Out of U32 range")
                    input_field.setStyleSheet("")
                        
                except ValueError:
                    input_field.setStyleSheet("border: 1px solid red;")
                    valid_input = False
                    value = 0
                    
                new_values.append(value)
            
            if valid_input:
                self._data.x = new_values[0]
                self._data.y = new_values[1]
                self._data.z = new_values[2]
                
                self.valueChanged.emit(tuple(new_values))
                self.mark_modified()

        except Exception as e:
            print(f"Error in Uint3Input._on_value_changed: {e}")

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

class U64Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("UInt64")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < 0 or value > 18446744073709551615:
            raise ValueError("Out of U64 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

class S64Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("SInt64")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < -9223372036854775808 or value > 9223372036854775807:
            raise ValueError("Out of S64 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

class S16Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("SInt16")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < -32768 or value > 32767:
            raise ValueError("Out of S16 range")
        return value
        
    def update_display(self):
        if self._data and hasattr(self._data, 'value'):
            self.line_edit.setText(str(self._data.value))

class U16Input(NumberInput):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit.setPlaceholderText("UInt16")
        
    def validate_and_convert(self, text):
        value = int(text)
        if value < 0 or value > 65535:
            raise ValueError("Out of U16 range")
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

class AABBInput(BaseValueWidget):
    valueChanged = Signal(tuple) 

    _LABEL_W  = 33                 
    _FIELD_W  = 75

    def __init__(self, data=None, parent=None):
        super().__init__(parent)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(2)     
        grid.setVerticalSpacing(2)
        grid.setAlignment(Qt.AlignLeft)
        self.layout.addLayout(grid)

        for i, name in enumerate("XYZ", 1):
            hdr = QLabel(name)
            hdr.setAlignment(Qt.AlignCenter)
            grid.addWidget(hdr, 0, i)

        def make_row(row_idx: int, title: str):
            lbl = QLabel(title)
            lbl.setFixedWidth(self._LABEL_W)
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            grid.addWidget(lbl, row_idx, 0)

            edits = []
            for col in range(1, 4):
                le = QLineEdit()
                le.setValidator(QDoubleValidator())
                le.setFixedWidth(self._FIELD_W)
                le.setAlignment(Qt.AlignLeft)
                grid.addWidget(le, row_idx, col)
                edits.append(le)
            return edits

        self.min_edits = make_row(1, "Min:")
        self.max_edits = make_row(2, "Max:")

        self.layout.addStretch()

        for le in (*self.min_edits, *self.max_edits):
            le.textEdited.connect(self._on_value_changed)

        if data:
            self.set_data(data)

    def update_display(self):
        if not self._data:
            return
        mins = (self._data.min.x, self._data.min.y, self._data.min.z)
        maxs = (self._data.max.x, self._data.max.y, self._data.max.z)
        for le, val in zip(self.min_edits, mins):
            le.setText(f"{val:.8g}")
        for le, val in zip(self.max_edits, maxs):
            le.setText(f"{val:.8g}")
            
    def setValues(self, values):
        if len(values) != 6:
            return
        for le, v in zip(self.min_edits + self.max_edits, values):
            le.setText(str(v))

    def getValues(self):
        try:
            return tuple(float(le.text() or "0") for le in (self.min_edits + self.max_edits))
        except ValueError:
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def _on_value_changed(self):
        if not self._data:
            return
        try:
            vals = [float(le.text() or "0") for le in (self.min_edits + self.max_edits)]
            (self._data.min.x, self._data.min.y, self._data.min.z,
             self._data.max.x, self._data.max.y, self._data.max.z) = vals
            self.valueChanged.emit(tuple(vals))
            self.mark_modified()
        except ValueError:
            pass  


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
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.raw_data = None
        self.max_size = 0
        self.overwrite_mode = True 
        self.edit_second_digit = False
        
        layout = QHBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        
        self.text_field = OverwriteHexLineEdit(self, hex_widget=self)
        font = self.text_field.font()
        font.setFamily("Courier New")
        font.setPointSize(9) 
        self.text_field.setFont(font)
        self.text_field.textEdited.connect(self._on_text_edited) 
        self.text_field.setMinimumWidth(15)
        
        
        layout.addWidget(self.text_field, 1) 
        
        self.normal_palette = self.text_field.palette()
        self.warning_palette = QPalette(self.normal_palette)
        self.warning_palette.setColor(QPalette.Base, QColor(255, 240, 240)) 
        
        self.setLayout(layout)
        
        size_policy = self.sizePolicy()
        size_policy.setHorizontalPolicy(QSizePolicy.Expanding)
        self.setSizePolicy(size_policy)
        
    def update_display(self):
        if not self.raw_data:
            self.text_field.setText("")
            return
            
        cursor_pos = self.text_field.cursorPosition()
            
        try:
            raw_bytes = self.raw_data.raw_bytes

            if not isinstance(raw_bytes, bytes):
                raw_bytes = bytes(raw_bytes)
            
            hex_string = raw_bytes.hex().upper()
            formatted_hex = ' '.join(hex_string[i:i+2] for i in range(0, len(hex_string), 2))
            
            self.text_field.blockSignals(True)
            self.text_field.setText(formatted_hex)
            if cursor_pos <= len(formatted_hex):
                self.text_field.setCursorPosition(cursor_pos) 
            self.text_field.blockSignals(False)
        
            current_size = len(raw_bytes)
            
            if self.max_size > 0:
                char_width = self.text_field.fontMetrics().averageCharWidth()
                width = (self.max_size * 3.05) * char_width 
                self.text_field.setMinimumWidth(min(800, max(100, width))) 
                
            if self.max_size > 0 and current_size >= self.max_size:
                self.text_field.setPalette(self.warning_palette)
            else:
                self.text_field.setPalette(self.normal_palette)
        except Exception as e:
            print(f"Error displaying raw bytes data: {e}")
            self.text_field.setText("[Error displaying data]")
    
    def set_data(self, data):
        if not data or not isinstance(data, RawBytesData):
            self.raw_data = None
            self.max_size = 0
            self.update_display()
            return
            
        self.raw_data = data   
        
        self.max_size = data.field_size
                
        self.update_display()
    
    def get_data(self):
        return self.raw_data
    
    def _on_text_edited(self, text):
        """Handle text editing in overwrite mode"""
        if not self.raw_data:
            return
            
        try:
            cursor_pos = self.text_field.cursorPosition()
            
            original_text = self.text_field.text()
            
            text_no_spaces = ''.join(c for c in text if c in '0123456789abcdef')
            
            if text_no_spaces:
                if len(text_no_spaces) % 2 != 0:
                    text_no_spaces += '0'
                    
                new_bytes = bytes.fromhex(text_no_spaces)
                
                if self.max_size > 0 and len(new_bytes) > self.max_size:
                    new_bytes = new_bytes[:self.max_size]
            else:
                new_bytes = bytes(self.max_size)
            
            self._update_raw_bytes(new_bytes)
            
            self.update_display()
            
            new_cursor_pos = cursor_pos
            if original_text != text:
                new_cursor_pos += 1
                if new_cursor_pos < len(self.text_field.text()) and self.text_field.text()[new_cursor_pos] == ' ':
                    new_cursor_pos += 1
            
            if new_cursor_pos <= len(self.text_field.text()):
                self.text_field.setCursorPosition(new_cursor_pos)
            
            self.valueChanged.emit(new_bytes)
            self.mark_modified()
            
        except Exception as e:
            print(f"Error processing hex input: {e}")
    
    def _update_raw_bytes(self, new_bytes):
        """Update the raw bytes data using the appropriate attribute"""
        self.raw_data.raw_bytes = new_bytes

class OverwriteHexLineEdit(QLineEdit):
    def __init__(self, parent=None, hex_widget=None):
        super().__init__(parent)
        self.hex_widget = hex_widget

    def keyPressEvent(self, event):
        text = event.text()
        if text and text.upper() in "0123456789abcdef":
            pos = self.cursorPosition()
            full_text = self.text()
            
            effective_index = len(full_text[:pos].replace(" ", "")) 
            byte_index = effective_index // 2
            nibble_index = effective_index % 2

            raw_bytes = self.hex_widget.raw_data.raw_bytes
            raw_bytes_list = list(raw_bytes)
            if byte_index >= len(raw_bytes_list):
                return

            current_byte = raw_bytes_list[byte_index]
            new_digit = int(text, 16)
            if nibble_index == 0:
                new_byte = (new_digit << 4) | (current_byte & 0x0F)
            else:
                new_byte = (current_byte & 0xF0) | new_digit

            raw_bytes_list[byte_index] = new_byte
            new_bytes = bytes(raw_bytes_list)

            self.hex_widget._update_raw_bytes(new_bytes)
            self.hex_widget.update_display()

            new_cursor = pos + 1
            formatted = self.text()
            if new_cursor < len(formatted) and formatted[new_cursor] == ' ':
                new_cursor += 1
            self.setCursorPosition(new_cursor)

            self.hex_widget.valueChanged.emit(new_bytes)
            self.hex_widget.mark_modified()
        else:
            super().keyPressEvent(event)

class StringInput(BaseValueWidget):
    """Widget for editing string values"""
    valueChanged = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.line_edit.textChanged.connect(self._on_text_changed)
        
        self.minimum_width = 150
        self.resource_indicator = None
        self.open_button = None
        self.add_open_button = None
        
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_button_states)
        self._update_timer.setInterval(1000)

    def update_display(self):
        if self._data:
            self.line_edit.blockSignals(True)
            self.line_edit.setText(self._data.value.rstrip('\x00'))
            self.line_edit.blockSignals(False)
            fm = QFontMetrics(self.line_edit.font())
            text_width = fm.horizontalAdvance(self._data.value) + 10
            new_width = max(text_width, self.minimum_width)
            self.line_edit.setFixedWidth(new_width)
        
        if isinstance(self._data, ResourceData):
            if self.resource_indicator is None:
                self.resource_indicator = QLabel("Resource")
                self.resource_indicator.setStyleSheet("color: yellow; padding: 2px; border-radius: 2px;")
                self.layout.addWidget(self.resource_indicator)
            
            if self.open_button is None:
                self.open_button = QToolButton()
                self.open_button.setText(self.tr("Open"))
                self.open_button.setToolTip(self.tr("Open resource file"))
                self.open_button.setFixedWidth(60)
                self.open_button.clicked.connect(self._on_open_clicked)
                self.layout.addWidget(self.open_button)
            
            if self.add_open_button is None:
                self.add_open_button = QToolButton()
                self.add_open_button.setText(self.tr("Add & Open"))
                self.add_open_button.setToolTip(self.tr("Add resource file to project and open it"))
                self.add_open_button.setFixedWidth(85)
                self.add_open_button.clicked.connect(self._on_add_open_clicked)
                self.layout.addWidget(self.add_open_button)
            
            self.layout.addStretch()
            
            self._update_button_states()
            self._update_timer.start()

    def _update_button_states(self):
        if self.open_button is None:
            return
        
        self.open_button.setToolTip("Open resource file")
        
        if self.add_open_button:
            self.add_open_button.setToolTip("Add resource file to project and open it")

    def _get_app_window(self):
        widget = self
        while widget:
            if hasattr(widget, 'handler') and hasattr(widget.handler, 'app'):
                return widget.handler.app
            widget = widget.parent()
        return None

    def _on_open_clicked(self):
        if not isinstance(self._data, ResourceData):
            return
        
        resource_path = self._data.value.rstrip('\x00')
        if not resource_path:
            QMessageBox.information(self, self.tr("Open Resource"), self.tr("Resource path is empty"))
            return
        
        app_window = self._get_app_window()
        if not app_window:
            QMessageBox.warning(self, self.tr("Open Resource"), self.tr("Unable to access application window"))
            return
        
        if not hasattr(app_window, 'proj_dock') or not app_window.proj_dock.project_dir:
            QMessageBox.information(self, self.tr("Open Resource"),
                self.tr('You are not in project mode. Please open a project ("File" > "New Mod/Open Project")'))
            return
        
        self._open_resource_file(app_window, resource_path, add_to_project=False)
    
    def _on_add_open_clicked(self):
        if not isinstance(self._data, ResourceData):
            return
        
        resource_path = self._data.value.rstrip('\x00')
        if not resource_path:
            QMessageBox.information(self, self.tr("Add & Open Resource"), self.tr("Resource path is empty"))
            return
        
        app_window = self._get_app_window()
        if not app_window:
            QMessageBox.warning(self, self.tr("Add & Open Resource"), self.tr("Unable to access application window"))
            return
        
        if not hasattr(app_window, 'proj_dock') or not app_window.proj_dock.project_dir:
            QMessageBox.information(self, self.tr("Add & Open Resource"),
                self.tr('You are not in project mode. Please open a project ("File" > "New Mod/Open Project")'))
            return
        
        self._open_resource_file(app_window, resource_path, add_to_project=True)

    def _open_resource_file(self, app_window, resource_path, add_to_project=False):
        from utils.resource_file_utils import (
            find_resource_in_paks, 
            find_resource_in_filesystem,
            get_path_prefix_for_game,
            copy_resource_to_project
        )
        
        proj_dock = app_window.proj_dock
        
        if add_to_project:
            project_dir = proj_dock.project_dir
            if not project_dir:
                QMessageBox.information(self, self.tr("Add & Open Resource"), 
                    self.tr("No project is currently open."))
                return
            
            path_prefix = get_path_prefix_for_game(app_window.current_game)
            
            dest_path = copy_resource_to_project(
                resource_path, 
                project_dir,
                proj_dock.unpacked_dir,
                path_prefix,
                proj_dock._pak_cached_reader,
                proj_dock._pak_selected_paks
            )
            
            if dest_path:
                try:
                    with open(dest_path, "rb") as f:
                        data = f.read()
                    app_window.add_tab(dest_path, data)
                    
                    if hasattr(proj_dock, '_refresh_proj'):
                        proj_dock._refresh_proj()
                    
                    QMessageBox.information(self, self.tr("Add & Open Resource"), 
                        f"File added to project and opened:\n{dest_path}")
                except Exception as e:
                    QMessageBox.critical(self, self.tr("Add & Open Resource"), 
                        f"File was added but failed to open:\n{str(e)}")
            else:
                QMessageBox.critical(self, self.tr("Add & Open Resource"), 
                    f"Error: Resource file not found.\n\nResource: {resource_path}\n\nSearched in both PAK files and system files.")
            return
        
        file_data = None
        file_path = None
        
        pak_result = find_resource_in_paks(
            resource_path,
            proj_dock._pak_cached_reader,
            proj_dock._pak_selected_paks
        )
        if pak_result:
            file_path, file_data = pak_result
        
        if not file_data:
            path_prefix = get_path_prefix_for_game(app_window.current_game)
            fs_result = find_resource_in_filesystem(
                resource_path,
                proj_dock.unpacked_dir,
                path_prefix
            )
            if fs_result:
                file_path, file_data = fs_result
        
        if file_data:
            app_window.add_tab(file_path, file_data)
        else:
            QMessageBox.critical(self, self.tr("Open Resource"),
                f"Error: Resource file not found.\n\nResource: {resource_path}\n\nSearched in both PAK files and system files.")

    def _on_text_changed(self, text):
        if self._data:
            self._data.value = text
            self.valueChanged.emit(text)
            self.mark_modified()
            
            fm = QFontMetrics(self.line_edit.font())
            text_width = fm.horizontalAdvance(text) + 10
            new_width = max(text_width, self.minimum_width)
            self.line_edit.setFixedWidth(new_width)
            
            if hasattr(self._data, "is_gameobject_or_folder_name") and self._data.is_gameobject_or_folder_name:
                if isinstance(self._data.is_gameobject_or_folder_name, dict):
                    node_dict = self._data.is_gameobject_or_folder_name
                    
                    current_name = node_dict["data"][0]
                    id_part = current_name[current_name.find("(ID:"):] if "(ID:" in current_name else ""
                    new_node_name = f"{text} {id_part}"
                    node_dict["data"][0] = new_node_name
                    
                    parent_widget = self
                    tree_view = None
                    while parent_widget and not tree_view:
                        parent_widget = parent_widget.parent()
                        if hasattr(parent_widget, 'tree'):
                            tree_view = parent_widget.tree
                        elif isinstance(parent_widget, QTreeView):
                            tree_view = parent_widget
                        
                    if tree_view and tree_view.model():
                        model = tree_view.model()
                        
                        for visible_item in tree_view.findChildren(QLabel):
                            if "(ID:" in visible_item.text() and visible_item.text().endswith(id_part):
                                visible_item.setText(new_node_name)
                        
                        for i in range(model.rowCount()):
                            parent_index = model.index(i, 0)
                            for j in range(model.rowCount(parent_index)):
                                child_index = model.index(j, 0, parent_index)
                                tree_view.update(child_index)
                        
                        tree_view.repaint()

class UserDataInput(BaseValueWidget):
    valueChanged = Signal(str) 
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.line_edit = QLineEdit()
        self.line_edit.setReadOnly(True) 
        self.line_edit.setMinimumWidth(200) 
        self.line_edit.setAlignment(Qt.AlignLeft)
        self.layout.addWidget(self.line_edit)
        self.modify_button = QToolButton()
        self.modify_button.setText(self.tr("Modify…"))
        self.modify_button.setToolTip(self.tr("Edit userdata string and instance type"))
        self.modify_button.clicked.connect(self._on_modify_clicked)
        self.layout.addWidget(self.modify_button)
        self.layout.addStretch()
        self.line_edit.textChanged.connect(self._on_value_changed)

    def update_display(self):
        if not self._data:
            return
        display_text = f"{self._data.string}"
        self.line_edit.setText(display_text)

    def _on_value_changed(self, text):
        if self._data:
            self._data.string = text
            self.valueChanged.emit(text)
            self.mark_modified()

    def _find_viewer(self):
        parent_widget = self
        while parent_widget:
            parent_widget = parent_widget.parent()
            if hasattr(parent_widget, 'scn') and hasattr(parent_widget, 'handler'):
                return parent_widget
        return None

    def _on_modify_clicked(self):
        viewer = self._find_viewer()
        if not viewer or not hasattr(viewer, 'scn'):
            return
        scn = viewer.scn
        if getattr(scn, 'has_embedded_rsz', False):
            return

        current_instance_id = getattr(self._data, 'value', 0) or 0
        default_type_name = None
        default_string = getattr(self._data, 'string', '') or ''

        if current_instance_id > 0 and current_instance_id < len(scn.instance_infos):
            try:
                type_id = scn.instance_infos[current_instance_id].type_id
                if viewer.type_registry:
                    tinfo = viewer.type_registry.get_type_info(type_id)
                    if tinfo and 'name' in tinfo:
                        default_type_name = tinfo['name']
            except Exception:
                pass
            try:
                rui = scn._rsz_userdata_dict.get(current_instance_id)
                if rui:
                    default_string = scn._rsz_userdata_str_map.get(rui, default_string)
            except Exception:
                pass
        else:
            default_type_name = getattr(self._data, 'orig_type', '') or ''

        new_string, ok = QInputDialog.getText(
            self,
            self.tr("Modify UserData String"),
            self.tr("Enter new UserData string:"),
            QLineEdit.Normal,
            default_string
        )
        if not ok:
            return

        type_dialog = ComponentSelectorDialog(self, viewer.type_registry, required_parent_name="via.UserData")
        type_dialog.setWindowTitle(self.tr("Select UserData Instance Type"))
        if default_type_name:
            try:
                type_dialog.search_input.setText(default_type_name)
            except Exception:
                pass
        if not type_dialog.exec_():
            return
        selected_type = type_dialog.get_selected_component()
        if not selected_type:
            return

        try:
            success = viewer.object_operations.modify_userdata_field(self._data, new_string, selected_type)
            if success:
                self.line_edit.setText(new_string)
        except Exception:
            pass

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
        
        self.color_button = ColorPreviewButton()
        self.color_button.setFixedSize(24, 24)
        self.color_button.setHasAlpha(True)
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
        
        self.color_button.setColor(r, g, b, a)
    
    def _show_color_dialog(self):
            if not self._data:
                return

            init = QColor(self._data.r, self._data.g, self._data.b, self._data.a)

            dlg = QColorDialog(init, self)
            dlg.setOption(QColorDialog.DontUseNativeDialog, True)
            dlg.setOption(QColorDialog.ShowAlphaChannel,    True)

            hex_edit: QLineEdit | None = dlg.findChild(QLineEdit, "qt_color_hexLineEdit")
            if not hex_edit:
                for w in dlg.findChildren(QLineEdit):
                    if w.placeholderText().startswith("#") or w.text().startswith("#"):
                        hex_edit = w
                        break

            if hex_edit:
                hex_edit.setInputMask("")             
                hex_edit.setMaxLength(9)              
                hex_edit.setPlaceholderText("#RRGGBBAA")
                rx  = QRegularExpression(r"^#[0-9A-Fa-f]{8}$")
                hex_edit.setValidator(QRegularExpressionValidator(rx))

            def write_html(c: QColor):
                if not hex_edit:
                    return
                text = f"#{c.red():02X}{c.green():02X}{c.blue():02X}{c.alpha():02X}"

                QTimer.singleShot(0, lambda t=text: (
                    hex_edit.blockSignals(True),
                    hex_edit.setText(t),
                    hex_edit.blockSignals(False))
                )

            write_html(init)
            dlg.currentColorChanged.connect(write_html)  

            for sld in dlg.findChildren(QSlider):
                if sld.minimum() == 0 and sld.maximum() == 255:
                    sld.valueChanged.connect(
                        lambda v, d=dlg: write_html(d.currentColor().withAlpha(v))
                    )
                    break
            
            excepted = False
            if hex_edit:
                    def on_hex(text: str):
                        try:
                            if len(text) == 7 or len(text) == 9:  
                                r,g,b = int(text[1:3],16), int(text[3:5],16), int(text[5:7],16)
                                a     = int(text[7:9],16)
                            else:
                                return
                            dlg.setCurrentColor(QColor(r,g,b,a))
                        except Exception:
                            excepted = True
                    hex_edit.textChanged.connect(on_hex)

            if dlg.exec_() and not excepted:
                    final = hex_edit.text() if hex_edit and len(hex_edit.text()) == 9 else None
                    if final:
                        r,g,b,a = int(final[1:3],16), int(final[3:5],16), int(final[5:7],16), int(final[7:9],16)
                    else:
                        c = dlg.currentColor()
                        r,g,b,a = c.red(), c.green(), c.blue(), c.alpha()

                    self._data.r, self._data.g, self._data.b, self._data.a = r, g, b, a
                    self.update_display()
                    self.valueChanged.emit((r,g,b,a))
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

class Vec3ColorInput(VectorClipboardMixin, BaseValueWidget):
    """Widget for editing Vec3 RGB color values as floats"""
    valueChanged = Signal(tuple)

    clipboard_precision = 6

    def __init__(self, data=None, parent=None):
        super().__init__(parent)

        self.color_button = ColorPreviewButton()
        self.color_button.setFixedSize(24, 24)
        self.color_button.setHasAlpha(False)
        self.color_button.clicked.connect(self._show_color_dialog)
        
        grid = QGridLayout()
        grid.setSpacing(2)
        grid.setContentsMargins(0, 0, 0, 0)
        
        grid.addWidget(self.color_button, 0, 0)
        grid.setColumnMinimumWidth(1, 6)

        self.inputs = []
        for i, comp in enumerate(['R', 'G', 'B']):
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
            validator = QDoubleValidator()
            validator.setDecimals(6)
            line_edit.setValidator(validator)
            line_edit.setFixedWidth(60)
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
            
            if i < 2:
                grid.setColumnMinimumWidth(col_offset + 2, 3)
                
            self.inputs.append(line_edit)
        
        self.layout.addLayout(grid)
        self._setup_clipboard_buttons()
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
                        value = 0.0
                    else:
                        value = float(text)
                except ValueError:
                    valid = False
                    value = 0.0
                
                values.append(value)
                
                input_field.setProperty("invalid", not valid)
                input_field.style().unpolish(input_field)
                input_field.style().polish(input_field)
                
                if not valid:
                    input_field.setText(str(value))
            
            self._data.x = values[0]
            self._data.y = values[1]
            self._data.z = values[2]
            
            self._update_color_button()
            
            if valid:
                self.valueChanged.emit(tuple(values))
                self.mark_modified()
                
        except ValueError:
            pass

    def update_display(self):
        if not self._data:
            return
            
        values = [self._data.x, self._data.y, self._data.z]
        for input_field, val in zip(self.inputs, values):
            input_field.setText(f"{val:.6g}")
            
        self._update_color_button()
    
    def _update_color_button(self):
        """Update the color button appearance based on current RGB values"""
        if not self._data:
            return
            
        # Clamp RGB values between 0 and 1 for display purposes
        r = max(0, min(1, float(self._data.x))) * 255
        g = max(0, min(1, float(self._data.y))) * 255
        b = max(0, min(1, float(self._data.z))) * 255
        
        self.color_button.setColor(int(r), int(g), int(b), 255)
    
    def _show_color_dialog(self):
        """Open color picker dialog and update values if user selects a color"""
        if not self._data:
            return
            
        # Convert float values [0.0-1.0] to integer values [0-255] for QColor
        r = int(max(0, min(1, float(self._data.x))) * 255)
        g = int(max(0, min(1, float(self._data.y))) * 255)
        b = int(max(0, min(1, float(self._data.z))) * 255)
        
        initial_color = QColor(r, g, b)
        
        dialog = QColorDialog(initial_color, self)
        dialog.setWindowTitle("Select Color")
        dialog.setOption(QColorDialog.ShowAlphaChannel, False)  # No alpha channel
        
        if dialog.exec_():
            color = dialog.currentColor()
            # Convert integer values [0-255] back to float values [0.0-1.0]
            self._data.x = color.red() / 255.0
            self._data.y = color.green() / 255.0
            self._data.z = color.blue() / 255.0
            
            self.update_display()
            
            self.valueChanged.emit((self._data.x, self._data.y, self._data.z))
            self.mark_modified()
    
    def setValues(self, values):
        for input_field, val in zip(self.inputs, values):
            input_field.setText(f"{val:.6g}")
        self._update_color_button()
    
    def getValues(self):
        try:
            return tuple(float(input_field.text() or "0") for input_field in self.inputs)
        except ValueError:
            return (0.0, 0.0, 0.0)
    
    def _on_value_changed(self):
        """Handle changes from manual input of RGB values"""
        if not self._data:
            return
            
        try:
            values = []
            for input_field in self.inputs:
                text = input_field.text()
                if not text or text == '-':
                    values.append(0.0)
                else:
                    values.append(float(text))
            
            self._data.x = values[0]
            self._data.y = values[1]
            self._data.z = values[2]
            
            self._update_color_button()
            
            self.valueChanged.emit(tuple(values))
            self.mark_modified()
        except ValueError:
            pass

class CapsuleInput(BaseValueWidget):
    """Widget for editing capsule collision shapes (start point, end point, and radius)"""
    valueChanged = Signal(tuple)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        
        grid = QGridLayout()
        grid.setSpacing(4) 
        grid.setAlignment(Qt.AlignLeft)
        self.layout.addLayout(grid)
        
        grid.addWidget(QLabel("Start:"), 0, 0, alignment=Qt.AlignRight)
        grid.addWidget(QLabel("End:"), 1, 0, alignment=Qt.AlignRight)
        grid.addWidget(QLabel("Radius:"), 2, 0, alignment=Qt.AlignRight)
        
        self.start_inputs = []
        for i, coord in enumerate(['X', 'Y', 'Z']):
            grid.addWidget(QLabel(coord), 0, (i*2)+1, alignment=Qt.AlignRight | Qt.AlignVCenter)
            
            line_edit = QLineEdit()
            line_edit.setValidator(QDoubleValidator())
            line_edit.setFixedWidth(80)
            line_edit.setAlignment(Qt.AlignLeft)
            line_edit.setStyleSheet("margin-left: 6px;") 
            grid.addWidget(line_edit, 0, (i*2)+2)
            self.start_inputs.append(line_edit)
            
        self.end_inputs = []
        for i, coord in enumerate(['X', 'Y', 'Z']):
            grid.addWidget(QLabel(coord), 1, (i*2)+1, alignment=Qt.AlignRight | Qt.AlignVCenter)
            
            line_edit = QLineEdit()
            line_edit.setValidator(QDoubleValidator())
            line_edit.setFixedWidth(80)
            line_edit.setAlignment(Qt.AlignLeft)
            line_edit.setStyleSheet("margin-left: 6px;") 
            grid.addWidget(line_edit, 1, (i*2)+2)
            self.end_inputs.append(line_edit)
            
        self.radius_input = QLineEdit()
        self.radius_input.setValidator(QDoubleValidator())
        self.radius_input.setFixedWidth(80)
        self.radius_input.setAlignment(Qt.AlignLeft)
        self.radius_input.setStyleSheet("margin-left: 6px;")
        grid.addWidget(self.radius_input, 2, 2)
        
        self.layout.addStretch()
        
        if data:
            self.set_data(data)
            
        for input_field in self.start_inputs + self.end_inputs:
            input_field.textEdited.connect(self._on_value_changed)
        self.radius_input.textEdited.connect(self._on_value_changed)

    def update_display(self):
        """Update input fields from data object"""
        if not self._data:
            return
            
        if hasattr(self._data, 'start') and hasattr(self._data.start, 'x'):
            start_values = [self._data.start.x, self._data.start.y, self._data.start.z]
            for input_field, val in zip(self.start_inputs, start_values):
                input_field.setText(f"{val:.8g}")
                
        if hasattr(self._data, 'end') and hasattr(self._data.end, 'x'):
            end_values = [self._data.end.x, self._data.end.y, self._data.end.z]
            for input_field, val in zip(self.end_inputs, end_values):
                input_field.setText(f"{val:.8g}")
                
        if hasattr(self._data, 'radius'):
            self.radius_input.setText(f"{self._data.radius:.8g}")

    def _on_value_changed(self):
        """Handle input changes and update the data model"""
        if not self._data:
            return
            
        try:
            start_values = []
            for input_field in self.start_inputs:
                text = input_field.text()
                if not text or text == '-':
                    start_values.append(0.0)
                else:
                    start_values.append(float(text))
                    
            end_values = []
            for input_field in self.end_inputs:
                text = input_field.text()
                if not text or text == '-':
                    end_values.append(0.0)
                else:
                    end_values.append(float(text))
                    
            radius_text = self.radius_input.text()
            radius_value = 0.0
            if radius_text and radius_text != '-':
                radius_value = float(radius_text)
                
            self._data.start.x = start_values[0]
            self._data.start.y = start_values[1]
            self._data.start.z = start_values[2]
            
            self._data.end.x = end_values[0]
            self._data.end.y = end_values[1]
            self._data.end.z = end_values[2]
            
            self._data.radius = radius_value
            
            combined_values = (*start_values, *end_values, radius_value)
            self.valueChanged.emit(combined_values)
            self.mark_modified()
            
        except ValueError:
            pass  # Ignore invalid input during typing
            
    def setValues(self, values):
        """Set all input values at once (start_x, start_y, start_z, end_x, end_y, end_z, radius)"""
        if len(values) != 7:
            return
            
        for i, val in enumerate(values[:3]):
            self.start_inputs[i].setText(str(val))
            
        for i, val in enumerate(values[3:6]):
            self.end_inputs[i].setText(str(val))
            
        self.radius_input.setText(str(values[6]))
    
    def getValues(self):
        """Get all values as a tuple (start_x, start_y, start_z, end_x, end_y, end_z, radius)"""
        try:
            start_values = [float(input_field.text() or "0") for input_field in self.start_inputs]
            end_values = [float(input_field.text() or "0") for input_field in self.end_inputs]
            radius = float(self.radius_input.text() or "0")
            
            return (*start_values, *end_values, radius)
        except ValueError:
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

class AreaInput(BaseValueWidget):
    valueChanged = Signal(list)
    
    def __init__(self, data=None, parent=None):
        super().__init__(parent)
        grid = QGridLayout()
        grid.setSpacing(2)
        grid.setAlignment(Qt.AlignLeft)
        self.inputs = []

        grid.addWidget(QLabel("p0.x"), 0, 0, alignment=Qt.AlignRight)
        p0x_edit = QLineEdit()
        p0x_edit.setValidator(QDoubleValidator())
        grid.addWidget(p0x_edit, 0, 1)
        grid.addWidget(QLabel("p0.y"), 0, 2, alignment=Qt.AlignRight)
        p0y_edit = QLineEdit()
        p0y_edit.setValidator(QDoubleValidator())
        grid.addWidget(p0y_edit, 0, 3)

        grid.addWidget(QLabel("p1.x"), 1, 0, alignment=Qt.AlignRight)
        p1x_edit = QLineEdit()
        p1x_edit.setValidator(QDoubleValidator())
        grid.addWidget(p1x_edit, 1, 1)
        grid.addWidget(QLabel("p1.y"), 1, 2, alignment=Qt.AlignRight)
        p1y_edit = QLineEdit()
        p1y_edit.setValidator(QDoubleValidator())
        grid.addWidget(p1y_edit, 1, 3)

        grid.addWidget(QLabel("p2.x"), 2, 0, alignment=Qt.AlignRight)
        p2x_edit = QLineEdit()
        p2x_edit.setValidator(QDoubleValidator())
        grid.addWidget(p2x_edit, 2, 1)
        grid.addWidget(QLabel("p2.y"), 2, 2, alignment=Qt.AlignRight)
        p2y_edit = QLineEdit()
        p2y_edit.setValidator(QDoubleValidator())
        grid.addWidget(p2y_edit, 2, 3)

        grid.addWidget(QLabel("p3.x"), 3, 0, alignment=Qt.AlignRight)
        p3x_edit = QLineEdit()
        p3x_edit.setValidator(QDoubleValidator())
        grid.addWidget(p3x_edit, 3, 1)
        grid.addWidget(QLabel("p3.y"), 3, 2, alignment=Qt.AlignRight)
        p3y_edit = QLineEdit()
        p3y_edit.setValidator(QDoubleValidator())
        grid.addWidget(p3y_edit, 3, 3)

        grid.addWidget(QLabel("height"), 4, 0, alignment=Qt.AlignRight)
        height_edit = QLineEdit()
        height_edit.setValidator(QDoubleValidator())
        grid.addWidget(height_edit, 4, 1)

        grid.addWidget(QLabel("bottom"), 5, 0, alignment=Qt.AlignRight)
        bottom_edit = QLineEdit()
        bottom_edit.setValidator(QDoubleValidator())
        grid.addWidget(bottom_edit, 5, 1)

        self.inputs = [
            p0x_edit, p0y_edit,
            p1x_edit, p1y_edit,
            p2x_edit, p2y_edit,
            p3x_edit, p3y_edit,
            height_edit, bottom_edit
        ]
        self.layout.addLayout(grid)
        self.layout.addStretch()
        
        if data:
            self.set_data(data)

    def update_display(self):
        if not self._data:
            return
        vals = [
            self._data.p0.x, self._data.p0.y,
            self._data.p1.x, self._data.p1.y,
            self._data.p2.x, self._data.p2.y,
            self._data.p3.x, self._data.p3.y,
            self._data.height, self._data.bottom
        ]
        for inp, v in zip(self.inputs, vals):
            inp.setText(f"{v:.8g}")

    def _on_value_changed(self):
        if not self._data:
            return
        try:
            vals = []
            for i, inp in enumerate(self.inputs):
                t = inp.text()
                vals.append(float(t) if t else 0.0)

            self._data.p0.x, self._data.p0.y = vals[0], vals[1]
            self._data.p1.x, self._data.p1.y = vals[2], vals[3]
            self._data.p2.x, self._data.p2.y = vals[4], vals[5]
            self._data.p3.x, self._data.p3.y = vals[6], vals[7]
            self._data.height, self._data.bottom = vals[8], vals[9]
            self.valueChanged.emit(vals)
            self.mark_modified()
        except ValueError:
            pass
