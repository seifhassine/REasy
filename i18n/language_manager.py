from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Optional
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QLocale, QLibraryInfo, QEvent, Signal, Qt
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QTranslator


def _resource_path(relative_path: str) -> str:
    if getattr(sys, "frozen", False):
        base_dir = Path(sys.argv[0]).resolve().parent
    else:
        base_dir = Path(__file__).resolve().parents[1]
    full_path = base_dir / relative_path
    if full_path.exists():
        return str(full_path)
    return os.path.join(os.getcwd(), relative_path)


@dataclass(frozen=True)
class LanguageInfo:
    code: str
    name: str
    qt_locale: str | None = None


class LanguageManager(QObject):
    language_changed = Signal(str)

    _instance: Optional["LanguageManager"] = None

    SUPPORTED_LANGUAGES: Dict[str, LanguageInfo] = {
        "en": LanguageInfo(code="en", name="English", qt_locale="en"),
        "zh-CN": LanguageInfo(code="zh-CN", name="中文（简体）", qt_locale="zh_CN"),
    }

    def __init__(self) -> None:
        super().__init__()
        self._app_translator: Optional[QTranslator] = None
        self._qt_translator: Optional[QTranslator] = None
        self._current_language: str = "en"
        self._initialized: bool = False

    @classmethod
    def instance(cls) -> "LanguageManager":
        if cls._instance is None:
            cls._instance = LanguageManager()
        return cls._instance

    def initialize(self, app: QApplication, settings: dict) -> None:
        if self._initialized:
            return
        preferred = settings.get("ui_language", "system")
        lang = self.detect(preferred)
        self.set_language(lang, settings=settings, emit_signal=False)
        self._initialized = True

    def detect(self, preferred: str) -> str:
        if preferred and preferred != "system" and preferred in self.SUPPORTED_LANGUAGES:
            return preferred
        for ui_lang in QLocale.system().uiLanguages():
            norm = ui_lang.replace('_', '-').lower()
            for code in self.SUPPORTED_LANGUAGES.keys():
                if code.lower() == norm:
                    return code
            base = norm.split('-')[0]
            for code in self.SUPPORTED_LANGUAGES.keys():
                if code.lower().split('-')[0] == base:
                    return code
        return "en"

    def available_languages(self) -> List[LanguageInfo]:
        return list(self.SUPPORTED_LANGUAGES.values())

    def current_language(self) -> str:
        return self._current_language

    def set_language(self, code: str, settings: Optional[dict] = None, emit_signal: bool = True) -> bool:
        if code not in self.SUPPORTED_LANGUAGES:
            code = "en"
        if code == self._current_language and self._app_translator and self._qt_translator:
            return True
        if self._app_translator:
            QCoreApplication.removeTranslator(self._app_translator)
            self._app_translator = None
        if self._qt_translator:
            QCoreApplication.removeTranslator(self._qt_translator)
            self._qt_translator = None
        qt_locale = self.SUPPORTED_LANGUAGES[code].qt_locale or code
        qt_translator = QTranslator()
        qtbase_dir = QLibraryInfo.path(QLibraryInfo.TranslationsPath)
        qtbase_file = f"qtbase_{qt_locale}.qm"
        qtbase_path = os.path.join(qtbase_dir, qtbase_file)
        bundled_qtbase_path = _resource_path(os.path.join("resources", "i18n", qtbase_file))
        if os.path.exists(bundled_qtbase_path):
            qtbase_path = bundled_qtbase_path
        if os.path.exists(qtbase_path):
            if qt_translator.load(qtbase_path):
                QCoreApplication.installTranslator(qt_translator)
                self._qt_translator = qt_translator
        app_translator = QTranslator()
        app_qm_name = f"REasy_{code}.qm"
        app_qm_path = _resource_path(os.path.join("resources", "i18n", app_qm_name))
        if os.path.exists(app_qm_path) and app_translator.load(app_qm_path):
            QCoreApplication.installTranslator(app_translator)
            self._app_translator = app_translator
        else:
            self._app_translator = None
        self._current_language = code
        locale = QLocale(qt_locale)
        direction = Qt.RightToLeft if locale.textDirection() == Qt.RightToLeft else Qt.LeftToRight
        app = QGuiApplication.instance()
        if app is not None:
            app.setLayoutDirection(direction)
        if settings is not None:
            settings["ui_language"] = code
        if emit_signal:
            self.language_changed.emit(code)
        if app is not None:
            app.sendEvent(app, QEvent(QEvent.LanguageChange))
        return True

