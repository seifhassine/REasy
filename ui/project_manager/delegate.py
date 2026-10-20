from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPalette, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
)

lazy from .bookmarks import normalize_tag
lazy from .constants import make_close_pixmap, make_plus_pixmap, make_star_pixmap

_ACTION_HIT_SIZE, _ACTION_SPACING = 26, 1
_ACTION_RIGHT, _MIN_TEXT_WIDTH = 3, 60
_Action = tuple[QPixmap, str]
_BOOKMARK_SCOPE_COLORS = {"pak": "#f59e0b", "project": "#4a7c59", "unpacked": "#64748b"}
_TAG_PALETTE = (
    "#4caf50", "#2196f3", "#ff9800", "#9c27b0", "#e91e63",
    "#00bcd4", "#8bc34a", "#ff5722", "#607d8b", "#3f51b5",
)


def _chip_fg(color: QColor) -> QColor:
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return QColor("#111111") if luminance > 150 else QColor("#ffffff")


def _event_point(event) -> QPoint:
    """``pos()`` is deprecated on mouse events; help events have only ``pos()``."""
    position = getattr(event, "position", None)
    return position().toPoint() if callable(position) else event.pos()


def _tag_color(tag: str) -> QColor:
    color_hash = 0
    for character in normalize_tag(tag):
        color_hash = (color_hash * 31 + ord(character)) & 0xFFFFFFFF
    return QColor(_TAG_PALETTE[color_hash % len(_TAG_PALETTE)])


