"""REasy's main application window and UI orchestration."""

import os
import sys
import weakref

from file_handlers.factory import get_handler_for_data, is_handler_type

from ui.better_find_dialog import BetterFindDialog
from ui.file_tab import FileTab, UNSAVED_CHANGES_STR
from ui.guid_converter import create_guid_converter_dialog
from ui.about_dialog import AboutDialog
from ui.outdated_files_dialog import OutdatedFilesDialog
from ui.update_notification import UpdateNotificationManager
from ui.rsz_differ_dialog import RszDifferDialog
from ui.file_list_generator_dialog import FileListGeneratorDialog
from ui.rsz_enum_prompt import RszEnumPromptController
from settings import DEFAULT_SETTINGS, load_settings, normalize_settings, save_settings
from app_config import CURRENT_VERSION, GAMES
from ui.changelog_dialog import ChangelogDialog
from ui.settings_dialog import SettingsDialog
from ui.styles import get_color_scheme, get_main_stylesheet
from utils.app_paths import resource_path

from PySide6.QtCore import (
    Qt,
    QTimer,
    QUrl,
)
from PySide6.QtGui import (
    QIcon,
    QAction,
    QKeySequence,
    QDesktopServices,
    QColor,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QSizePolicy,
    QMessageBox,
    QFileDialog,
    QInputDialog,
    QVBoxLayout,
    QLabel,
    QDialog,
    QStatusBar,
    QDialogButtonBox,
    QListWidget,
    QListWidgetItem,
)

from ui.console_logger import ConsoleWidget, ConsoleRedirector
from ui.detachable_tabs import CustomNotebook, FloatingTabWindow
from ui.directory_search import search_directory_for_type
from ui.highlight_menu_controller import HighlightMenuController
from ui.homepage import HomePageStack, HomePageWidget
from ui.scene.opengl_setup import create_surface_anchor
from tools.hash_calculator import HashCalculator

from ui.project_manager.project_picker_dialog import ProjectPickerDialog  # noqa: E402
from ui.project_manager.source_dialog import SelectSourceDialog  # noqa: E402
from ui.project_manager import (  # noqa: E402
    ProjectManager, ProjectWorkspaceController, PROJECTS_ROOT, ensure_projects_root
)

RECENTLY_CLOSED_FILES_LIMIT = 20


class _LazySceneController:
    def __init__(self, app):
        self._app = app

    def __getattr__(self, name):
        from ui.scene.scn_scene_workspace import ScnSceneController
        self._app.scenes = controller = ScnSceneController(self._app)
        return getattr(controller, name)

def set_app_icon(window):
    try:
        icon_path = resource_path("resources/icons/reasy_editor_logo.ico", required=True)
        window.setWindowIcon(QIcon(str(icon_path)))
    except IOError as e:
        print("Failed to set window icon:", e)


class REasyEditorApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.current_game = None
        self.setWindowTitle(
            self.tr("REasy Editor v{version}").format(version=CURRENT_VERSION)
        )
        set_app_icon(self)

        try:
            self.settings = load_settings()
        except Exception as e:
            self.settings = normalize_settings()
            print(f"Error loading settings: {e}")

        ensure_projects_root()
        self.current_project = None
        self.proj_dock = ProjectManager(self, None)
        self.addDockWidget(Qt.LeftDockWidgetArea, self.proj_dock)
        self.proj_dock.hide()

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        # This child must exist before the main window is first shown. Otherwise
        # Qt recreates the native window when the first mesh preview is opened.
        self._opengl_surface_anchor = create_surface_anchor(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        self.home_widget = HomePageWidget(
            on_open_file=self.on_open,
            on_new_project=self.new_project,
            on_open_project=self.open_project,
            on_reopen_last=self.reopen_last_closed_file,
            parent=self,
        )
        self.notebook = CustomNotebook()
        self.notebook.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.notebook.setMinimumSize(50, 50)
        self.notebook.app_instance = self
        self.notebook._set_icon_callback = set_app_icon
        self.tabs = weakref.WeakValueDictionary()
        self.home_stack = HomePageStack(self.notebook, self.home_widget)
        main_layout.addWidget(self.home_stack.widget)

        self._shared_find_dialog = None
        self._pak_browser = None
        self._guid_converter_dialog = None
        self._outdated_files_dialog = None
        self._rsz_field_value_finder_dialog = None
        self._rsz_differ_dialog = None
        self._file_list_generator_dialog = None
        history = self.settings.get("recently_closed_files", [])
        self._closed_file_history = [f for f in history if isinstance(f, str) and f][-RECENTLY_CLOSED_FILES_LIMIT:]
        self.recently_closed_menu = None
        self.scene_menu = None
        self.scenes = _LazySceneController(self)

        self.update_notification = UpdateNotificationManager(self, CURRENT_VERSION)
        self._update_menu = None

        self.highlight_menu_controller = HighlightMenuController(self)

        self._create_menus()

        self.notebook.currentChanged.connect(self._update_highlight_menu_visibility)
        self.notebook.currentChanged.connect(lambda _index: self.scenes.refresh_actions())
        self.notebook.currentChanged.connect(lambda _index: self._refresh_homepage())
        self.proj_dock.visibilityChanged.connect(lambda _visible: self._refresh_homepage())

        self.status_bar = QStatusBar()
        self.status_bar.setContentsMargins(0, 0, 0, 0)
        self.status_bar.setMaximumHeight(20)
        self.status_bar.setStyleSheet("""
            QStatusBar {
                margin: 0;
                padding: 0;
                border-top: 1px solid #cccccc;
            }
            QStatusBar::item {
                border: none;
            }
        """)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(self.tr("Ready"))

        self._apply_style(self._build_theme_colors())

        self.console_widget = ConsoleWidget()
        self.console_widget.setMaximumHeight(100)
        self.console_widget.setVisible(self.settings.get("show_debug_console", True))
        main_layout.addWidget(self.console_widget)

        self.project_workspace = ProjectWorkspaceController(self, self.notebook, self.tabs)

        if self.settings.get("show_debug_console", True):
            sys.stdout = ConsoleRedirector(self.console_widget, sys.stdout)
            sys.stderr = ConsoleRedirector(self.console_widget, sys.stderr)
            print("Debug console started.")

        self.resize(1160, 920)

        self.setAcceptDrops(True)

        last_seen = self.settings.get("last_seen_version", "")
        if last_seen != CURRENT_VERSION:
            QTimer.singleShot(600, self._show_changelog_if_needed)
        self._refresh_homepage()

    def _refresh_homepage(self):
        show_notebook = self.notebook.count() > 0 or self.proj_dock.isVisible()
        recent_label = self.tr("No recently closed files yet.")
        if self._closed_file_history:
            _, _, decoded_target = ProjectManager.decode_history_entry(self._closed_file_history[-1])
            recent_label = self.tr("Last closed: {filename}").format(
                filename=os.path.basename(decoded_target)
            )
        self.home_stack.refresh(show_notebook, recent_label, bool(self._closed_file_history))

    def _internal_drag(self, event):
        return event.mimeData().hasFormat("application/x-qabstractitemmodeldatalist")

    def _show_changelog_if_needed(self):
        last_seen = self.settings.get("last_seen_version", "")
        if last_seen != CURRENT_VERSION:
            dlg = ChangelogDialog(self, CURRENT_VERSION)
            dlg.exec()
            self.settings["last_seen_version"] = CURRENT_VERSION
            save_settings(self.settings)

    def dragEnterEvent(self, event):
        if self._internal_drag(event):
            event.ignore()
            return

        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event):
        if self._internal_drag(event):
            event.ignore()
            return

        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                self._open_path(url.toLocalFile())
            event.acceptProposedAction()

    def _open_path(self, path: str):
        file_path = path
        if not os.path.isfile(file_path):
            return False
        try:
            with open(file_path, "rb") as f:
                data = f.read()
            self.add_tab(file_path, data)
            return True
        except Exception as e:
            QMessageBox.critical(
                self,
                self.tr("Error"),
                self.tr("Failed to load {path}: {error}").format(
                    path=file_path, error=e
                ),
            )
            return False

    def _create_menus(self):
        menubar = self.menuBar()
        self.update_notification.update_update_menu(force=True, menubar=menubar)
        configured_shortcuts = self.settings.get("keyboard_shortcuts", {})

        def shortcut(name):
            return QKeySequence(
                configured_shortcuts.get(name, DEFAULT_SETTINGS["keyboard_shortcuts"].get(name, ""))
            )

        file_menu = menubar.addMenu(self.tr("File"))

        open_act = QAction(self.tr("Open File..."), self)
        open_act.setObjectName("file_open")
        open_act.setShortcut(shortcut("file_open"))
        open_act.triggered.connect(self.on_open)

        new_proj_act = QAction(self.tr("New Project (Create Mod)..."), self)
        open_proj_act = QAction(self.tr("Project Library..."), self)
        close_proj_act = QAction(self.tr("Close Project"), self)
        new_proj_act.triggered.connect(self.new_project)
        open_proj_act.triggered.connect(self.open_project)
        close_proj_act.triggered.connect(self.close_project)
        file_menu.insertSeparator(open_act)
        file_menu.insertAction(open_act, new_proj_act)
        file_menu.insertAction(open_act, open_proj_act)
        file_menu.insertAction(open_act, close_proj_act)

        file_menu.addSeparator()

        file_menu.addAction(open_act)

        save_act = QAction(self.tr("Save"), self)
        save_act.setObjectName("file_save")
        save_act.setShortcut(shortcut("file_save"))
        save_act.triggered.connect(self.on_direct_save)
        file_menu.addAction(save_act)

        save_as_act = QAction(self.tr("Save As..."), self)
        save_as_act.setObjectName("file_save_as")
        save_as_act.setShortcut(shortcut("file_save_as"))
        save_as_act.triggered.connect(self.on_save)
        file_menu.addAction(save_as_act)

        restore_backup_act = QAction(self.tr("Restore Backup..."), self)
        restore_backup_act.triggered.connect(self.on_restore_backup)
        file_menu.addAction(restore_backup_act)

        reload_act = QAction(self.tr("Reload"), self)
        reload_act.setObjectName("file_reload")
        reload_act.setShortcut(shortcut("file_reload"))
        reload_act.triggered.connect(self.reload_file)
        file_menu.addAction(reload_act)

        close_tab_act = QAction(self.tr("Close Tab"), self)
        close_tab_act.setObjectName("file_close_tab")
        close_tab_act.setShortcut(shortcut("file_close_tab"))
        close_tab_act.triggered.connect(self.close_current_tab)
        file_menu.addAction(close_tab_act)

        reopen_closed_act = QAction(self.tr("Reopen Last Closed File"), self)
        reopen_closed_act.setObjectName("file_reopen_closed")
        reopen_closed_act.setShortcut(shortcut("file_reopen_closed"))
        reopen_closed_act.triggered.connect(self.reopen_last_closed_file)
        file_menu.addAction(reopen_closed_act)

        self.recently_closed_menu = file_menu.addMenu(self.tr("Recently Closed Files"))
        self.recently_closed_menu.aboutToShow.connect(self._populate_recently_closed_menu)

        file_menu.addSeparator()

        settings_act = QAction(self.tr("Settings"), self)
        settings_act.triggered.connect(self.open_settings_dialog)
        file_menu.addAction(settings_act)

        exit_act = QAction(self.tr("Exit"), self)
        exit_act.triggered.connect(self.close)
        file_menu.addAction(exit_act)

        find_menu = menubar.addMenu(self.tr("Find"))

        find_act = QAction(self.tr("Find"), self)
        find_act.setObjectName("find_search")
        find_act.setShortcut(shortcut("find_search"))
        find_act.triggered.connect(self.open_find_dialog)
        find_menu.addAction(find_act)

        guid_act = QAction(self.tr("Search Directory for GUID"), self)
        guid_act.setObjectName("find_search_guid")
        guid_act.setShortcut(shortcut("find_search_guid"))
        guid_act.triggered.connect(self.search_directory_for_guid)
        find_menu.addAction(guid_act)

        text_act = QAction(self.tr("Search Directory for Text"), self)
        text_act.setObjectName("find_search_text")
        text_act.setShortcut(shortcut("find_search_text"))
        text_act.triggered.connect(self.search_directory_for_text)
        find_menu.addAction(text_act)

        num_act = QAction(self.tr("Search Directory for Number"), self)
        num_act.setObjectName("find_search_number")
        num_act.setShortcut(shortcut("find_search_number"))
        num_act.triggered.connect(self.search_directory_for_number)
        find_menu.addAction(num_act)

        hex_act = QAction(self.tr("Search Directory for Hex"), self)
        hex_act.setObjectName("find_search_hex")
        hex_act.setShortcut(shortcut("find_search_hex"))
        hex_act.triggered.connect(self.search_directory_for_hex)
        find_menu.addAction(hex_act)

        rsz_field_act = QAction(self.tr("Find RSZ Field Value"), self)
        rsz_field_act.setObjectName("find_rsz_field_value")
        rsz_field_act.setShortcut(shortcut("find_rsz_field_value"))
        rsz_field_act.triggered.connect(self.open_rsz_field_value_finder)
        find_menu.addAction(rsz_field_act)

        view_menu = menubar.addMenu(self.tr("View"))

        prev_tab_act = QAction(self.tr("Previous Tab"), self)
        prev_tab_act.setObjectName("view_prev_tab")
        prev_tab_act.setShortcut(shortcut("view_prev_tab"))
        prev_tab_act.triggered.connect(self.goto_previous_tab)
        view_menu.addAction(prev_tab_act)

        next_tab_act = QAction(self.tr("Next Tab"), self)
        next_tab_act.setObjectName("view_next_tab")
        next_tab_act.setShortcut(shortcut("view_next_tab"))
        next_tab_act.triggered.connect(self.goto_next_tab)
        view_menu.addAction(next_tab_act)

        dbg_act = QAction(self.tr("Toggle Debug Console"), self)
        dbg_act.setObjectName("view_debug_console")
        dbg_act.setShortcut(shortcut("view_debug_console"))
        dbg_act.triggered.connect(
            lambda: self.toggle_debug_console(
                not self.settings.get("show_debug_console", True)
            )
        )
        view_menu.addAction(dbg_act)

        self.scene_menu = menubar.addMenu(self.tr("Scene"))
        self.scene_menu.aboutToShow.connect(lambda: self.scenes.populate_scene_menu(self.scene_menu))

        tools_menu = menubar.addMenu(self.tr("Tools"))
        guid_conv_act = QAction(self.tr("GUID Converter"), self)
        guid_conv_act.triggered.connect(self.open_guid_converter)
        tools_menu.addAction(guid_conv_act)

        hash_calc_act = QAction(self.tr("Hash Calculator"), self)
        hash_calc_act.triggered.connect(self.open_hash_calculator)
        tools_menu.addAction(hash_calc_act)

        outdated_files_action = QAction(self.tr("Outdated Files Detector"), self)
        outdated_files_action.triggered.connect(self.open_outdated_files_detector)
        tools_menu.addAction(outdated_files_action)

        rsz_differ_act = QAction(self.tr("RSZ Diff Viewer"), self)
        rsz_differ_act.triggered.connect(self.open_rsz_differ)
        tools_menu.addAction(rsz_differ_act)

        pak_browser_act = QAction(self.tr("PAK Browser"), self)
        pak_browser_act.triggered.connect(self.open_pak_browser)
        tools_menu.addAction(pak_browser_act)

        file_list_gen_act = QAction(self.tr("File List Generator"), self)
        file_list_gen_act.triggered.connect(self.open_file_list_generator)
        tools_menu.addAction(file_list_gen_act)

        tools_menu.addSeparator()

        csv_extractor_act = QAction(self.tr("CSV Extractor (RSZ Data Matcher)"), self)
        csv_extractor_act.triggered.connect(self.open_rsz_csv_extractor)
        tools_menu.addAction(csv_extractor_act)

        help_menu = menubar.addMenu(self.tr("Help"))
        about_act = QAction(self.tr("About"), self)
        wiki_act = QAction(self.tr("REasy Wiki"), self)
        about_act.triggered.connect(self.show_about)
        wiki_act.triggered.connect(self.show_wiki)
        help_menu.addAction(about_act)
        help_menu.addAction(wiki_act)

        donate_menu = menubar.addMenu(self.tr("Donate"))
        donate_act = QAction(self.tr("Support REasy"), self)
        donate_act.triggered.connect(self.show_donate_dialog)
        donate_menu.addAction(donate_act)

        self.highlight_menu_controller.create_menu(menubar)

    def _update_highlight_menu_visibility(self):
        current_tab = self.get_active_tab()
        is_rsz = False
        if current_tab and hasattr(current_tab, 'handler'):
            is_rsz = is_handler_type(current_tab.handler, "RszHandler")
        self.highlight_menu_controller.update_menu_visibility(is_rsz)
        self._update_general_shortcut_state()

    def _update_general_shortcut_state(self):
        disabled = bool(getattr(self.get_active_tab(), "suppress_general_shortcuts", False))
        shortcuts = self.settings.get("keyboard_shortcuts", {})
        for action in self.findChildren(QAction):
            try:
                name = action.objectName()
                if name in shortcuts:
                    action.setShortcut(QKeySequence() if disabled else QKeySequence(shortcuts.get(name, "")))
            except RuntimeError:
                pass

    def new_project(self):
        name, ok = QInputDialog.getText(self, self.tr("New Project"), self.tr("Project name:"))
        if not ok or not name.strip():
            return

        game = self.proj_dock._choose_game()
        if not game:
            return

        choose_paks = SelectSourceDialog.prompt(self, game)
        if choose_paks is None:
            return

        use_paks = bool(choose_paks)

        if not use_paks:
            start_dir = str(self.settings.get("unpacked_path", ""))
            folder = QFileDialog.getExistingDirectory(
                self,
                self.tr("Locate unpacked files for {game}").format(game=game),
                start_dir,
                QFileDialog.ShowDirsOnly
            )
            if not folder:
                return

            expected = self.proj_dock.expected_native_tuple(game)
            if expected:
                test = os.path.join(folder, *expected)
                if not os.path.isdir(test):
                    QMessageBox.warning(
                        self, self.tr("Invalid unpacked folder"),
                        self.tr(
                            "The folder you selected doesn't contain:\n"
                            "  {expected}\n"
                            "Please select the correct unpacked game directory."
                        ).format(expected=os.path.join(*expected)))
                    return

            self.settings["unpacked_path"] = folder
            self.save_settings()
        else:
            start_dir = str(self.settings.get("unpacked_path", ""))
            folder = QFileDialog.getExistingDirectory(
                self,
                self.tr("Locate game directory (contains .pak)"),
                start_dir,
                QFileDialog.ShowDirsOnly
            )
            if not folder:
                return

            if not self.proj_dock.has_valid_paks(folder, ignore_mod_paks=True):
                QMessageBox.warning(self, self.tr("Invalid game folder"), self.tr("No .pak files found in the selected directory."))
                return

        mod_dir = os.path.join(PROJECTS_ROOT, game, name.strip())
        os.makedirs(mod_dir, exist_ok=True)

        self.project_workspace.activate(mod_dir, game)
        if use_paks:
            self.proj_dock.switch_tab("pak")
            self.proj_dock.apply_pak_root(folder)
        else:
            self.proj_dock.apply_unpacked_root(folder)

    def open_project(self):
        dlg = ProjectPickerDialog(
            PROJECTS_ROOT,
            GAMES,
            current_project=self.current_project,
            on_project_delete=self.project_workspace.delete_project,
            parent=self,
        )
        if dlg.exec() != QDialog.Accepted:
            return

        if dlg.wants_new_project():
            self.new_project()
            return

        entry = dlg.selected_project()
        if not entry:
            return

        self.project_workspace.open(entry.path, entry.game)

    def close_project(self):
        self.project_workspace.close()

    def _confirm_tabs_close(self, tabs, *, apply_discards=True) -> bool:
        discard_tabs = []
        for tab in tabs:
            if not tab or not tab.modified:
                continue
            filename = (
                os.path.basename(tab.filename) if tab.filename else self.tr("Untitled")
            )
            answer = QMessageBox.question(
                self,
                FileTab.tr(UNSAVED_CHANGES_STR),
                self.tr(
                    "The file {filename} has unsaved changes.\nSave before closing?"
                ).format(filename=filename),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
            )
            if answer == QMessageBox.Cancel:
                return False
            if answer == QMessageBox.Yes:
                tab.on_save()
                if tab.modified:
                    return False
            else:
                discard_tabs.append(tab)

        if apply_discards:
            for tab in discard_tabs:
                discard = getattr(tab, "discard_changes", None)
                if callable(discard):
                    discard()
                else:
                    tab.modified = False
                    tab.update_tab_title()
        return True

    def _shrink_project_dock(self):
        min_w = max(360, self.proj_dock.minimumSizeHint().width())
        self.resizeDocks([self.proj_dock], [min_w], Qt.Horizontal)

    def _show_singleton_dialog(self, attribute, factory):
        dialog = getattr(self, attribute, None)
        if dialog is None:
            dialog = factory()
            setattr(self, attribute, dialog)
            dialog.setAttribute(Qt.WA_DeleteOnClose, True)
            dialog.destroyed.connect(lambda *_: setattr(self, attribute, None))
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        return dialog

    def open_pak_browser(self):
        from ui.pak_browser_dialog import PakBrowserDialog

        self._show_singleton_dialog("_pak_browser", lambda: PakBrowserDialog(self))

    def open_file_list_generator(self):
        self._show_singleton_dialog(
            "_file_list_generator_dialog", lambda: FileListGeneratorDialog(self)
        )

    def open_rsz_csv_extractor(self):
        from ui.rsz_csv_extractor_dialog import RszCsvExtractorDialog

        dialog = RszCsvExtractorDialog(self, self.settings)
        dialog.exec()

    def _theme_accent_color(self) -> QColor:
        default_color = DEFAULT_SETTINGS["tree_highlight_color"]
        color_value = self.settings.get("tree_highlight_color", default_color)
        color = QColor(color_value)
        if not color.isValid():
            color = QColor(default_color)
        return color

    def _build_theme_colors(self) -> dict:
        return get_color_scheme(self._theme_accent_color().name())

    def _apply_style(self, colors):
        self.setStyleSheet(get_main_stylesheet(colors))
        self.home_widget.set_theme(colors, self._theme_accent_color().name())

    def toggle_debug_console(self, show: bool):
        if hasattr(self, "console_widget"):
            self.console_widget.setVisible(show)

            if show:
                if isinstance(sys.stdout, ConsoleRedirector):
                    return
                sys.stdout = ConsoleRedirector(self.console_widget, sys.stdout)
                sys.stderr = ConsoleRedirector(self.console_widget, sys.stderr)
                print("Debug console started.")
            else:
                if hasattr(sys.stdout, "original_stream"):
                    sys.stdout = sys.stdout.original_stream
                if hasattr(sys.stderr, "original_stream"):
                    sys.stderr = sys.stderr.original_stream

            self.settings["show_debug_console"] = show
            self.save_settings()

    def save_settings(self):
        save_settings(self.settings)

    def set_rsz_json_path(self, json_path: str, *, save: bool = True) -> None:
        if json_path != self.settings.get("rcol_json_path", ""):
            self.settings["enum_prompt_checked_json_path"] = ""
        self.settings["rcol_json_path"] = json_path
        if save:
            self.save_settings()

    def closeEvent(self, event):
        if hasattr(self, '_shared_find_dialog') and self._shared_find_dialog:
            try:
                self._shared_find_dialog.close()
            except RuntimeError:
                pass
        if not self._confirm_tabs_close(list(self.tabs.values())):
            event.ignore()
            return
        for tab in list(self.tabs.values()):
            try:
                tab.cleanup()
            except Exception as exc:
                print(f"Warning: Error cleaning up tab during shutdown: {exc}")
        event.accept()

    def update_from_app_settings(self):
        """Update handler settings from the application settings"""
        active_tabs = set(self.project_workspace.sessions.active_tabs())
        for tab in self.tabs.values():
            if hasattr(tab, 'handler') and is_handler_type(tab.handler, "RszHandler"):
                tab.handler.set_advanced_mode(self.settings.get("show_rsz_advanced", True))
                tab.handler.set_confirmation_prompts(self.settings.get("confirmation_prompt", True))
                if tab in active_tabs:
                    tab.handler.set_game_version(self.settings.get("game_version", "RE4"))

    def open_settings_dialog(self):
        SettingsDialog(self).exec()

    def apply_keyboard_shortcuts(self):
        shortcuts = self.settings.get("keyboard_shortcuts", {})
        for action in self.findChildren(QAction):
            action_name = action.objectName()
            if action_name in shortcuts and (shortcut_text := shortcuts[action_name]):
                try:
                    action.setShortcut(QKeySequence(shortcut_text))
                    print(f"Applied shortcut: {action_name} -> {shortcut_text}")
                except Exception as e:
                    print(f"Error setting shortcut for {action_name}: {e}")
        self._update_general_shortcut_state()

        if hasattr(self, "menuBar"):
            menubar = self.menuBar()
            if menubar:
                menubar.update()

    def open_guid_converter(self):
        self._show_singleton_dialog(
            "_guid_converter_dialog", lambda: create_guid_converter_dialog(self)
        )

    def open_hash_calculator(self):
        self._show_singleton_dialog("hash_calculator", HashCalculator)

    def open_outdated_files_detector(self):
        registry_path = self.settings.get("rcol_json_path", None)
        self._show_singleton_dialog(
            "_outdated_files_dialog", lambda: OutdatedFilesDialog(self, registry_path)
        )

    def open_rsz_differ(self):
        game_version = self.game_dropdown.currentText() if hasattr(self, 'game_dropdown') else "RE4"
        json_path = self.settings.get("rcol_json_path", None)
        self._show_singleton_dialog(
            "_rsz_differ_dialog", lambda: RszDifferDialog(self, game_version, json_path)
        )

    def search_directory_for_number(self):
        search_directory_for_type(self, 'number')

    def search_directory_for_text(self):
        search_directory_for_type(self, 'text')

    def search_directory_for_guid(self):
        search_directory_for_type(self, 'guid')

    def search_directory_for_hex(self):
        search_directory_for_type(self, 'hex')

    def open_rsz_field_value_finder(self):
        """Open the RSZ field value finder window."""
        from ui.rsz_field_value_finder_dialog import RszFieldValueFinderDialog

        self._show_singleton_dialog(
            "_rsz_field_value_finder_dialog",
            lambda: RszFieldValueFinderDialog(self, self.settings),
        )

    def open_find_dialog(self):
        active = self.get_active_tab()
        if not active:
            QMessageBox.critical(self, self.tr("Error"), self.tr("No active tab for searching."))
            return

        if is_handler_type(active.handler, "MsgHandler"):
            QMessageBox.information(self, self.tr("Search in MSG"), self.tr("MSG files have a built-in search at the top of the editor. Please use that search bar."))
            return

        for window in self.notebook._floating_windows:
            if window.page == active.notebook_widget:
                active.open_find_dialog()
                return
        if not self._shared_find_dialog or not isinstance(self._shared_find_dialog, BetterFindDialog):
            self._shared_find_dialog = BetterFindDialog(file_tab=active, parent=self, shared_mode=True)
            self.notebook.currentChanged.connect(self._on_tab_changed_for_find)
        else:
            self._shared_find_dialog.set_file_tab(active)

        self._shared_find_dialog.show()
        if not self._shared_find_dialog.isFloating():
            self._shared_find_dialog.raise_()
            self._shared_find_dialog.activateWindow()

    def _on_tab_changed_for_find(self):
        self._update_highlight_menu_visibility()

        if hasattr(self, '_shared_find_dialog') and self._shared_find_dialog and self._shared_find_dialog.isVisible():
            active = self.get_active_tab()
            if active:
                is_detached = False
                for window in self.notebook._floating_windows:
                    if window.page == active.notebook_widget:
                        is_detached = True
                        break

                if not is_detached:
                    self._shared_find_dialog.set_file_tab(active)

    def _check_and_close_shared_find_dialog(self):
        """Close the shared find dialog if no tabs are left in the main window"""
        has_main_tabs = False
        for i in range(self.notebook.count()):
            widget = self.notebook.widget(i)
            if widget:
                is_detached = False
                for window in self.notebook._floating_windows:
                    if window.page == widget:
                        is_detached = True
                        break
                if not is_detached:
                    has_main_tabs = True
                    break

        if not has_main_tabs and hasattr(self, '_shared_find_dialog') and self._shared_find_dialog:
            try:
                if self._shared_find_dialog.isVisible():
                    self._shared_find_dialog.close()
            except RuntimeError:
                pass

    def add_tab(self, filename=None, data=None, pak_source_path=None, pak_project_dir=None):
        if self.scenes.route_owned_open(filename, pak_source_path, pak_project_dir):
            return None
        if filename:
            abs_fn = os.path.abspath(filename)
            for tab in self.project_workspace.sessions.active_tabs():
                if tab.filename and os.path.abspath(tab.filename) == abs_fn:
                    if tab.modified:
                        ans = QMessageBox.question(
                            self,
                            FileTab.tr(UNSAVED_CHANGES_STR),
                            self.tr(
                                "The file {filename} has unsaved changes.\n"
                                "Save before reopening?"
                            ).format(filename=os.path.basename(filename)),
                            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                        )
                        if ans == QMessageBox.Cancel:
                            return
                        elif ans == QMessageBox.Yes:
                            tab.on_save()
                        else:
                            tab.modified = False
                            tab.update_tab_title()
                    index = self.notebook.indexOf(tab.notebook_widget)
                    if index != -1:
                        self.notebook.setCurrentIndex(index)
                    else:
                        for window in self.project_workspace.sessions.windows_for([tab]):
                            window.show()
                            window.raise_()
                            window.activateWindow()
                    return tab

        tab = None
        try:
            handler = get_handler_for_data(data, filename)
            if hasattr(handler, 'needs_json_path') and handler.needs_json_path():
                if not self.settings.get("rcol_json_path"):
                    msg = QMessageBox(QMessageBox.Warning,
                        self.tr("JSON Path Not Set"),
                        self.tr("RSZ type registry JSON path is not set.\nWould you like to set it now?"),
                        QMessageBox.Yes | QMessageBox.No)
                    if msg.exec() == QMessageBox.Yes:
                        self.open_settings_dialog()
                    return None

            tab = FileTab(
                None,
                filename,
                data,
                app=self,
                pak_source_path=pak_source_path,
                pak_project_dir=self._source_project_dir(filename, pak_project_dir),
                handler=handler,
            )
            if data is not None and not tab.initial_load_complete:
                if tab.notebook_widget:
                    tab.notebook_widget.deleteLater()
                return None
            if is_handler_type(getattr(tab, "handler", None), "RszHandler"):
                RszEnumPromptController.maybe_prompt_for_loaded_rsz(self)
            tab.parent_notebook = self.notebook
            tab_label = os.path.basename(filename) if filename else self.tr("Untitled")
            _ = self.notebook.addTab(tab.notebook_widget, tab_label)
            self.tabs[tab.notebook_widget] = tab
            self.project_workspace.sessions.add_tab(tab)
            self.notebook.setCurrentWidget(tab.notebook_widget)
            self._update_highlight_menu_visibility()
            self.scenes.refresh_actions()
            self.scenes.refresh_buttons()
            self._refresh_homepage()
            return tab

        except Exception as e:
            QMessageBox.critical(
                self,
                self.tr("Error"),
                self.tr("Failed to open file: {error}").format(error=e),
            )
            if tab and hasattr(tab, 'notebook_widget') and tab.notebook_widget:
                try:
                    tab.notebook_widget.deleteLater()
                except Exception as e:
                    print(f"Error closing tab: {e}")
            return None

    def _source_project_dir(self, filename: str | None, explicit: str | None = None) -> str | None:
        if explicit or not filename:
            return explicit
        root = getattr(self.project_workspace.sessions.get(self.project_workspace.sessions.active_key), "path", None)
        try:
            return root if root and os.path.commonpath([os.path.abspath(filename), os.path.abspath(root)]) == os.path.abspath(root) else None
        except ValueError:
            return None

    def attach_pak_source_tab(self, tab, pak_path: str, project_dir: str | None = None) -> None:
        if tab is None or not pak_path:
            return
        tab.pak_source_path = pak_path
        project_dir = project_dir or getattr(self.proj_dock, "project_dir", None)
        tab.pak_project_dir = project_dir
        tab.pak_data_loader = lambda source_path: self.proj_dock.read_project_pak_file(
            project_dir,
            source_path,
        )
        self.scenes.attach_tab_document(tab)
        self.scenes.refresh_actions()
        self.scenes.refresh_buttons()

    def get_active_tab(self):
        active_tabs = self.project_workspace.sessions.active_tabs()
        aw = QApplication.activeWindow()
        widgets = [self.notebook.currentWidget(), QApplication.focusWidget()]
        if isinstance(aw, FloatingTabWindow):
            widgets.insert(0, aw.centralWidget())
        for widget in widgets:
            tab = self._resolve_tab_from_widget(widget)
            if tab in active_tabs or getattr(tab, "suppress_general_shortcuts", False):
                return tab
        return None

    def get_active_tree(self):
        active_tab = self.get_active_tab()
        if not active_tab:
            return None

        if active_tab.viewer and hasattr(active_tab.viewer, "tree"):
            return active_tab.viewer.tree
        return active_tab.tree

    def on_open(self):
        fn, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open File"),
            "",
            "RE Files (*.uvar* *.scn* *.user* *.pfb* *.msg* *.efx* *.cfil* *.motbank* *.mcambank* *.tex* *.mesh* *.mdf2* *.sbnk* *.spck* *.wel*);;SCN Files (*.scn*);;User Files (*.user*);;UVAR Files (*.uvar*);;PFB Files (*.pfb*);;MSG Files (*.msg*);;EFX Files (*.efx*);;CFIL Files (*.cfil*);;MOTBANK Files (*.motbank*);;MCAMBANK Files (*.mcambank*);;Texture Files (*.tex*);;DDS Files (*.dds*);;Mesh Files (*.mesh*);;Material Definition Files (*.mdf2*);;Sound Files (*.sbnk* *.spck*);;Wwise Event List (*.wel*);;All Files (*.*)"
        )
        if not fn:
            return
        try:
            with open(fn, "rb") as f:
                data = f.read()

            self.add_tab(fn, data)

        except Exception as e:
            QMessageBox.critical(self, self.tr("Error"), str(e))

    def on_direct_save(self):
        active = self.get_active_tab()
        if active:
            active.direct_save()
        else:
            QMessageBox.critical(self, self.tr("Error"), self.tr("No active tab to save."))

    def on_save(self):
        active = self.get_active_tab()
        if active:
            active.on_save()
        else:
            QMessageBox.critical(self, self.tr("Error"), self.tr("No active tab to save."))

    def reload_file(self):
        active = self.get_active_tab()
        if active:
            active.reload_file()
        else:
            QMessageBox.critical(self, self.tr("Error"), self.tr("No active tab to reload."))

    def close_current_tab(self):
        tab = self.get_active_tab()
        if not tab:
            return
        index = self.notebook.indexOf(tab.notebook_widget)
        if index == -1:
            windows = self.project_workspace.sessions.windows_for([tab])
            if windows:
                windows[0].close()
                index = self.notebook.indexOf(tab.notebook_widget)
        if index != -1:
            self.close_tab(index)

    def _resolve_tab_from_widget(self, widget):
        w = widget
        while w is not None:
            if w in self.tabs:
                return self.tabs.get(w)
            ft = getattr(w, "_reasy_file_tab", None)
            if ft is not None:
                return ft
            if hasattr(w, 'parentWidget'):
                w = w.parentWidget()
            else:
                break
        return None

    def _save_closed_file_history(self):
        self._closed_file_history = [f for f in self._closed_file_history if isinstance(f, str) and f][-RECENTLY_CLOSED_FILES_LIMIT:]
        self.settings["recently_closed_files"] = list(self._closed_file_history)
        save_settings(self.settings)

    def _record_closed_file(self, filename):
        if not filename:
            return
        if filename in self._closed_file_history:
            self._closed_file_history.remove(filename)
        self._closed_file_history.append(filename)
        self._save_closed_file_history()
        self._refresh_homepage()

    def _clear_recently_closed_files(self):
        self._closed_file_history.clear()
        self._save_closed_file_history()
        self._refresh_homepage()

    def reopen_closed_file(self, filename=None, notify_if_empty=False):
        if filename is not None:
            candidates = [filename]
        else:
            candidates = list(reversed(self._closed_file_history))

        attempted = False
        for target in candidates:
            attempted = True
            project_dir, is_pak_entry, decoded_target = ProjectManager.decode_history_entry(target)

            if project_dir and not self.project_workspace.is_active(project_dir):
                if not os.path.isdir(project_dir):
                    success = False
                else:
                    self.project_workspace.activate(
                        project_dir,
                        on_loaded=lambda target=target: self.reopen_closed_file(target),
                    )
                    return
            else:
                success = (
                    self.proj_dock.reopen_pak_history_entry(decoded_target)
                    if is_pak_entry else self._open_path(decoded_target)
                )
                if not success and project_dir and not is_pak_entry and not os.path.isabs(decoded_target):
                    project_target = os.path.join(project_dir, *decoded_target.replace("\\", "/").split("/"))
                    success = self._open_path(project_target)

            if success:
                self._closed_file_history.remove(target)
                self._save_closed_file_history()
                self._refresh_homepage()
                return

            prompt = QMessageBox(self)
            prompt.setIcon(QMessageBox.Critical)
            prompt.setWindowTitle(self.tr("Reopen Closed File"))
            prompt.setText(self.tr(
                "Failed to reopen {filename}. Remove it from recently closed files?"
            ).format(filename=os.path.basename(decoded_target)))
            prompt.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            if prompt.exec_() == QMessageBox.Yes:
                self._closed_file_history.remove(target)
                self._save_closed_file_history()
                self._refresh_homepage()
            break

        if not attempted and notify_if_empty and filename is None and not self._closed_file_history:
            QMessageBox.information(
                self,
                self.tr("Reopen Closed File"),
                self.tr("No recently closed files to reopen."),
            )

    def _populate_recently_closed_menu(self):
        if not self.recently_closed_menu:
            return

        self.recently_closed_menu.clear()
        if not self._closed_file_history:
            empty_action = self.recently_closed_menu.addAction(self.tr("No recently closed files"))
            empty_action.setEnabled(False)
            return

        for filename in reversed(self._closed_file_history):
            _, _, display_path = ProjectManager.decode_history_entry(filename)
            action = self.recently_closed_menu.addAction(os.path.basename(display_path))
            action.setToolTip(display_path)
            action.triggered.connect(lambda _checked=False, fn=filename: self.reopen_closed_file(fn))

        self.recently_closed_menu.addSeparator()
        self.recently_closed_menu.addAction(self.tr("Clear Recently Closed Files"), self._clear_recently_closed_files)

    def reopen_last_closed_file(self):
        self.reopen_closed_file(notify_if_empty=True)

    def _close_tab_object(self, tab, *, record_history=True):
        widget = tab.notebook_widget
        leave_fullscreen = getattr(tab, "leave_view_fullscreen", None)
        if callable(leave_fullscreen):
            try:
                leave_fullscreen(defer_update=False)
            except TypeError:
                leave_fullscreen()
            except RuntimeError:
                pass
        for window in self.project_workspace.sessions.windows_for([tab]):
            try:
                window.close_without_reattach()
            except RuntimeError:
                pass

        if record_history and tab.filename:
            session = self.project_workspace.sessions.session_for_tab(tab)
            self._record_closed_file(ProjectManager.encode_history_entry(
                tab.pak_source_path or tab.filename,
                session.path if session else None,
                is_pak=bool(tab.pak_source_path),
            ))

        self.tabs.pop(widget, None)
        if (index := self.notebook.indexOf(widget)) != -1:
            self.notebook.removeTab(index)
        self.project_workspace.sessions.remove_tab(tab)
        tab.cleanup()
        self._check_and_close_shared_find_dialog()
        self.scenes.refresh_actions()
        self.scenes.refresh_buttons()
        self._refresh_homepage()

    def close_tab(self, index):
        widget = self.notebook.widget(index)
        tab = self.tabs.get(widget)
        if tab and self._confirm_tabs_close([tab]):
            self._close_tab_object(tab)

    def show_about(self):
        dialog = AboutDialog(self)
        dialog.exec()

    def show_wiki(self):
        QDesktopServices.openUrl(QUrl("https://github.com/seifhassine/REasy-Wiki"))

    def show_donate_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr("Support REasy Editor"))
        layout = QVBoxLayout(dialog)

        thank_you_label = QLabel(self.tr("Thank you for your feedback and support!\nYour contributions help keep this project going."))
        thank_you_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(thank_you_label)

        link_label = QLabel('<a href="https://linktr.ee/seifhassine">https://linktr.ee/seifhassine</a>')
        link_label.setAlignment(Qt.AlignCenter)
        link_label.setOpenExternalLinks(True)
        layout.addWidget(link_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok)
        buttons.accepted.connect(dialog.accept)
        layout.addWidget(buttons)

        dialog.setMinimumWidth(300)
        dialog.exec()

    def on_restore_backup(self):
        """Show dialog with available backups for the current file"""
        active = self.get_active_tab()
        if not active:
            QMessageBox.critical(self, self.tr("Error"), self.tr("No active tab to restore the backup of."))
            return

        if not active.filename:
            QMessageBox.critical(self, self.tr("Error"), self.tr("File has not been saved yet."))
            return

        backups = active.find_matching_backups()
        if not backups:
            QMessageBox.information(self, self.tr("No Backups"), self.tr("No backup files found for this file."))
            return

        dialog = QDialog(self)
        dialog.setWindowTitle(self.tr("Available Backups for {filename}").format(
            filename=os.path.basename(active.filename)
        ))
        dialog.setMinimumWidth(400)

        layout = QVBoxLayout(dialog)

        backup_list = QListWidget()
        for friendly_time, path, filename in backups:
            item = QListWidgetItem(f"{friendly_time}")
            item.setData(Qt.UserRole, path)
            item.setToolTip(filename)
            backup_list.addItem(item)

        layout.addWidget(QLabel(self.tr("Select a backup to restore:")))
        layout.addWidget(backup_list)

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        layout.addWidget(button_box)

        button_box.accepted.connect(dialog.accept)
        button_box.rejected.connect(dialog.reject)

        if dialog.exec() == QDialog.Accepted:
            selected = backup_list.currentItem()
            if not selected:
                QMessageBox.critical(self, self.tr("Error"), self.tr("No backup selected."))
                return

            backup_path = selected.data(Qt.UserRole)
            friendly_time = selected.text()

            confirm_msg = self.tr(
                "Are you sure you want to restore the backup from:\n"
                "{time}?\n\nCurrent changes will be lost."
            ).format(time=friendly_time)
            confirm = QMessageBox.question(
                self,
                self.tr("Confirm Restore"),
                confirm_msg,
                QMessageBox.Yes | QMessageBox.No
            )

            if confirm == QMessageBox.Yes:
                success = active.restore_backup(backup_path)
                if success:
                    QMessageBox.information(self, self.tr("Success"), self.tr("Backup restored successfully"))

    def goto_previous_tab(self):
        current_index = self.notebook.currentIndex()
        if current_index > 0:
            self.notebook.setCurrentIndex(current_index - 1)

    def goto_next_tab(self):
        current_index = self.notebook.currentIndex()
        if current_index < self.notebook.count() - 1:
            self.notebook.setCurrentIndex(current_index + 1)
