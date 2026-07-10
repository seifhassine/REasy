from __future__ import annotations
import os
import re
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui  import QPainter, QColor, QPixmap

from app_config import GAME_NATIVE_PATHS
from utils.app_paths import application_root


BASE_DIR = application_root()

PROJECTS_ROOT = BASE_DIR / "projects"


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def slug(text: str) -> str:
    """Filesystem‑friendly name."""
    return re.sub(r"[^\w\- ]", "", text).strip()

def ensure_projects_root() -> None:
    ensure_dir(PROJECTS_ROOT)

EXPECTED_NATIVE = GAME_NATIVE_PATHS

def make_plus_pixmap(sz: QSize = QSize(14, 14)):
    pm = QPixmap(sz)
    pm.fill(Qt.transparent)
    p  = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor(0, 180, 0))
    c  = sz.width() // 2
    p.drawLine(c, 3, c, sz.height() - 3)
    p.drawLine(3, c, sz.width() - 3, c)
    p.end()
    return pm
