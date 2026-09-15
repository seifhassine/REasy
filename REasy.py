#!/usr/bin/env python3
"""entrypoint for REasy."""

import ctypes
import multiprocessing
import sys
import tempfile
from pathlib import Path

from app_config import CURRENT_VERSION, GAMES

__all__ = ["CURRENT_VERSION", "GAMES", "FileTab", "REasyEditorApp", "main"]


def _launched_from_temp() -> bool:
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False
    try:
        Path(sys.executable).resolve().relative_to(
            Path(tempfile.gettempdir()).resolve()
        )
    except ValueError:
        return False
    return True


def _show_zip_error() -> None:
    chinese = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x03FF == 0x0004
    message = (
        "无法直接从 ZIP 压缩包中运行 REasy。\n\n"
        "请先解压整个压缩包，然后从解压后的 REasy 文件夹中打开 REasy.exe。"
        if chinese
        else "REasy cannot be run directly from a ZIP archive.\n\n"
        "Extract the entire archive first, then open REasy.exe from the extracted REasy folder."
    )
    ctypes.windll.user32.MessageBoxW(None, message, "REasy", 0x10)


def main():
    if _launched_from_temp():
        _show_zip_error()
        return 1

    from application import main as run_application

    return run_application()


def __getattr__(name):
    if name == "FileTab":
        from ui.file_tab import FileTab

        return FileTab
    if name == "REasyEditorApp":
        from ui.main_window import REasyEditorApp

        return REasyEditorApp
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
