from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt, Signal, QModelIndex, QAbstractTableModel
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

lazy from ui.editor_widgets import styled_label, tool_button
from .mcambank_file import ErrFlags, MotionCameraBankElement, McambankFile


def _field_label(text: str) -> QLabel:
    label = styled_label(text, tone="muted")
    label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    return label


class MotionCameraTableModel(QAbstractTableModel):
    HEADERS = ("Path", "BankID", "BankType", "MaskBit")

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._items: list[MotionCameraBankElement] = []
        self._has_bank = False

    def set_bank(self, bank: Optional[McambankFile]):
        self.beginResetModel()
        self._has_bank = bank is not None
        self._items = [] if bank is None else bank.items
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        if parent.isValid():
            return 0
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole):
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            if 0 <= section < len(self.HEADERS):
                return self.HEADERS[section]
            return None
        return section + 1

    def flags(self, index: QModelIndex):
        base = super().flags(index)
        if not index.isValid():
            return base
        return base | Qt.ItemIsEditable

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._items)):
            return None

        entry = self._items[index.row()]

        if role == Qt.TextAlignmentRole and index.column():
            return int(Qt.AlignRight | Qt.AlignVCenter)
        if role in (Qt.DisplayRole, Qt.EditRole):
            column = index.column()
            if column == 0:
                return entry.path
            if column == 1:
                return str(entry.bank_id)
            if column == 2:
                return str(entry.bank_type)
            if column == 3:
                return str(entry.bank_type_mask_bit)
        return None

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole):
        if role != Qt.EditRole or not index.isValid():
            return False
        if not (0 <= index.row() < len(self._items)):
            return False

        entry = self._items[index.row()]
        column = index.column()

        if column == 0:
            entry.path = str(value)
        else:
            attribute = {
                1: "bank_id",
                2: "bank_type",
                3: "bank_type_mask_bit",
            }.get(column)
            if attribute is None:
                return False
            try:
                parsed = int(value, 0) if isinstance(value, str) else int(value)
            except (TypeError, ValueError):
                return False
            if not 0 <= parsed <= 0xFFFFFFFF:
                return False
            setattr(entry, attribute, parsed)

        self.dataChanged.emit(index, index, [Qt.DisplayRole, Qt.EditRole])
        return True

    def insertRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()):
        if parent.isValid() or not self._has_bank:
            return False
        if count <= 0:
            return False
        row = max(0, min(row, len(self._items)))
        self.beginInsertRows(QModelIndex(), row, row + count - 1)
        for offset in range(count):
            self._items.insert(row + offset, MotionCameraBankElement())
        self.endInsertRows()
        return True

    def removeRows(self, row: int, count: int, parent: QModelIndex = QModelIndex()):
        if parent.isValid() or not self._has_bank:
            return False
        if count <= 0 or row < 0 or row >= len(self._items):
            return False
        last = min(row + count, len(self._items)) - 1
        self.beginRemoveRows(QModelIndex(), row, last)
        del self._items[row:last + 1]
        self.endRemoveRows()
        return True


