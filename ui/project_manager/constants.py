from __future__ import annotations

import math
import os

from PySide6.QtCore import QPointF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap

from app_config import GAME_NATIVE_PATHS
from utils.app_paths import application_root


BASE_DIR = application_root()

PROJECTS_ROOT = BASE_DIR / "projects"


def ensure_dir(path: str | os.PathLike) -> None:
    os.makedirs(path, exist_ok=True)


def ensure_projects_root() -> None:
    ensure_dir(PROJECTS_ROOT)


EXPECTED_NATIVE = GAME_NATIVE_PATHS


def _pixmap_canvas(size: QSize) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    return pixmap, painter


def make_plus_pixmap(sz: QSize = QSize(14, 14)) -> QPixmap:
    pixmap, painter = _pixmap_canvas(sz)
    painter.setPen(QColor(0, 180, 0))
    center = sz.width() // 2
    painter.drawLine(center, 3, center, sz.height() - 3)
    painter.drawLine(3, center, sz.width() - 3, center)
    painter.end()
    return pixmap


def make_star_pixmap(filled: bool = False, sz: QSize = QSize(14, 14)) -> QPixmap:
    pixmap, painter = _pixmap_canvas(sz)
    color = QColor("#ffd700") if filled else QColor("#c8c8c8")
    painter.setPen(color)
    if filled:
        painter.setBrush(color)

    cx, cy = sz.width() / 2.0, sz.height() / 2.0
    outer = min(cx, cy) - 1.0
    inner = outer * 0.45
    points = []
    for i in range(10):
        angle = -math.pi / 2 + i * math.pi / 5
        radius = outer if i % 2 == 0 else inner
        points.append(QPointF(cx + radius * math.cos(angle), cy + radius * math.sin(angle)))

    path = QPainterPath()
    path.moveTo(points[0])
    for point in points[1:]:
        path.lineTo(point)
    path.closeSubpath()
    painter.drawPath(path)
    painter.end()
    return pixmap


def make_bookmark_pixmap(sz: QSize = QSize(16, 16), color: str = "#ffd700") -> QPixmap:
    pixmap, painter = _pixmap_canvas(sz)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(color))

    w, h = sz.width(), sz.height()
    notch = w * 0.32
    path = QPainterPath()
    path.moveTo(1.5, 0.5)
    path.lineTo(w - 1.5, 0.5)
    path.lineTo(w - 1.5, h - 1.5)
    path.lineTo(w / 2.0, h - 1.5 - notch)
    path.lineTo(1.5, h - 1.5)
    path.closeSubpath()
    painter.drawPath(path)
    painter.end()
    return pixmap


def make_close_pixmap(sz: QSize = QSize(11, 11)) -> QPixmap:
    pixmap, painter = _pixmap_canvas(sz)
    pen = QPen(QColor(224, 90, 90), 1.6)
    pen.setCapStyle(Qt.RoundCap)
    painter.setPen(pen)
    margin = 1.6
    painter.drawLine(
        QPointF(margin, margin),
        QPointF(sz.width() - margin, sz.height() - margin),
    )
    painter.drawLine(
        QPointF(sz.width() - margin, margin),
        QPointF(margin, sz.height() - margin),
    )
    painter.end()
    return pixmap
