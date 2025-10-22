#!/usr/bin/env python3
import argparse
import os
import shutil
import subprocess
import tempfile
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
    for py_file in sorted(project_root.rglob("*.py")):
        if "__pycache__" in py_file.parts:
            continue
        source_files.append(str(py_file.relative_to(project_root)))
    
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
    if not ts_files:
        ts_files = [ts_dir / "REasy_zh-CN.ts"]

    with tempfile.NamedTemporaryFile(
        "w", suffix=".lst", delete=False, dir=project_root
    ) as tmp:
        for file_path in source_files:
            tmp.write(f"{file_path}\n")
        file_list_name = Path(tmp.name).name

    try:
        for ts_file in ts_files:
            print(f"Updating {ts_file.name}...")
            cmd = [
                exe,
                *lupdate_options,
                f"@{file_list_name}",
                "-ts",
                str(ts_file.relative_to(project_root)),
            ]
            subprocess.check_call(cmd, cwd=project_root)
    finally:
        Path(project_root / file_list_name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
