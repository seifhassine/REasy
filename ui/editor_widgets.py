from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QWidget,
)
from shiboken6 import isValid


EDITOR_TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 100
EDITOR_META_ROLE = EDITOR_TITLE_ROLE + 1


class EmbeddedPopupComboBox(QComboBox):
    """Show Qt's native combo view as a child of the containing window."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._popup_container = None
        self._popup_window_flags = None

    def showPopup(self) -> None:  # noqa: N802
        if not self.isEnabled() or not self.count():
            return
        if self._popup_container is not None and self._popup_container.isVisible():
            self.hidePopup()
            return

        parent = self.window()
        view = self.view()
        if self._popup_container is None:
            self._popup_container = view.window()
            self._popup_window_flags = self._popup_container.windowFlags()
        popup = self._popup_container
        popup.setParent(parent, Qt.WindowType.Widget)
        rows = min(self.count(), max(1, self.maxVisibleItems()))
        height = rows * max(1, view.sizeHintForRow(0)) + 2 * popup.frameWidth()
        text_width = max(
            view.fontMetrics().horizontalAdvance(self.itemText(row))
            for row in range(self.count())
        )
        width = max(
            self.width(),
            view.sizeHintForColumn(self.modelColumn()) + 4,
            text_width + 24,
        )
        width = min(width, parent.width() - 4)
        height = min(height, parent.height() - 4)
        anchor = parent.mapFromGlobal(self.mapToGlobal(QPoint(0, self.height())))
        x = max(2, min(anchor.x(), parent.width() - width - 2))
        y = anchor.y()
        if y + height > parent.height() - 2:
            y -= self.height() + height
        popup.setGeometry(x, max(2, y), width, height)
        popup.show()
        popup.raise_()
        view.scrollTo(view.currentIndex())
        view.setFocus(Qt.FocusReason.PopupFocusReason)
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def hidePopup(self) -> None:  # noqa: N802
        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)
        popup = getattr(self, "_popup_container", None)
        if popup is None:
            return
        try:
            popup.hide()
            if not popup.isWindow():
                popup.setParent(self, self._popup_window_flags)
        except RuntimeError:
            self._popup_container = None

    def eventFilter(self, _watched, event) -> bool:  # noqa: N802
        popup = self._popup_container
        if popup is None or not isValid(popup) or not popup.isVisible():
            return False

        event_type = event.type()
        if (
            event_type == QEvent.Type.KeyPress
            and event.key() == Qt.Key.Key_Escape
        ):
            self.hidePopup()
            self.setFocus(Qt.FocusReason.PopupFocusReason)
            return True
        if event_type == QEvent.Type.MouseButtonPress:
            position = event.globalPosition().toPoint()
            if all(
                not widget.rect().contains(widget.mapFromGlobal(position))
                for widget in (self, popup)
            ):
                self.hidePopup()
        elif event_type in (
            QEvent.Type.ApplicationDeactivate,
            QEvent.Type.WindowDeactivate,
        ):
            self.hidePopup()
        return False


class EditorListItemDelegate(QStyledItemDelegate):
    """Paint compact title/metadata rows while retaining native controls."""

    def __init__(self, parent=None, *, row_height: int = 36):
        super().__init__(parent)
        self._row_height = max(34, int(row_height))

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(super().sizeHint(option, index).width(), self._row_height)

    def paint(
        self,
        painter: QPainter,
        option: QStyleOptionViewItem,
        index,
    ) -> None:
        styled = QStyleOptionViewItem(option)
        self.initStyleOption(styled, index)
        styled.text = ""
        style = styled.widget.style() if styled.widget else None
        if style is None:
            return super().paint(painter, option, index)
        style.drawControl(
            QStyle.ControlElement.CE_ItemViewItem,
            styled,
            painter,
            styled.widget,
        )

        rect = style.subElementRect(
            QStyle.SubElement.SE_ItemViewItemText,
            styled,
            styled.widget,
        ).adjusted(7, 2, -5, -2)
        selected = bool(styled.state & QStyle.StateFlag.State_Selected)
        enabled = bool(styled.state & QStyle.StateFlag.State_Enabled)
        palette = styled.palette
        title_color = (
            palette.highlightedText().color()
            if selected
            else palette.text().color()
        )
        meta_color = QColor(title_color)
        meta_color.setAlpha(165 if enabled else 90)

        painter.save()
        title_font = QFont(styled.font)
        title_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(title_font)
        painter.setPen(title_color)
        painter.drawText(
            rect.adjusted(0, 0, 0, -16),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            str(index.data(EDITOR_TITLE_ROLE) or index.data() or ""),
        )
        painter.setFont(styled.font)
        painter.setPen(meta_color)
        painter.drawText(
            rect.adjusted(0, 16, 0, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            str(index.data(EDITOR_META_ROLE) or ""),
        )
        painter.restore()
