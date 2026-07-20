from __future__ import annotations
import json
import zipfile
import tempfile
from pathlib import Path

from tools.pak_exporter import _ensure_packer, run_packer

_MODINFO_NAME = "modinfo.ini"
_PROJECT_CONFIG_NAME = ".reasy_project.json"


def _load_project_config(project_dir: Path) -> dict:
    cfg_path = project_dir / _PROJECT_CONFIG_NAME
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text())
        except Exception:
            pass
    return {}


def _write_modinfo(zf: zipfile.ZipFile, project_dir: Path, cfg: dict) -> None:
    modinfo = project_dir / _MODINFO_NAME
    if modinfo.exists():
        zf.write(modinfo, _MODINFO_NAME)
        return

    lines = []
    for key in ("name", "description", "author", "version"):
        if value := cfg.get(key):
            lines.append(f"{key}: {value}")
    if screenshot := cfg.get("screenshot"):
        lines.append(f"screenshot: {Path(screenshot).name}")
    if lines:
        zf.writestr(_MODINFO_NAME, "\n".join(lines))


def _write_screenshot(zf: zipfile.ZipFile, project_dir: Path, cfg: dict) -> None:
    screenshot = cfg.get("screenshot")
    if not screenshot:
        return
    screenshot_path = Path(screenshot)
    if not screenshot_path.is_absolute():
        screenshot_path = project_dir / screenshot_path
    if screenshot_path.exists():
        zf.write(screenshot_path, screenshot_path.name)


def _write_bundled_pak(zf: zipfile.ZipFile, project_dir: Path, pak_name: str) -> None:
    _ensure_packer(auto_download=True)
    with tempfile.TemporaryDirectory() as temp_dir:
        pak_tmp = Path(temp_dir) / f"{pak_name}.pak"
        code, output = run_packer(str(project_dir), str(pak_tmp))
        if code != 0:
            raise RuntimeError(f"PAK build failed:\n{output}")
        zf.writestr(pak_tmp.name, pak_tmp.read_bytes())


def _write_project_files(zf: zipfile.ZipFile, project_dir: Path) -> None:
    for path in project_dir.rglob("*"):
        if not path.is_file() or path.name == _PROJECT_CONFIG_NAME:
            continue
        zf.write(path, str(path.relative_to(project_dir)))


def create_fluffy_zip(project_dir: Path, zip_path: Path):
    project_dir = Path(project_dir)
    cfg = _load_project_config(project_dir)
    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path.with_suffix(".zip")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        _write_modinfo(zf, project_dir, cfg)
        _write_screenshot(zf, project_dir, cfg)
        if cfg.get("bundle_pak", False):
            _write_bundled_pak(zf, project_dir, cfg.get("pak_name", project_dir.name))
        else:
            _write_project_files(zf, project_dir)
