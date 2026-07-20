from __future__ import annotations
from PySide6.QtCore    import Qt, QEvent
from PySide6.QtWidgets import QApplication, QStyledItemDelegate, QStyle, QStyleOptionViewItem

from .constants import make_plus_pixmap

_ICON_WIDTH, _ICON_PADDING, _ICON_LEFT = 14, 0, 2
_ICON_TOP, _MIN_TEXT_WIDTH, _HIDDEN_TEXT_OFFSET = 4, 60, 4


# ---------------------------------------------------------------------------
class _ActionIconsDelegate(QStyledItemDelegate):
    """Shared action-icon layout, painting, and hit testing."""

    def __init__(self, parent):
        super().__init__(parent)
        self.column_width = 200
        style = QApplication.instance().style()
        self.plus = make_plus_pixmap()
        self.open = style.standardIcon(QStyle.SP_ArrowRight).pixmap(_ICON_WIDTH, _ICON_WIDTH)

    def set_column_width(self, width: int):
        """Update current column width for icon visibility calculations."""
        self.column_width = width

    def _action_layout(self, option, show_open: bool) -> tuple[bool, int]:
        icon_space = _ICON_LEFT + _ICON_WIDTH + ((_ICON_PADDING + _ICON_WIDTH) if show_open else 0)
        visible = min(option.rect.width(), self.column_width) >= icon_space + _MIN_TEXT_WIDTH
        return visible, icon_space + _ICON_PADDING if visible else _HIDDEN_TEXT_OFFSET

    def _paint_actions(self, painter, option, index, primary_icon, show_open: bool):
        visible, text_offset = self._action_layout(option, show_open)
        if visible:
            x = option.rect.left() + _ICON_LEFT
            y = option.rect.top() + _ICON_TOP
            painter.drawPixmap(x, y, primary_icon)
            if show_open:
                painter.drawPixmap(x + _ICON_WIDTH + _ICON_PADDING, y, self.open)

        text_option = QStyleOptionViewItem(option)
        text_option.rect = option.rect.adjusted(text_offset, 0, 0, 0)
        if text_option.rect.width() > 0:
            super().paint(painter, text_option, index)

    def _action_at(self, event, option, show_open: bool) -> str | None:
        visible, _text_offset = self._action_layout(option, show_open)
        if not visible:
            return None

        relative_x = event.pos().x() - option.rect.left()
        if _ICON_LEFT <= relative_x <= _ICON_LEFT + _ICON_WIDTH:
            return "primary"

        open_left = _ICON_LEFT + _ICON_WIDTH + _ICON_PADDING
        return "open" if show_open and open_left <= relative_x <= open_left + _ICON_WIDTH else None


# ---------------------------------------------------------------------------
class _ActionsDelegate(_ActionIconsDelegate):
    """Draws icons and routes clicks to ProjectManager helpers."""

    def __init__(self, mgr, for_project: bool):
        parent = mgr.tree_proj if for_project else mgr.tree_sys
        super().__init__(parent)
        self.mgr, self.for_project = mgr, for_project

        style = QApplication.instance().style()
        self.close = style.standardIcon(QStyle.SP_MessageBoxCritical).pixmap(_ICON_WIDTH, _ICON_WIDTH)

    # ---------------- painting --------------------------------------
    def paint(self, painter, option, index):
        is_dir = index.model().isDir(index)
        primary_icon = self.close if self.for_project else self.plus
        self._paint_actions(painter, option, index, primary_icon, not is_dir)

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        is_dir = model.isDir(index)
        action = self._action_at(ev, option, not is_dir)
        if action is None:
            return False

        path = model.filePath(index)

        # First icon (add/remove)
        if action == "primary":
            if not self.for_project:
                self.mgr._copy_to_project(path)       
            else:
                self.mgr._remove_from_project(path)    
            return True

        # Second icon (open) - only for files
        if action == "open" and not is_dir:
            self.mgr._open_in_editor(path, warn_project_copy=not self.for_project)
            return True

        return False


# ---------------------------------------------------------------------------
class _PakActionsDelegate(_ActionIconsDelegate):
    """Draws action icons (+ and open) and uses model decoration for type icons."""

    def __init__(self, mgr):
        super().__init__(mgr.tree_pak)
        self.mgr = mgr

    def paint(self, painter, option, index):
        is_leaf = not index.model().hasChildren(index)
        self._paint_actions(painter, option, index, self.plus, is_leaf)

    def _extract_path(self, path) -> bool:
        if not isinstance(path, str) or not path:
            return False
        if path.endswith('/'):
            if self.mgr and hasattr(self.mgr, "_extract_folder_by_prefix"):
                self.mgr._extract_folder_by_prefix(path)
        else:
            self.mgr._extract_from_paks_to_project([path])
        return True

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        is_leaf = not model.hasChildren(index)
        action = self._action_at(ev, option, is_leaf)
        if action is None:
            return False

        path = index.data(Qt.UserRole + 1)
        if not isinstance(path, str) or not path:
            path = index.data(Qt.DisplayRole)

        if action == "primary":
            return self._extract_path(path)
        if action == "open" and is_leaf and isinstance(path, str) and path:
            self.mgr._open_pak_path_in_editor(path)
            return True
        return False
