from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPalette
from PySide6.QtWidgets import QToolTip, QWidget


class ViewRail(QWidget):
    """Horizontal switcher for the Project Browser views.

    A compact horizontal switcher strip along the top of the Project
    Browser. Each item is a hand-drawn glyph above a bold title and a
    small subtitle, so every view stays identifiable at a glance; the
    active item is tinted by the app accent and gets an underline plus
    a soft highlight pill.
    """

    ITEM_H = 52
    ICON_SIZE = 17.0
    TOP_MARGIN = 3
    SPACING = 3
    RAIL_HEIGHT = 58
    SIDE_MARGIN = 6
    TITLE_DY = 23
    SUB_DY = 36

    currentChanged = Signal(str)

    def __init__(self, parent: QWidget | None = None,
                 accent_provider: Callable[[], QColor] | None = None):
        super().__init__(parent)
        self.setObjectName("projectBrowserViewRail")
        self._accent_provider = accent_provider
        self._current = ""
        self._hover = -1
        self._rects: list[QRect] = []
        self._items = (
            ("sys", self.tr("Game Files"), self.tr("unpacked copy"),
             self.tr("Game Files\n\nExtracted game assets, used as read-only reference."),
             self._paint_unpacked),
            ("proj", self.tr("My Mod"), self.tr("your project"),
             self.tr("My Mod\n\nThe mod's own files. Edits here are what gets exported."),
             self._paint_project),
            ("pak", self.tr("PAK Files"), self.tr("game archives"),
             self.tr("PAK Files\n\nPaths inside the game's .pak archives."),
             self._paint_pak),
            ("bm", self.tr("Bookmarks"), self.tr("saved paths"),
             self.tr("Bookmarks\n\nSaved paths and folders you visit often."),
             self._paint_bookmark),
        )
        self._title_font, self._sub_font = self._make_fonts()
        self.setFixedHeight(self.RAIL_HEIGHT)
        self.setCursor(Qt.PointingHandCursor)
        self.setMouseTracking(True)

    def _make_fonts(self):
        title = QFont(self.font())
        title.setWeight(QFont.DemiBold)
        sub = QFont(self.font())
        if sub.pointSizeF() > 0:
            sub.setPointSizeF(max(6.4, round(sub.pointSizeF() * 0.78, 1)))
        elif sub.pixelSize() > 0:
            sub.setPixelSize(max(9, round(sub.pixelSize() * 0.78)))
        else:
            sub.setPointSizeF(7.0)
        return title, sub

    # -- public API --------------------------------------------------------

    def set_current(self, view_id: str) -> None:
        """Highlight *view_id* without emitting :attr:`currentChanged`."""
        if view_id == self._current:
            return
        self._current = view_id
        self.update()

    def current(self) -> str:
        return self._current

    # -- events ------------------------------------------------------------

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
        if event.button() != Qt.LeftButton:
            return
        idx = self._item_at(event.position().toPoint())
        if 0 <= idx < len(self._items):
            view_id = self._items[idx][0]
            if view_id != self._current:
                self._current = view_id
                self.update()
                self.currentChanged.emit(view_id)

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

    # -- painting ----------------------------------------------------------

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        p.fillRect(self.rect(), QColor("#232427"))
        p.fillRect(0, self.height() - 1, self.width(), 1, QColor("#33343a"))

        pal = self.palette()
        window_text = QColor(pal.color(QPalette.Current, QPalette.WindowText))
        accent = self._accent()

        if not self._rects:
            self._layout_items()
        for i, (view_id, caption, subcaption, _tip, painter_fn) in enumerate(self._items):
            r = self._rects[i]
            active = view_id == self._current
            hovered = i == self._hover

            if active or hovered:
                bg = QColor(accent) if active else QColor(window_text)
                bg.setAlpha(26 if active else 16)
                p.setPen(Qt.NoPen)
                p.setBrush(bg)
                p.drawRoundedRect(r.adjusted(2, 2, -2, -2), 8, 8)

            if active:
                p.setPen(Qt.NoPen)
                p.setBrush(accent)
                p.drawRoundedRect(
                    QRectF(r.left() + 10, r.y() + r.height() - 3.5, r.width() - 20, 3),
                    1.5, 1.5,
                )

            if active:
                color = QColor(accent)
            elif hovered:
                color = QColor(window_text)
                color.setAlpha(220)
            else:
                color = QColor(window_text)
                color.setAlpha(160)

            caption_color = QColor(accent) if active else QColor(color)
            caption_color.setAlpha(235 if (active or hovered) else 150)

            p.save()
            p.translate(r.center().x() - self.ICON_SIZE / 2.0, r.top() + 3)
            painter_fn(p, color)
            p.restore()

            p.setFont(self._title_font)
            metrics = p.fontMetrics()
            elided = metrics.elidedText(caption, Qt.ElideRight, r.width() - 4)
            p.setPen(QPen(caption_color))
            p.drawText(
                QRectF(r.left(), r.top() + self.TITLE_DY, r.width(), 13),
                Qt.AlignHCenter | Qt.AlignVCenter,
                elided,
            )

            sub_color = QColor(caption_color)
            sub_color.setAlpha(int(caption_color.alpha() * 0.72))
            p.setFont(self._sub_font)
            smetrics = p.fontMetrics()
            selided = smetrics.elidedText(subcaption, Qt.ElideRight, r.width() - 4)
            p.setPen(QPen(sub_color))
            p.drawText(
                QRectF(r.left(), r.top() + self.SUB_DY, r.width(), 13),
                Qt.AlignHCenter | Qt.AlignVCenter,
                selided,
            )

    def _accent(self) -> QColor:
        color = None
        if self._accent_provider is not None:
            try:
                color = self._accent_provider()
            except Exception:
                color = None
        if not isinstance(color, QColor) or not color.isValid():
            color = QColor(self.palette().color(QPalette.Current, QPalette.Highlight))
        return color

    def _layout_items(self):
        self._rects = []
        n = len(self._items)
        if not n:
            return
        avail = self.width() - self.SIDE_MARGIN * 2 - self.SPACING * (n - 1)
        w = max(44, avail // n)
        x = self.SIDE_MARGIN
        y = self.TOP_MARGIN
        for _ in self._items:
            self._rects.append(QRect(x, y, w, self.ITEM_H))
            x += w + self.SPACING

    def _item_at(self, pos) -> int:
        if not self._rects:
            self._layout_items()
        for i, r in enumerate(self._rects):
            if r.contains(pos):
                return i
        return -1

    # -- glyphs ------------------------------------------------------------

    def _pen(self, color: QColor, width: float = 1.5) -> QPen:
        pen = QPen(color, width)
        pen.setCapStyle(Qt.RoundCap)
        pen.setJoinStyle(Qt.RoundJoin)
        return pen

    def _paint_unpacked(self, p: QPainter, color: QColor):
        """Open folder."""
        s = self.ICON_SIZE
        p.setPen(self._pen(color))
        p.setBrush(Qt.NoBrush)
        back = QPainterPath()
        back.moveTo(1.5, s - 2.2)
        back.lineTo(1.5, 3.4)
        back.quadTo(1.5, 2.3, 2.6, 2.3)
        back.lineTo(6.0, 2.3)
        back.quadTo(6.9, 2.3, 7.4, 3.05)
        back.lineTo(8.5, 4.6)
        back.lineTo(s - 2.0, 4.6)
        p.drawPath(back)
        front = QPainterPath()
        front.moveTo(1.5, s - 2.2)
        front.lineTo(4.3, 6.4)
        front.lineTo(s - 1.6, 6.4)
        front.lineTo(s - 4.4, s - 2.2)
        front.closeSubpath()
        p.drawPath(front)

    def _paint_project(self, p: QPainter, color: QColor):
        """Briefcase."""
        s = self.ICON_SIZE
        p.setPen(self._pen(color))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(QRectF(1.8, 5.8, s - 3.6, 9.2), 2.0, 2.0)
        hx0, hx1 = s * 0.36, s * 0.64
        handle = QPainterPath()
        handle.moveTo(hx0, 5.8)
        handle.lineTo(hx0, 4.1)
        handle.quadTo(hx0, 3.0, hx0 + 1.1, 3.0)
        handle.lineTo(hx1 - 1.1, 3.0)
        handle.quadTo(hx1, 3.0, hx1, 4.1)
        handle.lineTo(hx1, 5.8)
        p.drawPath(handle)
        p.drawLine(QPointF(1.8, 9.9), QPointF(s - 1.8, 9.9))

    def _paint_pak(self, p: QPainter, color: QColor):
        """Isometric archive cube."""
        s = self.ICON_SIZE
        cx = s / 2.0
        p.setPen(self._pen(color))
        p.setBrush(Qt.NoBrush)
        cube = QPainterPath()
        cube.moveTo(cx, 1.9)
        cube.lineTo(s - 2.4, 5.3)
        cube.lineTo(s - 2.4, 11.7)
        cube.lineTo(cx, s - 1.9)
        cube.lineTo(2.4, 11.7)
        cube.lineTo(2.4, 5.3)
        cube.closeSubpath()
        p.drawPath(cube)
        p.drawLine(QPointF(2.4, 5.3), QPointF(cx, 8.6))
        p.drawLine(QPointF(s - 2.4, 5.3), QPointF(cx, 8.6))
        p.drawLine(QPointF(cx, 8.6), QPointF(cx, s - 1.9))

    def _paint_bookmark(self, p: QPainter, color: QColor):
        """Filled bookmark ribbon."""
        s = self.ICON_SIZE
        path = QPainterPath()
        path.moveTo(4.6, 2.2)
        path.lineTo(s - 4.6, 2.2)
        path.lineTo(s - 4.6, s - 2.1)
        path.lineTo(s / 2.0, s - 5.4)
        path.lineTo(4.6, s - 2.1)
        path.closeSubpath()
        p.setPen(Qt.NoPen)
        p.setBrush(color)
        p.drawPath(path)
