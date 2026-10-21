import json
import os

from PySide6.QtCore import QT_TRANSLATE_NOOP, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

lazy from app_config import GAMES
lazy from i18n.language_manager import LanguageManager
lazy from settings import DEFAULT_SETTINGS
lazy from tools.wwise_toolchain import (
    suggested_wwise_browse_path,
    validate_wwise_installation,
    wwise_profile_for_game,
)
lazy from ui.editor_widgets import styled_label
lazy from ui.keyboard_shortcuts import create_shortcuts_tab
lazy from ui.styles import current_colors


class SettingsDialog(QDialog):
    """Edit application settings and apply their runtime side effects."""

    _TRANSLATION_LANGUAGES = (
        ("en", QT_TRANSLATE_NOOP("SettingsDialog", "English")),
        ("ar", QT_TRANSLATE_NOOP("SettingsDialog", "Arabic")),
        ("es", QT_TRANSLATE_NOOP("SettingsDialog", "Spanish")),
        ("fr", QT_TRANSLATE_NOOP("SettingsDialog", "French")),
        ("de", QT_TRANSLATE_NOOP("SettingsDialog", "German")),
        ("it", QT_TRANSLATE_NOOP("SettingsDialog", "Italian")),
        ("ja", QT_TRANSLATE_NOOP("SettingsDialog", "Japanese")),
        ("ko", QT_TRANSLATE_NOOP("SettingsDialog", "Korean")),
        ("pt", QT_TRANSLATE_NOOP("SettingsDialog", "Portuguese")),
        ("ru", QT_TRANSLATE_NOOP("SettingsDialog", "Russian")),
        ("zh-CN", QT_TRANSLATE_NOOP("SettingsDialog", "Chinese (Simplified)")),
        ("zh-TW", QT_TRANSLATE_NOOP("SettingsDialog", "Chinese (Traditional)")),
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
        paths = self.settings.get("wwise_install_paths", {})
        self._wwise_paths = dict(paths) if isinstance(paths, dict) else {}
        self.wwise_game = ""

        self.setWindowTitle(self.tr("Settings"))
        # Tall enough for the General tab, but never past the screen so OK/Cancel stay visible.
        available = self.screen().availableGeometry()
        self.resize(min(700, available.width() - 40), min(700, available.height() - 60))
        self._build_ui()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(8, 8, 8, 8)
        main_layout.setSpacing(8)

        tab_widget = QTabWidget()
        main_layout.addWidget(tab_widget, 1)

        general_tab = QScrollArea()
        general_tab.setWidgetResizable(True)
        general_tab.setFrameShape(QFrame.NoFrame)
        tab_widget.addTab(general_tab, self.tr("General"))
        general_body = QWidget()
        general_tab.setWidget(general_body)
        general_layout = QVBoxLayout(general_body)
        general_layout.setContentsMargins(0, 8, 4, 0)
        general_layout.setSpacing(8)
        row_labels = []

        def add_row(form, text, field):
            label = QLabel(text)
            row_labels.append(label)
            form.addRow(label, field)
            return label

        game_form = self._add_section(general_layout, self.tr("Game"))
        self.game_version_combo = QComboBox()
        self.game_version_combo.addItems(GAMES)
        self.game_version_combo.setCurrentText(self.settings.get("game_version", "RE4"))
        add_row(game_form, self.tr("Game Version (Reload Required):"), self._fixed_field(self.game_version_combo))

        self.json_entry = QLineEdit(self.settings.get("rcol_json_path", ""))
        self.json_browse_button = self._browse_button()
        add_row(game_form, self.tr("RSZ JSON Path:"), self._path_row(self.json_entry, self.json_browse_button))

        self.wwise_label = QLabel()
        self.wwise_label.setWordWrap(True)
        self.wwise_entry = QLineEdit()
        self.wwise_browse_button = self._browse_button()
        game_form.addRow(self.wwise_label, self._path_row(self.wwise_entry, self.wwise_browse_button))
        self._on_wwise_game_changed(self.game_version_combo.currentText())

        tools_form = self._add_section(general_layout, self.tr("Tools"))
        self.vgmstream_entry = QLineEdit(self.settings.get("vgmstream_cli_path", ""))
        self.vgmstream_browse_button = self._browse_button()
        add_row(
            tools_form, self.tr("VGMStream CLI Path:"),
            self._path_row(self.vgmstream_entry, self.vgmstream_browse_button),
        )
        self.blender_entry = QLineEdit(self.settings.get("blender_path", ""))
        self.blender_entry.setPlaceholderText(self.tr("Found automatically when installed"))
        self.blender_browse_button = self._browse_button()
        add_row(
            tools_form, self.tr("Blender Executable (.blend export/import):"),
            self._path_row(self.blender_entry, self.blender_browse_button),
        )

        interface_form = self._add_section(general_layout, self.tr("Interface"))
        self.theme_color_button = QPushButton()
        self.theme_color_button.setFixedSize(44, 22)
        self.theme_color_button.setToolTip(self.tr("Select Theme Color"))
        self.theme_color_button.setCursor(Qt.PointingHandCursor)
        self._theme_color_text = styled_label(tone="muted", mono=True)
        theme_color_row = QHBoxLayout()
        theme_color_row.setContentsMargins(0, 0, 0, 0)
        theme_color_row.setSpacing(8)
        theme_color_row.addWidget(self.theme_color_button)
        theme_color_row.addWidget(self._theme_color_text)
        theme_color_row.addStretch()
        self._update_theme_color_button()
        add_row(interface_form, self.tr("Theme Color:"), theme_color_row)

        self.ui_language_combo = QComboBox()
        self.ui_language_combo.addItem(self.tr("System"), "system")
        for info in LanguageManager.instance().available_languages():
            self.ui_language_combo.addItem(info.name, info.code)
        ui_language_index = self.ui_language_combo.findData(
            self.settings.get("ui_language", "system")
        )
        if ui_language_index >= 0:
            self.ui_language_combo.setCurrentIndex(ui_language_index)
        add_row(
            interface_form, self.tr("UI Language (Restart Recommended):"),
            self._fixed_field(self.ui_language_combo),
        )

        self.translation_combo = QComboBox()
        for code, name in self._TRANSLATION_LANGUAGES:
            self.translation_combo.addItem(self.tr(name), code)
        translation_index = self.translation_combo.findData(
            self.settings.get("translation_target_language", "en")
        )
        if translation_index >= 0:
            self.translation_combo.setCurrentIndex(translation_index)
        add_row(
            interface_form, self.tr("Translation Target Language:"),
            self._fixed_field(self.translation_combo),
        )

        self.debug_box = QCheckBox(self.tr("Show Debug Console"))
        self.debug_box.setChecked(self.settings.get("show_debug_console", False))
        interface_form.addRow(self.debug_box)

        editor_form = self._add_section(general_layout, self.tr("Editor"))
        self.rsz_advanced_box = QCheckBox(
            self.tr("Show advanced settings for RSZ files (Reload Required)")
        )
        self.rsz_advanced_box.setChecked(self.settings.get("show_rsz_advanced", True))
        editor_form.addRow(self.rsz_advanced_box)

        self.backup_box = QCheckBox(self.tr("Create backup on save"))
        self.backup_box.setChecked(self.settings.get("backup_on_save", True))
        editor_form.addRow(self.backup_box)

        self.save_workspace_box = QCheckBox(self.tr("Remember workspace between sessions"))
        self.save_workspace_box.setChecked(
            self.settings.get("save_workspace_on_close", True)
        )
        editor_form.addRow(self.save_workspace_box)

        self.confirmation_prompt_box = QCheckBox(
            self.tr("Show confirmation prompts for RSZ actions")
        )
        self.confirmation_prompt_box.setChecked(
            self.settings.get("confirmation_prompt", True)
        )
        editor_form.addRow(self.confirmation_prompt_box)

        self.verify_rsz_crc_on_open_box = QCheckBox(
            self.tr("Verify RSZ CRCs against registry when opening files")
        )
        self.verify_rsz_crc_on_open_box.setChecked(
            self.settings.get("verify_rsz_crc_on_open", True)
        )
        editor_form.addRow(self.verify_rsz_crc_on_open_box)

        self.shader_cache_spin = QSpinBox()
        self.shader_cache_spin.setRange(0, 1 << 20)
        self.shader_cache_spin.setSingleStep(256)
        self.shader_cache_spin.setValue(self.settings["parsed_shader_cache_mb"])
        shader_cache_label = add_row(
            editor_form, self.tr("Parsed shader cache (MB):"), self._fixed_field(self.shader_cache_spin)
        )
        shader_cache_label.setToolTip(self.tr(
            "Memory for parsed shader files no open view uses, kept for later views; the least recently used go "
            "first. 0 keeps only what open views use."
        ))
        self.shader_cache_spin.setToolTip(shader_cache_label.toolTip())
        general_layout.addStretch()

        # One label column width for every section, so the fields line up.
        label_width = max(label.sizeHint().width() for label in row_labels)
        for label in row_labels:
            label.setMinimumWidth(label_width)
        self.wwise_label.setFixedWidth(label_width)

        shortcuts_tab = create_shortcuts_tab()
        tab_widget.addTab(shortcuts_tab, self.tr("Keyboard Shortcuts"))
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
            lambda *_args: shortcuts_tab.edit_shortcut(self.shortcuts, self)
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        main_layout.addWidget(buttons)

        buttons.accepted.connect(self._apply_and_accept)
        buttons.rejected.connect(self.reject)
        self.json_browse_button.clicked.connect(self._browse_json)
        self.vgmstream_browse_button.clicked.connect(self._browse_vgmstream)
        self.blender_browse_button.clicked.connect(self._browse_blender)
        self.wwise_browse_button.clicked.connect(self._browse_wwise)
        self.game_version_combo.currentTextChanged.connect(
            self._on_wwise_game_changed
        )
        self.theme_color_button.clicked.connect(self._choose_theme_color)

    @staticmethod
    def _add_section(layout, title):
        card = QFrame()
        card.setProperty("surface", "card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(0, 0, 0, 0)
        card_layout.setSpacing(0)
        header = styled_label(title, surface="header", heading="section")
        header.setContentsMargins(8, 5, 8, 5)
        card_layout.addWidget(header)
        form = QFormLayout()
        form.setContentsMargins(10, 8, 10, 10)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        card_layout.addLayout(form)
        layout.addWidget(card)
        return form

    def _browse_button(self):
        button = QPushButton(self.tr("Browse..."))
        button.setProperty("compact", True)
        return button

    @staticmethod
    def _path_row(entry, button):
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(entry, 1)
        row.addWidget(button)
        return row

    @staticmethod
    def _fixed_field(widget):
        widget.setMinimumWidth(220)
        return widget

    def _update_theme_color_button(self):
        color_value = self.selected_theme_color
        self._theme_color_text.setText(color_value.upper())
        colors = current_colors()
        self.theme_color_button.setStyleSheet(
            f"QPushButton {{ background-color: {color_value}; border: 1px solid {colors['border_strong']};"
            f" border-radius: 3px; padding: 0px; min-width: 0px; min-height: 0px; }}"
            f" QPushButton:hover {{ border-color: {colors['text_muted']}; }}"
        )

    def _choose_theme_color(self):
        color = QColorDialog.getColor(
            QColor(self.selected_theme_color),
            self,
            self.tr("Select Theme Color"),
        )
        if color.isValid():
            self.selected_theme_color = color.name()
            self._update_theme_color_button()

    def _browse_json(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select JSON file"),
            os.path.dirname(self.json_entry.text()) if self.json_entry.text() else "",
            self.tr("JSON Files (*.json)"),
        )
        if file_path:
            self.json_entry.setText(file_path)

    def _browse_vgmstream(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select VGMStream CLI"),
            os.path.dirname(self.vgmstream_entry.text()) if self.vgmstream_entry.text() else "",
            self.tr("Executable Files (*)"),
        )
        if file_path:
            self.vgmstream_entry.setText(file_path)

    def _browse_blender(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Select Blender"),
            os.path.dirname(self.blender_entry.text()) if self.blender_entry.text() else "",
            self.tr("Executable Files (*)"),
        )
        if file_path:
            self.blender_entry.setText(file_path)

    def _remember_wwise_path(self):
        if not self.wwise_game:
            return
        value = self.wwise_entry.text().strip()
        if value:
            self._wwise_paths[self.wwise_game] = value
        else:
            self._wwise_paths.pop(self.wwise_game, None)

    def _on_wwise_game_changed(self, game):
        self._remember_wwise_path()
        profile = wwise_profile_for_game(game)
        self.wwise_game = profile.game if profile else ""
        enabled = profile is not None
        self.wwise_entry.setEnabled(enabled)
        self.wwise_browse_button.setEnabled(enabled)
        if not profile:
            self.wwise_label.setText(
                self.tr("Wwise authoring — no sound profile for {game}").format(
                    game=game
                )
            )
            self.wwise_entry.clear()
            self.wwise_entry.setPlaceholderText(
                self.tr("Add a sound profile to enable WAV authoring")
            )
            return
        self.wwise_label.setText(
            self.tr("Wwise for {game} — required {required}:").format(
                game=profile.display_name,
                required=profile.required_version_text,
            )
        )
        self.wwise_entry.setPlaceholderText(
            self.tr("Select Wwise {version} installation").format(
                version=profile.required_version_text
            )
        )
        self.wwise_entry.setText(self._wwise_paths.get(profile.game, ""))

    def _browse_wwise(self):
        profile = wwise_profile_for_game(self.wwise_game)
        if profile is None:
            return
        selected = QFileDialog.getExistingDirectory(
            self,
            self.tr(
                "{game}: select Wwise {required} (recommended {recommended})"
            ).format(
                required=profile.required_version_text,
                game=profile.display_name,
                recommended=profile.recommended_version,
            ),
            suggested_wwise_browse_path(self.wwise_entry.text().strip()),
            QFileDialog.Option.ShowDirsOnly,
        )
        if not selected:
            return
        try:
            installation = validate_wwise_installation(selected, profile.game)
        except ValueError as exc:
            QMessageBox.warning(
                self,
                self.tr("Incompatible Wwise Installation"),
                str(exc),
            )
            return
        self.wwise_entry.setText(str(installation.root))

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
                        self.tr("Error"),
                        self.tr("Invalid RSZ type registry JSON file."),
                    )
                    return
            except Exception:
                QMessageBox.critical(
                    self, self.tr("Error"), self.tr("Invalid JSON file.")
                )
                return
        elif new_json_path:
            QMessageBox.critical(
                self,
                self.tr("Error"),
                self.tr("The specified JSON file does not exist."),
            )
            return

        app = self.app_window
        app.set_rsz_json_path(new_json_path, save=False)
        self.settings["vgmstream_cli_path"] = self.vgmstream_entry.text().strip()
        self.settings["blender_path"] = self.blender_entry.text().strip()
        self._remember_wwise_path()
        for game, path in tuple(self._wwise_paths.items()):
            profile = wwise_profile_for_game(game)
            if profile is None or not path:
                continue
            try:
                installation = validate_wwise_installation(path, profile.game)
            except ValueError as exc:
                QMessageBox.critical(
                    self,
                    self.tr("Incompatible Wwise Installation"),
                    str(exc),
                )
                return
            self._wwise_paths[profile.game] = str(installation.root)
        self.settings["wwise_install_paths"] = {
            game: path for game, path in self._wwise_paths.items() if path
        }
        self.settings["show_debug_console"] = self.debug_box.isChecked()
        self.settings["show_rsz_advanced"] = self.rsz_advanced_box.isChecked()
        self.settings["backup_on_save"] = self.backup_box.isChecked()
        self.settings["save_workspace_on_close"] = self.save_workspace_box.isChecked()
        self.settings["confirmation_prompt"] = self.confirmation_prompt_box.isChecked()
        self.settings["verify_rsz_crc_on_open"] = self.verify_rsz_crc_on_open_box.isChecked()
        self.settings["parsed_shader_cache_mb"] = self.shader_cache_spin.value()
        self.settings["keyboard_shortcuts"] = self.shortcuts
        self.settings["tree_highlight_color"] = self.selected_theme_color
        self.settings["game_version"] = self.game_version_combo.currentText()

        translation_index = self.translation_combo.currentIndex()
        if translation_index >= 0:
            self.settings["translation_target_language"] = self.translation_combo.itemData(
                translation_index
            )

        app.toggle_debug_console(self.debug_box.isChecked())
        app._apply_style(app._build_theme_colors())
        if hasattr(app, "project_workspace"):
            app.project_workspace.apply_style()
        app.update_from_app_settings()
        app.apply_keyboard_shortcuts()

        new_ui_language = self.ui_language_combo.currentData()
        language_changed = new_ui_language != self.settings.get("ui_language", "system")
        self.settings["ui_language"] = new_ui_language
        app.save_settings()

        if language_changed:
            QMessageBox.information(
                self,
                self.tr("Language Changed"),
                self.tr("UI language will be applied after restart."),
            )
        self.accept()
