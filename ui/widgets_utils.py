from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton
from PySide6.QtGui import QColor, QPainter, QPixmap, QPen, QBrush


# ============================================================================
# Color preview UI utilities
# ============================================================================

CHECKER_LIGHT = QColor(255, 255, 255)
CHECKER_DARK = QColor(204, 204, 204)
CHECKER_SIZE = 4  # Size of each square in pixels


def _paint_checkerboard(painter, rect):
    painter.save()
    painter.setClipRect(rect)

    for y in range(rect.top(), rect.bottom() + 1, CHECKER_SIZE):
        for x in range(rect.left(), rect.right() + 1, CHECKER_SIZE):
            col = (x - rect.left()) // CHECKER_SIZE
            row = (y - rect.top()) // CHECKER_SIZE
            color = CHECKER_LIGHT if (col + row) % 2 == 0 else CHECKER_DARK
            painter.fillRect(x, y, CHECKER_SIZE, CHECKER_SIZE, color)

    painter.restore()


def create_color_preview_pixmap(r, g, b, a=255, width=24, height=24):
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.transparent)
    
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, False)
    
    color = QColor(int(r), int(g), int(b), int(a))
    rect = pixmap.rect()
    
    if a < 255:
        _paint_checkerboard(painter, rect)
    
    painter.fillRect(rect, color)
    
    painter.setPen(QPen(QColor(136, 136, 136), 1))
    painter.drawRect(rect.adjusted(0, 0, -1, -1))
    
    painter.end()
    return pixmap


def get_color_preview_brush(r, g, b, a=255, size=24):
    if a >= 255:
        return QBrush(QColor(int(r), int(g), int(b)))
    
    pixmap = QPixmap(size, size)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, False)
    
    _paint_checkerboard(painter, pixmap.rect())
    
    painter.fillRect(pixmap.rect(), QColor(int(r), int(g), int(b), int(a)))
    
    painter.end()
    
    return QBrush(pixmap)


class ColorPreviewButton(QPushButton):
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self._color = QColor(255, 255, 255, 255)
        self._has_alpha = True
        self.setStyleSheet("QPushButton { border: 1px solid #888888; }")
    
    def setColor(self, r, g, b, a=255):
        self._color = QColor(int(r), int(g), int(b), int(a))
        self.update()
    
    def setHasAlpha(self, has_alpha):
        self._has_alpha = has_alpha
        self.update()
    
    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        
        rect = self.rect().adjusted(1, 1, -1, -1)
        
        if self._has_alpha and self._color.alpha() < 255:
            _paint_checkerboard(painter, rect)
        
        painter.fillRect(rect, self._color)
        
        painter.setPen(QPen(QColor(136, 136, 136), 1))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))
        
        painter.end()
