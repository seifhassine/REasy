from __future__ import annotations

import os
from pathlib import Path
from typing import Optional, Tuple

from tools.github_downloader import StaticToolDownloader

_FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

_downloader = StaticToolDownloader(
    asset_url=_FFMPEG_URL,
    cache_subdir="ffmpeg",
    exe_name="ffmpeg.exe" if os.name == "nt" else "ffmpeg",
    display_name="FFmpeg",
)


def ffmpeg_status() -> Tuple[bool, Optional[str]]:
    return _downloader.status()


def ensure_ffmpeg(*, auto_download: bool = True, parent_window=None) -> Path:
    return _downloader.ensure(auto_download=auto_download, parent_window=parent_window)
