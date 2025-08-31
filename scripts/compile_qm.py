#!/usr/bin/env python3
import glob
import os
import shutil
import subprocess
from pathlib import Path

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
    for ts in glob.glob(str(ts_dir / "*.ts")):
        qm = ts[:-3] + ".qm"
        if not os.path.exists(qm) or os.path.getmtime(qm) < os.path.getmtime(ts):
            subprocess.check_call([exe, ts, "-qm", qm])

if __name__ == "__main__":
    main()
