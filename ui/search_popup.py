"""A search field over a two-column list in a popup: SearchPopup.pick(entries, at, hint, receiver). With fit the
popup is as wide as its entries, up to the screen."""

from __future__ import annotations

from PySide6.QtCore import QAbstractTableModel, QEvent, QModelIndex, QPoint, Qt
from PySide6.QtWidgets import QApplication, QFrame, QHeaderView, QLineEdit, QTreeView, QVBoxLayout, QWidget

lazy from ui.editor_widgets import search_field
lazy from ui.styles import qcolor

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

    def pick(self, entries: list[tuple], at: QPoint, hint: str, receiver, custom=None, fit: bool = False) -> None:
        self._receiver, self._custom = receiver, custom
        self.search.setPlaceholderText(hint)
        self.search.clear()
        self.entries.load(entries)
        self.list.setCurrentIndex(self.entries.index(0, 0))
        bounds = (QApplication.screenAt(at) or QApplication.primaryScreen()).availableGeometry()
        margins, view, wide = self.layout().contentsMargins(), self.list, 460
        if fit:
            wide = min(bounds.width(), max(wide, view.sizeHintForColumn(0) + view.columnWidth(1) + margins.left()
                                           + margins.right() + 2 * (self.frameWidth() + view.frameWidth())
                                           + view.verticalScrollBar().sizeHint().width()))
        self.resize(wide, 380)
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
