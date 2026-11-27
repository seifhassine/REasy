from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

lazy from app_config import GAMES
lazy from utils.app_paths import application_root

lazy from .project_config import load_project_config, project_config_path
lazy from .project_sessions import ProjectSessionManager


@dataclass(frozen=True)
class ProjectEntry:
    path: Path
    game: str
    name: str
    description: str
    author: str
    version: str
    source: str
    screenshot: str
    modified: float
    external: bool

    @property
    def modified_label(self) -> str:
        return datetime.fromtimestamp(self.modified).strftime("%Y-%m-%d")


def default_projects_dir() -> Path:
    return application_root() / "projects"


def project_game(project_dir: Path | str) -> str | None:
    path = Path(project_dir).resolve()
    games = {game.upper(): game for game in GAMES}
    configured = load_project_config(path).get("game")
    if isinstance(configured, str) and configured.upper() in games:
        return games[configured.upper()]
    return games.get(path.parent.name.upper())


class ProjectLibrary:
    """Projects under the projects folder (<game>/<name>) plus folders added from anywhere else."""

    def __init__(self, settings: dict):
        self.settings = settings

    @property
    def root(self) -> Path:
        return Path(self.settings["projects_dir"] or default_projects_dir())

    def entries(self) -> list[ProjectEntry]:
        folders: dict[str, tuple[Path, bool]] = {}
        root = self.root
        for game_dir in root.iterdir() if root.is_dir() else ():
            for folder in game_dir.iterdir() if game_dir.is_dir() else ():
                if folder.is_dir() and not folder.name.startswith("."):
                    folders[ProjectSessionManager.key_for(folder)] = (folder, False)
        for folder in self.settings["project_folders"]:
            if os.path.isdir(folder):
                folders.setdefault(ProjectSessionManager.key_for(folder), (Path(folder), True))
        entries = [_entry(folder, external) for folder, external in folders.values()]
        return sorted(entries, key=lambda entry: (entry.game.upper(), entry.name.lower()))

    def add(self, project_dir: Path | str) -> None:
        path = Path(project_dir).resolve()
        if ProjectSessionManager.key_for(path) not in {ProjectSessionManager.key_for(e.path) for e in self.entries()}:
            self.settings["project_folders"].append(str(path))

    def remove(self, project_dir: Path | str) -> None:
        key = ProjectSessionManager.key_for(project_dir)
        self.settings["project_folders"] = [
            folder for folder in self.settings["project_folders"] if ProjectSessionManager.key_for(folder) != key]


def _entry(project_dir: Path, external: bool) -> ProjectEntry:
    cfg = load_project_config(project_dir)
    cfg_game = cfg.get("game")
    cfg_path = project_config_path(project_dir)
    cfg_mtime = cfg_path.stat().st_mtime if cfg_path.exists() else 0
    return ProjectEntry(
        path=project_dir,
        game=cfg_game if isinstance(cfg_game, str) and cfg_game.strip() else project_dir.parent.name,
        name=str(cfg.get("name") or project_dir.name),
        description=str(cfg.get("description") or ""),
        author=str(cfg.get("author") or ""),
        version=str(cfg.get("version") or ""),
        source="PAKs" if cfg.get("pak_game_dir") else "Folder",
        screenshot=str(cfg.get("screenshot") or ""),
        modified=max(project_dir.stat().st_mtime, cfg_mtime),
        external=external,
    )
