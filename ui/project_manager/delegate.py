from __future__ import annotations

from PySide6.QtCore import QEvent, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from .bookmarks import normalize_tag
from .constants import make_close_pixmap, make_plus_pixmap, make_star_pixmap

_ICON_WIDTH, _ICON_PADDING, _ICON_LEFT = 14, 0, 2
_ICON_TOP, _MIN_TEXT_WIDTH, _HIDDEN_TEXT_OFFSET = 4, 60, 4
_Action = tuple[QPixmap, str]
_TAG_PALETTE = (
    "#4caf50", "#2196f3", "#ff9800", "#9c27b0", "#e91e63",
    "#00bcd4", "#8bc34a", "#ff5722", "#607d8b", "#3f51b5",
)


def _chip_fg(color: QColor) -> QColor:
    luminance = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
    return QColor("#111111") if luminance > 150 else QColor("#ffffff")


def _tag_color(tag: str) -> QColor:
    """Return a deterministic color for a normalized tag."""
    color_hash = 0
    for character in normalize_tag(tag):
        color_hash = (color_hash * 31 + ord(character)) & 0xFFFFFFFF
    return QColor(_TAG_PALETTE[color_hash % len(_TAG_PALETTE)])


# ---------------------------------------------------------------------------
class _ActionIconsDelegate(QStyledItemDelegate):
    """Shared action-icon layout, painting, and hit testing.

    Subclasses implement `_row_actions(index)` returning a list of
    ``(pixmap, action_id)`` pairs and route clicks in ``editorEvent``.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.column_width = 200
        style = QApplication.instance().style()
        self.plus = make_plus_pixmap()
        self.open = style.standardIcon(QStyle.SP_ArrowRight).pixmap(_ICON_WIDTH, _ICON_WIDTH)
        self.star_on = make_star_pixmap(True)
        self.star_off = make_star_pixmap(False)

    def set_column_width(self, width: int):
        """Update current column width for icon visibility calculations."""
        self.column_width = width

    def _row_actions(self, index) -> list[_Action]:
        raise NotImplementedError

    def _action_layout(self, option, count: int) -> tuple[bool, int]:
        icon_space = _ICON_LEFT + count * _ICON_WIDTH + max(0, count - 1) * _ICON_PADDING
        visible = min(option.rect.width(), self.column_width) >= icon_space + _MIN_TEXT_WIDTH
        return visible, icon_space + _ICON_PADDING if visible else _HIDDEN_TEXT_OFFSET

    def paint(self, painter, option, index):
        actions = self._row_actions(index)
        visible, text_offset = self._action_layout(option, len(actions))
        if visible:
            x = option.rect.left() + _ICON_LEFT
            y = option.rect.top() + _ICON_TOP
            for icon, _action_id in actions:
                painter.drawPixmap(x, y, icon)
                x += _ICON_WIDTH + _ICON_PADDING

        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(text_offset, 0, 0, 0)
        if text_option.rect.width() > 0:
            super().paint(painter, text_option, index)

    def _action_at(self, event, option, count: int) -> int | None:
        visible, _text_offset = self._action_layout(option, count)
        if not visible:
            return None

        relative_x = event.pos().x() - option.rect.left()
        for i in range(count):
            left = _ICON_LEFT + i * (_ICON_WIDTH + _ICON_PADDING)
            if left <= relative_x <= left + _ICON_WIDTH:
                return i
        return None


# ---------------------------------------------------------------------------
class _ActionsDelegate(_ActionIconsDelegate):
    """System/Project files: bookmark, add/remove, and open actions."""

    def __init__(self, mgr, for_project: bool):
        parent = mgr.tree_proj if for_project else mgr.tree_sys
        super().__init__(parent)
        self.mgr, self.for_project = mgr, for_project

        self.close = make_close_pixmap()

    def _row_state(self, index):
        path = self.mgr._index_path(index)
        is_dir = self.mgr._index_is_dir(index)
        bookmark_info = self.mgr._bookmark_info_for_path(path, self.for_project)
        bookmarked = self.mgr.bookmarks.is_bookmarked(*bookmark_info)
        actions = []
        if self.for_project:
            actions.append((self.close, "primary"))
            actions.append((self.star_on if bookmarked else self.star_off, "bookmark"))
        else:
            actions.append((self.star_on if bookmarked else self.star_off, "bookmark"))
            actions.append((self.plus, "primary"))
            if not is_dir:
                actions.append((self.open, "open"))
        return actions, path, is_dir, bookmark_info

    def _row_actions(self, index):
        return self._row_state(index)[0]

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        actions, path, is_dir, bookmark_info = self._row_state(index)
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

        if action_id == "open" and not is_dir:
            self.mgr._open_in_editor(path, warn_project_copy=not self.for_project)
            return True

        return False


# ---------------------------------------------------------------------------
class _PakActionsDelegate(_ActionIconsDelegate):
    """PAK files/folders: bookmark, add-to-project, and open actions."""

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
        if not folder:
            actions.append((self.open, "open"))
        return actions, path, bool(folder), bookmark_info

    def _row_actions(self, index):
        return self._row_state(index)[0]

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
        actions, path, is_folder, bookmark_info = self._row_state(index)
        pos = self._action_at(ev, option, len(actions))
        if pos is None:
            return False

        action_id = actions[pos][1]
        if action_id == "bookmark":
            self.mgr.bookmarks.toggle_bookmark(*bookmark_info)
            return True
        if action_id == "primary":
            return self._extract_path(path)
        if action_id == "open" and not is_folder and path:
            self.mgr._open_pak_path_in_editor(path)
            return True
        return False


# ---------------------------------------------------------------------------
class _BookmarksDelegate(_ActionIconsDelegate):
    """Bookmarks list: open and unbookmark actions on the Path column."""

    def __init__(self, tree, open_bookmark, remove_bookmark):
        super().__init__(tree)
        self._open_bookmark = open_bookmark
        self._remove_bookmark = remove_bookmark

    def _row_actions(self, index):
        if index.column() != 0:
            return []
        return [(self.open, "open"), (self.star_on, "remove")]

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
        if action_id == "open":
            self._open_bookmark(bookmark_id)
            return True
        if action_id == "remove":
            self._remove_bookmark(bookmark_id)
            return True
        return False


# ---------------------------------------------------------------------------
class _ChipDelegate(QStyledItemDelegate):
    """Paint right-aligned colored chips; subclasses supply ``(label, color)`` pairs."""

    _CHIP_HEIGHT = 18
    _CHIP_SPACING = 4

    def _chips(self, index) -> list[tuple[str, QColor]]:
        raise NotImplementedError

    def _total_width(self, font_metrics, chips) -> int:
        if not chips:
            return 0
        labels_width = sum(font_metrics.horizontalAdvance(label) + 12 for label, _ in chips)
        return labels_width + self._CHIP_SPACING * (len(chips) - 1)

    def paint(self, painter, option, index):
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        chips = self._chips(index)
        if not chips:
            return
        painter.save()
        fm = option.fontMetrics
        total = self._total_width(fm, chips)
        x = option.rect.right() - 2 - total
        y = option.rect.top() + (option.rect.height() - self._CHIP_HEIGHT) // 2
        for label, color in chips:
            width = fm.horizontalAdvance(label) + 12
            rect = QRectF(x, y, width, self._CHIP_HEIGHT)
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(rect, self._CHIP_HEIGHT / 2, self._CHIP_HEIGHT / 2)
            painter.setPen(_chip_fg(color))
            painter.drawText(rect, Qt.AlignCenter, label)
            x += width + self._CHIP_SPACING
        painter.restore()

    def sizeHint(self, option, index):
        width = self._total_width(option.fontMetrics, self._chips(index)) + 4
        return QSize(width, self._CHIP_HEIGHT + 6)


class _TagChipsDelegate(_ChipDelegate):
    """Render a bookmark's tags as colored inline chips."""

    def _chips(self, index):
        tags = index.data(Qt.UserRole)
        return [(tag, _tag_color(tag)) for tag in tags] if isinstance(tags, (list, tuple)) else []


class _ScopeBadgeDelegate(_ChipDelegate):
    """Render a bookmark's scope as a colored badge."""

    _SCOPE_COLORS = {"pak": "#f59e0b", "project": "#4a7c59", "unpacked": "#64748b"}

    def _chips(self, index):
        label = str(index.data(Qt.DisplayRole) or "")
        if not label:
            return []
        color = QColor(self._SCOPE_COLORS.get(index.data(Qt.UserRole), self._SCOPE_COLORS["unpacked"]))
        return [(label, color)]
