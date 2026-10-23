from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QTabBar, QToolButton, QWidget

lazy from ui.scene.device_frames import FrameWindow
lazy from ui.styles import current_colors

_WIDTH, _IMAGE_HEIGHT, _PAD, _GAP, _OFFSET = 320, 200, 8, 6, 4
_WAKE_MS, _LIVE_MS = 500, 250


class TabPreview(QWidget):

    def __init__(self, bar: QTabBar):
        super().__init__(bar, Qt.ToolTip | Qt.FramelessWindowHint | Qt.WindowTransparentForInput
                         | Qt.WindowDoesNotAcceptFocus)
        for attribute in (Qt.WA_ShowWithoutActivating, Qt.WA_TransparentForMouseEvents):
            self.setAttribute(attribute)
        self._index = -1
        self._title = self._path = ""
        self._image: QPixmap | None = None
        self._wake = QTimer(self)
        self._wake.setSingleShot(True)
        self._wake.setInterval(_WAKE_MS)
        self._wake.timeout.connect(self._present)
        self._live = QTimer(self)
        self._live.setInterval(_LIVE_MS)
        self._live.timeout.connect(self._refresh)
        bar.setAttribute(Qt.WA_Hover)
        bar.installEventFilter(self)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind in (QEvent.HoverEnter, QEvent.HoverMove):
            self._hover(self._tab_at(event.position().toPoint()))
        elif kind == QEvent.ToolTip:
            return self._tab_at(event.pos()) >= 0
        elif kind == QEvent.HoverLeave:
            self._index = -1
            self._close()
        elif kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick, QEvent.Wheel, QEvent.Hide):
            self._close()
        return False

    def _tab_at(self, pos: QPoint) -> int:
        bar = self.parentWidget()
        return -1 if isinstance(bar.childAt(pos), QToolButton) else bar.tabAt(pos)

    def _page(self) -> QWidget | None:
        return self.parentWidget().parentWidget().widget(self._index)

    def _hover(self, index: int) -> None:
        if index == self._index:
            return
        self._index = index
        if index < 0 or QApplication.mouseButtons() != Qt.NoButton:
            self._close()
            return
        self._ask_frames(self._page())
        if self.isVisible():
            self._present()
        else:
            self._wake.start()

    def _present(self) -> None:
        self._wake.stop()
        if self._refresh():
            self.show()
            self._live.start()

    def _refresh(self) -> bool:
        bar, page = self.parentWidget(), self._page()
        modal = QApplication.activeModalWidget()
        if page is None or not bar.isVisible() or QApplication.activeWindow() is None \
                or QApplication.activePopupWidget() is not None or (modal is not None and modal is not bar.window()) \
                or QApplication.mouseButtons() != Qt.NoButton:
            self._close()
            return False
        self._ask_frames(page)
        self._title, self._path = bar.tabText(self._index), bar.tabToolTip(self._index)
        if self._path == self._title:
            self._path = ""
        self._image = None if page.isVisible() else self._picture(page)
        self.resize(_WIDTH, self._layout()[-1])
        self._place(bar.tabRect(self._index))
        self.update()
        return True

    def _close(self) -> None:
        self._wake.stop()
        self._live.stop()
        self._image = None
        self.hide()

    @staticmethod
    def _ask_frames(page: QWidget | None) -> None:
        if page is not None and not page.isVisible():
            for window in page.findChildren(FrameWindow):
                if window.isVisibleTo(page):
                    window.image_wanted.emit()

    def _picture(self, page: QWidget) -> QPixmap | None:
        stack = page.parentWidget()
        if page.size() != stack.size():
            page.resize(stack.size())
        grabbed = page.grab()
        if grabbed.isNull():
            return None
        ratio = self.parentWidget().devicePixelRatioF()
        picture = grabbed.scaled(QSize(_WIDTH - 2 * _PAD, _IMAGE_HEIGHT) * ratio, Qt.KeepAspectRatio,
                                 Qt.SmoothTransformation)
        picture.setDevicePixelRatio(ratio)
        return picture

    def _fonts(self) -> tuple[QFont, QFont]:
        title = QFont(self.font())
        title.setWeight(QFont.Weight.DemiBold)
        return title, self.font()

    def _layout(self) -> tuple[QRect, QRect | None, QRect | None, int]:
        title_font, path_font = self._fonts()
        width = _WIDTH - 2 * _PAD
        title = QRect(_PAD, _PAD, width, QFontMetrics(title_font).height())
        bottom = title.bottom()
        path = None
        if self._path:
            path = QRect(_PAD, bottom + 3, width, QFontMetrics(path_font).height())
            bottom = path.bottom()
        image = None
        if self._image is not None:
            size = self._image.deviceIndependentSize().toSize()
            image = QRect(_PAD + (width - size.width()) // 2, bottom + 1 + _GAP, size.width(), size.height())
            bottom = image.bottom()
        return title, path, image, bottom + 1 + _PAD

    def _place(self, tab: QRect) -> None:
        bar = self.parentWidget()
        screen = bar.screen().availableGeometry()
        below = bar.mapToGlobal(QPoint(tab.left(), tab.bottom() + 1 + _OFFSET))
        x = max(screen.left(), min(below.x(), screen.right() + 1 - self.width()))
        y = below.y()
        if y + self.height() > screen.bottom() + 1:
            y = bar.mapToGlobal(QPoint(0, tab.top() - _OFFSET)).y() - self.height()
        self.move(x, y)

    def paintEvent(self, _event):
        colors = current_colors()
        title_rect, path_rect, image_rect, _height = self._layout()
        title_font, path_font = self._fonts()
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(colors["menu_bg"]))
        painter.setPen(QColor(colors["border"]))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        painter.setFont(title_font)
        painter.setPen(QColor(colors["text_bright"]))
        title = QFontMetrics(title_font).elidedText(self._title, Qt.ElideRight, title_rect.width())
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, title)
        if path_rect is not None:
            painter.setFont(path_font)
            painter.setPen(QColor(colors["text_muted"]))
            path = QFontMetrics(path_font).elidedText(self._path, Qt.ElideMiddle, path_rect.width())
            painter.drawText(path_rect, Qt.AlignLeft | Qt.AlignVCenter, path)
        if image_rect is not None:
            painter.drawPixmap(image_rect.topLeft(), self._image)
            painter.setPen(QColor(colors["border_subtle"]))
            painter.drawRect(image_rect.adjusted(-1, -1, 0, 0))


__all__ = ["TabPreview"]
