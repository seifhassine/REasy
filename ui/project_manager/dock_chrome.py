from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, Qt
from PySide6.QtGui import QPalette, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QStyle, QToolButton, QWidget

# Arrows pointing toward the window edge the browser collapses to.
_MINIMIZE_ARROW = {
    Qt.LeftDockWidgetArea: "‹",
    Qt.RightDockWidgetArea: "›",
    Qt.TopDockWidgetArea: "▲",
    Qt.BottomDockWidgetArea: "▼",
}
# Arrows on the side tab pointing back into the window.
_EXPAND_ARROW = {
    Qt.LeftDockWidgetArea: "›",
    Qt.RightDockWidgetArea: "‹",
    Qt.TopDockWidgetArea: "▼",
    Qt.BottomDockWidgetArea: "▲",
}


class DockTitleBar(QWidget):
    """Title bar for the Project Browser dock: minimize / redock, never close.

    Mouse events are deliberately left unhandled: they propagate to the
    QDockWidget itself, which keeps its native drag / float / re-dock behavior.
    """

    def __init__(self, dock):
        super().__init__(dock)
        self.setObjectName("projectBrowserTitleBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.title_label = QLabel(dock.windowTitle(), self)

        self.redock_btn = QToolButton(self)
        self.redock_btn.setAutoRaise(True)
        self.redock_btn.setIcon(self.style().standardIcon(QStyle.SP_TitleBarNormalButton))
        self.redock_btn.setToolTip(dock.tr("Dock back into the main window"))
        self.redock_btn.clicked.connect(dock.redock)

        self.min_btn = QToolButton(self)
        self.min_btn.setAutoRaise(True)
        self.min_btn.setToolTip(dock.tr("Minimize to side tab"))
        self.min_btn.clicked.connect(dock.minimize_to_side_tab)

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 1, 1, 1)
        lay.setSpacing(1)
        lay.addWidget(self.title_label, 1)
        lay.addWidget(self.redock_btn)
        lay.addWidget(self.min_btn)

    def sync(self, floating: bool, area):
        self.redock_btn.setVisible(floating)
        self.min_btn.setText(_MINIMIZE_ARROW.get(area, "‹"))


class SideTab(QWidget):
    """Edge tab shown while the Project Browser is minimized."""

    THICKNESS = 22
    LENGTH = 150

    def __init__(self, dock):
        super().__init__(dock.app_win)
        self._dock = dock
        self._area = None
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(dock.windowTitle())
        dock.app_win.installEventFilter(self)

    def eventFilter(self, obj, event):
        if obj is self.parentWidget() and event.type() == QEvent.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    def set_area(self, area):
        if area != self._area:
            self._area = area
            vertical = area in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea)
            self.setFixedSize(*(self.THICKNESS, self.LENGTH) if vertical else (self.LENGTH, self.THICKNESS))
        self._reposition()

    def _reposition(self):
        host = self.parentWidget()
        if host is None:
            return
        x = (host.width() - self.width()) // 2
        y = (host.height() - self.height()) // 2
        if self._area == Qt.RightDockWidgetArea:
            x = host.width() - self.width() - 1
        elif self._area == Qt.TopDockWidgetArea:
            y = 1
        elif self._area == Qt.BottomDockWidgetArea:
            y = host.height() - self.height() - 1
        else:
            x = 1
        self.move(x, y)
        self.raise_()

    def enterEvent(self, _event):
        self._hover = True
        self.update()

    def leaveEvent(self, _event):
        self._hover = False
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._dock.restore_from_side_tab()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        group = QPalette.Current
        if self._hover:
            bg, fg = QPalette.Highlight, QPalette.HighlightedText
        else:
            bg, fg = QPalette.Button, QPalette.ButtonText
        p.setPen(QPen(pal.color(group, QPalette.Mid)))
        p.setBrush(pal.color(group, bg))
        p.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 7, 7)
        text = f"{_EXPAND_ARROW.get(self._area, '›')}  {self._dock.windowTitle()}"
        p.setPen(pal.color(group, fg))
        p.translate(self.rect().center())
        if self._area == Qt.LeftDockWidgetArea:
            p.rotate(-90)
        elif self._area == Qt.RightDockWidgetArea:
            p.rotate(90)
        p.drawText(
            QRectF(-self.LENGTH / 2, -self.THICKNESS / 2, self.LENGTH, self.THICKNESS),
            Qt.AlignCenter,
            text,
        )
