"""EFX scalar/vector controls shared by parameter and channel dialogs."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QWidget, QHBoxLayout, QVBoxLayout, QGridLayout, QFormLayout,
                              QLineEdit, QComboBox, QCheckBox, QPushButton, QColorDialog,
                              QDialog, QDialogButtonBox, QScrollArea, QTabWidget, QMessageBox)

from .efx_file import EfxRecord, PRIMITIVES, schema_for_version
from .editing import structural_fields, typed_value


class ValueWidget(QWidget):
    def __init__(self, kind, value, definition=None, version=2818730, parent=None):
        super().__init__(parent)
        self.kind, self.definition = kind, definition or {}
        self.layout = QHBoxLayout(self); self.layout.setContentsMargins(0, 0, 0, 0)
        self.color_value = value
        enum = schema_for_version(version)['Enums'].get(self.definition.get('Classname'))
        if kind == 'Color':
            self.control = QPushButton()
            self.control.clicked.connect(self._color)
            self._color_label()
        elif kind == 'Bool':
            self.control = QCheckBox(); self.control.setChecked(value)
        elif enum:
            self.control = QComboBox(); self.control.setEditable(True)
            for item in enum['Items']:
                number = item.get('Value', 0)
                self.control.addItem(f"{number}: {item['Name']}", number)
            index = self.control.findData(value)
            if index >= 0:
                self.control.setCurrentIndex(index)
            else:
                self.control.setEditText(str(value))
        elif kind in PRIMITIVES and len(PRIMITIVES[kind]) > 1:
            self.control = []
            for v in value:
                edit = QLineEdit(repr(v)); self.layout.addWidget(edit); self.control.append(edit)
            return
        else:
            self.control = QLineEdit(str(value))
        self.layout.addWidget(self.control)
        if kind in ('S32', 'U32', 'U16', 'U8') and 'flag' in self.definition.get('Name', '').lower():
            button = QPushButton('Bits'); button.clicked.connect(self._flags); self.layout.addWidget(button)
        if kind == 'String' and any(token in self.definition.get('Name', '').lower() for token in ('path', 'resource')):
            button = QPushButton('…'); button.setMaximumWidth(32); button.clicked.connect(self._resource); self.layout.addWidget(button)

    def _color_label(self):
        channels = int(self.color_value).to_bytes(4, 'little')
        self.control.setText('#' + channels.hex().upper())
        self.control.setStyleSheet(f'border-left: 20px solid rgb({channels[0]},{channels[1]},{channels[2]})')

    def _color(self):
        color = QColorDialog.getColor(QColor(*int(self.color_value).to_bytes(4, 'little')), self, 'Color', QColorDialog.ShowAlphaChannel)
        if color.isValid():
            self.color_value = int.from_bytes(bytes(color.getRgb()), 'little'); self._color_label()

    def _flags(self):
        try:
            current = self.value()
        except ValueError as error:
            QMessageBox.warning(self, 'Invalid flags', str(error)); return
        count = {'U8': 8, 'U16': 16}.get(self.kind, 32)
        dialog = QDialog(self); dialog.setWindowTitle('Flags'); layout = QVBoxLayout(dialog); grid = QGridLayout()
        boxes = []
        for i in range(count):
            box = QCheckBox(f'{i} (0x{1 << i:X})'); box.setChecked(bool(current & (1 << i)))
            boxes.append(box); grid.addWidget(box, i // 4, i % 4)
        layout.addLayout(grid); buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        if dialog.exec():
            value = sum(1 << i for i, box in enumerate(boxes) if box.isChecked())
            if self.kind == 'S32' and value >= 0x80000000:
                value -= 0x100000000
            self.control.setText(str(value))

    def _resource(self):
        from PySide6.QtWidgets import QFileDialog
        import re
        path, _ = QFileDialog.getOpenFileName(self, 'Resource')
        if path:
            normalized = path.replace('\\', '/')
            match = re.search(r'(?i)/natives/[^/]+/(.+)', normalized)
            if not match:
                QMessageBox.warning(self, 'Resource path', 'Choose a file inside natives/<platform>, or enter its virtual resource path directly.')
                return
            value = match.group(1)
            self.control.setText(re.sub(r'\.\d+(?:\.(?:x64|stm))?$', '', value))

    def value(self):
        if self.kind == 'Color':
            value = self.color_value
        elif self.kind == 'Bool':
            value = self.control.isChecked()
        else:
            if isinstance(self.control, list):
                conversion = float if PRIMITIVES[self.kind][-1] == 'f' else lambda v: int(v, 0) if v.lower().startswith(('0x', '-0x')) else int(v)
                value = [conversion(edit.text()) for edit in self.control]
            else:
                text = self.control.currentText().split(':', 1)[0] if isinstance(self.control, QComboBox) else self.control.text()
                value = text if self.kind == 'String' else float(text) if self.kind == 'F32' else int(text, 16 if text.lower().startswith(('0x', '-0x')) else 10)
        return typed_value(self.kind, value, self.definition)


class ParameterDialog(QDialog):
    def __init__(self, entry, path, version, parent=None):
        super().__init__(parent)
        self.setWindowTitle(entry.name); self.resize(690, 680)
        self.controls = []
        layout = QVBoxLayout(self); tabs = QTabWidget(); layout.addWidget(tabs)

        def record_form(record, prefix, form, label=''):
            structural = structural_fields(record)
            for name, field in record.fields.items():
                if name in structural:
                    continue
                if isinstance(field.value, EfxRecord):
                    record_form(field.value, prefix + (name,), form, label + name + '.')
                elif field.kind in PRIMITIVES or field.kind == 'String':
                    widget = ValueWidget(field.kind, field.value, field.definition, version)
                    form.addRow(label + name, widget); self.controls.append((prefix + (name,), widget))

        for ai, attribute in enumerate(entry.attributes):
            widget = QWidget(); form = QFormLayout(widget)
            record_form(attribute.body, tuple(path) + ('attributes', str(ai), 'fields'), form)
            if form.rowCount():
                scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(widget)
                tabs.addTab(scroll, attribute.name)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.operations = []

    def _accept(self):
        try:
            self.operations = [{'op': 'set', 'path': path, 'value': widget.value()} for path, widget in self.controls]
        except (ValueError, OverflowError) as error:
            QMessageBox.warning(self, 'Invalid value', str(error)); return
        self.accept()
