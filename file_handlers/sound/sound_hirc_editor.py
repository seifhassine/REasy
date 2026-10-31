"""Event action picker and the Wwise property dialog for HIRC objects."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .sound_metadata import SoundMetadata
lazy from .sound_property_editor import object_editor
lazy from ui.editor_widgets import styled_label
lazy from ui.icons import line_icon
lazy from ui.styles import set_style_role


def _button(text, icon):
    button = QPushButton(text)
    button.setIcon(line_icon(icon))
    set_style_role(button, compact=True)
    return button


def _resize_within_screen(dialog, width, height):
    screen = dialog.screen()
    if screen is not None:
        height = min(height, int(screen.availableGeometry().height() * 0.85))
    dialog.resize(width, height)


class ActionPickerDialog(QDialog):
    """Order-preserving Event Action picker."""

    def __init__(self, actions, objects, selected_ids=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(self.tr("Choose Event Actions"))
        self.resize(760, 430)
        self._actions = {action.object_id: action for action in actions}
        self._objects = {obj.object_id: obj for obj in objects}
        self._metadata = getattr(parent, "_sound_metadata", SoundMetadata())

        layout = QVBoxLayout(self)
        hint = styled_label(self.tr("Actions run from top to bottom. Add existing Actions, then reorder them."), tone="muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        columns = QHBoxLayout()
        columns.setSpacing(8)
        available = QVBoxLayout()
        available.setSpacing(4)
        available.addWidget(styled_label(self.tr("Available"), heading="caption"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText(self.tr("Filter actions"))
        self.filter_edit.setClearButtonEnabled(True)
        available.addWidget(self.filter_edit)
        self.available_list = QListWidget()
        self.available_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        available.addWidget(self.available_list)
        columns.addLayout(available, 1)

        moves = QVBoxLayout()
        moves.setSpacing(4)
        moves.addStretch()
        self.add_button = _button(self.tr("Add"), "arrow_right")
        self.add_button.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.remove_button = _button(self.tr("Remove"), "arrow_left")
        for button in (self.add_button, self.remove_button):
            button.setMinimumWidth(92)
            moves.addWidget(button)
        moves.addStretch()
        columns.addLayout(moves)

        selected = QVBoxLayout()
        selected.setSpacing(4)
        selected.addWidget(styled_label(self.tr("Used by event"), heading="caption"))
        self.selected_list = QListWidget()
        self.selected_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        selected.addWidget(self.selected_list)
        order = QHBoxLayout()
        order.setSpacing(4)
        self.up_button = _button(self.tr("Move Up"), "arrow_up")
        self.down_button = _button(self.tr("Move Down"), "arrow_down")
        order.addWidget(self.up_button)
        order.addWidget(self.down_button)
        order.addStretch()
        selected.addLayout(order)
        columns.addLayout(selected, 1)
        layout.addLayout(columns)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        for action_id in selected_ids:
            self.selected_list.addItem(self._item(action_id))
        self._refill_available()
        self.filter_edit.textChanged.connect(self._refill_available)
        self.add_button.clicked.connect(self._add_selected)
        self.remove_button.clicked.connect(self._remove_selected)
        self.up_button.clicked.connect(lambda: self._move_selected(-1))
        self.down_button.clicked.connect(lambda: self._move_selected(1))
        self.available_list.itemDoubleClicked.connect(lambda _item: self._add_selected())
        self.selected_list.itemDoubleClicked.connect(lambda _item: self._remove_selected())

    def _label(self, action_id: int) -> str:
        action = self._actions.get(action_id)
        if action is None:
            return self.tr("Missing Action {id}").format(id=action_id)
        settings = action.settings
        if action.target_kind in {"state", "switch"} and settings:
            group_kind = f"{action.target_kind}_group"
            value_kind = f"{action.target_kind}_value"
            target = (
                f"{self._metadata.label(group_kind, settings.group_id or 0)} → "
                f"{self._metadata.label(value_kind, settings.value_id or 0)}"
            )
        elif action.target_kind == "game_parameter":
            target = self._metadata.id_label("game_parameter", action.raw_id)
        elif action.target_kind == "trigger":
            target = self._metadata.id_label("trigger", action.raw_id)
        elif action.target_kind == "event":
            target = self._metadata.id_label("event", action.target_id, event=True)
        elif action.target_kind == "all":
            target = self.tr("All objects")
        else:
            obj = self._objects.get(action.target_id)
            target = (
                f"{obj.type_name} {action.target_id}" if obj else
                self._metadata.external_object_label(action.target_id)
            )
        return self.tr("{name} → {target}  (Action {action_id})").format(
            name=action.action_name,
            target=target,
            action_id=action.object_id,
        )

    def _item(self, action_id: int) -> QListWidgetItem:
        item = QListWidgetItem(self._label(action_id))
        item.setData(Qt.UserRole, int(action_id))
        return item

    def selected_action_ids(self) -> tuple[int, ...]:
        return tuple(
            int(self.selected_list.item(row).data(Qt.UserRole))
            for row in range(self.selected_list.count())
        )

    def _refill_available(self):
        used = set(self.selected_action_ids())
        needle = self.filter_edit.text().strip().casefold()
        self.available_list.clear()
        for action_id in self._actions:
            item = self._item(action_id)
            if action_id not in used and (not needle or needle in item.text().casefold()):
                self.available_list.addItem(item)

    @staticmethod
    def _selected_rows(widget: QListWidget) -> list[int]:
        rows = sorted({widget.row(item) for item in widget.selectedItems()})
        return rows or ([widget.currentRow()] if widget.currentRow() >= 0 else [])

    def _add_selected(self):
        for row in self._selected_rows(self.available_list):
            self.selected_list.addItem(self._item(self.available_list.item(row).data(Qt.UserRole)))
        self._refill_available()

    def _remove_selected(self):
        for row in reversed(self._selected_rows(self.selected_list)):
            self.selected_list.takeItem(row)
        self._refill_available()

    def _move_selected(self, direction: int):
        rows = self._selected_rows(self.selected_list)
        if not rows or rows[0] + direction < 0 or rows[-1] + direction >= self.selected_list.count():
            return
        ids = list(self.selected_action_ids())
        for row in rows if direction < 0 else reversed(rows):
            ids[row], ids[row + direction] = ids[row + direction], ids[row]
        self.selected_list.clear()
        for action_id in ids:
            self.selected_list.addItem(self._item(action_id))
        for row in (value + direction for value in rows):
            self.selected_list.item(row).setSelected(True)
        self.selected_list.setCurrentRow(rows[0] + direction)


def _number(text, label, *, bits=32, allow_zero=True):
    value = str(text).strip()
    try:
        parsed = int(value, 0)
    except ValueError:
        parsed = int(value, 10)
    limit = (1 << bits) - 1
    if parsed < 0 or parsed > limit or (not allow_zero and parsed == 0):
        raise ValueError(f"{label} must be between {0 if allow_zero else 1} and {limit}.")
    return parsed


class RowsEditor(QWidget):
    """Compact add/remove table shared by the structured property dialogs."""

    def __init__(self, headers, rows=(), defaults=(), parent=None, *, fixed=False):
        super().__init__(parent)
        self.defaults = tuple(map(str, defaults))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        layout.addWidget(self.table)
        if not fixed:
            buttons = QHBoxLayout()
            buttons.setSpacing(4)
            add = _button(self.tr("Add Row"), "add")
            remove = _button(self.tr("Remove Row"), "remove")
            up, down = _button(self.tr("Move Up"), "arrow_up"), _button(self.tr("Move Down"), "arrow_down")
            add.clicked.connect(lambda: self.add_row(self.defaults))
            remove.clicked.connect(self.remove_rows)
            up.clicked.connect(lambda: self.move_row(-1))
            down.clicked.connect(lambda: self.move_row(1))
            buttons.addWidget(add)
            buttons.addWidget(remove)
            buttons.addWidget(up)
            buttons.addWidget(down)
            buttons.addStretch()
            layout.addLayout(buttons)
        for index, row in enumerate(rows):
            self.add_row(row)
            self.table.item(index, 0).setData(Qt.ItemDataRole.UserRole, index)

    def origins(self):
        return [
            item.data(Qt.ItemDataRole.UserRole) if (item := self.table.item(row, 0)) else None
            for row in range(self.table.rowCount())
        ]

    def add_row(self, values=()):
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column in range(self.table.columnCount()):
            value = values[column] if column < len(values) else ""
            self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def remove_rows(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows and self.table.currentRow() >= 0:
            rows = [self.table.currentRow()]
        for row in rows:
            self.table.removeRow(row)

    def move_row(self, direction):
        row, target = self.table.currentRow(), self.table.currentRow() + direction
        if row < 0 or target < 0 or target >= self.table.rowCount():
            return
        for column in range(self.table.columnCount()):
            first, second = self.table.takeItem(row, column), self.table.takeItem(target, column)
            first_widget, second_widget = self.table.cellWidget(row, column), self.table.cellWidget(target, column)
            if first_widget:
                self.table.removeCellWidget(row, column)
            if second_widget:
                self.table.removeCellWidget(target, column)
            if second:
                self.table.setItem(row, column, second)
            if first:
                self.table.setItem(target, column, first)
            if second_widget:
                self.table.setCellWidget(row, column, second_widget)
            if first_widget:
                self.table.setCellWidget(target, column, first_widget)
        self.table.selectRow(target)

    def values(self):
        return [
            [
                self.table.item(row, column).text().strip()
                if self.table.item(row, column)
                else ""
                for column in range(self.table.columnCount())
            ]
            for row in range(self.table.rowCount())
        ]


class HircPropertiesDialog(QDialog):
    def __init__(self, obj, parent=None, objects=()):
        super().__init__(parent)
        if not objects and parent is not None and getattr(parent, "_parse_result", None):
            objects = parent._parse_result.objects
        self.obj, self._payload = obj, None
        self.setWindowTitle(self.tr("{type} {id}").format(type=obj.type_name, id=obj.object_id))
        _resize_within_screen(self, 1040, 780)
        layout = QVBoxLayout(self)
        self.editor = None
        if obj.structure is not None and obj.structure.complete:
            self.editor = object_editor(obj, objects, getattr(parent, "_sound_metadata", SoundMetadata()), self)
            layout.addWidget(self.editor, 1)
        else:
            note = styled_label(self.tr("This object's layout is not decoded; its bytes are kept as they are."), self, tone="muted")
            note.setWordWrap(True)
            layout.addWidget(note, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def payload(self):
        return self.editor.payload() if self.editor is not None else self.obj.payload

    def accept(self):
        self._payload = self.payload()
        super().accept()

    def edited_payload(self):
        return self._payload
