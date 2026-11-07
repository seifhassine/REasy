"""See docs/internals/motion-editor.md, Trees."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QDoubleSpinBox, QFormLayout, QScrollArea, QSpinBox, QWidget

lazy from ui.editor_widgets import styled_label


class TreeVariables(QScrollArea):
    changed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self._values: dict = {}

    def show_variables(self, held: tuple[list, dict] | None) -> None:
        body = QWidget()
        form = QFormLayout(body)
        rows, self._values = held or ([], {})
        if not rows:
            form.addRow(styled_label(self.tr("The tree reads no variables, or no character is previewed to read "
                                             "its .uvar files from."), body))
        for guid, name, default in rows:
            value = self._values.get(guid, default)
            if isinstance(value, bool):
                field = QCheckBox(body)
                field.setChecked(value)
                field.toggled.connect(lambda checked, key=guid: self._set(key, checked))
            elif isinstance(value, int):
                field = QSpinBox(body)
                field.setRange(-2 ** 31, 2 ** 31 - 1)
                field.setValue(value)
                field.valueChanged.connect(lambda number, key=guid: self._set(key, number))
            else:
                field = QDoubleSpinBox(body)
                field.setRange(-1e6, 1e6)
                field.setDecimals(4)
                field.setSingleStep(0.05)
                field.setValue(value)
                field.valueChanged.connect(lambda number, key=guid: self._set(key, number))
            form.addRow(name, field)
        self.setWidget(body)

    def _set(self, guid: bytes, value) -> None:
        self._values[guid] = value
        self.changed.emit()
