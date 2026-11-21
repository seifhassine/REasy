"""See docs/internals/gcp-format.md, Editing."""
from __future__ import annotations

import re
import uuid
from functools import cache

from PySide6.QtCore import (QAbstractTableModel, QEvent, QItemSelection, QItemSelectionModel, QModelIndex,
                            QSortFilterProxyModel, Qt, QTimer, Signal)
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import QApplication, QColorDialog, QHeaderView, QLineEdit, QTableView, QVBoxLayout, QWidget

lazy from ui.editor_widgets import editor_strip, search_field, styled_label, tool_button, toolbar_separator
lazy from ui.styles import current_colors, qcolor

from .document import GcpDocument
from .gcp_file import PATTERNS

_COLOR = re.compile(r"#?((?:[0-9a-fA-F]{2}){3,4})")
_GUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}")


@cache
def _swatch(color: tuple[int, int, int, int]) -> QIcon:
    pixmap = QPixmap(14, 14)
    pixmap.fill(QColor(*color))
    return QIcon(pixmap)


def _hex(color) -> str:
    return "#{:02X}{:02X}{:02X}{:02X}".format(*color)


class PresetModel(QAbstractTableModel):
    refused = Signal(str)

    def __init__(self, document: GcpDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, patterns = document, PATTERNS[document.gcp.version]
        self.columns = ["name", *range(patterns), *(["alpha"] if document.gcp.version == 2 else []), "guid"]
        pick = self.tr("Type #RRGGBB or #RRGGBBAA, or double-click to pick.")
        self.headers = {
            "name": (self.tr("Name"), self.tr("The file keeps only the murmur3 hash of a preset's name; unknown names "
                                              "show as 0x and the hash. Type a name, or 0x and a hash.")),
            "alpha": (self.tr("Enable Alpha"), self.tr("On: the preset's alpha replaces the element's.")),
            "guid": ("GUID", self.tr("Elements refer to a preset by its GUID."))}
        if patterns > 1:
            self.headers.update({pattern: (self.tr("Pattern {0}").format(pattern), self.tr(
                "The colour while the preset's PatternNo is {0} (0 unless a script sets it). ").format(pattern) + pick)
                for pattern in range(patterns)})
        else:
            self.headers[0] = (self.tr("Colour"), pick)
        self._changed()
        document.changed.connect(self._changed)

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.document.presets)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):
        if orientation == Qt.Orientation.Vertical:
            return str(section) if role == Qt.ItemDataRole.DisplayRole else None
        shown = {Qt.ItemDataRole.DisplayRole: 0, Qt.ItemDataRole.ToolTipRole: 1}.get(role)
        return None if shown is None else self.headers[self.columns[section]][shown]

    def flags(self, index: QModelIndex):
        flags = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return flags | (Qt.ItemFlag.ItemIsUserCheckable if self.columns[index.column()] == "alpha"
                        else Qt.ItemFlag.ItemIsEditable)

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        preset, column = self.document.presets[index.row()], self.columns[index.column()]
        if column == "alpha":
            return (Qt.CheckState.Checked if preset.enable_alpha else Qt.CheckState.Unchecked) \
                if role == Qt.ItemDataRole.CheckStateRole else None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            return self.document.name(preset) if column == "name" else str(preset.guid) if column == "guid" else \
                _hex(preset.colors[column])
        if role == Qt.ItemDataRole.DecorationRole and type(column) is int:
            return _swatch(preset.colors[column])
        if role == Qt.ItemDataRole.ToolTipRole and column == "name":
            return f"0x{preset.name_hash:08X}"
        if role == Qt.ItemDataRole.ForegroundRole and column in ("name", "guid"):
            field = "guid" if column == "guid" else "name_hash"
            if getattr(preset, field) in self._clashes[field]:
                return qcolor(current_colors()["danger"])
            if column == "name" and preset.name_hash not in self.document.names:
                return qcolor(current_colors()["text_muted"])
        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:
        document, column = self.document, self.columns[index.column()]
        preset = document.presets[index.row()]
        try:
            if column == "alpha":
                document.set_values(preset, self.tr("Set Enable Alpha"),
                                    enable_alpha=Qt.CheckState(value) == Qt.CheckState.Checked)
            elif column == "name":
                document.rename(preset, value)
            elif column == "guid":
                document.set_values(preset, self.tr("Set GUID"), guid=self._guid(value))
            else:
                colors = list(preset.colors)
                colors[column] = self._color(value, colors[column])
                document.set_values(preset, self.tr("Set colour"), colors=colors)
        except ValueError as error:
            self.refused.emit(str(error))
            return False
        return True

    def _guid(self, text: str) -> uuid.UUID:
        try:
            return uuid.UUID(text.strip())
        except ValueError:
            raise ValueError(self.tr("Type a GUID as 01234567-89ab-cdef-0123-456789abcdef.")) from None

    def _color(self, text: str, held: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        if (found := _COLOR.fullmatch(text.strip())) is None:
            raise ValueError(self.tr("Type a colour as #RRGGBB or #RRGGBBAA."))
        parts = tuple(bytes.fromhex(found.group(1)))
        return parts if len(parts) == 4 else (*parts, held[3])

    def _changed(self, change=None) -> None:
        self._clashes = {field: set(self.document.clashes(field)) for field in ("guid", "name_hash")}
        if change is not None and change.structural:
            self.beginResetModel()
            self.endResetModel()
        elif self.document.presets:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.document.presets) - 1, len(self.columns) - 1))


