from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QWidget,
)
from shiboken6 import isValid

lazy from ui.icons import line_icon
lazy from ui.styles import current_colors, set_style_role


EDITOR_TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 100
EDITOR_META_ROLE = EDITOR_TITLE_ROLE + 1


def styled_label(text: str = "", parent=None, **roles) -> QLabel:
    label = QLabel(text, parent)
    if roles:
        set_style_role(label, **roles)
    return label


def toolbar_separator(height: int = 16) -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.VLine)
    line.setFixedHeight(height)
    return line


def tool_button(icon: str, tooltip: str = "", slot=None, *, text: str = "", parent=None) -> QToolButton:
    """Flat icon button; with `text` it shows the text beside the icon."""
    button = QToolButton(parent)
    button.setAutoRaise(True)
    button.setIcon(line_icon(icon))
    button.setToolTip(tooltip)
    if text:
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    else:
        button.setAccessibleName(tooltip)
    if slot is not None:
        button.clicked.connect(slot)
    return button


def editor_strip(surface: str = "toolbar", margins=(8, 4, 8, 4), spacing: int = 4) -> tuple[QWidget, QHBoxLayout]:
    strip = QWidget()
    set_style_role(strip, surface=surface)
    row = QHBoxLayout(strip)
    row.setContentsMargins(*margins)
    row.setSpacing(spacing)
    return strip, row


def strip_button(button, icon: str, *, variant: str = "ghost", tooltip: str = ""):
    button.setIcon(line_icon(icon))
    if tooltip:
        button.setToolTip(tooltip)
    set_style_role(button, variant=variant, compact=True)
    return button


def pane_header(title, *trailing) -> QWidget:
    """Header strip with a panel title (text or label) and trailing widgets on the right."""
    header, row = editor_strip("header")
    label = title if isinstance(title, QLabel) else QLabel(title)
    set_style_role(label, heading="panel")
    row.addWidget(label, 1)
    for widget in trailing:
        row.addWidget(widget)
    return header


def delete_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setIcon(line_icon("delete", current_colors()["danger"]))
    set_style_role(button, variant="danger")
    button.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
    return button


def icon_action(menu, icon: str, text: str, danger: bool = False):
    return menu.addAction(line_icon(icon, current_colors()["danger"] if danger else None), text)


def set_invalid(widget, invalid: bool) -> None:
    if bool(widget.property("invalid")) != invalid:
        set_style_role(widget, invalid=invalid)


def search_field(edit: QLineEdit) -> QLineEdit:
    edit.setClearButtonEnabled(True)
    edit.addAction(line_icon("search"), QLineEdit.ActionPosition.LeadingPosition)
    return edit


class DisclosureButton(QToolButton):
    """Checkable text button with a chevron that follows its checked state."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.setText(text)
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        set_style_role(self, variant="disclosure")
        self.toggled.connect(self._update_icon)
        self._update_icon(False)

    def _update_icon(self, expanded: bool) -> None:
        self.setIcon(line_icon("chevron_down" if expanded else "chevron_right"))


class ElidedLabel(QLabel):
    """One-line label that elides its text instead of widening the layout."""

    def __init__(self, parent=None, mode=Qt.ElideRight):
        super().__init__(parent)
        self._mode = mode
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, _event):
        painter = QPainter(self)
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), self._mode, rect.width())
        self.style().drawItemText(painter, rect, Qt.AlignLeft | Qt.AlignVCenter, self.palette(),
                                  self.isEnabled(), text, self.foregroundRole())


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

    @staticmethod
    def _title_font(font: QFont) -> QFont:
        title_font = QFont(font)
        title_font.setWeight(QFont.Weight.DemiBold)
        return title_font

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        lines = QFontMetrics(self._title_font(option.font)).height() + option.fontMetrics.height()
        return QSize(super().sizeHint(option, index).width(), max(self._row_height, lines + 4))

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

        title_font = self._title_font(styled.font)
        title_metrics = QFontMetrics(title_font)
        meta_metrics = QFontMetrics(styled.font)
        title_height, meta_height = title_metrics.height(), meta_metrics.height()
        top = rect.top() + max(0, (rect.height() - title_height - meta_height) // 2)
        align = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        title = str(index.data(EDITOR_TITLE_ROLE) or index.data() or "")
        meta = str(index.data(EDITOR_META_ROLE) or "")

        painter.save()
        painter.setFont(title_font)
        painter.setPen(title_color)
        painter.drawText(
            QRect(rect.left(), top, rect.width(), title_height),
            align,
            title_metrics.elidedText(title, Qt.TextElideMode.ElideRight, rect.width()),
        )
        painter.setFont(styled.font)
        painter.setPen(meta_color)
        painter.drawText(
            QRect(rect.left(), top + title_height, rect.width(), meta_height),
            align,
            meta_metrics.elidedText(meta, Qt.TextElideMode.ElideRight, rect.width()),
        )
        painter.restore()
