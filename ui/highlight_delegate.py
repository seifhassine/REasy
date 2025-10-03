from PySide6.QtWidgets import QStyledItemDelegate
from PySide6.QtGui import QPalette


class HighlightDelegate(QStyledItemDelegate):
    def __init__(self, highlight_manager=None, parent=None):
        super().__init__(parent)
        self.highlight_manager = highlight_manager
        self.default_row_height = 24
        
    def paint(self, painter, option, index):
        should_highlight = False
        if self.highlight_manager and index.isValid():
            item_id = self._get_index_identifier(index)
            should_highlight = self.highlight_manager.is_item_highlighted(item_id)
        
        if should_highlight:
            modified_option = option
            modified_option.palette.setColor(QPalette.Text, self.highlight_manager.highlight_color)
            modified_option.palette.setColor(QPalette.HighlightedText, self.highlight_manager.highlight_color)
            super().paint(painter, modified_option, index)
        else:
            super().paint(painter, option, index)
    
    def _get_index_identifier(self, index):
        path = []
        current = index
        while current.isValid():
            path.append(current.row())
            current = current.parent()
        return tuple(reversed(path))
    
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        tree_view = self.parent()
        if tree_view and hasattr(tree_view, 'default_row_height'):
            self.default_row_height = tree_view.default_row_height
        size.setHeight(self.default_row_height)
        return size
