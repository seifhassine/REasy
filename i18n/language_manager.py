from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QCoreApplication, QLibraryInfo, QLocale, QTranslator
from PySide6.QtGui import QGuiApplication

from utils.app_paths import resource_path


@dataclass(frozen=True)
class LanguageInfo:
    code: str
    name: str
    qt_locale: str | None = None


class LanguageManager:
    _instance: ClassVar["LanguageManager | None"] = None

    SUPPORTED_LANGUAGES: ClassVar[dict[str, LanguageInfo]] = {
        "en": LanguageInfo(code="en", name="English", qt_locale="en"),
        "zh-CN": LanguageInfo(code="zh-CN", name="中文（简体）", qt_locale="zh_CN"),
    }

    def __init__(self) -> None:
        self._app_translator: QTranslator | None = None
        self._qt_translator: QTranslator | None = None
        self._current_language = "en"
        self._initialized = False

    @classmethod
    def instance(cls) -> "LanguageManager":
        if cls._instance is None:
            cls._instance = LanguageManager()
        return cls._instance

    def initialize(self, settings: dict) -> None:
        if self._initialized:
            return
        self.set_language(self.detect(settings.get("ui_language", "system")))
        self._initialized = True

    def detect(self, preferred: str) -> str:
        if preferred and preferred != "system" and preferred in self.SUPPORTED_LANGUAGES:
            return preferred
        for ui_lang in QLocale.system().uiLanguages():
            norm = ui_lang.replace('_', '-').lower()
            for code in self.SUPPORTED_LANGUAGES:
                if code.lower() == norm:
                    return code
            base = norm.split('-')[0]
            for code in self.SUPPORTED_LANGUAGES:
                if code.lower().split('-')[0] == base:
                    return code
        return "en"

    def available_languages(self) -> list[LanguageInfo]:
        return list(self.SUPPORTED_LANGUAGES.values())

    def current_language(self) -> str:
        return self._current_language

    @staticmethod
    def _install(path: Path) -> QTranslator | None:
        translator = QTranslator()
        if not path.exists() or not translator.load(str(path)):
            return None
        QCoreApplication.installTranslator(translator)
        return translator

    def set_language(self, code: str) -> bool:
        requested_supported = code in self.SUPPORTED_LANGUAGES
        if not requested_supported:
            code = "en"
        if self._initialized and code == self._current_language:
            return requested_supported

        for translator in (self._app_translator, self._qt_translator):
            if translator:
                QCoreApplication.removeTranslator(translator)
        self._app_translator = self._qt_translator = None

        qt_locale = self.SUPPORTED_LANGUAGES[code].qt_locale or code
        qtbase_file = f"qtbase_{qt_locale}.qm"
        bundled_qtbase = resource_path(Path("resources/i18n") / qtbase_file)
        system_qtbase = Path(QLibraryInfo.path(QLibraryInfo.TranslationsPath)) / qtbase_file
        self._qt_translator = self._install(
            bundled_qtbase if bundled_qtbase.exists() else system_qtbase
        )
        self._app_translator = self._install(
            resource_path(Path("resources/i18n") / f"REasy_{code}.qm")
        )
        self._current_language = code

        app = QGuiApplication.instance()
        if isinstance(app, QGuiApplication):
            app.setLayoutDirection(QLocale(qt_locale).textDirection())
        return requested_supported

