from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from .constants import PROJECTS_ROOT

CONFIG_NAME = ".reasy_project.json"


def project_config_path(project_dir: Path | str) -> Path:
    path = (Path(project_dir).resolve() / CONFIG_NAME).resolve()
    if not path.is_relative_to(PROJECTS_ROOT.resolve()): raise ValueError("Invalid project path")
    return path


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
    if not path.is_relative_to(PROJECTS_ROOT.resolve()): raise ValueError("Invalid project path")
    path.write_text(json.dumps(dict(config), indent=2), encoding="utf-8")

def update_project_config(project_dir: Path | str, updates: Mapping[str, object]) -> None:
    config = load_project_config(project_dir)
    config.update(updates)
    save_project_config(project_dir, config)
