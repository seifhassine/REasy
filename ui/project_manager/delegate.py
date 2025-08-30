from __future__ import annotations
from PySide6.QtCore    import Qt, QEvent
from PySide6.QtWidgets import QApplication, QStyledItemDelegate, QStyle, QStyleOptionViewItem

from .constants import make_plus_pixmap

# ---------------------------------------------------------------------------
class _ActionsDelegate(QStyledItemDelegate):
    """Draws icons and routes clicks to ProjectManager helpers."""

    def __init__(self, mgr, for_project: bool):
        parent = mgr.tree_proj if for_project else mgr.tree_sys
        super().__init__(parent)
        self.mgr, self.for_project = mgr, for_project
        self.column_width = 200

        style      = QApplication.instance().style()
        self.plus  = make_plus_pixmap()
        self.open  = style.standardIcon(QStyle.SP_ArrowRight).pixmap(14,14)
        self.dir_ic  = style.standardIcon(QStyle.SP_DirIcon).pixmap(14,14)
        self.file_ic = style.standardIcon(QStyle.SP_FileIcon).pixmap(14,14)
        self.close = style.standardIcon(QStyle.SP_MessageBoxCritical).pixmap(14,14)

    def set_column_width(self, width: int):
        """Update current column width for icon visibility calculations."""
        self.column_width = width

    # ---------------- painting --------------------------------------
    def paint(self, painter, option, index):
        is_dir = index.model().isDir(index)
        available_width = min(option.rect.width(), self.column_width)

        ICON_W, PAD, LEFT = 16, 4, 2
        icon_space_needed = LEFT + ICON_W + (PAD + ICON_W if not is_dir else 0)
        text_min_space = 60

        draw_icons = available_width >= (icon_space_needed + text_min_space)

        if draw_icons:
            x = option.rect.left() + LEFT
            painter.drawPixmap(x, option.rect.top()+4, self.close if self.for_project else self.plus)
            if not is_dir:
                painter.drawPixmap(x + ICON_W + PAD, option.rect.top()+4, self.open)

        text_offset = (icon_space_needed + PAD if draw_icons else 4)
        opt = QStyleOptionViewItem(option)
        opt.rect = option.rect.adjusted(text_offset, 0, 0, 0)
        
        if opt.rect.width() > 0:
            super().paint(painter, opt, index)

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        
        available_width = min(option.rect.width(), self.column_width)
        is_dir = model.isDir(index)
        ICON_W, PAD, LEFT = 16, 4, 2
        icon_space_needed = LEFT + ICON_W + (PAD + ICON_W if not is_dir else 0)
        text_min_space = 60
        
        # Only handle icon clicks if icons are visible
        if available_width < (icon_space_needed + text_min_space):
            return False
        
        relx   = ev.pos().x() - option.rect.left()
        path   = model.filePath(index)
        is_dir = model.isDir(index)

        # First icon (add/remove)
        if LEFT <= relx <= LEFT + ICON_W:
            if not self.for_project:
                self.mgr._copy_to_project(path)       
            else:
                self.mgr._remove_from_project(path)    
            return True
        
        open_l = LEFT + ICON_W + PAD
        open_r = open_l + ICON_W
        # Second icon (open) - only for files
        if open_l <= relx <= open_r and not is_dir:
            self.mgr._open_in_editor(path)
            return True
        
        return False


# ---------------------------------------------------------------------------
class _PakActionsDelegate(QStyledItemDelegate):
    """Draws action icons (+ and open) and uses model decoration for type icons."""

    def __init__(self, mgr):
        super().__init__(mgr.tree_pak)
        self.mgr = mgr
        self.column_width = 200

        style      = QApplication.instance().style()
        self.plus    = make_plus_pixmap()
        self.open    = style.standardIcon(QStyle.SP_ArrowRight).pixmap(14,14)

    def set_column_width(self, width: int):
        self.column_width = width

    def paint(self, painter, option, index):
        model = index.model()
        is_leaf = not model.hasChildren(index)
        available_width = min(option.rect.width(), self.column_width)
        ICON_W, PAD, LEFT = 16, 4, 2
        icon_space_needed = (LEFT + ICON_W + (PAD + ICON_W if is_leaf else 0))
        text_min_space = 60
        draw_icons = available_width >= (icon_space_needed + text_min_space)

        if draw_icons:
            x = option.rect.left() + LEFT
            painter.drawPixmap(x, option.rect.top()+4, self.plus)
            if is_leaf:
                painter.drawPixmap(x + ICON_W + PAD, option.rect.top()+4, self.open)

        text_offset = (icon_space_needed + PAD if draw_icons else 4)
        opt = QStyleOptionViewItem(option)
        opt.rect = option.rect.adjusted(text_offset, 0, 0, 0)
        if opt.rect.width() > 0:
            super().paint(painter, opt, index)

    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        is_leaf = not model.hasChildren(index)
        available_width = min(option.rect.width(), self.column_width)
        ICON_W, PAD, LEFT = 16, 4, 2
        icon_space_needed = (LEFT + ICON_W + (PAD + ICON_W if is_leaf else 0))
        text_min_space = 60
        if available_width < (icon_space_needed + text_min_space):
            return False
        relx = ev.pos().x() - option.rect.left()
        path = index.data(Qt.UserRole + 1)
        if not isinstance(path, str) or not path:
            path = index.data(Qt.UserRole + 2)

        if LEFT <= relx <= LEFT + ICON_W:
            if isinstance(path, str) and path:
                if path.endswith('/'):
                    if self.mgr and hasattr(self.mgr, "_extract_folder_by_prefix"):
                        self.mgr._extract_folder_by_prefix(path)
                else:
                    self.mgr._extract_from_paks_to_project([path])
                return True
            return False
        open_l = LEFT + ICON_W + PAD
        open_r = open_l + ICON_W
        if is_leaf and open_l <= relx <= open_r:
            if isinstance(path, str) and path:
                self.mgr._open_pak_path_in_editor(path)
                return True
        return False