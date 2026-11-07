"""See docs/internals/jcns-editor.md."""

from __future__ import annotations

from PySide6.QtCore import QObject, QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QWidget,
)

lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon

from file_handlers.document_commands import Change
from ui.outline_model import OutlineModel

from ..document import JcnsDocument, Table
from ..edits import JointEdits
from ..model import Record, Text


class JcnsOutline(OutlineModel):
    def __init__(self, document: JcnsDocument, names: dict[int, str], parent: QObject | None = None):
        super().__init__(document.root, parent)
        self.document, self.names = document, names
        document.changed.connect(self._on_changed)

    def children_of(self, item) -> list:
        return self.document.children(item)

    def owner_of(self, item) -> object | None:
        return self.document.parent(item)

    def display(self, item, row: int, column: int, role: int):
        if role == Qt.DisplayRole:
            if isinstance(item, Table):
                return item.name if column == 0 else str(len(item.owner.links.get(item.name) or ()))
            return f"{row}  {self.label(item)}" if column == 0 else item.kind
        if role == Qt.DecorationRole and column == 0:
            return line_icon("list" if isinstance(item, Table) else "box")
        return None

    def label(self, record: Record) -> str:
        hashes = self.document.root.links.get("name_hashes") or ()
        for name, value in record.values.items():
            if name.endswith("_name_index") and isinstance(value, int) and 0 <= value < len(hashes):
                value = hashes[value].values["hash"]
            elif not name.endswith("_name_hash") or not value:
                continue
            return self.names.get(value, f"0x{value:08X}")
        return ""

    def _on_changed(self, change: Change) -> None:
        if change.field == "values":
            self.refresh(change.target)
        else:
            self.reset(self.document.root)


class JcnsInspector(QScrollArea):
    def __init__(self, edits: JointEdits, parent: QWidget | None = None):
        super().__init__(parent)
        self.edits, self.document, self.enums = edits, edits.document, edits.enums
        self.record: Record | None = None
        self.setWidgetResizable(True)
        self.setWidget(QWidget(self))
        self.form = QFormLayout(self.widget())
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.document.changed.connect(self._on_changed)

    def show_record(self, record: Record | None) -> None:
        self.record = record
        while self.form.rowCount():
            self.form.removeRow(0)
        if record is None:
            return
        layout = self.document.layout(record)
        self.form.addRow(styled_label(record.kind, self.widget(), heading="caption"))
        for name, value in record.values.items():
            self.form.addRow(name, self._editor(layout, name, value))
        for link in layout.links:
            if not link.record:
                text = record.links.get(link.name)
                edit = QLineEdit(text.value if isinstance(text, Text) else "", self.widget())
                edit.setProperty("field", link.name)
                edit.editingFinished.connect(self._text_edited)
                self.form.addRow(link.name, edit)

    def _editor(self, layout, name: str, value) -> QWidget:
        members = self.enums.members(layout.enums.get(name, ()))
        if members and isinstance(value, int):
            editor = QComboBox(self.widget())
            for label, code in sorted(members.items(), key=lambda item: item[1]):
                editor.addItem(label, code)
            if editor.findData(value) < 0:
                editor.addItem(str(value), value)
            editor.setCurrentIndex(editor.findData(value))
            editor.currentIndexChanged.connect(self._enum_changed)
        elif isinstance(value, tuple):
            editor = QWidget(self.widget())
            row = QHBoxLayout(editor)
            row.setContentsMargins(0, 0, 0, 0)
            for lane, item in enumerate(value):
                row.addWidget(self._number(float(item), name, lane))
        elif isinstance(value, float):
            editor = self._number(value, name)
        else:
            editor = QSpinBox(self.widget())
            low, high = layout.codec.limits.get(name, (-(1 << 31), (1 << 31) - 1))
            editor.setRange(max(low, -(1 << 31)), min(high, (1 << 31) - 1))
            editor.setValue(int(value))
            editor.editingFinished.connect(self._int_edited)
        editor.setProperty("field", name)
        return editor

    def _number(self, value: float, name: str, lane: int = -1) -> QDoubleSpinBox:
        editor = QDoubleSpinBox(self.widget())
        editor.setRange(-1e9, 1e9)
        editor.setDecimals(6)
        editor.setValue(value)
        editor.setProperty("field", name)
        editor.setProperty("lane", lane)
        editor.editingFinished.connect(self._float_edited)
        return editor

    def _enum_changed(self, _index: int) -> None:
        sender = self.sender()
        self.edits.set_value(self.record, sender.property("field"), sender.currentData(), raw=True)

    def _int_edited(self) -> None:
        sender = self.sender()
        self.edits.set_value(self.record, sender.property("field"), sender.value(), raw=True)

    def _float_edited(self) -> None:
        sender = self.sender()
        name, lane = sender.property("field"), sender.property("lane")
        value = sender.value()
        if lane >= 0:
            current = list(self.record.values[name])
            current[lane] = value
            value = tuple(current)
        self.edits.set_value(self.record, name, value, raw=True)

    def _text_edited(self) -> None:
        sender = self.sender()
        self.edits.set_text(self.record, sender.property("field"), sender.text(), raw=True)

    def _on_changed(self, change: Change) -> None:
        if change.target is not self.record:
            return
        for editor in self.widget().findChildren(QWidget):
            if (name := editor.property("field")) is None:
                continue
            value = self.record.values.get(name)
            with QSignalBlocker(editor):
                if isinstance(editor, QComboBox):
                    editor.setCurrentIndex(max(editor.findData(value), 0))
                elif isinstance(editor, QDoubleSpinBox):
                    editor.setValue(float(value[editor.property("lane")] if editor.property("lane") >= 0 else value))
                elif isinstance(editor, QSpinBox):
                    editor.setValue(int(value))
                elif isinstance(editor, QLineEdit):
                    text = self.record.links.get(name)
                    editor.setText(text.value if isinstance(text, Text) else "")
