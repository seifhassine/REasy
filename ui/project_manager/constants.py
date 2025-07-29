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
        "REResistance","SF6","MHWilds","MHRise","O2")},
    **{g: ("natives", "x64") for g in ("RE2", "RE7", "DMC5")},
}

GAME_HINT = {
    "RE4":  r"...\re_chunk_000\natives\STM",
    "RE8":  r"...\re_chunk_000\natives\STM",
    "RE2":  r"...\re_chunk_000\natives",
    "RE2RT":r"...\re_chunk_000\natives",
    "RE3":  r"...\re_chunk_000\natives",
    "RE3RT":r"...\re_chunk_000\natives",
    "REResistance": r"...\re_chunk_000\natives",
    "RE7":  r"...\re_chunk_000\natives\Win64",
    "RE7RT":r"...\re_chunk_000\natives\Win64",
    "MHWilds": r"...\natives\STM",
    "MHRise":  r"...\re_chunk_000\natives",
    "DMC5":    r"...\re_chunk_000\natives",
    "SF6":     r"...\re_chunk_000\natives",
    "O2":      r"...\re_chunk_000\natives",
    "DD2":     r"...\re_chunk_000\natives",
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
