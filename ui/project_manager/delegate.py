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
        self.column_width = 200  # Default column width

        style      = QApplication.instance().style()
        self.plus  = make_plus_pixmap()
        self.open  = style.standardIcon(QStyle.SP_DialogOpenButton)   .pixmap(14,14)
        self.close = style.standardIcon(QStyle.SP_MessageBoxCritical).pixmap(14,14)

    def set_column_width(self, width: int):
        """Update the current column width for icon visibility calculations."""
        self.column_width = width

    # ---------------- painting --------------------------------------
    def paint(self, painter, option, index):
        is_dir = index.model().isDir(index)
        available_width = min(option.rect.width(), self.column_width)
        
        # Only draw icons if there's sufficient space
        icon_space_needed = 40  # Space needed for both action icons
        text_min_space = 60     # Minimum space needed for filename text
        
        draw_icons = available_width >= (icon_space_needed + text_min_space)
        
        if draw_icons:
            x = option.rect.left() + 2
            painter.drawPixmap(x, option.rect.top()+4, self.close if self.for_project else self.plus)
            if not is_dir:
                painter.drawPixmap(x+18, option.rect.top()+4, self.open)
        
        # Adjust text area based on whether icons are drawn
        text_offset = icon_space_needed if draw_icons else 4
        opt = QStyleOptionViewItem(option)
        opt.rect = option.rect.adjusted(text_offset, 0, 0, 0)
        
        # Ensure text doesn't overflow the available space
        if opt.rect.width() > 0:
            super().paint(painter, opt, index)

    # ---------------- clicks ----------------------------------------
    def editorEvent(self, ev, model, option, index):
        if ev.type() != QEvent.MouseButtonRelease or ev.button() != Qt.LeftButton:
            return False
        
        available_width = min(option.rect.width(), self.column_width)
        icon_space_needed = 40
        text_min_space = 60
        
        # Only handle icon clicks if icons are visible
        if available_width < (icon_space_needed + text_min_space):
            return False
        
        relx   = ev.pos().x() - option.rect.left()
        path   = model.filePath(index)
        is_dir = model.isDir(index)

        # First icon (add/remove)
        if 0 <= relx <= 16:
            if not self.for_project:
                self.mgr._copy_to_project(path)       
            else:
                self.mgr._remove_from_project(path)    
            return True
        
        # Second icon (open) - only for files
        if 18 <= relx <= 34 and not is_dir:
            self.mgr._open_in_editor(path)
            return True
        
        return False