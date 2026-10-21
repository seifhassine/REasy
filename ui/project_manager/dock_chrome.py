from __future__ import annotations

import weakref

from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QWidget

lazy from ui.editor_widgets import styled_label, tool_button
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors

# Glyph text kept for tests and screen readers; the button shows the icon.
_MINIMIZE_ARROW = {
    Qt.LeftDockWidgetArea: "‹",
    Qt.RightDockWidgetArea: "›",
    Qt.TopDockWidgetArea: "▲",
    Qt.BottomDockWidgetArea: "▼",
}
_MINIMIZE_ICON = {
    Qt.LeftDockWidgetArea: "chevron_left",
    Qt.RightDockWidgetArea: "chevron_right",
    Qt.TopDockWidgetArea: "chevron_up",
    Qt.BottomDockWidgetArea: "chevron_down",
}


class DockTitleBar(QWidget):
    """Unhandled mouse events pass to the QDockWidget for its native drag, float and re-dock."""

    def __init__(self, dock):
        super().__init__(dock)
        self.setObjectName("projectBrowserTitleBar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.title_label = styled_label(dock.windowTitle(), self, heading="panel")

        self.redock_btn = tool_button("dock", self.tr("Dock back into the main window"), dock.redock, parent=self)
        self.min_btn = tool_button(
            "chevron_left", self.tr("Minimize to side tab"), dock.minimize_to_side_tab, parent=self
        )
        for button in (self.redock_btn, self.min_btn):
            button.setIconSize(QSize(14, 14))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 2, 3, 2)
        lay.setSpacing(2)
        lay.addWidget(self.title_label, 1)
        lay.addWidget(self.redock_btn)
        lay.addWidget(self.min_btn)

    def sync(self, floating: bool, area):
        self.redock_btn.setVisible(floating)
        self.min_btn.setText(_MINIMIZE_ARROW.get(area, "‹"))
        self.min_btn.setIcon(line_icon(_MINIMIZE_ICON.get(area, "chevron_left")))

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            child = self.childAt(event.pos())
            if child is self.redock_btn or child is self.min_btn:
                super().mouseDoubleClickEvent(event)
                return
            try:
                dock = self.parentWidget()  # not stored: the dock holds this bar (a cycle)
                if dock.isFloating():
                    dock.redock()
                else:
                    dock.setFloating(True)
                event.accept()
                return
            except Exception:
                pass
        super().mouseDoubleClickEvent(event)


class SideTab(QWidget):
    """Edge tab shown while the Project Browser is minimized."""

    THICKNESS = 24
    LENGTH = 150
    ICON_SIZE = 12

    def __init__(self, dock):
        super().__init__(dock.app_win)
        # Weak: the dock holds this tab, so a strong reference would make a cycle for the garbage collector.
        self._dock = weakref.ref(dock)
        self._area = None
        self._hover = False
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(dock.windowTitle())
        dock.app_win.installEventFilter(self)

    @property
    def dock(self):
        return self._dock()

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
            self.dock.restore_from_side_tab()

    def _accent_edge(self) -> QRect:
        r = self.rect()
        if self._area == Qt.RightDockWidgetArea:
            return QRect(r.right() - 1, 0, 2, r.height())
        if self._area == Qt.TopDockWidgetArea:
            return QRect(0, 0, r.width(), 2)
        if self._area == Qt.BottomDockWidgetArea:
            return QRect(0, r.bottom() - 1, r.width(), 2)
        return QRect(0, 0, 2, r.height())

    def paintEvent(self, _event):
        colors = current_colors()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(colors["border"])))
        p.setBrush(QColor(colors["surface_hover" if self._hover else "header_bg"]))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 3, 3)
        if self._hover:
            p.fillRect(self._accent_edge(), QColor(colors["accent"]))
        p.translate(self.rect().center())
        if self._area == Qt.LeftDockWidgetArea:
            p.rotate(-90)
        elif self._area == Qt.RightDockWidgetArea:
            p.rotate(90)
        # Drawn in rotated space: "down" there points into the window on the left, right and top edges.
        arrow = "chevron_up" if self._area == Qt.BottomDockWidgetArea else "chevron_down"
        title = self.dock.windowTitle()
        metrics = p.fontMetrics()
        gap = 4
        width = self.ICON_SIZE + gap + metrics.horizontalAdvance(title)
        left = -width / 2
        icon = line_icon(arrow, colors["text_bright" if self._hover else "text_muted"])
        p.drawPixmap(
            QRectF(left, -self.ICON_SIZE / 2, self.ICON_SIZE, self.ICON_SIZE).toRect(),
            icon.pixmap(QSize(self.ICON_SIZE, self.ICON_SIZE), self.devicePixelRatioF()),
        )
        p.setPen(QColor(colors["text_bright" if self._hover else "text"]))
        p.drawText(
            QRectF(left + self.ICON_SIZE + gap, -self.THICKNESS / 2, width, self.THICKNESS),
            Qt.AlignLeft | Qt.AlignVCenter,
            title,
        )
