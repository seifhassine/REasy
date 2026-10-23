from __future__ import annotations

import os
import platform
from pathlib import Path

lazy from PySide6.QtCore import QCoreApplication
lazy from PySide6.QtWidgets import QMessageBox

lazy from settings import app_settings, remember_setting
lazy from tools.github_downloader import GitHubToolDownloader, show_download_failure

EXE_NAME = "vgmstream-cli.exe" if os.name == "nt" else "vgmstream-cli"
SETTINGS_KEY = "vgmstream_cli_path"
_downloader = None


def _asset_preferences(system: str, machine: str) -> list[str]:
    if system == "windows" or os.name == "nt":
        return ["win64" if "64" in machine else "win32"]
    return {"darwin": ["macos", "mac"], "linux": ["linux"]}.get(system, ["win64"])


def _asset_url(tag, assets):
    for preference in _asset_preferences(platform.system().lower(), platform.machine().lower()):
        for asset in assets:
            name = asset.get("name", "").lower()
            if preference in name and name.endswith((".zip", ".tar.gz")):
                return asset.get("browser_download_url", "")
    archive = "vgmstream-win64.zip" if os.name == "nt" else "vgmstream-linux-cli.tar.gz"
    base = "https://github.com/vgmstream/vgmstream/releases"
    return f"{base}/download/{tag}/{archive}" if tag else f"{base}/latest/download/{archive}"


def _get_downloader() -> GitHubToolDownloader:
    global _downloader
    if _downloader is None:
        _downloader = GitHubToolDownloader(
            owner_repo="vgmstream/vgmstream",
            cache_subdir="vgmstream_cli",
            exe_name=EXE_NAME,
            asset_url_fn=_asset_url,
            display_name="vgmstream-cli",
        )
    return _downloader


def vgmstream_path(settings) -> str | None:
    configured = str((settings or {}).get(SETTINGS_KEY, "")).strip()
    if configured and Path(configured).is_file():
        return configured
    downloaded = _get_downloader().exe_path
    return str(downloaded) if downloaded.is_file() else None


def ensure_vgmstream(parent, app) -> str | None:
    if found := vgmstream_path(app_settings(app)):
        return found
    downloader = _get_downloader()
    _needed, latest = downloader.status()
    translate = QCoreApplication.translate
    if QMessageBox.question(
        parent,
        translate("SoundViewer", "Download VGMStream CLI?"),
        translate("SoundViewer", "VGMStream is required for preview and WAV export. Download {version} now?").format(
            version=latest or translate("SoundViewer", "the latest release")),
        QMessageBox.Yes | QMessageBox.No,
    ) != QMessageBox.Yes:
        return None
    try:
        executable = downloader.ensure(auto_download=True, parent_window=parent)
    except Exception as exc:
        show_download_failure(parent, exc)
        return None
    remember_setting(app, SETTINGS_KEY, str(executable))
    return str(executable)
