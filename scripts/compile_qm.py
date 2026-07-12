#!/usr/bin/env python3
import os
import shutil
import subprocess
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from i18n.catalog import CatalogValidationError, validate_catalog

def find_lrelease() -> str:
    cand = shutil.which("pyside6-lrelease") or shutil.which("lrelease")
    if cand:
        return cand
    import PySide6  # type: ignore
    pkg_dir = Path(PySide6.__file__).resolve().parent
    root = pkg_dir.parents[1]
    candidates = [
        root / "Qt" / "bin" / ("lrelease.exe" if os.name == "nt" else "lrelease"),
        pkg_dir / ("lrelease.exe" if os.name == "nt" else "lrelease"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    raise SystemExit("lrelease not found. Ensure PySide6 is installed with Qt tools.")

def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    ts_dir = project_root / "resources" / "i18n"
    if not ts_dir.exists():
        return
    exe = find_lrelease()
    for ts in sorted(ts_dir.glob("*.ts")):
        try:
            validate_catalog(ts)
        except CatalogValidationError as exc:
            raise SystemExit(str(exc)) from exc
        qm = ts.with_suffix(".qm")
        if not qm.exists() or qm.stat().st_mtime < ts.stat().st_mtime:
            subprocess.check_call([exe, str(ts), "-qm", str(qm)])

if __name__ == "__main__":
    main()
