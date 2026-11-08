from PySide6.QtWidgets import QToolButton
lazy from PySide6.QtWidgets import QColorDialog, QMenu
lazy from PySide6.QtGui import QActionGroup, QColor, QIcon, QPainter, QPen, QPixmap
lazy from PySide6.QtCore import Qt

lazy from ui.icons import line_icon
lazy from ui.styles import current_colors


def _swatch_icon(color):
    colors = current_colors()
    icon = QIcon()
    for state, ring, width in (
        (QIcon.Off, colors["border_strong"], 1),
        (QIcon.On, colors["text_bright"], 2),
    ):
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.fillRect(2, 2, 12, 12, color)
        pen = QPen(QColor(ring), width)
        pen.setJoinStyle(Qt.MiterJoin)
        painter.setPen(pen)
        inset = 2 if width == 1 else 1
        painter.drawRect(inset, inset, 15 - 2 * inset, 15 - 2 * inset)
        painter.end()
        icon.addPixmap(pixmap, QIcon.Normal, state)
        icon.addPixmap(pixmap, QIcon.Active, state)
    return icon


class HighlightButton(QToolButton):
    def __init__(self, tree, parent=None):
        super().__init__(parent)
        self._tree = tree
        self.color = QColor(255, 165, 0)
        self.setAutoRaise(True)
        self.setCheckable(True)
        self.setPopupMode(QToolButton.MenuButtonPopup)
        self.setToolTip(self.tr("Highlight mode: click nodes to highlight them"))
        self.toggled.connect(self._apply)
        menu = QMenu(self)
        self._presets = QActionGroup(menu)
        self._presets.setExclusionPolicy(QActionGroup.ExclusionPolicy.ExclusiveOptional)
        self._presets.triggered.connect(lambda action: self.set_color(action.data()))
        for name, color in (
            (self.tr("Orange"), self.color),
            (self.tr("Red"), QColor(255, 0, 0)),
            (self.tr("Green"), QColor(0, 255, 0)),
            (self.tr("Blue"), QColor(0, 150, 255)),
            (self.tr("Yellow"), QColor(255, 255, 0)),
            (self.tr("Magenta"), QColor(255, 0, 255)),
            (self.tr("Cyan"), QColor(0, 255, 255)),
        ):
            action = menu.addAction(_swatch_icon(color), name)
            action.setData(color)
            action.setCheckable(True)
            action.setChecked(color == self.color)
            self._presets.addAction(action)
        menu.addSeparator()
        menu.addAction(self.tr("Custom Color...")).triggered.connect(self._choose_color)
        self.setMenu(menu)
        self._apply()

    def set_color(self, color):
        self.color = color
        for action in self._presets.actions():
            action.setChecked(action.data() == color)
        self.setChecked(True)
        self._apply()

    def _choose_color(self):
        color = QColorDialog.getColor(self.color, self, self.tr("Choose Highlight Color"))
        if color.isValid():
            self.set_color(color)

    def _apply(self):
        pen = QColor(self.color)
        pen.setAlpha(96)
        self._tree.highlight_pen = pen if self.isChecked() else None
        self.setIcon(line_icon("brush", self.color.name() if self.isChecked() else None))
