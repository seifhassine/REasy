from __future__ import annotations

import os

from app_config import GAME_NATIVE_PATHS
from utils.app_paths import application_root


BASE_DIR = application_root()

PROJECTS_ROOT = BASE_DIR / "projects"


def ensure_dir(path: str | os.PathLike) -> None:
    os.makedirs(path, exist_ok=True)


def ensure_projects_root() -> None:
    ensure_dir(PROJECTS_ROOT)


EXPECTED_NATIVE = GAME_NATIVE_PATHS
