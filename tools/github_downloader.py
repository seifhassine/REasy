from __future__ import annotations

import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import Callable, Optional, Tuple

import requests


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent.parent


def _http_json(url: str, timeout: int = 20) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r)


def _download_and_extract_archive(asset_url: str, cache_dir: Path, display_name: str, *, parent_window=None):
    from ui.project_manager.pak_status_dialog import run_with_progress

    def _do_download(br):
        br.text.emit(f"Connecting to {display_name}…")
        with requests.get(asset_url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            got, buf = 0, io.BytesIO()
            for chunk in r.iter_content(chunk_size=8192):
                buf.write(chunk)
                got += len(chunk)
                if total:
                    pct = int(got * 100 / total)
                    br.prog.emit(pct)
                    br.text.emit(f"Downloading {display_name}… {pct}%")
        br.text.emit("Extracting…")
        buf.seek(0)
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        if asset_url.endswith(".tar.gz"):
            import tarfile
            with tarfile.open(fileobj=buf, mode="r:gz") as tf:
                tf.extractall(cache_dir)
        else:
            with zipfile.ZipFile(buf) as zf:
                zf.extractall(cache_dir)

    run_with_progress(parent_window, f"Download {display_name}", _do_download)


class GitHubToolDownloader:
    def __init__(self, owner_repo: str, cache_subdir: str, exe_name: str,
                 asset_url_fn: Callable[[Optional[str], list[dict]], str],
                 display_name: str = ""):
        self.owner_repo = owner_repo
        self.display_name = display_name or exe_name
        self._api_latest = f"https://api.github.com/repos/{owner_repo}/releases/latest"
        self.cache_dir = _get_base_dir() / "downloads" / cache_subdir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.exe_path = self.cache_dir / exe_name
        self._version_file = self.cache_dir / "VERSION"
        self._asset_url_fn = asset_url_fn
        self._tag_fetched = False
        self._cached_latest: Optional[str] = None

    def status(self) -> Tuple[bool, Optional[str]]:
        if not self._tag_fetched:
            try:
                self._cached_latest = _http_json(self._api_latest).get("tag_name")
            except Exception:
                self._cached_latest = None
            self._tag_fetched = True
        latest = self._cached_latest
        if not self.exe_path.exists() or not self._version_file.exists():
            return True, latest
        if latest and latest != self._version_file.read_text().strip():
            return True, latest
        return False, latest

    def ensure(self, *, auto_download: bool = True, parent_window=None) -> Path:
        need, latest = self.status()
        if need:
            if not auto_download:
                raise RuntimeError(f"{self.display_name} not present or outdated")
            self._download(latest, parent_window=parent_window)
        return self.exe_path

    def _resolve_asset_url(self, tag: Optional[str]) -> str:
        try:
            url = (f"https://api.github.com/repos/{self.owner_repo}/releases/tags/{tag}"
                   if tag else self._api_latest)
            assets = _http_json(url).get("assets", [])
        except Exception:
            assets = []
        return self._asset_url_fn(tag, assets)

    def _download(self, tag: Optional[str], *, parent_window=None) -> Path:
        asset_url = self._resolve_asset_url(tag)
        _download_and_extract_archive(
            asset_url,
            self.cache_dir,
            f"{self.display_name} {tag or 'latest'}",
            parent_window=parent_window,
        )
        if self.exe_path.exists() and os.name != "nt":
            self.exe_path.chmod(self.exe_path.stat().st_mode | 0o755)
        self._version_file.write_text(tag or "latest")
        return self.exe_path


class StaticToolDownloader:
    def __init__(self, *, asset_url: str, cache_subdir: str, exe_name: str, display_name: str = ""):
        self.asset_url = asset_url
        self.display_name = display_name or exe_name
        self.cache_dir = _get_base_dir() / "downloads" / cache_subdir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.exe_name = exe_name

    @property
    def exe_path(self) -> Path:
        direct = self.cache_dir / self.exe_name
        if direct.exists():
            return direct
        for p in self.cache_dir.rglob(self.exe_name):
            if p.is_file():
                return p
        return direct

    def status(self) -> Tuple[bool, Optional[str]]:
        return (not self.exe_path.exists(), None)

    def ensure(self, *, auto_download: bool = True, parent_window=None) -> Path:
        if self.exe_path.exists():
            return self.exe_path
        if not auto_download:
            raise RuntimeError(f"{self.display_name} is not present")
        _download_and_extract_archive(self.asset_url, self.cache_dir, self.display_name, parent_window=parent_window)
        exe = self.exe_path
        if not exe.exists():
            raise RuntimeError(f"{self.display_name} download completed but executable was not found")
        if os.name != "nt":
            exe.chmod(exe.stat().st_mode | 0o755)
        return exe