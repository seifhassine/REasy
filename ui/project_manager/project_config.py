from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .constants import PROJECTS_ROOT

CONFIG_NAME = ".reasy_project.json"
_INVALID_PROJECT_PATH = "Invalid project path"


def _validated_project_dir(project_dir: Path | str) -> Path:
    try:
        root = PROJECTS_ROOT.resolve(strict=True)
        requested = Path(project_dir).resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(_INVALID_PROJECT_PATH) from exc

    for project in root.glob("*/*"):
        try:
            trusted = project.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if trusted == requested and trusted.is_relative_to(root) and project.is_dir():
            return project

    raise ValueError(_INVALID_PROJECT_PATH)


def project_config_path(project_dir: Path | str) -> Path:
    return _validated_project_dir(project_dir) / CONFIG_NAME


def load_project_config(project_dir: Path | str) -> dict:
    path = project_config_path(project_dir)
    if not path.is_file():
        return {}
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return config if isinstance(config, dict) else {}


def save_project_config(project_dir: Path | str, config: Mapping[str, object]) -> None:
    path = project_config_path(project_dir)
    path.write_text(json.dumps(dict(config), indent=2) + "\n", encoding="utf-8")


def update_project_config(project_dir: Path | str, updates: Mapping[str, object]) -> None:
    config = load_project_config(project_dir)
    config.update(updates)
    save_project_config(project_dir, config)
