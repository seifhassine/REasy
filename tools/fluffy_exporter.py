from __future__ import annotations
import json
import zipfile
import tempfile
from pathlib import Path

from tools.pak_exporter import _ensure_packer, run_packer

def create_fluffy_zip(project_dir: Path, zip_path: Path):
    project_dir = Path(project_dir)
    cfg_path    = project_dir / ".reasy_project.json"
    cfg = {}
    if cfg_path.exists():
        try:
            cfg = json.loads(cfg_path.read_text())
        except Exception:
            cfg = {}

    bundle   = cfg.get("bundle_pak", False)
    pak_name = cfg.get("pak_name", project_dir.name)

    if zip_path.suffix.lower() != ".zip":
        zip_path = zip_path.with_suffix(".zip")

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        modinfo = project_dir / "modinfo.ini"
        if modinfo.exists():
            zf.write(modinfo, "modinfo.ini")
        else:
            lines = []
            for key in ("name", "description", "author", "version"):
                if val := cfg.get(key):
                    lines.append(f"{key}: {val}")
            if ss := cfg.get("screenshot"):
                lines.append(f"screenshot: {Path(ss).name}")
            if lines:
                zf.writestr("modinfo.ini", "\n".join(lines))

        if ss := cfg.get("screenshot"):
            ssf = Path(ss)
            if not ssf.is_absolute():
                ssf = project_dir / ssf
            if ssf.exists():
                zf.write(ssf, ssf.name)

        if bundle:
            _ensure_packer(auto_download=True)
            with tempfile.TemporaryDirectory() as td:
                pak_tmp = Path(td) / f"{pak_name}.pak"
                code, out = run_packer(str(project_dir), str(pak_tmp))
                if code != 0:
                    raise RuntimeError(f"PAK build failed:\n{out}")
                zf.writestr(pak_tmp.name, pak_tmp.read_bytes())
        else:
            for f in project_dir.rglob("*"):
                if not f.is_file() or f.name == ".reasy_project.json":
                    continue
                rel = f.relative_to(project_dir)
                zf.write(f, str(rel))