class GcpEditor(QWidget):
    modified_changed = Signal(bool)

    def __init__(self, document: GcpDocument, parent: QWidget | None = None):
        super().__init__(parent)
        self.document, self._kept = document, []
        self.model = PresetModel(document, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.table = QTableView(self)
        self.table.setModel(self.proxy)
        self.table.setEditTriggers(QTableView.EditTrigger.EditKeyPressed | QTableView.EditTrigger.AnyKeyPressed)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.doubleClicked.connect(self._double_clicked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toolbar())
        layout.addWidget(self.table, 1)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.setInterval(8000)
        self._clear.timeout.connect(self._show_problems)
        for keys, slot, widget in ((QKeySequence.StandardKey.Undo, document.undo_stack.undo, self),
                                   (QKeySequence.StandardKey.Redo, document.undo_stack.redo, self),
                                   (QKeySequence.StandardKey.Delete, self._delete, self.table),
                                   (QKeySequence.StandardKey.Copy, self._copy, self.table),
                                   (QKeySequence.StandardKey.Paste, self._paste, self.table),
                                   ("Ctrl+D", self._duplicate, self.table), ("Alt+Up", self._move_up, self.table),
                                   ("Alt+Down", self._move_down, self.table)):
            QShortcut(keys, widget, slot).setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut if widget is self
                                                     else Qt.ShortcutContext.WidgetShortcut)
        document.undo_stack.cleanChanged.connect(self._clean_changed)
        document.changed.connect(self._show_problems)
        self.model.refused.connect(self._refused)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.proxy.modelReset.connect(self._restore)
        self._selection_changed()
        self._show_problems()

    def event(self, event) -> bool:
        # Accepting ShortcutOverride gives the filter Ctrl+F before the window's own Find.
        if event.type() == QEvent.Type.ShortcutOverride and event.matches(QKeySequence.StandardKey.Find):
            self.filter.setFocus()
            event.accept()
            return True
        return super().event(event)

    def _toolbar(self) -> QWidget:
        strip, row = editor_strip()
        stack = self.document.undo_stack
        undo = tool_button("undo", self.tr("Undo (Ctrl+Z)"), stack.undo)
        redo = tool_button("redo", self.tr("Redo (Ctrl+Y)"), stack.redo)
        undo.setEnabled(stack.canUndo())
        redo.setEnabled(stack.canRedo())
        stack.canUndoChanged.connect(undo.setEnabled)
        stack.canRedoChanged.connect(redo.setEnabled)
        add = tool_button("add", self.tr("Add a preset after the selection"), self._add, text=self.tr("Add"))
        self.selected_only = duplicate, delete, up, down = (
            tool_button("copy", self.tr("Duplicate (Ctrl+D)"), self._duplicate),
            tool_button("delete", self.tr("Delete (Del)"), self._delete),
            tool_button("arrow_up", self.tr("Move up (Alt+Up)"), self._move_up),
            tool_button("arrow_down", self.tr("Move down (Alt+Down)"), self._move_down))
        self.filter = search_field(QLineEdit())
        self.filter.setPlaceholderText(self.tr("Filter by name, colour or GUID"))
        self.filter.setFixedWidth(240)
        self.filter.textChanged.connect(self.proxy.setFilterFixedString)
        self.status = styled_label(tone="danger")
        for widget in (undo, redo, toolbar_separator(), add, duplicate, delete, toolbar_separator(), up, down,
                       toolbar_separator(), self.filter):
            row.addWidget(widget)
        row.addWidget(self.status, 1)
        return strip

    def selected_rows(self) -> list[int]:
        return sorted({self.proxy.mapToSource(index).row() for index in self.table.selectionModel().selectedIndexes()})

    def select_presets(self, presets) -> None:
        wanted = set(map(id, presets))
        self._kept = [(preset, column) for preset in self.document.presets if id(preset) in wanted
                      for column in range(len(self.model.columns))]
        self._restore()

    def _restore(self) -> None:
        selection, rows = QItemSelection(), {id(preset): row for row, preset in enumerate(self.document.presets)}
        for preset, column in self._kept:
            if (index := self.proxy.mapFromSource(self.model.index(rows.get(id(preset), -1), column))).isValid():
                selection.select(index, index)
        self.table.selectionModel().select(selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
        if not selection.isEmpty():
            self.table.selectionModel().setCurrentIndex(selection.indexes()[0],
                                                        QItemSelectionModel.SelectionFlag.NoUpdate)
            self.table.scrollTo(selection.indexes()[0])

    def _selection_changed(self, *_) -> None:
        presets = self.document.presets
        self._kept = [(presets[self.proxy.mapToSource(index).row()], index.column())
                      for index in self.table.selectionModel().selectedIndexes()]
        for button in self.selected_only:
            button.setEnabled(bool(self._kept))

    def _add(self) -> None:
        rows = self.selected_rows()
        made = self.document.new_presets([None])
        self.document.insert(rows[-1] + 1 if rows else len(self.document.presets), made, self.tr("Add preset"))
        self.select_presets(made)

    def _duplicate(self) -> None:
        if rows := self.selected_rows():
            made = self.document.new_presets([self.document.presets[row] for row in rows])
            self.document.insert(rows[-1] + 1, made, self.tr("Duplicate presets"))
            self.select_presets(made)

    def _delete(self) -> None:
        if rows := self.selected_rows():
            self.document.remove([self.document.presets[row] for row in rows])
            if presets := self.document.presets:
                self.select_presets([presets[min(rows[0], len(presets) - 1)]])

    def _move_up(self) -> None:
        self.document.move([self.document.presets[row] for row in self.selected_rows()], -1)

    def _move_down(self) -> None:
        self.document.move([self.document.presets[row] for row in self.selected_rows()], 1)

    def _copy(self) -> None:
        if (text := self.table.currentIndex().data(Qt.ItemDataRole.EditRole)) is not None:
            QApplication.clipboard().setText(text)

    def _paste(self) -> None:
        text = QApplication.clipboard().text().strip()
        kind = "colour" if _COLOR.fullmatch(text) else "guid" if _GUID.fullmatch(text) else "name"
        with self.document.macro(self.tr("Paste")):
            for index in self.table.selectionModel().selectedIndexes():
                column = self.model.columns[index.column()]
                if kind == ("colour" if type(column) is int else column):
                    self.proxy.setData(index, text)

    def _double_clicked(self, index: QModelIndex) -> None:
        column = self.model.columns[index.column()]
        if type(column) is not int:
            if column != "alpha":
                self.table.edit(index)
            return
        held = self.document.presets[self.proxy.mapToSource(index).row()].colors[column]
        color = QColorDialog.getColor(QColor(*held), self, self.tr("Colour"),
                                      QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if color.isValid():
            self.proxy.setData(index, _hex(color.getRgb()))

    def _clean_changed(self, clean: bool) -> None:
        self.modified_changed.emit(not clean)

    def _refused(self, text: str) -> None:
        self.status.setText(text)
        self._clear.start()

    def _show_problems(self, _change=None) -> None:
        if not self._clear.isActive():
            self.status.setText("\n".join(self.document.problems()))
