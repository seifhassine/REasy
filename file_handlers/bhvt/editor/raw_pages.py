"""See docs/internals/bhvt-editor.md, Raw view."""

from __future__ import annotations

import copy
import uuid
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QScrollArea,
                               QScrollBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

lazy from ui.editor_widgets import editor_strip, tool_button

from file_handlers.document_commands import ReplaceContents

from ..document import BhvtDocument
from ..edits import MANAGED, READ_ONLY, nested_codec, set_attribute, set_fields, shown_fields
from ..model import Record, Tree
from ..references import own_variables, variable_references, variables
from ..rules import display_names, rules_of

_DATA = (int, float, str, bytes, list, Record, type(None))


def nested(value) -> bool:
    return isinstance(value, list) and bool(value) and isinstance(value[0], (list, Record))


def show_value(value) -> str:
    if isinstance(value, bytes):
        return str(uuid.UUID(bytes_le=value))
    if not isinstance(value, list):
        return "" if value is None else str(value)
    if nested(value):
        return QCoreApplication.translate("BhvtEditor", "{0} entries").format(len(value))
    return "/".join(value) if value and isinstance(value[0], str) else " ".join(map(show_value, value))


def parse_value(text: str, current):
    if isinstance(current, list):
        if current and isinstance(current[0], str):
            return text.split("/")
        return [parse_value(part, current[0] if current else 0) for part in text.replace(",", " ").split()]
    if isinstance(current, bytes):
        return uuid.UUID(text).bytes_le
    return (text or None) if current is None else type(current)(text)


def _boxed(title: str, table: _Table) -> QGroupBox:
    box = QGroupBox(title)
    QVBoxLayout(box).addWidget(TablePage(table))
    return box


def _row(blank) -> object:
    return Record(copy.deepcopy(blank)) if isinstance(blank, dict) else copy.deepcopy(blank)


class RecordInspector(QScrollArea):
    def __init__(self, document: BhvtDocument, record: object, owner: object | None = None, codec=None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self.owner = document, owner
        self.setWidgetResizable(True)
        body = QWidget()
        self.setWidget(body)
        self._fill(QFormLayout(body), record, codec, {} if codec is None else codec.blanks)

    def _fill(self, form: QFormLayout, record: object, codec, blanks: dict) -> None:
        if isinstance(vars(record).get("name"), str):
            form.addRow(self.tr("Name"), self._name(record))
        for entry in shown_fields(record, codec):
            if isinstance(entry, tuple):
                form.addRow(_boxed(", ".join(entry), GroupTable(self.document, record, entry, blanks)))
                continue
            value = getattr(record, entry)
            if not isinstance(value, _DATA):
                continue
            if entry in READ_ONLY:
                form.addRow(entry, QLabel(show_value(value)))
            elif entry == "attribute" and isinstance(self.owner, Tree):
                form.addRow(entry, self._flags(record))
            elif isinstance(value, Record):
                box = QGroupBox(entry)
                inner = nested_codec(self.owner, entry)
                self._fill(QFormLayout(box), value, None, {} if inner is None else inner.blanks)
                form.addRow(box)
            elif nested(value) or isinstance(blanks.get(entry), dict):
                form.addRow(_boxed(entry, RecordTable(self.document, record, entry, blanks.get(entry))))
            else:
                form.addRow(entry, self._line(record, entry))

    def _name(self, record: object) -> QLineEdit:
        edit = QLineEdit(record.name)
        edit.editingFinished.connect(lambda: edit.text() != record.name and set_fields(
            self.document, record, self.tr("Rename"), name=edit.text()))
        return edit

    def _line(self, record: object, field: str) -> QLineEdit:
        edit = QLineEdit(show_value(getattr(record, field)))

        def commit():
            try:
                new = parse_value(edit.text(), getattr(record, field))
            except ValueError:
                edit.setText(show_value(getattr(record, field)))
                return
            if new != getattr(record, field):
                set_fields(self.document, record, self.tr("Edit {0}").format(field), **{field: new})
        edit.editingFinished.connect(commit)
        return edit

    def _flags(self, record: object) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        for bit, name in display_names("NodeAttribute", self.owner.version).items():
            check = QCheckBox(name)
            check.setChecked(bool(record.attribute & bit))
            check.toggled.connect(lambda on, bit=bit: set_attribute(
                self.document, self.owner, record, record.attribute | bit if on else record.attribute & ~bit))
            row.addWidget(check)
        return box


class _Table(QTableWidget):
    resizable = True

    def __init__(self, document: BhvtDocument, columns: list[str], rows: list[dict], fixed: set[str],
                 parent: QWidget | None = None):
        super().__init__(len(rows), len(columns), parent)
        self.document, self.columns = document, columns
        self.setHorizontalHeaderLabels(columns)
        for row, values in enumerate(rows):
            for column, name in enumerate(columns):
                cell = QTableWidgetItem(show_value(values.get(name)))
                if name not in values or name in fixed or nested(values[name]):
                    cell.setFlags(cell.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.setItem(row, column, cell)
        self.setMinimumHeight(self.horizontalHeader().sizeHint().height() + 2 * self.frameWidth() +
                              self.verticalHeader().defaultSectionSize() * min(max(len(rows), 1), 8))
        self.cellChanged.connect(self._commit)

    def selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.selectedIndexes()})

    def _commit(self, row: int, column: int) -> None:
        raise NotImplementedError


