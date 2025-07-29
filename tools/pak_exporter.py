"""
tools/pak_exporter.py
─────────────────────
Download / update the external **REE.Packer** tool and invoke it to build a
PAK archive from the current mod directory.

Public helpers
--------------
packer_status()          →  (needs_download: bool, latest_tag: str | None)
ensure_packer()          →  Path to REE.Packer.exe (downloads if missing/out‑dated)
run_packer(src_dir, dst) →  (exit_code: int, full_console_output: str)
"""
from __future__ import annotations
import json
import io
import shutil
import subprocess
import sys
import urllib.request
import zipfile
import requests
from pathlib import Path
from typing import Tuple, Optional

_OWNER_REPO = "seifhassine/REE.PAK.Tool"
_API_LATEST = f"https://api.github.com/repos/{_OWNER_REPO}/releases/latest"
_EXE_NAME   = "REE.Packer.exe"

CACHE_DIR     = Path.cwd() / "downloads" / "pak_packer"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_EXE_PATH     = CACHE_DIR / _EXE_NAME
_VERSION_FILE = CACHE_DIR / "VERSION"  

_TAG_FETCHED   = False
_CACHED_LATEST : Optional[str] = None


def packer_status() -> Tuple[bool, Optional[str]]:
    global _TAG_FETCHED, _CACHED_LATEST

    if not _TAG_FETCHED:
        try:
            data = _http_json(_API_LATEST)
            _CACHED_LATEST = data.get("tag_name")
        except Exception:
            _CACHED_LATEST = None
        _TAG_FETCHED = True

    latest = _CACHED_LATEST
    if not _EXE_PATH.exists() or not _VERSION_FILE.exists():
        return True, latest

    on_disk = _VERSION_FILE.read_text().strip()
    if latest and latest != on_disk:
        return True, latest

    return False, latest


def _http_json(url: str, timeout: int = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _download_release(tag: Optional[str], *, parent_window=None) -> Path:
    if tag:
        zip_url = f"https://github.com/{_OWNER_REPO}/releases/download/{tag}/REE.Packer.zip"
    else:
        zip_url = f"https://github.com/{_OWNER_REPO}/releases/latest/download/REE.Packer.zip"

    from ui.project_manager.pak_status_dialog import run_with_progress

    def _do_download(br):
        br.text.emit(f"Connecting to GitHub release {tag or 'latest'}…")
        with requests.get(zip_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            got   = 0
            buf   = io.BytesIO()
            for chunk in r.iter_content(chunk_size=8192):
                buf.write(chunk)
                got += len(chunk)
                if total:
                    pct = int(got * 100 / total)
                    br.prog.emit(pct)
                    br.text.emit(f"Downloading {pct}%")
        br.text.emit("Extracting…")
        buf.seek(0)
        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
        with zipfile.ZipFile(buf) as zf:
            zf.extractall(CACHE_DIR)

        _VERSION_FILE.write_text(tag or "latest")

    run_with_progress(parent_window, f"Download REE.Packer {tag or 'latest'}", _do_download)
    return _EXE_PATH
def _ensure_packer(*, auto_download=True, parent_window=None) -> Path:
    """
    Ensure the packer exe is present and up‑to‑date.
    If auto_download is False and we’re out‑of‑date, raises RuntimeError.
    """
    need, latest = packer_status()
    if need:
        if not auto_download:
            raise RuntimeError("Packer not present or outdated")
        _download_release(latest, parent_window=parent_window)
    return _EXE_PATH


def run_packer(
    src_dir: str | Path, dest_pak: str | Path,
    *, parent=None, auto_download=True
) -> Tuple[int, str]:
    exe = _ensure_packer(auto_download=auto_download, parent_window=parent)
    cmd = [str(exe), str(src_dir), str(dest_pak)]
    proc = subprocess.Popen(
        cmd,
        cwd=exe.parent,          
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    out, _ = proc.communicate()
    return proc.returncode, out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: python -m tools.pak_exporter <src_dir> <dest.pak>")
        sys.exit(1)
    code, out = run_packer(sys.argv[1], sys.argv[2])
    print(out)
    sys.exit(code)
