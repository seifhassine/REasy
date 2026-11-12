"""See docs/internals/bhvt-editor.md, Pickers."""

from __future__ import annotations

import functools
from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QPoint, Qt
from PySide6.QtWidgets import QApplication, QFrame, QHeaderView, QLineEdit, QTreeView, QVBoxLayout, QWidget

lazy from ui.editor_widgets import search_field
lazy from ui.styles import qcolor

from ..model import Hfsm
from ..references import asset, own_variables, slot_types, tag_hash, tag_names, user_data
from ..rules import authoring
from ..summaries import short_name

_MOVES = {Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown}


class _Entries(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[tuple] = []
        self.rows: list[tuple] = []
        self._keys: list[str] | None = None

    def load(self, entries: list[tuple]) -> None:
        self.beginResetModel()
        self.entries, self.rows, self._keys = entries, entries, None
        self.endResetModel()

    def filter(self, text: str, extra: tuple | None) -> None:
        tokens = text.casefold().replace(".", " ").split()
        if self._keys is None:
            self._keys = [f"{entry[0]} {entry[1]}".casefold() for entry in self.entries]
        rows = self.entries if not tokens else sorted(
            (entry for entry, key in zip(self.entries, self._keys) if all(token in key for token in tokens)),
            key=lambda entry: not entry[0].casefold().startswith(tokens[0]))
        self.beginResetModel()
        self.rows = rows if extra is None else [extra, *rows]
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 2

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole:
            return self.rows[index.row()][index.column()]
        if index.column() == 1 and role == Qt.ItemDataRole.ForegroundRole:
            return qcolor("text_subtle")
        if index.column() == 1 and role == Qt.ItemDataRole.TextAlignmentRole:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None


class SearchPopup(QFrame):
    def __init__(self, parent: QWidget, run):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._run, self._receiver, self._custom = run, None, None
        column = QVBoxLayout(self)
        column.setContentsMargins(4, 4, 4, 4)
        column.setSpacing(4)
        self.search = search_field(QLineEdit(self))
        self.entries = _Entries(self)
        self.list = QTreeView(self)
        self.list.setModel(self.entries)
        self.list.setRootIsDecorated(False)
        self.list.setHeaderHidden(True)
        self.list.setUniformRowHeights(True)
        self.list.setAllColumnsShowFocus(True)
        self.list.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.list.header().setStretchLastSection(False)
        self.list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        column.addWidget(self.search)
        column.addWidget(self.list, 1)
        self.search.textChanged.connect(self._filter)
        self.search.installEventFilter(self)
        self.list.clicked.connect(self._choose)
        self.resize(460, 380)

    def pick(self, entries: list[tuple], at: QPoint, hint: str, receiver, custom=None) -> None:
        self._receiver, self._custom = receiver, custom
        self.search.setPlaceholderText(hint)
        self.search.clear()
        self.entries.load(entries)
        self.list.setCurrentIndex(self.entries.index(0, 0))
        bounds = (QApplication.screenAt(at) or QApplication.primaryScreen()).availableGeometry()
        self.move(max(bounds.left(), min(at.x(), bounds.right() - self.width())),
                  max(bounds.top(), min(at.y(), bounds.bottom() - self.height())))
        self.show()
        self.search.setFocus()

    def hideEvent(self, event) -> None:
        self._receiver = self._custom = None
        super().hideEvent(event)

    def eventFilter(self, watched, event) -> bool:
        if event.type() != QEvent.Type.KeyPress:
            return False
        if event.key() in _MOVES:
            self.list.keyPressEvent(event)
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._choose(self.list.currentIndex())
        elif event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            return False
        return True

    def _filter(self, text: str) -> None:
        self.entries.filter(text, None if self._custom is None or not text.strip() else self._custom(text.strip()))
        self.list.setCurrentIndex(self.entries.index(0, 0))

    def _choose(self, index: QModelIndex) -> None:
        if index.isValid() and self._receiver is not None:
            value, receiver = self.entries.rows[index.row()][2], self._receiver
            self.hide()
            self._run(receiver, value)


def below(widget: QWidget) -> QPoint:
    return widget.mapToGlobal(widget.rect().bottomLeft())


def class_entries(model, slot: str, base: str) -> list[tuple[str, str, str]]:
    rsz = model.blocks[slot]
    used = authoring(rsz.game_version)["usage"]["hfsm" if isinstance(model, Hfsm) else "tree"].get(slot, {})
    return [(short_name(name), name.rsplit(".", 1)[0] + (f"   {used[name]:,}" if name in used else ""), name)
            for name in sorted(slot_types(rsz, base), key=lambda name: (-used.get(name, 0), name))]


def type_entries(rsz, base: str) -> list[tuple[str, str, str]]:
    return [(short_name(name), name.rsplit(".", 1)[0], name) for name in slot_types(rsz, base)]


def user_data_entries(rsz, base: str, path: str, held: int) -> list[tuple[str, str, object]]:
    registry = rsz.type_registry
    return [(asset(user, path)[1].rsplit("/", 1)[-1], short_name(registry.get_type_info(user.type_id)["name"]), user)
            for user in user_data(rsz, base, path) if user.instance_id != held]


def node_entries(structure, nodes: list, home=None) -> list[tuple[str, str, object]]:
    return [(node.name, " / ".join(item.name for item in structure.path(node)[:-1]), node)
            for node in sorted(nodes, key=lambda node: structure.parent.get(id(node)) is not home)]


def variable_entries(document, model, summaries) -> list[tuple[str, str, bytes]]:
    own = {id(uvar): uvar.header.name or "" for uvar in own_variables(model)}
    files = {id(uvar): Path(path).name for path, uvar in document.owner_variables.items()}
    return [(variable.name, f"{variable.type.name}   {own.get(id(source)) or files.get(id(source), '')}", guid)
            for guid, (source, variable) in summaries.found.items()]


@functools.cache
def tag_entries(game: str) -> list[tuple[str, str, int]]:
    first = {name: rank for rank, name in enumerate(authoring(game)["tag_enums"])}
    rows = [(*name.rpartition(".")[::-2], value) for value, name in tag_names(game).items()]
    return sorted(rows, key=lambda row: (first.get(row[1], len(first)), row[1]))


def custom_tag(text: str) -> tuple[str, str, int] | None:
    if text[:2].lower() == "0x":
        try:
            value = int(text, 16)
        except ValueError:
            return None
        return (text, "", value) if 0 <= value <= 0xFFFFFFFF else None
    return (text, f"0x{tag_hash(text):08X}", tag_hash(text)) if "." in text.strip(".") else None
