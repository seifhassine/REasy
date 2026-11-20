from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path

lazy from utils.app_paths import PROJECT_CONFIG_NAME, PROJECT_METADATA_FOLDER


def project_config_path(project_dir: Path | str) -> Path:
    return (Path(project_dir).resolve() / PROJECT_CONFIG_NAME).resolve()


def project_files(project_dir: Path | str) -> Iterator[tuple[str, Path]]:
    project_dir = Path(project_dir)
    for path in project_dir.rglob("*"):
        relative = path.relative_to(project_dir)
        if path.is_file() and PROJECT_METADATA_FOLDER not in relative.parts:
            yield relative.as_posix(), path


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
    path.write_text(json.dumps(dict(config), indent=2), encoding="utf-8")


def update_project_config(project_dir: Path | str, updates: Mapping[str, object]) -> None:
    config = load_project_config(project_dir)
    config.update(updates)
    save_project_config(project_dir, config)
