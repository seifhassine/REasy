#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
from pathlib import Path


def find_lupdate() -> str:
    cand = shutil.which("pyside6-lupdate") or shutil.which("lupdate")
    if cand:
        return cand
    import PySide6
    pkg_dir = Path(PySide6.__file__).resolve().parent
    root = pkg_dir.parents[1]
    candidates = [
        root / "Qt" / "bin" / ("lupdate.exe" if os.name == "nt" else "lupdate"),
        pkg_dir / ("lupdate.exe" if os.name == "nt" else "lupdate"),
    ]
    for p in candidates:
        if p.exists():
            return str(p)
    raise SystemExit("lupdate not found. Ensure PySide6 is installed with Qt tools.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-obsolete", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    
    project_root = Path(__file__).resolve().parents[1]
    ts_dir = project_root / "resources" / "i18n"
    ts_dir.mkdir(parents=True, exist_ok=True)
    
    source_files = []
    main_file = project_root / "REasy.py"
    if main_file.exists():
        source_files.append(str(main_file))
    
    ui_dir = project_root / "ui"
    if ui_dir.exists():
        for py_file in ui_dir.rglob("*.py"):
            source_files.append(str(py_file))
    
    if not source_files:
        print("No source files found.")
        return
    
    exe = find_lupdate()
    lupdate_options = []
    if args.no_obsolete:
        lupdate_options.append("-no-obsolete")
    if args.verbose:
        lupdate_options.append("-verbose")
    
    ts_files = list(ts_dir.glob("*.ts"))
    if ts_files:
        for ts_file in ts_files:
            print(f"Updating {ts_file.name}...")
            cmd = [exe] + lupdate_options + source_files + ["-ts", str(ts_file)]
            subprocess.check_call(cmd)
    else:
        ts_file = ts_dir / "REasy_zh-CN.ts"
        cmd = [exe] + lupdate_options + source_files + ["-ts", str(ts_file)]
        subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
