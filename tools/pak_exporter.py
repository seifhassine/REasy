from __future__ import annotations
import subprocess
import sys
from pathlib import Path
from typing import Tuple

from tools.github_downloader import GitHubToolDownloader, _get_base_dir, _http_json  # noqa: F401

_OWNER_REPO = "seifhassine/REE.PAK.Tool"


def _packer_asset_url(tag, _assets):
    if tag:
        return f"https://github.com/{_OWNER_REPO}/releases/download/{tag}/REE.Packer.zip"
    return f"https://github.com/{_OWNER_REPO}/releases/latest/download/REE.Packer.zip"


_downloader = GitHubToolDownloader(
    owner_repo=_OWNER_REPO,
    cache_subdir="pak_packer",
    exe_name="REE.Packer.exe",
    asset_url_fn=_packer_asset_url,
    display_name="REE.Packer",
)

CACHE_DIR     = _downloader.cache_dir
_EXE_PATH     = _downloader.exe_path
_VERSION_FILE = _downloader._version_file


def packer_status() -> Tuple[bool, str | None]:
    return _downloader.status()


def _ensure_packer(*, auto_download=True, parent_window=None) -> Path:
    return _downloader.ensure(auto_download=auto_download, parent_window=parent_window)


def run_packer(
    src_dir: str | Path, dest_pak: str | Path,
    *, parent=None, auto_download=True
) -> Tuple[int, str]:
    exe = _ensure_packer(auto_download=auto_download, parent_window=parent)
    cmd = [str(exe), str(src_dir), str(dest_pak)]
    proc = subprocess.Popen(
        cmd, cwd=exe.parent,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
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