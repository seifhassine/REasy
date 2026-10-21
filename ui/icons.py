"""Line icons from resources/icons/line, recolored per icon mode."""
from functools import cache

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

lazy from utils.app_paths import resource_path

NORMAL = "#b8bbc2"
ACTIVE = "#f3f4f6"
DISABLED = "#575a61"
_SIZES = (16, 20, 24, 32, 48)


@cache
def _source(name: str) -> str:
    return resource_path(f"resources/icons/line/{name}.svg", required=True).read_text(encoding="utf-8")


def line_pixmap(name: str, color: str, size: int) -> QPixmap:
    """`size` is in device pixels; callers set the device pixel ratio."""
    renderer = QSvgRenderer(QByteArray(_source(name).replace("currentColor", color).encode()))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@cache
def line_icon(name: str, color: str | None = None) -> QIcon:
    """Icon `name` in `color`, or muted with a bright hover state when no color is given."""
    icon = QIcon()
    normal, active = color or NORMAL, color or ACTIVE
    for size in _SIZES:
        icon.addPixmap(line_pixmap(name, normal, size), QIcon.Normal)
        icon.addPixmap(line_pixmap(name, active, size), QIcon.Active)
        icon.addPixmap(line_pixmap(name, active, size), QIcon.Selected)
        icon.addPixmap(line_pixmap(name, DISABLED, size), QIcon.Disabled)
    return icon
