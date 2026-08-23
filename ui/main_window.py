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
    QByteArray,
    Qt,
    QTimer,
    QUrl,
    QSize,
)
from PySide6.QtGui import (
    QIcon,
    QAction,
    QKeySequence,
    QDesktopServices,
    QColor,
    QPainter,
    QPen,
    QPixmap,
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
    QToolButton,
    QDockWidget,
)

from ui.console_logger import ConsoleWidget, ConsoleRedirector
from ui.ai.chat_dock import AiChatDock
from ui.breadcrumbs import BreadcrumbBar
from ui.detachable_tabs import CustomNotebook, FloatingTabWindow
from ui.editor_groups import EditorGroupHost
from ui.directory_search import search_directory_for_type
from ui.highlight_menu_controller import HighlightMenuController
from ui.homepage import HomePageStack, HomePageWidget
from ui.scene.opengl_setup import create_surface_anchor
from tools.hash_calculator import HashCalculator

from ui.project_manager.project_picker_dialog import ProjectPickerDialog  # noqa: E402
from ui.project_manager.source_dialog import SelectSourceDialog  # noqa: E402
from ui.project_manager.project_sessions import save_modified_tabs  # noqa: E402
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
        self._set_app_icon_callback = set_app_icon
        self.notebook = CustomNotebook()
        self.notebook.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.notebook.setMinimumSize(50, 50)
        self.notebook.app_instance = self
        self.notebook._set_icon_callback = set_app_icon
        self.editor_groups = EditorGroupHost(self.notebook, self, central_widget)
        self.breadcrumbs = BreadcrumbBar(self, central_widget)
        editor_shell = QWidget(central_widget)
        editor_shell.setObjectName("editorShell")
        editor_layout = QVBoxLayout(editor_shell)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.setSpacing(0)
        editor_layout.addWidget(self.breadcrumbs)
        editor_layout.addWidget(self.editor_groups, 1)
        self.tabs = weakref.WeakValueDictionary()
        self.home_stack = HomePageStack(editor_shell, self.home_widget)
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

        self.editor_groups.activePageChanged.connect(self._on_active_page_changed)
        self.editor_groups.layoutChanged.connect(self._refresh_homepage)
        self.proj_dock.visibilityChanged.connect(lambda _visible: self._refresh_homepage())

        self.status_bar = QStatusBar()
        self.status_bar.setContentsMargins(0, 0, 0, 0)
        self.status_bar.setMaximumHeight(20)
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage(self.tr("Ready"))

        self._apply_style(self._build_theme_colors())

        self.console_widget = ConsoleWidget()
        self.output_dock = QDockWidget(self.tr("Output"), self)
        self.output_dock.setObjectName("outputDock")
        self.output_dock.setAllowedAreas(Qt.BottomDockWidgetArea | Qt.TopDockWidgetArea)
        self.output_dock.setWidget(self.console_widget)
        self.addDockWidget(Qt.BottomDockWidgetArea, self.output_dock)
        self.output_dock.setVisible(self.settings.get("show_debug_console", True))
        self.output_dock.visibilityChanged.connect(self.output_action.setChecked)
        self.output_dock.visibilityChanged.connect(
            lambda visible: self.settings.__setitem__("show_debug_console", bool(visible))
        )

        self.project_workspace = ProjectWorkspaceController(
            self, self.notebook, self.tabs, self.editor_groups
        )
        self.ai_chat_dock = AiChatDock(self)
        self.addDockWidget(Qt.RightDockWidgetArea, self.ai_chat_dock)
        self._ai_chat_visibility_tracking = False
        ai_chat_visible = bool(self.settings.get("show_ai_chat", False))
        ai_chat_action = QAction(self.tr("AI Assistant"), self)
        self.ai_chat_action = ai_chat_action
        ai_chat_action.setText(self.tr("AI Assistant"))
        ai_chat_action.setObjectName("view_ai_chat")
        ai_chat_action.setCheckable(True)
        ai_chat_action.setChecked(ai_chat_visible)
        ai_chat_action.setShortcut(
            QKeySequence(
                self.settings.get("keyboard_shortcuts", {}).get(
                    "view_ai_chat",
                    DEFAULT_SETTINGS["keyboard_shortcuts"]["view_ai_chat"],
                )
            )
        )
        self.view_menu.addSeparator()
        self.view_menu.addAction(ai_chat_action)
        self.ai_chat_dock.setVisible(ai_chat_visible)
        ai_chat_action.triggered.connect(
            self._on_ai_chat_action_triggered
        )

        self.ai_chat_button = QToolButton(self.menuBar())
        self.ai_chat_button.setObjectName("aiAssistantMenuButton")
        self.ai_chat_button.setText(self.tr("AI"))
        self.ai_chat_button.setAccessibleName(self.tr("AI Assistant"))
        self.ai_chat_button.setToolTip(
            self.tr("Show or hide AI Assistant")
        )
        self.ai_chat_button.setCheckable(True)
        self.ai_chat_button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.ai_chat_button.setIconSize(QSize(12, 12))
        self.ai_chat_button.setFixedHeight(18)
        self.ai_chat_button.setChecked(ai_chat_visible)
        self.ai_chat_button.clicked.connect(
            lambda _checked=False: ai_chat_action.trigger()
        )
        self.ai_chat_dock.visibilityChanged.connect(
            self._sync_ai_chat_controls
        )
        self.menuBar().setCornerWidget(
            self.ai_chat_button,
            Qt.TopRightCorner,
        )
        self._apply_ai_menu_button_style()
        self._ai_chat_visibility_tracking = True
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(
                self._stop_ai_chat_visibility_tracking
            )

        if self.settings.get("show_debug_console", True):
            sys.stdout = ConsoleRedirector(self.console_widget, sys.stdout)
            sys.stderr = ConsoleRedirector(self.console_widget, sys.stderr)
            print("Debug console started.")

        self.resize(1160, 920)

        self._apply_style(self._build_theme_colors())
        self._restore_window_layout()
        QTimer.singleShot(120, self._restore_workbench_session)

        self.setAcceptDrops(True)

        last_seen = self.settings.get("last_seen_version", "")
        if last_seen != CURRENT_VERSION:
            QTimer.singleShot(600, self._show_changelog_if_needed)
        self._refresh_homepage()

    def _refresh_homepage(self):
        show_notebook = self.editor_groups.count() > 0 or self.proj_dock.isVisible()
        recent_label = self.tr("No recently closed files yet.")
        if self._closed_file_history:
            _, _, decoded_target = ProjectManager.decode_history_entry(self._closed_file_history[-1])
            recent_label = self.tr("Last closed: {filename}").format(
                filename=os.path.basename(decoded_target)
            )
        self.home_stack.refresh(show_notebook, recent_label, bool(self._closed_file_history))

    def _on_active_page_changed(self, page=None):
        self._update_highlight_menu_visibility()
        self.scenes.refresh_actions()
        self.scenes.refresh_buttons()
        self._refresh_homepage()
        tab = self.tabs.get(page) if page is not None else self.get_active_tab()
        self.breadcrumbs.bind_tab(tab)
        self._on_tab_changed_for_find()

    def _notebooks(self):
        return self.editor_groups.notebooks()

    def _floating_windows(self):
        return self.editor_groups.all_floating_windows()

    def document_title_for_tab(self, tab) -> str:
        target = str(getattr(tab, "pak_source_path", "") or getattr(tab, "filename", "") or "")
        base = os.path.basename(target.replace("\\", "/")) if target else self.tr("Untitled")
        matches = []
        for candidate in list(self.tabs.values()):
            candidate_target = str(
                getattr(candidate, "pak_source_path", "")
                or getattr(candidate, "filename", "")
                or ""
            )
            candidate_base = os.path.basename(candidate_target.replace("\\", "/")) if candidate_target else self.tr("Untitled")
            if candidate_base.casefold() == base.casefold():
                matches.append((candidate, candidate_target))
        if len(matches) > 1 and target:
            normalized = target.replace("\\", "/").rstrip("/")
            parent_parts = [part for part in normalized.split("/")[:-1] if part]
            if parent_parts:
                suffix_length = 1
                while suffix_length < len(parent_parts):
                    suffix = "/".join(parent_parts[-suffix_length:]).casefold()
                    collisions = 0
                    for _candidate, other in matches:
                        other_parts = [part for part in other.replace("\\", "/").rstrip("/").split("/")[:-1] if part]
                        if "/".join(other_parts[-suffix_length:]).casefold() == suffix:
                            collisions += 1
                    if collisions == 1:
                        break
                    suffix_length += 1
                base = self.tr("{name} — {parent}").format(
                    name=base, parent="/".join(parent_parts[-suffix_length:])
                )
        return f"{base} *" if bool(getattr(tab, "modified", False)) else base

    def _refresh_document_titles(self):
        for tab in list(self.tabs.values()):
            try:
                tab.update_tab_title()
            except RuntimeError:
                pass

    def reveal_tab_in_project(self, tab) -> bool:
        if tab is None:
            return False
        if pak_path := getattr(tab, "pak_source_path", None):
            folder = pak_path.rsplit("/", 1)[0] + "/" if "/" in pak_path else ""
            return self.proj_dock._reveal_pak_folder(folder)
        filename = getattr(tab, "filename", None)
        if not filename:
            return False
        folder = os.path.dirname(os.path.abspath(filename))
        project = getattr(self.project_workspace.sessions.session_for_tab(tab), "path", None)
        try:
            in_project = bool(
                project and os.path.commonpath([folder, project]) == os.path.abspath(project)
            )
        except ValueError:
            in_project = False
        scope = "project" if in_project else "unpacked"
        return self.proj_dock._reveal_filesystem_folder(folder, scope)

    def _internal_drag(self, event):
        return event.mimeData().hasFormat("application/x-qabstractitemmodeldatalist")

    def _show_changelog_if_needed(self):
        last_seen = self.settings.get("last_seen_version", "")
        if last_seen != CURRENT_VERSION:
            self.show_changelog()

    def show_changelog(self):
        dialog = ChangelogDialog(self, CURRENT_VERSION)
        dialog.exec()
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

        def create_action(text, callback, shortcut_name=None):
            action = QAction(text, self)
            if shortcut_name:
                action.setObjectName(shortcut_name)
                action.setShortcut(shortcut(shortcut_name))
            action.triggered.connect(callback)
            return action

        def add_action(menu, text, callback, shortcut_name=None):
            action = create_action(text, callback, shortcut_name)
            menu.addAction(action)
            return action

        file_menu = menubar.addMenu(self.tr("File"))

        open_act = create_action(self.tr("Open File..."), self.on_open, "file_open")

        new_proj_act = create_action(self.tr("New Project (Create Mod)..."), self.new_project)
        open_proj_act = create_action(self.tr("Project Library..."), self.open_project)
        close_proj_act = create_action(self.tr("Close Project"), self.close_project)
        file_menu.insertSeparator(open_act)
        file_menu.insertAction(open_act, new_proj_act)
        file_menu.insertAction(open_act, open_proj_act)
        file_menu.insertAction(open_act, close_proj_act)

        file_menu.addSeparator()

        file_menu.addAction(open_act)

        add_action(file_menu, self.tr("Save"), self.on_direct_save, "file_save")
        add_action(
            file_menu,
            self.tr("Save All Modified Files"),
            self.on_save_all,
            "file_save_all",
        )
        add_action(file_menu, self.tr("Save As..."), self.on_save, "file_save_as")
        add_action(file_menu, self.tr("Restore Backup..."), self.on_restore_backup)
        add_action(file_menu, self.tr("Reload"), self.reload_file, "file_reload")
        add_action(file_menu, self.tr("Close Tab"), self.close_current_tab, "file_close_tab")
        add_action(
            file_menu,
            self.tr("Reopen Last Closed File"),
            self.reopen_last_closed_file,
            "file_reopen_closed",
        )

        self.recently_closed_menu = file_menu.addMenu(self.tr("Recently Closed Files"))
        self.recently_closed_menu.aboutToShow.connect(self._populate_recently_closed_menu)

        file_menu.addSeparator()

        add_action(file_menu, self.tr("Settings"), self.open_settings_dialog)
        add_action(file_menu, self.tr("Exit"), self.close)

        find_menu = menubar.addMenu(self.tr("Find"))

        add_action(find_menu, self.tr("Find"), self.open_find_dialog, "find_search")
        add_action(
            find_menu,
            self.tr("Search Directory for GUID"),
            self.search_directory_for_guid,
            "find_search_guid",
        )
        add_action(
            find_menu,
            self.tr("Search Directory for Text"),
            self.search_directory_for_text,
            "find_search_text",
        )
        add_action(
            find_menu,
            self.tr("Search Directory for Number"),
            self.search_directory_for_number,
            "find_search_number",
        )
        add_action(
            find_menu,
            self.tr("Search Directory for Hex"),
            self.search_directory_for_hex,
            "find_search_hex",
        )
        add_action(
            find_menu,
            self.tr("Find/Replace RSZ Field Value"),
            self.open_rsz_field_value_finder,
            "find_rsz_field_value",
        )

        view_menu = menubar.addMenu(self.tr("View"))
        self.view_menu = view_menu

        add_action(
            view_menu, self.tr("Previous Tab"), self.goto_previous_tab, "view_prev_tab"
        )
        add_action(view_menu, self.tr("Next Tab"), self.goto_next_tab, "view_next_tab")
        self.output_action = add_action(
            view_menu,
            self.tr("Toggle Output"),
            lambda: self.toggle_debug_console(
                not self.settings.get("show_debug_console", True)
            ),
            "view_debug_console",
        )
        self.output_action.setCheckable(True)
        self.output_action.setChecked(bool(self.settings.get("show_debug_console", True)))
        view_menu.addSeparator()
        add_action(
            view_menu,
            self.tr("Split Editor Right"),
            lambda: self.split_active_editor(Qt.Horizontal),
            "editor_split_right",
        )
        add_action(
            view_menu,
            self.tr("Split Editor Down"),
            lambda: self.split_active_editor(Qt.Vertical),
            "editor_split_down",
        )

        self.scene_menu = menubar.addMenu(self.tr("Scene"))
        self.scene_menu.aboutToShow.connect(lambda: self.scenes.populate_scene_menu(self.scene_menu))

        tools_menu = menubar.addMenu(self.tr("Tools"))
        add_action(tools_menu, self.tr("GUID Converter"), self.open_guid_converter)
        add_action(tools_menu, self.tr("Hash Calculator"), self.open_hash_calculator)
        add_action(
            tools_menu,
            self.tr("Outdated Files Detector"),
            self.open_outdated_files_detector,
        )
        add_action(tools_menu, self.tr("RSZ Diff Viewer"), self.open_rsz_differ)
        add_action(tools_menu, self.tr("PAK Browser"), self.open_pak_browser)
        add_action(tools_menu, self.tr("File List Generator"), self.open_file_list_generator)

        tools_menu.addSeparator()

        add_action(
            tools_menu,
            self.tr("CSV Extractor (RSZ Data Matcher)"),
            self.open_rsz_csv_extractor,
        )

        help_menu = menubar.addMenu(self.tr("Help"))
        add_action(help_menu, self.tr("About"), self.show_about)
        add_action(help_menu, self.tr("What's new?"), self.show_changelog)
        add_action(help_menu, self.tr("REasy Wiki"), self.show_wiki)

        donate_menu = menubar.addMenu(self.tr("Donate"))
        add_action(donate_menu, self.tr("Support REasy"), self.show_donate_dialog)

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
        self._apply_ai_menu_button_style()
        if hasattr(self, "ai_chat_dock"):
            self.ai_chat_dock.apply_theme()
        if hasattr(self, "project_workspace"):
            self.project_workspace.apply_style()

    def _apply_ai_menu_button_style(self):
        if not hasattr(self, "ai_chat_button"):
            return
        accent = self._theme_accent_color().name()
        self.ai_chat_button.setIcon(self._make_ai_bot_icon(accent))
        self.ai_chat_button.setStyleSheet(
            f"""
            QToolButton#aiAssistantMenuButton {{
                background: transparent;
                color: #c4c4c4;
                border: 1px solid transparent;
                border-radius: 6px;
                padding: 0px 8px;
                margin: 0px 6px 0px 4px;
                min-height: 0px;
                font-weight: 600;
            }}
            QToolButton#aiAssistantMenuButton:hover,
            QToolButton#aiAssistantMenuButton:checked {{
                background: {accent};
                color: white;
                border-color: {accent};
            }}
            """
        )

    @staticmethod
    def _make_ai_bot_icon(accent: str) -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)

        outline = QPen(QColor("#c4cdd8"), 1.2)
        painter.setPen(outline)
        painter.setBrush(QColor("#1f2b38"))
        painter.drawRoundedRect(3, 5, 10, 8, 2, 2)
        painter.drawLine(8, 2, 8, 5)
        painter.drawEllipse(7, 1, 2, 2)
        painter.drawLine(2, 9, 3, 9)
        painter.drawLine(13, 9, 14, 9)

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(accent))
        painter.drawEllipse(5, 8, 2, 2)
        painter.drawEllipse(9, 8, 2, 2)
        painter.end()
        return QIcon(pixmap)

    def _on_ai_chat_action_triggered(self, visible: bool):
        visible = bool(visible)
        self.ai_chat_dock.setVisible(visible)
        self._sync_ai_chat_controls(visible)

    def _sync_ai_chat_controls(self, visible: bool):
        visible = bool(visible)
        if self.ai_chat_button.isChecked() != visible:
            self.ai_chat_button.setChecked(visible)
        if self.ai_chat_action.isChecked() != visible:
            self.ai_chat_action.setChecked(visible)
        if (
            self._ai_chat_visibility_tracking
            and self.settings.get("show_ai_chat", False) != visible
        ):
            self.settings["show_ai_chat"] = visible
            self.save_settings()

    def _stop_ai_chat_visibility_tracking(self):
        self._ai_chat_visibility_tracking = False

    def toggle_debug_console(self, show: bool):
        if hasattr(self, "console_widget"):
            if hasattr(self, "output_dock"):
                self.output_dock.setVisible(show)
                if show:
                    self.output_dock.raise_()
            else:
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

    def split_active_editor(self, orientation=Qt.Horizontal):
        page = self.editor_groups.active_page()
        if page is not None:
            self.editor_groups.split_page(page, orientation)

    def save_settings(self):
        save_settings(self.settings)

    @staticmethod
    def _encode_window_bytes(value: QByteArray) -> str:
        return bytes(value.toBase64()).decode("ascii")

    @staticmethod
    def _decode_window_bytes(value) -> QByteArray:
        if not isinstance(value, str) or not value:
            return QByteArray()
        try:
            return QByteArray.fromBase64(value.encode("ascii"))
        except (TypeError, ValueError):
            return QByteArray()

    def _save_workbench_state(self) -> None:
        workbench = self.settings.setdefault("workbench", {})
        workbench.update({
            "state_version": 1,
            "window_geometry": self._encode_window_bytes(self.saveGeometry()),
            "window_state": self._encode_window_bytes(self.saveState(1)),
            "project_browser": self.proj_dock.capture_view_state(),
            "session": self._capture_workbench_session(),
        })
        self.settings["show_debug_console"] = bool(self.output_dock.isVisible())
        self.settings["show_ai_chat"] = bool(self.ai_chat_dock.isVisible())
        self.save_settings()

    def _capture_workbench_session(self) -> dict:
        projects = [
            {"path": session.path, "game": session.game}
            for session in self.project_workspace.sessions.project_sessions()
            if session.path
        ]
        active_tab = self.get_active_tab()
        entries = []
        for session in [
            self.project_workspace.sessions.get(None),
            *self.project_workspace.sessions.project_sessions(),
        ]:
            if session is None:
                continue
            for order, tab in enumerate(session.tabs):
                if not isinstance(tab, FileTab):
                    continue
                target = getattr(tab, "pak_source_path", None) or getattr(tab, "filename", None)
                if not target:
                    continue
                notebook = self.project_workspace.sessions.notebook_for(tab.notebook_widget)
                if notebook is not None:
                    group = self.editor_groups.notebooks().index(notebook)
                    tab_order = notebook.indexOf(tab.notebook_widget)
                else:
                    parent_notebook = getattr(tab, "parent_notebook", None)
                    group = (
                        self.editor_groups.notebooks().index(parent_notebook)
                        if parent_notebook in self.editor_groups.notebooks() else 0
                    )
                    tab_order = order
                window = next(
                    (candidate for candidate in self._floating_windows() if candidate.file_tab is tab),
                    None,
                )
                entries.append({
                    "target": self._history_entry_for_tab(tab),
                    "project": session.path,
                    "group": group,
                    "order": tab_order,
                    "active": tab is active_tab,
                    "detached": window is not None,
                    "window_geometry": (
                        self._encode_window_bytes(window.saveGeometry()) if window is not None else ""
                    ),
                })
        return {
            "projects": projects,
            "active_project": self.current_project,
            "tabs": entries,
            "editor_groups": self.editor_groups.snapshot(),
        }

    def _restore_window_layout(self) -> None:
        workbench = self.settings.get("workbench", {})
        if not isinstance(workbench, dict):
            return
        geometry = self._decode_window_bytes(workbench.get("window_geometry"))
        state = self._decode_window_bytes(workbench.get("window_state"))
        if not geometry.isEmpty():
            self.restoreGeometry(geometry)
        if not state.isEmpty():
            self.restoreState(state, 1)

    def _restore_workbench_session(self) -> None:
        if getattr(self, "_workbench_session_restored", False):
            return
        self._workbench_session_restored = True
        workbench = self.settings.get("workbench", {})
        if not isinstance(workbench, dict) or not workbench.get("restore_session", True):
            return
        snapshot = workbench.get("session", {})
        if not isinstance(snapshot, dict):
            return
        entries = [entry for entry in snapshot.get("tabs", []) if isinstance(entry, dict)]
        projects = [
            item for item in snapshot.get("projects", [])
            if isinstance(item, dict) and os.path.isdir(str(item.get("path", "")))
        ]
        orientation = (
            Qt.Vertical
            if snapshot.get("editor_groups", {}).get("orientation") == "vertical"
            else Qt.Horizontal
        )

        def restore_entries(project_path):
            matching = sorted(
                (entry for entry in entries if entry.get("project") == project_path),
                key=lambda entry: (int(entry.get("group", 0)), int(entry.get("order", 0))),
            )
            for entry in matching:
                tab = self._restore_workbench_entry(entry)
                if tab is None:
                    continue
                self.editor_groups.move_page_to_group(
                    tab.notebook_widget,
                    max(0, int(entry.get("group", 0))),
                    orientation,
                )
                if entry.get("detached"):
                    notebook = self.project_workspace.sessions.notebook_for(tab.notebook_widget)
                    window = notebook.detach_widget(tab.notebook_widget) if notebook is not None else None
                    geometry = self._decode_window_bytes(entry.get("window_geometry"))
                    if window is not None and not geometry.isEmpty():
                        window.restoreGeometry(geometry)

        queue = list(projects)

        def restore_next_project():
            if not queue:
                self._finish_workbench_restore(snapshot, workbench)
                return
            project = queue.pop(0)
            path = str(project["path"])
            self.project_workspace.activate(
                path,
                project.get("game"),
                on_loaded=lambda path=path: (restore_entries(path), restore_next_project()),
            )

        scratch_entries = [entry for entry in entries if not entry.get("project")]
        if scratch_entries:
            self.project_workspace.sessions.activate(None)
            restore_entries(None)
        restore_next_project()

    def _restore_workbench_entry(self, entry):
        encoded = str(entry.get("target", ""))
        if not encoded:
            return None
        _project, is_pak, target = ProjectManager.decode_history_entry(encoded)
        before = {id(tab) for tab in self.tabs.values()}
        success = self.proj_dock.reopen_pak_history_entry(target) if is_pak else self._open_path(target)
        if not success:
            QMessageBox.warning(
                self,
                self.tr("Restore Tab"),
                self.tr("Could not restore {path}.").format(path=target),
            )
            return None
        new_tab = next((tab for tab in self.tabs.values() if id(tab) not in before), None)
        if new_tab is not None:
            return new_tab
        active = self.get_active_tab()
        return active if isinstance(active, FileTab) and self._history_entry_for_tab(active) == encoded else None

    def _finish_workbench_restore(self, snapshot: dict, workbench: dict) -> None:
        active_project = snapshot.get("active_project")

        def finish():
            self.editor_groups.restore_layout(snapshot.get("editor_groups", {}))
            active_entry = next((entry for entry in snapshot.get("tabs", []) if entry.get("active")), None)
            if active_entry:
                encoded = str(active_entry.get("target", ""))
                for tab in self.project_workspace.sessions.active_tabs():
                    if isinstance(tab, FileTab) and self._history_entry_for_tab(tab) == encoded:
                        self.project_workspace.focus_open_tab(tab)
                        break
            self.proj_dock.restore_view_state(workbench.get("project_browser", {}))
            self._refresh_document_titles()
            self._refresh_homepage()

        if active_project and os.path.isdir(str(active_project)):
            session = self.project_workspace.sessions.get(
                self.project_workspace.sessions.key_for(active_project)
            )
            self.project_workspace.activate(
                active_project,
                getattr(session, "game", None),
                on_loaded=finish,
            )
        else:
            self.project_workspace.sessions.activate(None)
            self.current_project = self.current_game = None
            self.proj_dock.set_project(None)
            self.proj_dock.hide()
            self.project_workspace._sync_tabs()
            finish()

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
        self._save_workbench_state()
        if not self.settings.get("workbench", {}).get("restore_session", True):
            self._record_tabs_closed_on_shutdown()
        self._stop_ai_chat_visibility_tracking()
        if hasattr(self, "ai_chat_dock"):
            self.ai_chat_dock.shutdown()
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

    def open_rsz_instance(self, filepath, instance_id, type_registry):
        """Open or activate a file and reveal one RSZ instance in its editor."""
        from ui.scene.scn_raw_inspector import select_instance_in_tree

        key = os.path.normcase(os.path.abspath(filepath))
        tab = next((tab for tab in self.tabs.values() if getattr(tab, "filename", None)
                    and os.path.normcase(os.path.abspath(tab.filename)) == key), None)
        if tab:
            self.project_workspace.focus_open_tab(tab)

        self._rsz_type_registry_override = type_registry
        try:
            if tab and getattr(getattr(tab, "handler", None), "type_registry", None) is not type_registry:
                tab.reload_file()
            elif not tab and self._open_path(os.fspath(filepath)):
                tab = self.get_active_tab()
        finally:
            self._rsz_type_registry_override = None

        if not tab or getattr(getattr(tab, "handler", None), "type_registry", None) is not type_registry:
            return
        viewer = getattr(tab, "viewer", None)
        if preview_tabs := getattr(viewer, "_preview_tabs", None):
            preview_tabs.setCurrentIndex(0)
        select_instance_in_tree(getattr(viewer, "tree", None), instance_id)

    def open_find_dialog(self):
        active = self.get_active_tab()
        if not active:
            QMessageBox.critical(self, self.tr("Error"), self.tr("No active tab for searching."))
            return

        if is_handler_type(active.handler, "MsgHandler"):
            QMessageBox.information(self, self.tr("Search in MSG"), self.tr("MSG files have a built-in search at the top of the editor. Please use that search bar."))
            return

        for window in self._floating_windows():
            if window.page == active.notebook_widget:
                active.open_find_dialog()
                return
        if not self._shared_find_dialog or not isinstance(self._shared_find_dialog, BetterFindDialog):
            self._shared_find_dialog = BetterFindDialog(file_tab=active, parent=self, shared_mode=True)
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
                for window in self._floating_windows():
                    if window.page == active.notebook_widget:
                        is_detached = True
                        break

                if not is_detached:
                    self._shared_find_dialog.set_file_tab(active)

    def _check_and_close_shared_find_dialog(self):
        """Close the shared find dialog if no tabs are left in the main window"""
        has_main_tabs = any(notebook.count() for notebook in self._notebooks())

        if not has_main_tabs and hasattr(self, '_shared_find_dialog') and self._shared_find_dialog:
            try:
                if self._shared_find_dialog.isVisible():
                    self._shared_find_dialog.close()
            except RuntimeError:
                pass

    def add_tab(self, filename=None, data=None, pak_source_path=None, pak_project_dir=None, resource_context=None):
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
                    notebook = self.project_workspace.sessions.notebook_for(tab.notebook_widget)
                    index = notebook.indexOf(tab.notebook_widget) if notebook is not None else -1
                    if index != -1:
                        notebook.setCurrentIndex(index)
                        self.editor_groups.activate_page(tab.notebook_widget)
                    else:
                        for window in self.project_workspace.sessions.windows_for([tab]):
                            window.show()
                            window.raise_()
                            window.activateWindow()
                    return tab

        tab = None
        try:
            handler = get_handler_for_data(data, filename)
            if handler.open_externally(filename, data, pak_source_path):
                return None
            if resource_context is not None:
                handler.resource_context = resource_context
            if hasattr(handler, 'needs_json_path') and handler.needs_json_path():
                if not self.settings.get("rcol_json_path") and not getattr(self, "_rsz_type_registry_override", None):
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
            notebook = self.editor_groups.active_notebook()
            tab.parent_notebook = notebook
            tab_label = os.path.basename(filename) if filename else self.tr("Untitled")
            tab.notebook_widget._reasy_tab_tooltip = filename or tab_label
            index = notebook.addTab(tab.notebook_widget, tab_label)
            notebook.setTabToolTip(index, filename or tab_label)
            self.tabs[tab.notebook_widget] = tab
            self.project_workspace.sessions.add_tab(tab)
            notebook.setCurrentWidget(tab.notebook_widget)
            self._refresh_document_titles()
            self._update_highlight_menu_visibility()
            self.scenes.refresh_actions()
            self.scenes.refresh_buttons()
            self._refresh_homepage()
            self._on_active_page_changed(tab.notebook_widget)
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
        tab.notebook_widget._reasy_tab_tooltip = pak_path
        notebook = self.project_workspace.sessions.notebook_for(tab.notebook_widget)
        if notebook is not None:
            notebook.setTabToolTip(notebook.indexOf(tab.notebook_widget), pak_path)
        self._refresh_document_titles()
        if tab is self.get_active_tab():
            self.breadcrumbs.bind_tab(tab)
        self.scenes.attach_tab_document(tab)
        self.scenes.refresh_actions()
        self.scenes.refresh_buttons()

    def get_active_tab(self):
        active_tabs = self.project_workspace.sessions.active_tabs()
        aw = QApplication.activeWindow()
        widgets = [self.editor_groups.active_page(), QApplication.focusWidget()]
        if isinstance(aw, FloatingTabWindow):
            widgets.insert(0, aw.centralWidget())
        for widget in widgets:
            tab = self._resolve_tab_from_widget(widget)
            if tab in active_tabs or getattr(tab, "suppress_general_shortcuts", False):
                return tab
        return None

    def on_open(self):
        fn, _ = QFileDialog.getOpenFileName(
            self,
            self.tr("Open File"),
            "",
            "RE Files (*.uvar* *.scn* *.user* *.pfb* *.msg* *.efx* *.cfil* *.motbank* *.mcambank* *.tex* *.mesh* *.mdf2* *.sbnk* *.spck* *.wel* *.mov*);;SCN Files (*.scn*);;User Files (*.user*);;UVAR Files (*.uvar*);;PFB Files (*.pfb*);;MSG Files (*.msg*);;EFX Files (*.efx*);;CFIL Files (*.cfil*);;MOTBANK Files (*.motbank*);;MCAMBANK Files (*.mcambank*);;Texture Files (*.tex*);;DDS Files (*.dds*);;Mesh Files (*.mesh*);;Material Definition Files (*.mdf2*);;Sound Files (*.sbnk* *.spck*);;Wwise Event List (*.wel*);;MOV Files (*.mov*);;All Files (*.*)"
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

    def save_all_modified_files(self) -> dict:
        result = save_modified_tabs(
            self.project_workspace.sessions.all_tabs()
        )
        if result["requested_count"] == 0:
            message = self.tr("No modified files to save.")
        elif result["success"]:
            message = self.tr(
                "Saved {count} modified file(s)."
            ).format(count=result["saved_count"])
        else:
            message = self.tr(
                "Saved {saved} modified file(s); {failed} could not be saved."
            ).format(
                saved=result["saved_count"],
                failed=result["failed_count"],
            )
        self.status_bar.showMessage(message, 5000)
        return result

    def on_save_all(self):
        return self.save_all_modified_files()

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
        notebook = self.project_workspace.sessions.notebook_for(tab.notebook_widget)
        index = notebook.indexOf(tab.notebook_widget) if notebook is not None else -1
        if index == -1:
            windows = self.project_workspace.sessions.windows_for([tab])
            if windows:
                windows[0].close()
                notebook = self.project_workspace.sessions.notebook_for(tab.notebook_widget)
                index = notebook.indexOf(tab.notebook_widget) if notebook is not None else -1
        if index != -1:
            self.close_tab(index, notebook=notebook)

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

    def _history_entry_for_tab(self, tab):
        session = self.project_workspace.sessions.session_for_tab(tab)
        return ProjectManager.encode_history_entry(
            tab.pak_source_path or tab.filename,
            session.path if session else None,
            is_pak=bool(getattr(tab, "pak_source_path", None)),
        )

    def _record_tabs_closed_on_shutdown(self):
        entries = [
            self._history_entry_for_tab(tab)
            for tab in list(self.tabs.values())
            if getattr(tab, "filename", None)
        ]
        if not entries:
            return
        stale = set(entries)
        self._closed_file_history = (
            [f for f in self._closed_file_history if f not in stale] + entries
        )
        self._save_closed_file_history()

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
            self._record_closed_file(self._history_entry_for_tab(tab))

        self.tabs.pop(widget, None)
        self.editor_groups.remove_page(widget)
        self.editor_groups.prune_empty_groups()
        self.project_workspace.sessions.remove_tab(tab)
        tab.cleanup()
        self._refresh_document_titles()
        self._check_and_close_shared_find_dialog()
        self.scenes.refresh_actions()
        self.scenes.refresh_buttons()
        self._refresh_homepage()

    def close_tab(self, index, notebook=None):
        notebook = notebook or self.editor_groups.active_notebook()
        widget = notebook.widget(index)
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
        notebook = self.editor_groups.active_notebook()
        current_index = notebook.currentIndex()
        if current_index > 0:
            notebook.setCurrentIndex(current_index - 1)

    def goto_next_tab(self):
        notebook = self.editor_groups.active_notebook()
        current_index = notebook.currentIndex()
        if current_index < notebook.count() - 1:
            notebook.setCurrentIndex(current_index + 1)