class RecordTable(_Table):
    def __init__(self, document: BhvtDocument, owner: object, field: str, blank=None, parent: QWidget | None = None):
        items = getattr(owner, field)
        rows = [vars(item) if isinstance(item, Record) else {field: item} for item in items]
        columns = list(dict.fromkeys(name for row in rows for name in row))
        super().__init__(document, columns or (list(blank) if isinstance(blank, dict) else [field]), rows, set(),
                         parent)
        self.owner, self.field, self.blank, self.items = owner, field, blank, items
        self.resizable = not (isinstance(owner, Tree) and field in rules_of(owner).graph["actions"]["blocks"].values())

    def add_rows(self) -> None:
        if self.items:
            added = [copy.deepcopy(self.items[row]) for row in self.selected_rows() or [len(self.items) - 1]]
        elif self.blank is not None:
            added = [_row(self.blank)]
        else:
            return
        self._replace([*self.items, *added], self.tr("Add rows"))

    def remove_rows(self) -> None:
        if rows := set(self.selected_rows()):
            self._replace([item for row, item in enumerate(self.items) if row not in rows], self.tr("Remove rows"))

    def _replace(self, contents: list, text: str) -> None:
        self.document.push(ReplaceContents(self.document, self.owner, self.field, contents, text))

    def _commit(self, row: int, column: int) -> None:
        target, name = self.items[row], self.columns[column]
        current = getattr(target, name) if isinstance(target, Record) else target
        try:
            new = parse_value(self.item(row, column).text(), current)
        except ValueError:
            self.item(row, column).setText(show_value(current))
            return
        if new == current:
            return
        if isinstance(target, Record):
            set_fields(self.document, target, self.tr("Edit {0}").format(name), **{name: new})
        else:
            self._replace([new if at == row else item for at, item in enumerate(self.items)],
                          self.tr("Edit {0}").format(name))


class GroupTable(_Table):
    def __init__(self, document: BhvtDocument, record: object, members: tuple[str, ...], blanks: dict,
                 parent: QWidget | None = None):
        count = len(getattr(record, members[0]))
        rows = [{name: getattr(record, name)[row] for name in members} for row in range(count)]
        super().__init__(document, list(members), rows, MANAGED, parent)
        self.record, self.members, self.blanks = record, members, blanks
        self.resizable = not MANAGED & set(members)

    def add_rows(self) -> None:
        lists = {name: getattr(self.record, name) for name in self.members}
        rows = self.selected_rows() or [len(lists[self.members[0]]) - 1]
        self._set({name: [*items, *(copy.deepcopy(items[row]) if row >= 0 else _row(self.blanks[name])
                                    for row in rows)] for name, items in lists.items()}, self.tr("Add rows"))

    def remove_rows(self) -> None:
        if rows := set(self.selected_rows()):
            self._set({name: [item for at, item in enumerate(getattr(self.record, name)) if at not in rows]
                       for name in self.members}, self.tr("Remove rows"))

    def _set(self, values: dict, text: str) -> None:
        set_fields(self.document, self.record, text, **values)

    def _commit(self, row: int, column: int) -> None:
        name = self.columns[column]
        items = getattr(self.record, name)
        try:
            new = parse_value(self.item(row, column).text(), items[row])
        except ValueError:
            self.item(row, column).setText(show_value(items[row]))
            return
        if new != items[row]:
            self._set({name: [new if at == row else item for at, item in enumerate(items)]},
                      self.tr("Edit {0}").format(name))


class VariableTable(_Table):
    resizable = False

    def __init__(self, document: BhvtDocument, model: object, parent: QWidget | None = None):
        own = own_variables(model)
        labels = {id(own[0]): "variable", **{id(uvar): f"base_variables {index}" for index, uvar in enumerate(own[1:])},
                  **{id(uvar): Path(path).name for path, uvar in document.owner_variables.items()}}
        found = variables(model, document.owner_variables.values())
        rows = [{"guid": show_value(guid), **({"variable": found[guid][1].name, "uvar": labels[id(found[guid][0])]}
                                              if guid in found else {})} for guid in variable_references(model)]
        super().__init__(document, ["guid", "variable", "uvar"], rows, {"guid", "variable", "uvar"}, parent)


class TablePage(QWidget):
    def __init__(self, table: _Table, parent: QWidget | None = None):
        super().__init__(parent)
        self.table = table
        strip, row = editor_strip()
        for icon, text, slot in (("add", self.tr("Add rows (copies of the selected rows)"), table.add_rows),
                                 ("delete", self.tr("Remove rows"), table.remove_rows)):
            button = tool_button(icon, text, slot)
            button.setEnabled(table.resizable)
            row.addWidget(button)
        row.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(strip)
        layout.addWidget(table, 1)

    def verticalScrollBar(self) -> QScrollBar:
        return self.table.verticalScrollBar()
