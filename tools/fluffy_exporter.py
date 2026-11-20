"""See docs/internals/fluffy-mod-manager.md."""
from __future__ import annotations
lazy import zipfile
import tempfile
from pathlib import Path

from PySide6.QtCore import QCoreApplication

lazy from file_handlers.pak.writer import write_pak
lazy from ui.project_manager.bookmarks import BOOKMARKS_PROJECT_NAME
lazy from ui.project_manager.project_config import load_project_config, project_files
lazy from utils.app_paths import PROJECT_CONFIG_NAME

translate = QCoreApplication.translate
_MODINFO_NAME = "modinfo.ini"
MAX_CATEGORIES = 10
_MAX_LINE = 998
_MODINFO_KEYS = {
    "name": "name", "version": "version", "author": "author", "description": "description",
    "screenshot": "screenshot", "homepage": "homepage", "categories": "category", "addon_for": "AddonFor",
    "name_as_bundle": "NameAsBundle", "requires": "requires", "menu_priority": "MenuPriority",
    "dummy_mod": "DummyMod",
}


def _modinfo_lines(cfg: dict) -> list[tuple[str, str]]:
    lines = []
    for key, ini_key in _MODINFO_KEYS.items():
        value = cfg.get(key) or ""
        for item in value if key == "categories" else [Path(value).name if key == "screenshot" else value]:
            if text := "\\n".join(str(item).strip().splitlines()):
                lines.append((key, f"{ini_key}={text}"))
    return lines


def modinfo_problems(cfg: dict, project_dir: Path) -> dict[str, str]:
    lines = _modinfo_lines(cfg)
    problems = {}
    for key, line in lines:
        if not line.isascii():
            problems[key] = translate("FluffyExport", "Fluffy Mod Manager can't show {}.").format(
                " ".join(dict.fromkeys(char for char in line if not char.isascii())))
        elif len(line) > _MAX_LINE:
            problems[key] = translate("FluffyExport", "{} characters too long for Fluffy Mod Manager.").format(
                len(line) - _MAX_LINE)
    if sum(key == "categories" for key, _ in lines) > MAX_CATEGORIES:
        problems["categories"] = translate("FluffyExport", "Fluffy Mod Manager reads up to {} categories.").format(
            MAX_CATEGORIES)
    if cfg.get("screenshot") and not (project_dir / cfg["screenshot"]).is_file():
        problems["screenshot"] = translate("FluffyExport", "File not found.")
    return problems


def _write_bundled_pak(zf: zipfile.ZipFile, project_dir: Path, arcname: str, compression: str) -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        pak_tmp = Path(temp_dir) / "bundle.pak"
        write_pak(project_files(project_dir), pak_tmp, compression)
        zf.write(pak_tmp, arcname)


def _write_project_files(zf: zipfile.ZipFile, project_dir: Path, prefix: str) -> None:
    skipped = {PROJECT_CONFIG_NAME, BOOKMARKS_PROJECT_NAME,
               *(name.removeprefix(prefix) for name in zf.namelist() if name.startswith(prefix))}
    for name, path in project_files(project_dir):
        if name not in skipped:
            zf.write(path, prefix + name)


def create_fluffy_zip(project_dir: Path, zip_path: Path):
    project_dir = Path(project_dir)
    cfg = load_project_config(project_dir)
    options = (project_dir.parent / name for name in cfg.get("option_projects", ()))
    projects = list(dict.fromkeys([project_dir, *options]))
    configs = [load_project_config(path) for path in projects]
    missing = translate("FluffyExport", "Project not found.")
    problems = [f"{path.name}: {missing}" for path in projects if not path.is_dir()]
    problems += [f"{path.name}: {_MODINFO_KEYS[key]}: {problem}" for path, mod in zip(projects, configs)
                 for key, problem in modinfo_problems(mod, path).items()]
    if problems:
        raise ValueError("\n".join(problems))
    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path.with_suffix(".zip")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, mod in zip(projects, configs):
            prefix = f"{path.name}/" if len(projects) > 1 else ""
            if lines := _modinfo_lines(mod):
                zf.writestr(prefix + _MODINFO_NAME, "".join(f"{line}\r\n" for _, line in lines))
            if screenshot := mod.get("screenshot"):
                zf.write(path / screenshot, prefix + Path(screenshot).name)
            if mod.get("bundle_pak"):
                _write_bundled_pak(zf, path, prefix + Path(f"{mod.get('pak_name') or path.name}.pak").name,
                                   mod.get("pak_compression", "deflate"))
            else:
                _write_project_files(zf, path, prefix)
