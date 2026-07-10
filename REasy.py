#!/usr/bin/env python3
"""entrypoint for REasy."""

import multiprocessing

from app_config import CURRENT_VERSION, GAMES

__all__ = ["CURRENT_VERSION", "GAMES", "FileTab", "REasyEditorApp", "main"]


def main():
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