class _ActionIconsDelegate(QStyledItemDelegate):
    """Subclasses route clicks in ``editorEvent``."""

    def __init__(self, parent):
        super().__init__(parent)
        self.column_width = 200
        self.plus = make_plus_pixmap()
        self.star_on = make_star_pixmap(True)
        self.star_off = make_star_pixmap(False)
        parent.setMouseTracking(True)
        parent.viewport().setMouseTracking(True)

    def set_column_width(self, width: int):
        self.column_width = width

    def _row_actions(self, index) -> list[_Action]:
        raise NotImplementedError

    def _action_layout(self, option, count: int) -> tuple[bool, int]:
        action_space = count * _ACTION_HIT_SIZE + max(0, count - 1) * _ACTION_SPACING
        visible = min(option.rect.width(), self.column_width) >= action_space + _MIN_TEXT_WIDTH
        return visible, action_space + _ACTION_RIGHT if visible else 0

    def _action_rects(self, option, count: int) -> list[QRect]:
        visible, _reserved = self._action_layout(option, count)
        if not visible:
            return []
        y = option.rect.top() + max(0, (option.rect.height() - _ACTION_HIT_SIZE) // 2)
        right = option.rect.right() - _ACTION_RIGHT + 1
        rects = []
        for index in reversed(range(count)):
            left = right - _ACTION_HIT_SIZE
            rects.append((index, QRect(left, y, _ACTION_HIT_SIZE, _ACTION_HIT_SIZE)))
            right = left - _ACTION_SPACING
        return [rect for _index, rect in sorted(rects)]

    @staticmethod
    def _actions_revealed(option) -> bool:
        reveal_states = QStyle.State_MouseOver | QStyle.State_Selected | QStyle.State_HasFocus
        return bool(option.state & reveal_states)

    def _action_tooltip(self, action_id: str, index) -> str:
        return action_id.replace("_", " ").title()

    def paint(self, painter, option, index):
        actions = self._row_actions(index)
        visible, reserved = self._action_layout(option, len(actions))
        rects = self._action_rects(option, len(actions))
        if visible and self._actions_revealed(option):
            painter.save()
            for rect, (icon, _action_id) in zip(rects, actions):
                if option.state & QStyle.State_MouseOver:
                    painter.fillRect(rect.adjusted(2, 2, -2, -2), QColor(255, 255, 255, 18))
                x = rect.left() + (rect.width() - icon.width()) // 2
                y = rect.top() + (rect.height() - icon.height()) // 2
                painter.drawPixmap(x, y, icon)
            painter.restore()

        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(0, 0, -reserved, 0)
        if text_option.rect.width() > 0:
            super().paint(painter, text_option, index)

    def _action_at(self, event, option, count: int) -> int | None:
        if not self._actions_revealed(option):
            return None
        point = _event_point(event)
        for action_index, rect in enumerate(self._action_rects(option, count)):
            if rect.contains(point):
                return action_index
        return None

    def helpEvent(self, event, view, option, index):
        if event.type() != QEvent.ToolTip:
            return super().helpEvent(event, view, option, index)
        actions = self._row_actions(index)
        action_index = self._action_at(event, option, len(actions))
        if action_index is None:
            return super().helpEvent(event, view, option, index)
        QToolTip.showText(
            event.globalPos(),
            self._action_tooltip(actions[action_index][1], index),
            view,
            option.rect,
        )
        return True

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        hint.setHeight(max(_ACTION_HIT_SIZE, hint.height()))
        return hint


class _ActionsDelegate(_ActionIconsDelegate):
    """System/Project files: bookmark and add/remove actions."""

    def __init__(self, mgr, for_project: bool):
        parent = mgr.tree_proj if for_project else mgr.tree_sys
        super().__init__(parent)
        self.mgr, self.for_project = mgr, for_project

        self.close = make_close_pixmap()

    def _row_state(self, index):
        path = self.mgr._index_path(index)
        bookmark_info = self.mgr._bookmark_info_for_path(path, self.for_project)
        bookmarked = self.mgr.bookmarks.is_bookmarked(*bookmark_info)
        actions = []
        if self.for_project:
            actions.append((self.close, "primary"))
            actions.append((self.star_on if bookmarked else self.star_off, "bookmark"))
        else:
            actions.append((self.star_on if bookmarked else self.star_off, "bookmark"))
            actions.append((self.plus, "primary"))
        return actions, path, bookmark_info

    def _row_actions(self, index):
        return self._row_state(index)[0]

    def _action_tooltip(self, action_id: str, index) -> str:
        if action_id == "bookmark":
            return self.tr("Add or remove bookmark")
        if action_id == "primary":
            return self.tr("Remove from project") if self.for_project else self.tr("Extract to project")
        return super()._action_tooltip(action_id, index)

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        actions, path, bookmark_info = self._row_state(index)
        pos = self._action_at(ev, option, len(actions))
        if pos is None:
            return False

        action_id = actions[pos][1]
        if action_id == "bookmark":
            self.mgr.bookmarks.toggle_bookmark(*bookmark_info)
            return True

        if action_id == "primary":
            if not self.for_project:
                self.mgr._copy_to_project(path)
            else:
                self.mgr._remove_from_project(path)
            return True

        return False


class _PakActionsDelegate(_ActionIconsDelegate):
    """PAK files/folders: bookmark and add-to-project actions."""

    def __init__(self, mgr):
        super().__init__(mgr.tree_pak)
        self.mgr = mgr

    @staticmethod
    def _leaf_path(index):
        path = index.data(Qt.UserRole + 1)
        if not isinstance(path, str) or not path:
            path = index.data(Qt.DisplayRole)
        return path if isinstance(path, str) else ""

    @staticmethod
    def _folder_path(index):
        path = index.data(Qt.UserRole + 2)
        return path if isinstance(path, str) else ""

    def _row_state(self, index):
        folder = self._folder_path(index)
        path = folder or self._leaf_path(index)
        bookmark_info = ("pak", path, "", self.mgr.current_game or "")
        actions = []
        if path:
            bookmarked = self.mgr.bookmarks.is_bookmarked(*bookmark_info)
            actions.append((self.star_on if bookmarked else self.star_off, "bookmark"))
        actions.append((self.plus, "primary"))
        return actions, path, bookmark_info

    def _row_actions(self, index):
        return self._row_state(index)[0]

    def _action_tooltip(self, action_id: str, index) -> str:
        return {
            "bookmark": self.tr("Add or remove bookmark"),
            "primary": self.tr("Extract to project"),
        }.get(action_id, super()._action_tooltip(action_id, index))

    def _extract_path(self, path) -> bool:
        if not isinstance(path, str) or not path:
            return False
        if path.endswith('/'):
            self.mgr._extract_folder_by_prefix(path)
        else:
            self.mgr._extract_from_paks_to_project([path])
        return True

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        actions, path, bookmark_info = self._row_state(index)
        pos = self._action_at(ev, option, len(actions))
        if pos is None:
            return False

        action_id = actions[pos][1]
        if action_id == "bookmark":
            self.mgr.bookmarks.toggle_bookmark(*bookmark_info)
            return True
        if action_id == "primary":
            return self._extract_path(path)
        return False


class _BookmarkRowDelegate(_ActionIconsDelegate):
    def __init__(self, tree, remove_bookmark):
        super().__init__(tree)
        self._remove_bookmark = remove_bookmark

    def _row_actions(self, index):
        if index.column() != 0:
            return []
        return [(self.star_on, "remove")]

    def _action_tooltip(self, action_id: str, index) -> str:
        return self.tr("Remove bookmark")

    def helpEvent(self, event, view, option, index):
        if event.type() == QEvent.ToolTip:
            hidden = self._hidden_tags_at(event, option, index)
            if hidden:
                QToolTip.showText(event.globalPos(), hidden, view, option.rect)
                return True
        return super().helpEvent(event, view, option, index)

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        actions = self._row_actions(index)
        pos = self._action_at(ev, option, len(actions))
        if pos is None:
            return False

        bookmark_id = index.data(Qt.UserRole)
        if not isinstance(bookmark_id, str) or not bookmark_id:
            return False
        action_id = actions[pos][1]
        if action_id == "remove":
            self._remove_bookmark(bookmark_id)
            return True
        return False

    _CHIP_HEIGHT = 14
    _CHIP_SPACING = 4
    _CHIP_MAX_SHARE = 0.7

    def _subtitle_rect(self, option, reserved: int = 0) -> QRect:
        rect = option.rect.adjusted(8, 5, -reserved - 8, -5)
        return QRect(rect.left(), rect.top() + max(22, option.fontMetrics.height()) + 2,
                     rect.width(), max(18, option.fontMetrics.height()))

    def _tag_chip_layout(self, metrics, line: QRect, tags):
        if not tags or line.width() <= 0:
            return [], line.left()
        height = max(self._CHIP_HEIGHT, min(line.height(), metrics.height()))
        top = line.top() + (line.height() - height) // 2
        limit = int(line.width() * self._CHIP_MAX_SHARE)

        def chip_width(label: str) -> int:
            return metrics.horizontalAdvance(label) + 12

        entries: list[tuple[str, bool]] = []
        used = 0
        for index, tag in enumerate(tags):
            label = tag
            width = chip_width(label)
            if width > limit:
                label = metrics.elidedText(tag, Qt.ElideRight, max(0, limit - 12))
                width = chip_width(label)
            remaining = len(tags) - index - 1
            reserve = self._CHIP_SPACING + chip_width(f"+{remaining}") if remaining else 0
            step = width if not entries else width + self._CHIP_SPACING
            if entries and used + step + reserve > limit:
                break
            entries.append((label, False))
            used += step
        if overflow := len(tags) - len(entries):
            entries.append((f"+{overflow}", True))
        widths = [chip_width(label) for label, _overflow in entries]
        total = sum(widths) + self._CHIP_SPACING * (len(widths) - 1)
        left = max(line.left(), line.right() + 1 - total)
        chips = []
        for (label, overflow), width in zip(entries, widths):
            color = (QColor(_BOOKMARK_SCOPE_COLORS["unpacked"]) if overflow
                     else _tag_color(label))
            chips.append((QRect(left, top, width, height), label, color, overflow))
            left += width + self._CHIP_SPACING
        return chips, chips[0][0].left()

    def _hidden_tags_at(self, event, option, index) -> str:
        bookmark = index.data(Qt.UserRole + 1)
        if bookmark is None:
            return ""
        actions = self._row_actions(index)
        _visible, reserved = self._action_layout(option, len(actions))
        chips, _left = self._tag_chip_layout(
            option.fontMetrics, self._subtitle_rect(option, reserved), bookmark.tags)
        point = _event_point(event)
        for position, (chip, _label, _color, overflow) in enumerate(chips):
            if overflow and chip.contains(point):
                return self.tr("Tags: {tags}").format(tags=", ".join(bookmark.tags[position:]))
        return ""

    @staticmethod
    def _paint_chip(painter, chip: QRect, label: str, color: QColor) -> None:
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(QRectF(chip), chip.height() / 2, chip.height() / 2)
        painter.setPen(_chip_fg(color))
        painter.drawText(chip, Qt.AlignCenter, label)

    def paint(self, painter, option, index):
        bookmark = index.data(Qt.UserRole + 1)
        if bookmark is None:
            return super().paint(painter, option, index)
        background = QStyleOptionViewItem(option)
        self.initStyleOption(background, index)
        background.text = ""
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, background, painter, option.widget)
        actions = self._row_actions(index)
        visible, reserved = self._action_layout(option, len(actions))
        painter.save()
        painter.setClipRect(option.rect)
        rect = option.rect.adjusted(8, 5, -reserved - 8, -5)
        if rect.width() <= 0:
            painter.restore()
            return
        selected = bool(option.state & QStyle.State_Selected)
        foreground = option.palette.color(QPalette.HighlightedText if selected else QPalette.Text)
        muted = QColor(foreground)
        muted.setAlpha(175)
        scope = index.data(Qt.UserRole + 2)
        badge_width = min(rect.width(), option.fontMetrics.horizontalAdvance(scope) + 12)
        badge = QRectF(rect.right() - badge_width, rect.top() + 1,
                       badge_width, max(18, option.fontMetrics.height() + 2))
        color = QColor(_BOOKMARK_SCOPE_COLORS[bookmark.scope])
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        painter.drawRoundedRect(badge, 6, 6)
        painter.setPen(_chip_fg(color))
        painter.drawText(badge, Qt.AlignCenter, scope)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        line_height = max(22, option.fontMetrics.height())
        title = QRect(rect.left(), rect.top(), max(0, rect.width() - badge_width - 8), line_height)
        painter.setPen(foreground)
        painter.drawText(title, Qt.AlignVCenter, painter.fontMetrics().elidedText(index.data(), Qt.ElideMiddle, title.width()))
        painter.setFont(option.font)
        metrics = option.fontMetrics
        subtitle = self._subtitle_rect(option, reserved)
        chips, chips_left = self._tag_chip_layout(metrics, subtitle, bookmark.tags)
        directory = bookmark.path.rstrip('/').rpartition('/')[0]
        if directory:
            width = chips_left - 8 - subtitle.left() if chips else subtitle.width()
            if width > 16:
                painter.setPen(muted)
                painter.drawText(
                    QRect(subtitle.left(), subtitle.top(), width, subtitle.height()),
                    Qt.AlignVCenter,
                    metrics.elidedText(directory, Qt.ElideMiddle, width),
                )
        for chip, label, color, _overflow in chips:
            self._paint_chip(painter, chip, label, color)
        if visible and self._actions_revealed(option):
            for action_rect, (icon, _action_id) in zip(self._action_rects(option, len(actions)), actions):
                painter.drawPixmap(action_rect.center().x() - icon.width() // 2,
                                   action_rect.center().y() - icon.height() // 2, icon)
        painter.restore()

    def sizeHint(self, option, index):
        return QSize(120, max(56, option.fontMetrics.height() * 2 + 14))