class McambankViewer(QWidget):
    modified_changed = Signal(bool)

    FLAG_ORDER: Iterable[tuple[ErrFlags, str]] = (
        (ErrFlags.EMPTY, QT_TRANSLATE_NOOP("McambankViewer", "Empty")),
        (
            ErrFlags.NOT_FOUND_REF_ASSET,
            QT_TRANSLATE_NOOP("McambankViewer", "Missing Ref Asset"),
        ),
        (
            ErrFlags.NOT_FOUND_INCLUDE_ASSET,
            QT_TRANSLATE_NOOP("McambankViewer", "Missing Include Asset"),
        ),
    )

    def __init__(self, handler):
        super().__init__()
        self.handler = handler
        self._modified = False
        self._populating = False
        self.flag_checkboxes: dict[ErrFlags, QCheckBox] = {}
        self._setup_ui()
        self._populate()

    @property
    def modified(self) -> bool:
        return self._modified

    @modified.setter
    def modified(self, value: bool):
        if self._modified != value:
            self._modified = value
            self.modified_changed.emit(value)

    def _setup_ui(self):
        self.setObjectName("mcambankViewer")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header_group = QGroupBox(self.tr("Header"))
        header_layout = QGridLayout(header_group)
        header_layout.setHorizontalSpacing(8)
        header_layout.setVerticalSpacing(6)

        header_layout.addWidget(_field_label("Version:"), 0, 0)
        self.version_spin = QSpinBox()
        self.version_spin.setRange(0, 0xFFFF)
        self.version_spin.valueChanged.connect(self._on_version_changed)
        header_layout.addWidget(self.version_spin, 0, 1)

        header_layout.addWidget(_field_label("Master Size:"), 0, 2)
        self.master_size_edit = QLineEdit()
        self.master_size_edit.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.master_size_edit.setPlaceholderText("0")
        self.master_size_edit.setMaximumWidth(160)
        self.master_size_edit.editingFinished.connect(self._on_master_size_changed)
        header_layout.addWidget(self.master_size_edit, 0, 3)
        header_layout.setColumnStretch(4, 1)

        header_layout.addWidget(_field_label("Err Flags:"), 1, 0)
        flags_widget = QWidget()
        flags_layout = QHBoxLayout(flags_widget)
        flags_layout.setContentsMargins(0, 0, 0, 0)
        flags_layout.setSpacing(12)
        for flag, label in self.FLAG_ORDER:
            cb = QCheckBox(self.tr(label))
            cb.stateChanged.connect(self._on_err_flag_changed)
            flags_layout.addWidget(cb)
            self.flag_checkboxes[flag] = cb
        flags_layout.addStretch()
        header_layout.addWidget(flags_widget, 1, 1, 1, 4)

        layout.addWidget(header_group)

        path_group = QGroupBox(self.tr("External Resources"))
        path_layout = QGridLayout(path_group)
        path_layout.setHorizontalSpacing(8)
        path_layout.setVerticalSpacing(6)
        path_layout.setColumnStretch(1, 1)

        path_layout.addWidget(_field_label("User Variables:"), 0, 0)
        self.uvar_edit = QLineEdit()
        self.uvar_edit.textChanged.connect(self._on_uvar_changed)
        path_layout.addWidget(self.uvar_edit, 0, 1)

        path_layout.addWidget(_field_label("Joint Map:"), 1, 0)
        self.jmap_edit = QLineEdit()
        self.jmap_edit.textChanged.connect(self._on_jmap_changed)
        path_layout.addWidget(self.jmap_edit, 1, 1)

        layout.addWidget(path_group)

        self.table_model = MotionCameraTableModel(self)
        self.table_model.dataChanged.connect(self._on_model_modified)
        self.table_model.rowsInserted.connect(self._on_model_rows_changed)
        self.table_model.rowsRemoved.connect(self._on_model_rows_changed)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(2)
        self.add_btn = tool_button("add", slot=self._on_add, text=self.tr("Add"))
        self.del_btn = tool_button("delete", slot=self._on_delete, text=self.tr("Delete"))
        btn_row.addWidget(self.add_btn)
        btn_row.addWidget(self.del_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

        self.table = QTableView()
        self.table.setModel(self.table_model)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(MotionCameraTableModel.HEADERS)):
            header.setSectionResizeMode(col, QHeaderView.Interactive)
            header.resizeSection(col, 110)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        self.table.selectionModel().selectionChanged.connect(self._update_delete_button)
        layout.addWidget(self.table, 1)
        self._update_delete_button()

    def _update_delete_button(self, *_args):
        self.del_btn.setEnabled(self.table.selectionModel().hasSelection())

    def _populate(self):
        bank = self.handler.mcambank
        if not bank:
            return

        self._populating = True
        try:
            self.version_spin.blockSignals(True)
            self.version_spin.setValue(bank.version)
            self.version_spin.blockSignals(False)

            self.master_size_edit.blockSignals(True)
            self.master_size_edit.setText(str(bank.master_size))
            self.master_size_edit.blockSignals(False)

            combined_flags = bank.err_flags
            for flag, checkbox in self.flag_checkboxes.items():
                checkbox.blockSignals(True)
                checkbox.setChecked(bool(combined_flags & flag))
                checkbox.blockSignals(False)

            self.uvar_edit.blockSignals(True)
            self.uvar_edit.setText(bank.user_variables_path)
            self.uvar_edit.blockSignals(False)

            self.jmap_edit.blockSignals(True)
            self.jmap_edit.setText(bank.joint_map_path)
            self.jmap_edit.blockSignals(False)

            self.table_model.set_bank(bank)
        finally:
            self._populating = False

        self.modified = False
        self.handler.modified = False

    def _mark_modified(self):
        self.modified = True
        self.handler.modified = True

    def _on_version_changed(self, value: int):
        if self._populating or not self.handler.mcambank:
            return
        self.handler.mcambank.version = int(value)
        self._mark_modified()

    def _on_err_flag_changed(self, _state: int):
        if self._populating or not self.handler.mcambank:
            return
        combined = ErrFlags.NONE
        for flag, checkbox in self.flag_checkboxes.items():
            if checkbox.isChecked():
                combined |= flag
        self.handler.mcambank.err_flags = combined
        self._mark_modified()

    def _on_master_size_changed(self):
        bank = self.handler.mcambank
        if self._populating or not bank:
            return

        text = self.master_size_edit.text().strip()
        if not text:
            value = 0
        else:
            try:
                value = int(text, 0)
            except ValueError:
                self._restore_master_size(bank)
                return

        if not 0 <= value <= 0xFFFFFFFF:
            self._restore_master_size(bank)
            return

        if bank.master_size != value:
            bank.master_size = value
            self._mark_modified()

        self.master_size_edit.blockSignals(True)
        self.master_size_edit.setText(str(bank.master_size))
        self.master_size_edit.blockSignals(False)

    def _restore_master_size(self, bank: McambankFile):
        self.master_size_edit.blockSignals(True)
        self.master_size_edit.setText(str(bank.master_size))
        self.master_size_edit.blockSignals(False)

    def _on_uvar_changed(self, text: str):
        if self._populating or not self.handler.mcambank:
            return
        self.handler.mcambank.user_variables_path = text
        self._mark_modified()

    def _on_jmap_changed(self, text: str):
        if self._populating or not self.handler.mcambank:
            return
        self.handler.mcambank.joint_map_path = text
        self._mark_modified()

    def _on_model_modified(self, top_left: QModelIndex, bottom_right: QModelIndex):
        if self._populating or not self.handler.mcambank:
            return
        if top_left.isValid() and bottom_right.isValid():
            self._mark_modified()

    def _on_model_rows_changed(self, *_args):
        if self._populating or not self.handler.mcambank:
            return
        self._mark_modified()

    def _on_add(self):
        bank = self.handler.mcambank
        if not bank:
            return
        row = self.table_model.rowCount()
        if self.table_model.insertRows(row, 1):
            index = self.table_model.index(row, 0)
            self.table.setCurrentIndex(index)
            self.table.edit(index)

    def _on_delete(self):
        bank = self.handler.mcambank
        if not bank:
            return
        selection = self.table.selectionModel()
        if not selection:
            return
        rows = sorted({index.row() for index in selection.selectedIndexes()}, reverse=True)
        if not rows:
            return
        for row in rows:
            self.table_model.removeRows(row, 1)
