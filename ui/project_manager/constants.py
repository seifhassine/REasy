from __future__ import annotations
import os
import re
from pathlib import Path
import sys
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui  import QPainter, QColor, QPixmap


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.argv[0]).resolve().parent
    else:
        return Path(__file__).resolve().parents[2]

BASE_DIR = _get_base_dir()

PROJECTS_ROOT = BASE_DIR / "projects"
PROJECTS_ROOT.mkdir(parents=True, exist_ok=True)


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def slug(text: str) -> str:
    """Filesystem‑friendly name."""
    return re.sub(r"[^\w\- ]", "", text).strip()

def ensure_projects_root() -> None:
    ensure_dir(PROJECTS_ROOT)

EXPECTED_NATIVE: dict[str, tuple[str, ...]] = {
    **{g: ("natives", "stm") for g in (
        "RE4","RE8","RE2RT","RE3RT","RE7RT","RE3","DD2",
        "REResistance","SF6","MHWilds","MHRise","MHST3","O2", "Pragmata","KunitsuGami")},
    **{g: ("natives", "x64") for g in ("RE2", "RE7", "DMC5")},
}

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
