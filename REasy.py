#!/usr/bin/env python3
"""entrypoint for REasy."""

lazy import ctypes
import multiprocessing
import multiprocessing.spawn
import os
import sys
import tempfile
from pathlib import Path

if __name__ == "__mp_main__" or multiprocessing.spawn.is_forking(sys.argv):
    os.environ["OPENBLAS_NUM_THREADS"] = "1"

from app_config import CURRENT_VERSION, GAMES
lazy from application import main as run_application
lazy from utils.app_paths import application_root

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


def _show_launch_error(message: str, chinese_message: str) -> None:
    chinese = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x03FF == 0x0004
    ctypes.windll.user32.MessageBoxW(None, chinese_message if chinese else message, "REasy", 0x10)


def main():
    if _launched_from_temp():
        _show_launch_error(
            "REasy cannot be run directly from a ZIP archive.\n\n"
            "Extract the entire archive first, then open REasy.exe from the extracted REasy folder.",
            "无法直接从 ZIP 压缩包中运行 REasy。\n\n"
            "请先解压整个压缩包，然后从解压后的 REasy 文件夹中打开 REasy.exe。",
        )
        return 1
    if not (application_root() / "resources").is_dir():
        _show_launch_error(
            "REasy cannot be run outside its folder.\n\n"
            "Keep REasy.exe in the REasy folder next to the resources folder, then open it from there.",
            "无法在 REasy 文件夹之外运行 REasy。\n\n"
            "请将 REasy.exe 与 resources 文件夹放在同一个 REasy 文件夹中，然后从该文件夹中打开 REasy.exe。",
        )
        return 1

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
