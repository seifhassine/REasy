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

import io
import json
import shutil
import subprocess
import urllib.request
import zipfile
from pathlib import Path
from typing import Tuple, Optional

import requests
from ui.project_manager.pak_status_dialog import run_with_progress

_OWNER_REPO   = "seifhassine/REE.PAK.Tool"
_API_LATEST   = f"https://api.github.com/repos/{_OWNER_REPO}/releases/latest"
_ZIP_TEMPLATE = (
    "https://github.com/{repo}/releases/download/{tag}/REE.Packer.zip"
)

_EXE_NAME     = "REE.Packer.exe"

CACHE_DIR     = Path.cwd() / "downloads" / "pak_packer"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

EXE_PATH      = CACHE_DIR / _EXE_NAME     
META_JSON     = CACHE_DIR / "_version.json" 

def _http_json(url: str, timeout: int = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.load(resp)


def _download_release(tag: str, *, parent_window=None) -> None:
    """
    Download & extract the REE.Packer.zip for *tag* into `CACHE_DIR`.
    Shows a modal progress dialog via run_with_progress().
    """
    zip_url = _ZIP_TEMPLATE.format(repo=_OWNER_REPO, tag=tag)

    def _worker(bridge):
        bridge.text.emit(f"Connecting to GitHub ({tag}) …")

        with requests.get(zip_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            received = 0
            data = io.BytesIO()

            for chunk in r.iter_content(chunk_size=8192):
                data.write(chunk)
                received += len(chunk)
                if total:
                    pct = int(received * 100 / total)
                    bridge.prog.emit(pct)
                    bridge.text.emit(f"Downloading… {pct}%")

        bridge.text.emit("Extracting …")
        data.seek(0)

        if CACHE_DIR.exists():
            shutil.rmtree(CACHE_DIR)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(data) as zf:
            zf.extractall(CACHE_DIR)

        META_JSON.write_text(json.dumps({"tag": tag}, indent=2))

    run_with_progress(parent_window, "Download REE.Packer", _worker)


def packer_status() -> Tuple[bool, Optional[str]]:
    try:
        latest_tag = _http_json(_API_LATEST)["tag_name"]
    except Exception:                         
        latest_tag = None

    try:
        current_tag = json.loads(META_JSON.read_text())["tag"]
    except Exception:
        current_tag = None

    packer_present = EXE_PATH.exists()

    print("latest_tag:", latest_tag)
    if latest_tag is None:                    
        return (not packer_present), current_tag

    needs = (not packer_present) or (current_tag != latest_tag)
    return needs, latest_tag


def _ensure_packer(*, auto_download: bool = True, parent_window=None) -> Path:
    """
    Return a Path to **REE.Packer.exe**.
    Will download/update automatically if *auto_download* is True.
    """
    needs, latest = packer_status()

    if needs:
        if not auto_download:
            raise RuntimeError("REE.Packer is missing or out‑of‑date.")
        tag_to_get = latest or "latest"
        _download_release(tag_to_get, parent_window=parent_window)

    if not EXE_PATH.exists():
        raise RuntimeError("REE.Packer.exe not found after download!")
    return EXE_PATH


def run_packer(
    src_dir: str | Path,
    dest_pak: str | Path,
    *,
    parent=None,
    auto_download: bool = True,
) -> tuple[int, str]:
    exe = _ensure_packer(auto_download=auto_download, parent_window=parent)

    cmd = f'"{exe}" "{Path(src_dir)}" "{Path(dest_pak)}"'
    proc = subprocess.Popen(
        cmd,
        shell=True,
        cwd=CACHE_DIR,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        bufsize=1,
    )
    output = proc.communicate()[0]
    return proc.returncode, output

if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("usage: python -m tools.pak_exporter <src_dir> <dest.pak>")
        sys.exit(1)

    rc, out = run_packer(sys.argv[1], sys.argv[2])
    print(out)
    print("---- finished with", rc, "----")
