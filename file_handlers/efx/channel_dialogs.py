"""Key table and formula editors using the shared EFX channel transactions."""
import copy
import json

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QSpinBox, QComboBox,
                              QTableWidget, QTableWidgetItem, QPushButton, QDialogButtonBox,
                              QMessageBox, QPlainTextEdit, QLineEdit, QCheckBox, QWidget, QLabel)

from .channels import body_of, curve_snapshot, active_channels
from .editing import EfxEditor, locate
from .expressions import expression_text
from .graph import expression_owner


class CurvePlot(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); self.points = []; self.setMinimumHeight(160)

    def paintEvent(self, event):
        painter = QPainter(self); painter.fillRect(self.rect(), QColor('#20252b'))
        if not self.points:
            return
        xs, ys = zip(*self.points)
        xmin, xmax, ymin, ymax = min(xs), max(xs), min(ys), max(ys)
        dx, dy = max(xmax - xmin, 1.), max(ymax - ymin, 1.)
        transform = lambda p: QPointF(28 + (p[0]-xmin) / dx * (self.width()-56), self.height()-24 - (p[1]-ymin) / dy * (self.height()-48))
        painter.setPen(QPen(QColor('#63808c'), 1, Qt.PenStyle.DashLine))
        for a, b in zip(self.points, self.points[1:]):
            painter.drawLine(transform(a), transform(b))
        painter.setPen(QColor('#8dd6cb')); painter.setBrush(QColor('#8dd6cb'))
        for point in self.points:
            painter.drawEllipse(transform(point), 3, 3)
        painter.drawText(5, 14, f'{ymax:g}'); painter.drawText(5, self.height()-5, f'{ymin:g}')


