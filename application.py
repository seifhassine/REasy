"""Application bootstrap for native dependencies, Qt, and translations."""

import gc
import os
from pathlib import Path
import subprocess
import sys

import PySide6
from PySide6.QtCore import QLocale
from PySide6.QtWidgets import QApplication, QMessageBox, QStyleFactory

from i18n.language_manager import LanguageManager
from settings import load_settings
from utils.app_paths import application_root
from utils.native_build import ensure_fastmesh, ensure_fast_pakresolve


def _configure_garbage_collector() -> None:
    gc.collect(2)
    gc.freeze()
    _, generation_one, generation_two = gc.get_threshold()
    gc.set_threshold(50_000, generation_one * 2, generation_two * 2)


def _compile_translation_if_needed(settings: dict) -> None:
    base_dir = application_root()
    translation_dir = base_dir / "resources" / "i18n"
    executable_name = "lrelease.exe" if os.name == "nt" else "lrelease"
    candidates = (
        (base_dir / executable_name,)
        if getattr(sys, "frozen", False)
        else (Path(PySide6.__file__).resolve().parent / executable_name, Path(executable_name))
    )
    compiler = next((str(path) for path in candidates if path.exists()), None)
    language = LanguageManager.instance().detect(settings.get("ui_language", "system"))
    if not compiler or language == "en":
        return

    source = translation_dir / f"REasy_{language}.ts"
    compiled = translation_dir / f"REasy_{language}.qm"
    if source.exists() and (not compiled.exists() or compiled.stat().st_mtime < source.stat().st_mtime):
        print(f"i18n: compiling {source.name} -> {compiled.name} using {compiler}")
        subprocess.check_call([compiler, str(source), "-qm", str(compiled)])


def _prepare_native_modules() -> None:
    ensure_fast_pakresolve()
    ensure_fastmesh()


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)
    _configure_garbage_collector()
    _prepare_native_modules()

    # Import after native preparation because PAK modules use the native hash helper.
    from ui.main_window import REasyEditorApp

    QLocale.setDefault(QLocale.c())
    app = QApplication(argv)
    app.setStyle(QStyleFactory.create("Fusion"))

    settings = load_settings()
    _compile_translation_if_needed(settings)
    LanguageManager.instance().initialize(app, settings)

    window = REasyEditorApp()
    if len(argv) > 1 and not str(argv[1]).startswith("-"):
        filename = argv[1]
        try:
            window.add_tab(filename, Path(filename).read_bytes())
        except Exception as exc:
            QMessageBox.critical(None, "Error", str(exc))

    window.show()
    return app.exec()
