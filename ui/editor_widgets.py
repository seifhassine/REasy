from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter
from PySide6.QtWidgets import (
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)


EDITOR_TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 100
EDITOR_META_ROLE = EDITOR_TITLE_ROLE + 1


class EditorListItemDelegate(QStyledItemDelegate):
    """Paint compact title/metadata rows while retaining native controls."""

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(super().sizeHint(option, index).width(), 44)

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
        ).adjusted(7, 3, -5, -3)
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
            rect.adjusted(0, 0, 0, -15),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            str(index.data(EDITOR_TITLE_ROLE) or index.data() or ""),
        )
        painter.setFont(styled.font)
        painter.setPen(meta_color)
        painter.drawText(
            rect.adjusted(0, 18, 0, 0),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            str(index.data(EDITOR_META_ROLE) or ""),
        )
        painter.restore()
