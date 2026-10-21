from __future__ import annotations

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QToolTip, QWidget

lazy from ui.icons import line_icon
lazy from ui.styles import current_colors


class ViewRail(QWidget):
    RAIL_HEIGHT = 30
    ICON_SIZE = 16
    ICON_GAP = 6
    SIDE_MARGIN = 4
    ITEM_PADDING = 6
    ACCENT = 2

    currentChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("projectBrowserViewRail")
        self._current = ""
        self._hover = -1
        self._rects: list[QRect] = []
        self._compact = False
        self._keyboard_focus = False
        self._items = (
            ("sys", "folder_open", self.tr("Game Files"),
             self.tr("Game Files\n\nExtracted game assets, used as read-only reference.")),
            ("proj", "briefcase", self.tr("My Mod"),
             self.tr("My Mod\n\nThe mod's own files. Edits here are what gets exported.")),
            ("pak", "box", self.tr("PAK Files"),
             self.tr("PAK Files\n\nPaths inside the game's .pak archives.")),
            ("bm", "bookmark", self.tr("Bookmarks"),
             self.tr("Bookmarks\n\nSaved paths and folders you visit often.")),
        )
        self.setFixedHeight(self.RAIL_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.TabFocus)
        self.setAccessibleName(self.tr("Project Browser views"))

    def set_current(self, view_id: str) -> None:
        """Highlight *view_id* without emitting :attr:`currentChanged`."""
        if view_id == self._current:
            return
        self._current = view_id
        self._sync_accessible_description()
        self._layout_items()
        self.update()

    def current(self) -> str:
        return self._current

    def sizeHint(self) -> QSize:
        return QSize(len(self._items) * 72, self.RAIL_HEIGHT)

    def minimumSizeHint(self) -> QSize:
        return QSize(len(self._items) * (self.ICON_SIZE + 12), self.RAIL_HEIGHT)

    def _choose(self, index: int) -> None:
        if 0 <= index < len(self._items) and self._items[index][0] != self._current:
            self.set_current(self._items[index][0])
            self.currentChanged.emit(self._current)

    def _current_index(self) -> int:
        return next((i for i, item in enumerate(self._items) if item[0] == self._current), 0)

    def _sync_accessible_description(self) -> None:
        index = self._current_index()
        self.setAccessibleDescription(self._items[index][2] if self._current else "")

    def keyPressEvent(self, event):
        self._keyboard_focus = True
        index = self._current_index()
        key = event.key()
        if key == Qt.Key_Left:
            self._choose(max(0, index - 1))
        elif key == Qt.Key_Right:
            self._choose(min(len(self._items) - 1, index + 1))
        elif key == Qt.Key_Home:
            self._choose(0)
        elif key == Qt.Key_End:
            self._choose(len(self._items) - 1)
        else:
            super().keyPressEvent(event)

    def focusInEvent(self, event):
        # The focus frame is for keyboard users; focus the window hands over on activation stays unmarked.
        self._keyboard_focus = event.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason)
        self.update()
        super().focusInEvent(event)

    def focusOutEvent(self, event):
        self._keyboard_focus = False
        self.update()
        super().focusOutEvent(event)

    def changeEvent(self, event):
        if event.type() == QEvent.FontChange:
            self._layout_items()
            self.update()
        super().changeEvent(event)

    def leaveEvent(self, event):
        self._hover = -1
        self.update()
        super().leaveEvent(event)

    def mouseMoveEvent(self, event):
        idx = self._item_at(event.position().toPoint())
        if idx != self._hover:
            self._hover = idx
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._choose(self._item_at(event.position().toPoint()))

    def event(self, ev):
        if ev.type() == QEvent.ToolTip:
            idx = self._item_at(ev.pos())
            if idx >= 0:
                QToolTip.showText(ev.globalPos(), self._items[idx][3], self)
                return True
        return super().event(ev)

    def resizeEvent(self, event):
        self._layout_items()
        super().resizeEvent(event)

    def paintEvent(self, _event):
        colors = current_colors()
        accent = QColor(colors["accent"])
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(colors["panel"]))
        p.fillRect(0, self.height() - 1, self.width(), 1, QColor(colors["border_subtle"]))
        if not self._rects:
            self._layout_items()
        metrics = QFontMetrics(self.font())
        pens = {
            "active": QPen(QColor(colors["text_bright"])),
            "hover": QPen(QColor(colors["text"])),
            "idle": QPen(QColor(colors["text_muted"])),
        }
        icon_colors = {"active": colors["accent"], "hover": colors["text"], "idle": colors["text_muted"]}
        hover_fill = QColor(colors["surface"])
        dpr = self.devicePixelRatioF()
        for i, (view_id, icon_name, caption, _tip) in enumerate(self._items):
            r = self._rects[i]
            active = view_id == self._current
            hovered = i == self._hover
            if hovered and not active:
                p.fillRect(r.adjusted(0, 0, 0, -1), hover_fill)
            if active:
                p.fillRect(r.left(), self.height() - self.ACCENT, r.width(), self.ACCENT, accent)
            state = "active" if active else "hover" if hovered else "idle"

            room = r.width() - self.ICON_SIZE - self.ICON_GAP - 2 * self.ITEM_PADDING
            show_text = room > 0 and (active or not self._compact)
            text = metrics.elidedText(caption, Qt.ElideRight, room) if show_text else ""
            if len(text.rstrip("…")) < 3:
                text = ""
            text_width = metrics.horizontalAdvance(text) if text else 0
            content = self.ICON_SIZE + (self.ICON_GAP + text_width if text else 0)
            x = r.left() + (r.width() - content) // 2
            icon_top = r.top() + (r.height() - self.ICON_SIZE) // 2
            icon = line_icon(icon_name, icon_colors[state])
            p.drawPixmap(x, icon_top, icon.pixmap(QSize(self.ICON_SIZE, self.ICON_SIZE), dpr))
            if text:
                p.setPen(pens[state])
                p.drawText(QRect(x + self.ICON_SIZE + self.ICON_GAP, r.top(), text_width + 1, r.height()),
                           Qt.AlignLeft | Qt.AlignVCenter, text)

        if self.hasFocus() and self._keyboard_focus and self._rects:
            p.setPen(QPen(accent, 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(self._rects[self._current_index()]).adjusted(1.5, 1.5, -1.5, -2.5))

    def _layout_items(self):
        """Captions get their width plus an equal share of the rest; short of room only the current one is shown."""
        self._rects = []
        n = len(self._items)
        if not n:
            return
        avail = max(0, self.width() - self.SIDE_MARGIN * 2)
        metrics = QFontMetrics(self.font())
        natural = [self.ICON_SIZE + self.ICON_GAP + metrics.horizontalAdvance(item[2]) + 2 * self.ITEM_PADDING
                   for item in self._items]
        spare = avail - sum(natural)
        self._compact = spare < 0
        if not self._compact:
            widths = [width + spare * (i + 1) // n - spare * i // n for i, width in enumerate(natural)]
        else:
            widths = [avail * (i + 1) // n - avail * i // n for i in range(n)]
            current = self._current_index() if self._current else -1
            icon_width = self.ICON_SIZE + 2 * self.ITEM_PADDING
            active_width = min(natural[current], avail - (n - 1) * icon_width) if current >= 0 else 0
            if active_width > widths[current if current >= 0 else 0]:
                rest = avail - active_width
                others = [rest * (i + 1) // (n - 1) - rest * i // (n - 1) for i in range(n - 1)]
                widths = others[:current] + [active_width] + others[current:]
        x = self.SIDE_MARGIN
        for width in widths:
            self._rects.append(QRect(x, 0, width, self.RAIL_HEIGHT))
            x += width

    def _item_at(self, pos) -> int:
        if not self._rects:
            self._layout_items()
        for i, r in enumerate(self._rects):
            if r.contains(pos):
                return i
        return -1
