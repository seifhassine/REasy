"""Application settings dialog."""

import json
import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from app_config import GAMES
from i18n.language_manager import LanguageManager
from settings import DEFAULT_SETTINGS
from tools.ffmpeg_downloader import ensure_ffmpeg, ffmpeg_status
from ui.keyboard_shortcuts import create_shortcuts_tab


class SettingsDialog(QDialog):
    """Edit application settings and apply their runtime side effects."""

    _TRANSLATION_LANGUAGES = (
        ("en", "English"),
        ("ar", "Arabic"),
        ("es", "Spanish"),
        ("fr", "French"),
        ("de", "German"),
        ("it", "Italian"),
        ("ja", "Japanese"),
        ("ko", "Korean"),
        ("pt", "Portuguese"),
        ("ru", "Russian"),
        ("zh-CN", "Chinese (Simplified)"),
        ("zh-TW", "Chinese (Traditional)"),
    )

    def __init__(self, app_window):
        super().__init__(app_window)
        self.app_window = app_window
        self.settings = app_window.settings
        self.selected_theme_color = self.settings.get(
            "tree_highlight_color",
            DEFAULT_SETTINGS["tree_highlight_color"],
        )
        self.shortcuts = self.settings.get("keyboard_shortcuts", {}).copy()

        self.setWindowTitle(app_window.tr("Settings"))
        self.resize(500, 400)
        self._build_ui()

    def _build_ui(self):
        tr = self.app_window.tr
        main_layout = QVBoxLayout(self)

        tab_widget = QTabWidget()
        main_layout.addWidget(tab_widget)

        general_tab = QWidget()
        tab_widget.addTab(general_tab, tr("General"))
        general_layout = QVBoxLayout(general_tab)
        general_layout.setSpacing(15)

        general_layout.addWidget(QLabel(tr("RSZ JSON Path:")), 0, Qt.AlignBottom)
        json_path_layout = QHBoxLayout()
        json_path_layout.setContentsMargins(0, 0, 0, 0)
        self.json_entry = QLineEdit(self.settings.get("rcol_json_path", ""))
        self.json_browse_button = QPushButton(tr("Browse..."))
        json_path_layout.addWidget(self.json_entry)
        json_path_layout.addWidget(self.json_browse_button)
        general_layout.addLayout(json_path_layout)

        general_layout.addWidget(QLabel("VGMStream CLI Path:"), 0, Qt.AlignBottom)
        vgmstream_layout = QHBoxLayout()
        vgmstream_layout.setContentsMargins(0, 0, 0, 0)
        self.vgmstream_entry = QLineEdit(self.settings.get("vgmstream_cli_path", ""))
        self.vgmstream_browse_button = QPushButton("Browse...")
        vgmstream_layout.addWidget(self.vgmstream_entry)
        vgmstream_layout.addWidget(self.vgmstream_browse_button)
        general_layout.addLayout(vgmstream_layout)

        ffmpeg_layout = QHBoxLayout()
        ffmpeg_layout.setContentsMargins(0, 0, 0, 0)
        ffmpeg_layout.addWidget(QLabel("FFmpeg (Auto-download):"))
        self.ffmpeg_status_label = QLabel("")
        ffmpeg_layout.addWidget(self.ffmpeg_status_label)
        ffmpeg_layout.addStretch()
        self.ffmpeg_download_button = QPushButton("Download")
        ffmpeg_layout.addWidget(self.ffmpeg_download_button)
        general_layout.addLayout(ffmpeg_layout)

        game_version_layout = QHBoxLayout()
        game_version_layout.setContentsMargins(0, 0, 0, 0)
        game_version_layout.addWidget(QLabel(tr("Game Version (Reload Required):")))
        self.game_version_combo = QComboBox()
        self.game_version_combo.addItems(GAMES)
        self.game_version_combo.setCurrentText(self.settings.get("game_version", "RE4"))
        game_version_layout.addWidget(self.game_version_combo)
        general_layout.addLayout(game_version_layout)

        translation_layout = QHBoxLayout()
        translation_layout.setContentsMargins(0, 0, 0, 0)
        translation_layout.addWidget(QLabel(tr("Translation Target Language:")))
        self.translation_combo = QComboBox()
        for code, name in self._TRANSLATION_LANGUAGES:
            if code in {"zh-CN", "zh-TW"}:
                name = tr(name)
            self.translation_combo.addItem(name, code)
        translation_index = self.translation_combo.findData(
            self.settings.get("translation_target_language", "en")
        )
        if translation_index >= 0:
            self.translation_combo.setCurrentIndex(translation_index)
        translation_layout.addWidget(self.translation_combo)
        general_layout.addLayout(translation_layout)

        theme_color_layout = QHBoxLayout()
        theme_color_layout.setContentsMargins(0, 0, 0, 0)
        theme_color_layout.addWidget(QLabel(tr("Theme Color:")))
        self.theme_color_button = QPushButton()
        self.theme_color_button.setFixedWidth(140)
        self._update_theme_color_button()
        theme_color_layout.addWidget(self.theme_color_button)
        theme_color_layout.addStretch()
        general_layout.addLayout(theme_color_layout)

        self.dark_box = QCheckBox(tr("Dark Mode"))
        self.dark_box.setChecked(self.app_window.dark_mode)
        general_layout.addWidget(self.dark_box)

        self.debug_box = QCheckBox(tr("Show Debug Console"))
        self.debug_box.setChecked(self.settings.get("show_debug_console", True))
        general_layout.addWidget(self.debug_box)

        self.rsz_advanced_box = QCheckBox(
            tr("Show advanced settings for RSZ files (Reload Required)")
        )
        self.rsz_advanced_box.setChecked(self.settings.get("show_rsz_advanced", True))
        general_layout.addWidget(self.rsz_advanced_box)

        self.backup_box = QCheckBox(tr("Create backup on save"))
        self.backup_box.setChecked(self.settings.get("backup_on_save", True))
        general_layout.addWidget(self.backup_box)

        self.confirmation_prompt_box = QCheckBox(
            tr("Show confirmation prompts for RSZ actions")
        )
        self.confirmation_prompt_box.setChecked(
            self.settings.get("confirmation_prompt", True)
        )
        general_layout.addWidget(self.confirmation_prompt_box)

        self.verify_rsz_crc_on_open_box = QCheckBox(
            "Verify RSZ CRCs against registry when opening files"
        )
        self.verify_rsz_crc_on_open_box.setChecked(
            self.settings.get("verify_rsz_crc_on_open", True)
        )
        general_layout.addWidget(self.verify_rsz_crc_on_open_box)

        ui_language_layout = QHBoxLayout()
        ui_language_layout.setContentsMargins(0, 0, 0, 0)
        ui_language_layout.addWidget(QLabel(tr("UI Language (Restart Recommended):")))
        self.ui_language_combo = QComboBox()
        self.ui_language_combo.addItem("System", "system")
        for info in LanguageManager.instance().available_languages():
            self.ui_language_combo.addItem(info.name, info.code)
        ui_language_index = self.ui_language_combo.findData(
            self.settings.get("ui_language", "system")
        )
        if ui_language_index >= 0:
            self.ui_language_combo.setCurrentIndex(ui_language_index)
        ui_language_layout.addWidget(self.ui_language_combo)
        general_layout.addLayout(ui_language_layout)
        general_layout.addStretch()

        shortcuts_tab = create_shortcuts_tab()
        tab_widget.addTab(shortcuts_tab, tr("Keyboard Shortcuts"))
        for key in shortcuts_tab.shortcut_names:
            self.shortcuts.setdefault(
                key,
                DEFAULT_SETTINGS["keyboard_shortcuts"].get(key, ""),
            )
        shortcuts_tab.populate_shortcuts_list(self.shortcuts)
        shortcuts_tab.edit_shortcut_btn.clicked.connect(
            lambda: shortcuts_tab.edit_shortcut(self.shortcuts, self)
        )
        shortcuts_tab.reset_shortcut_btn.clicked.connect(
            lambda: shortcuts_tab.reset_shortcut(self.shortcuts, self)
        )
        shortcuts_tab.shortcuts_list.itemDoubleClicked.connect(
            lambda _item: shortcuts_tab.edit_shortcut(self.shortcuts, self)
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        main_layout.addWidget(buttons)

        buttons.accepted.connect(self._apply_and_accept)
        buttons.rejected.connect(self.reject)
        self.json_browse_button.clicked.connect(self._browse_json)
        self.vgmstream_browse_button.clicked.connect(self._browse_vgmstream)
        self.ffmpeg_download_button.clicked.connect(self._download_ffmpeg)
        self.theme_color_button.clicked.connect(self._choose_theme_color)
        self._refresh_ffmpeg_status()

    def _update_theme_color_button(self):
        color_value = self.selected_theme_color
        self.theme_color_button.setText(color_value.upper())
        color = QColor(color_value)
        text_color = "#000000"
        if color.isValid():
            brightness = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
            if brightness < 186:
                text_color = "#ffffff"
        self.theme_color_button.setStyleSheet(
            f"background-color: {color_value}; border: 1px solid #555555; color: {text_color};"
        )

    def _choose_theme_color(self):
        color = QColorDialog.getColor(
            QColor(self.selected_theme_color),
            self,
            self.app_window.tr("Select Theme Color"),
        )
        if color.isValid():
            self.selected_theme_color = color.name()
            self._update_theme_color_button()

    def _browse_json(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.app_window.tr("Select JSON file"),
            os.path.dirname(self.json_entry.text()) if self.json_entry.text() else "",
            "JSON Files (*.json)",
        )
        if file_path:
            self.json_entry.setText(file_path)

    def _browse_vgmstream(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select VGMStream CLI",
            os.path.dirname(self.vgmstream_entry.text()) if self.vgmstream_entry.text() else "",
            "Executable Files (*)",
        )
        if file_path:
            self.vgmstream_entry.setText(file_path)

    def _refresh_ffmpeg_status(self):
        need_download, _ = ffmpeg_status()
        self.ffmpeg_status_label.setText("Not downloaded" if need_download else "Installed")

    def _download_ffmpeg(self):
        try:
            ensure_ffmpeg(auto_download=True, parent_window=self)
            self._refresh_ffmpeg_status()
            QMessageBox.information(self, "FFmpeg", "FFmpeg download complete.")
        except Exception as exc:
            QMessageBox.critical(self, "FFmpeg", f"Failed to download FFmpeg\n{exc}")

    def _apply_and_accept(self):
        new_json_path = self.json_entry.text().strip()
        if new_json_path and os.path.exists(new_json_path):
            try:
                with open(new_json_path, "r") as registry_file:
                    data = json.load(registry_file)
                if (
                    not isinstance(data, dict)
                    or not data
                    or "fields" not in next(iter(data.values()))
                ):
                    QMessageBox.critical(
                        self,
                        "Error",
                        "Invalid RSZ type registry JSON file.",
                    )
                    return
            except Exception:
                QMessageBox.critical(self, "Error", "Invalid JSON file.")
                return
        elif new_json_path:
            QMessageBox.critical(
                self,
                self.app_window.tr("Error"),
                self.app_window.tr("The specified JSON file does not exist."),
            )
            return

        app = self.app_window
        app.set_rsz_json_path(new_json_path, save=False)
        self.settings["vgmstream_cli_path"] = self.vgmstream_entry.text().strip()
        self.settings["dark_mode"] = self.dark_box.isChecked()
        self.settings["show_debug_console"] = self.debug_box.isChecked()
        self.settings["show_rsz_advanced"] = self.rsz_advanced_box.isChecked()
        self.settings["backup_on_save"] = self.backup_box.isChecked()
        self.settings["confirmation_prompt"] = self.confirmation_prompt_box.isChecked()
        self.settings["verify_rsz_crc_on_open"] = self.verify_rsz_crc_on_open_box.isChecked()
        self.settings["keyboard_shortcuts"] = self.shortcuts
        self.settings["tree_highlight_color"] = self.selected_theme_color
        self.settings["game_version"] = self.game_version_combo.currentText()

        translation_index = self.translation_combo.currentIndex()
        if translation_index >= 0:
            self.settings["translation_target_language"] = self.translation_combo.itemData(
                translation_index
            )

        if app.dark_mode != self.dark_box.isChecked():
            app.set_dark_mode(self.dark_box.isChecked())
        app.toggle_debug_console(self.debug_box.isChecked())
        app._apply_style(app._build_theme_colors(app.dark_mode))
        app.update_from_app_settings()
        app.apply_keyboard_shortcuts()

        new_ui_language = self.ui_language_combo.currentData()
        language_changed = new_ui_language != self.settings.get("ui_language", "system")
        self.settings["ui_language"] = new_ui_language
        app.save_settings()

        if language_changed:
            QMessageBox.information(
                self,
                app.tr("Language Changed"),
                app.tr("UI language will be applied after restart."),
            )
        self.accept()