class ChannelDialog(QDialog):
    def __init__(self, editor, path, mode, parent=None):
        super().__init__(parent)
        self.editor = EfxEditor(copy.deepcopy(editor.document)); self.path = path; self.mode = mode
        self.operations = []; self.draft_dirty = False; self.setWindowTitle('Curve channels' if mode == 'curve' else 'Expression channels'); self.resize(790, 680)
        layout = QVBoxLayout(self); form = self.form = QFormLayout()
        self.channel = QSpinBox(); self.channel.setRange(0, 255); form.addRow('Channel / slot', self.channel)
        self.material = QCheckBox('Material'); form.addRow(self.material)
        self.indices = QLineEdit('[]'); form.addRow('Material indices', self.indices)
        self.properties = QLineEdit('{}'); form.addRow('Material property', self.properties)
        self.headers = QLineEdit('{}'); form.addRow('Material headers', self.headers)
        layout.addLayout(form)
        if mode == 'curve':
            self.value_type = QComboBox(); self.value_type.addItem('Float', 5); self.value_type.addItem('Int', 3)
            form.addRow('Value type', self.value_type)
            self.table = QTableWidget(0, 7); self.table.setHorizontalHeaderLabels(['Time', 'Value', 'Interpolation', 'Out X', 'Out Y', 'In X', 'In Y'])
            self.table.horizontalHeader().setStretchLastSection(True); layout.addWidget(self.table)
            keys = QHBoxLayout()
            for label, callback in [('Add key', self.add_key), ('Delete key', self.delete_key), ('Sort time', self.sort_keys)]:
                button = QPushButton(label); button.clicked.connect(callback); keys.addWidget(button)
            layout.addLayout(keys); self.plot = CurvePlot(); layout.addWidget(self.plot)
            self.table.itemChanged.connect(self._plot)
            self.table.itemChanged.connect(self._dirty)
            self.value_type.currentIndexChanged.connect(self._dirty)
        else:
            self.formula = QPlainTextEdit(); layout.addWidget(self.formula)
            self.formula.textChanged.connect(self._dirty)
        self.indices.textChanged.connect(self._dirty); self.properties.textChanged.connect(self._dirty)
        self.headers.textChanged.connect(self._dirty)
        actions = QHBoxLayout()
        for label, callback in [('Load channel', self.load_channel), ('Apply channel', self.apply_channel), ('Delete channel', self.delete_channel)]:
            button = QPushButton(label); button.clicked.connect(callback); actions.addWidget(button)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        body = body_of(locate(self.editor.document, path).value)
        if mode == 'curve':
            self.material.setChecked('mdfProperties' in body['clipData'].fields); self.material.setEnabled(False)
            rows = curve_snapshot(body)
            if rows:
                self.channel.setValue(rows[0]['channel'])
        elif 'expressions' in body.fields:
            channels = active_channels(body, 'expressionBits')
            if channels:
                self.channel.setValue(channels[0])
        else:
            self.material.setChecked(True)
        if mode == 'expression':
            available = any(name in body.fields for name in ('materialExpressions', 'materialExpressionsList'))
            self.material.setEnabled(available and 'expressions' in body.fields)
            self.material.toggled.connect(self.load_channel)
        self.load_channel()

    def _dirty(self, *_):
        self.draft_dirty = True

    def _body(self):
        return body_of(locate(self.editor.document, self.path).value)

    def load_channel(self):
        body = self._body(); channel = self.channel.value(); material = self.material.isChecked()
        self.indices.setEnabled(material); self.properties.setEnabled(material)
        self.headers.setEnabled(material and self.mode == 'expression')
        self.form.setRowVisible(self.indices, material); self.form.setRowVisible(self.properties, material)
        self.form.setRowVisible(self.headers, material and self.mode == 'expression')
        self.headers.setText(json.dumps({name: body[name] for name in ('materialExpressionCount', 'materialPropertyCount') if name in body.fields}))
        if self.mode == 'curve':
            rows = {row['channel']: row for row in curve_snapshot(body)}
            row = rows.get(channel, {'value_type': 5, 'keys': [{'time': 0., 'value': 0., 'interpolation': 2}]})
            self.value_type.setCurrentIndex(self.value_type.findData(row['value_type']))
            self.table.blockSignals(True); self.table.setRowCount(0)
            for key in row['keys']:
                self.add_key(key)
            self.table.blockSignals(False); self._plot()
            if material:
                self.indices.setText(json.dumps(body['clipData']['indices']))
                self.properties.setText(json.dumps(row.get('material', {'mdfPropertyHash': 0, 'unkn1': 0, 'unkn2': 0, 'unkn3': 5})))
        else:
            if material:
                flat = 'materialExpressionsList' in body.fields
                values = body['materialExpressionsList'] if flat else body['materialExpressions']['expressions']
                indices = body['materialIndices'] if flat else body['materialExpressions']['indices']
                targets = list(range(len(values))); self.indices.setText(json.dumps(indices))
            else:
                values, targets = body['expressions']['expressions'], active_channels(body, 'expressionBits')
            expression = values[targets.index(channel)] if channel in targets else None
            loc = locate(self.editor.document, self.path)
            parameters = expression_owner(self.editor.document, loc.document).expression_parameters
            try:
                self.formula.setPlainText(expression_text(expression, parameters) if expression else '0')
            except ValueError as error:
                self.formula.setPlainText(''); QMessageBox.warning(self, 'Expression', str(error))
            if material:
                keys = ('unkn1', 'unkn2', 'mdfPropertyHash', 'propertyComponentIndex')
                self.properties.setText(json.dumps({key: expression[key] if expression else 0 for key in keys}))
        self.draft_dirty = False

    def add_key(self, key=None):
        key = key if isinstance(key, dict) else {'time': 0., 'value': 0., 'interpolation': 2}
        row = self.table.rowCount(); self.table.insertRow(row)
        values = [key['time'], key['value'], key['interpolation'], *(key.get('tangents', {}).get(name, '') for name in ('out_x','out_y','in_x','in_y'))]
        for column, value in enumerate(values):
            self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def delete_key(self):
        for row in sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True):
            self.table.removeRow(row)
        self.draft_dirty = True
        self._plot()

    def _keys(self):
        keys = []
        for row in range(self.table.rowCount()):
            text = lambda col: self.table.item(row, col).text() if self.table.item(row, col) else ''
            key = {'time': float(text(0)), 'value': int(text(1)) if self.value_type.currentData() == 3 else float(text(1)), 'interpolation': int(text(2))}
            if key['interpolation'] == 5:
                key['tangents'] = {name: float(text(i+3)) for i, name in enumerate(('out_x', 'out_y', 'in_x', 'in_y'))}
            keys.append(key)
        return keys

    def _plot(self):
        try:
            self.plot.points = [(key['time'], key['value']) for key in self._keys()]
        except ValueError:
            self.plot.points = []
        self.plot.update()

    def sort_keys(self):
        try:
            keys = sorted(self._keys(), key=lambda key: key['time'])
        except ValueError as error:
            QMessageBox.warning(self, 'Invalid key', str(error)); return
        self.table.blockSignals(True); self.table.setRowCount(0)
        for key in keys:
            self.add_key(key)
        self.table.blockSignals(False); self._plot()

    def _apply(self, deleting=False):
        try:
            op = {'op': self.mode + ('-delete' if deleting else '-set'), 'path': self.path, 'channel': self.channel.value()}
            if self.material.isChecked():
                op['indices'] = json.loads(self.indices.text())
                if self.mode == 'expression':
                    op['material'] = True
                    op['headers'] = json.loads(self.headers.text())
            if not deleting:
                if self.mode == 'curve':
                    op['value'] = {'value_type': self.value_type.currentData(), 'keys': self._keys()}
                    if self.material.isChecked():
                        op['value']['material'] = json.loads(self.properties.text())
                else:
                    op['value'] = {'formula': self.formula.toPlainText()}
                    if self.material.isChecked():
                        op['value'].update(json.loads(self.properties.text()))
            self.editor.apply(op); self.operations.append(op); self.draft_dirty = False
            return True
        except (ValueError, KeyError, TypeError, OverflowError) as error:
            QMessageBox.warning(self, 'Channel edit failed', str(error)); return False

    def apply_channel(self):
        self._apply()

    def delete_channel(self):
        if self._apply(deleting=True):
            self.load_channel()

    def _accept(self):
        if not self.draft_dirty or self._apply():
            self.accept()
