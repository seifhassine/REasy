"""See docs/internals/efcsv-format.md, Editing."""
from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QItemSelectionModel, QModelIndex, Qt, QTimer, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QHeaderView, QTableView, QVBoxLayout, QWidget

lazy from file_handlers.efx.expression import number_text
lazy from ui.editor_widgets import editor_strip, styled_label, tool_button, toolbar_separator

from .document import EfcsvDocument


class TableModel(QAbstractTableModel):
    refused = Signal(str)

    def __init__(self, document: EfcsvDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document = document
        document.changed.connect(self._changed)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.document.table.rows)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else self.document.table.columns

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        return str(section) if role == Qt.ItemDataRole.DisplayRole else None

    def flags(self, index: QModelIndex):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEditable

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return number_text(self.document.table.rows[index.row()][index.column()])
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        try:
            number = float(value)
        except ValueError:
            self.refused.emit(self.tr("Type a number."))
            return False
        try:
            self.document.set_value(index.row(), index.column(), number)
        except ValueError as error:
            self.refused.emit(str(error))
            return False
        return True

    def _changed(self, _change=None) -> None:
        self.beginResetModel()
        self.endResetModel()


class EfcsvEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, document: EfcsvDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self._current = document, (0, 0)
        self.model = TableModel(document, self)
        self.table = QTableView(self)
        self.table.setModel(self.model)
        self.table.setEditTriggers(QTableView.EditTrigger.DoubleClicked | QTableView.EditTrigger.EditKeyPressed |
                                   QTableView.EditTrigger.AnyKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(self.table, 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(lambda: self.status.setText(""))
        for keys, slot, widget in ((QKeySequence.StandardKey.Undo, document.undo_stack.undo, self),
                                   (QKeySequence.StandardKey.Redo, document.undo_stack.redo, self),
                                   (QKeySequence.StandardKey.Delete, self._delete, self.table)):
            QShortcut(keys, widget, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut if widget is self
                                                     else Qt.ShortcutContext.WidgetShortcut)
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        self.model.refused.connect(self._refused)
        self.model.modelAboutToBeReset.connect(self._keep)
        self.model.modelReset.connect(self._restore)

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        stack = self.document.undo_stack
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo)
        undo.setEnabled(stack.canUndo())
        redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(undo.setEnabled)
        stack.canRedoChanged.connect(redo.setEnabled)
        add = tool_button("add", self.tr("Add a row after the selection"), self._add, text=self.tr("Add"))
        delete = tool_button("delete", self.tr("Delete the selected rows (Del)"), self._delete)
        self.status = styled_label(tone="danger")
        for widget in (undo, redo, toolbar_separator(), add, delete):
            row.addWidget(widget)
        row.addWidget(self.status, 1)
        return strip

    def selected_rows(self) -> list[int]:
        return sorted({index.row() for index in self.table.selectionModel().selectedIndexes()})

    def _add(self) -> None:
        rows = self.selected_rows()
        at = rows[-1] + 1 if rows else len(self.document.table.rows)
        self.document.insert(at)
        self.table.setCurrentIndex(self.model.index(at, 0))

    def _delete(self) -> None:
        if rows := self.selected_rows():
            self.document.remove(rows)

    def _keep(self) -> None:
        current = self.table.currentIndex()
        self._current = (current.row(), current.column()) if current.isValid() else self._current

    def _restore(self) -> None:
        rows, columns = self.model.rowCount(), self.model.columnCount()
        if rows and columns:
            index = self.model.index(min(self._current[0], rows - 1), min(self._current[1], columns - 1))
            self.table.selectionModel().setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _refused(self, text: str) -> None:
        self.status.setText(text)
        self._clear.start()
