from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor


class HighlightManager(QObject):
    highlight_toggled = Signal(bool)
    highlight_color_changed = Signal(QColor)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._enabled = False
        self._highlight_color = QColor(255, 165, 0)
        self._highlighted_items = set()
    
    @property
    def enabled(self):
        return self._enabled
    
    def toggle(self):
        self._enabled = not self._enabled
        self.highlight_toggled.emit(self._enabled)
        return self._enabled
    
    def set_enabled(self, enabled):
        if self._enabled != enabled:
            self._enabled = enabled
            self.highlight_toggled.emit(self._enabled)
    
    @property
    def highlight_color(self):
        return self._highlight_color
    
    def set_highlight_color(self, color):
        if isinstance(color, QColor) and color != self._highlight_color:
            self._highlight_color = color
            self.highlight_color_changed.emit(color)
    
    def add_highlighted_item(self, item_id):
        self._highlighted_items.add(item_id)
    
    def remove_highlighted_item(self, item_id):
        self._highlighted_items.discard(item_id)
    
    def is_item_highlighted(self, item_id):
        return item_id in self._highlighted_items
    
    def clear_highlights(self):
        self._highlighted_items.clear()
