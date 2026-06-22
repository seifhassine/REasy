import os
import shutil
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import QMessageBox, QSizePolicy, QStyle, QTabBar, QToolBar, QToolButton

from .project_sessions import ProjectSessionManager


class ProjectWorkspaceController:
    def __init__(self, host, notebook, tab_lookup):
        self.host = host
        self.sessions = ProjectSessionManager(notebook, tab_lookup)

        self.toolbar = QToolBar(host.tr("Projects"), host)
        self.toolbar.setObjectName("projectWorkspaceBar")
        self.toolbar.setMovable(False)
        self.toolbar.setContextMenuPolicy(Qt.PreventContextMenu)
        self.toolbar.toggleViewAction().setVisible(False)
        self.toolbar.hide()

        self.tab_bar = QTabBar(self.toolbar)
        self.tab_bar.setObjectName("projectWorkspaceTabs")
        self.tab_bar.setExpanding(True)
        self.tab_bar.setElideMode(Qt.ElideMiddle)
        self.tab_bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.tab_bar.setMinimumHeight(36)
        self.tab_bar.currentChanged.connect(self._on_tab_changed)
        self.toolbar.addWidget(self.tab_bar)
        self.host.addToolBar(Qt.TopToolBarArea, self.toolbar)
        self.set_dark_mode(getattr(host, "dark_mode", False))

    def set_dark_mode(self, dark_mode: bool):
        accent = self.host._theme_accent_color().name()
        bar_bg, tab_bg, hover_bg, foreground, muted, border = (
            ("#242424", "#343434", "#3f3f3f", "#f2f2f2", "#c4c4c4", "#505050")
            if dark_mode else ("#f3f4f6", "#fff", "#e7e9ed", "#1d1d1f", "#555b65", "#c9cdd3")
        )
        self.toolbar.setStyleSheet(f"""
            QToolBar#projectWorkspaceBar {{ background: {bar_bg}; border: none;
                border-bottom: 1px solid {border}; padding: 0; spacing: 0; }}
            QTabBar#projectWorkspaceTabs {{ background: {bar_bg}; border: none; }}
            QTabBar#projectWorkspaceTabs::tab {{ background: transparent; color: {muted};
                border: none; border-right: 1px solid {border}; padding: 8px 14px; min-width: 120px; }}
            QTabBar#projectWorkspaceTabs::tab:hover:!selected {{ background: {hover_bg}; color: {foreground}; }}
            QTabBar#projectWorkspaceTabs::tab:selected {{ background: {tab_bg}; color: {foreground};
                border-bottom: 3px solid {accent}; font-weight: 600; }}
            QToolButton#projectTabClose {{ background: transparent; color: {muted}; border: none;
                border-radius: 3px; font-size: 16px; font-weight: 600; }}
            QToolButton#projectTabClose:hover {{ background: #d32f2f; color: white; }}
        """)

    def _close_button(self, key):
        button = QToolButton(self.tab_bar)
        button.setObjectName("projectTabClose")
        button.setText("×")
        button.setToolTip(self.host.tr("Close project"))
        button.setFixedSize(20, 20)
        button.clicked.connect(lambda: self.close(key))
        return button

    def is_active(self, project_dir) -> bool:
        return ProjectSessionManager.key_for(project_dir) == self.sessions.active_key

    def _warn(self, title, message):
        QMessageBox.warning(self.host, self.host.tr(title), self.host.tr(message))

    def open(self, project_path: Path | str, game: str | None = None) -> bool:
        project_path = Path(project_path).resolve()
        if not project_path.is_dir():
            self._warn("Project not found", "That project folder no longer exists.")
            return False

        game = game or self.host.proj_dock.infer_project_game(project_path)
        if not game:
            self._warn("Invalid selection", "This folder is not a recognized REasy project.")
            return False

        self.activate(project_path, game)
        return True

    def activate(self, path: Path | str, game: str | None = None, on_loaded=None):
        path = str(Path(path).resolve())
        dock = self.host.proj_dock
        session = self.sessions.ensure_project(path, game or dock.infer_project_game(path))
        dialog = self.host._shared_find_dialog
        if session.key != self.sessions.active_key and dialog:
            try:
                dialog.close()
            except RuntimeError:
                self.host._shared_find_dialog = None
        self.sessions.activate(session.key)
        self._sync_tabs()

        self.host.current_project = session.path
        self.host.current_game = session.game
        dock.current_game = session.game
        if session.game:
            self.host.settings["last_game"] = session.game
            self.host.settings["game_version"] = session.game
        dock.sync_project_rsz_json(path, session.game, prompt_to_change_current=False)
        self.host.update_from_app_settings()
        self.host.save_settings()
        dock.show()
        self.host._shrink_project_dock()
        dock.set_project(path, on_loaded)
        self.host.status_bar.showMessage(f"Project: {os.path.basename(path)}", 3000)
        return session

    def delete_project(self, project_path: Path) -> bool:
        key = ProjectSessionManager.key_for(project_path)
        session = self.sessions.get(key)
        if session and not self.host._confirm_tabs_close(session.tabs, apply_discards=False):
            return False
        try:
            shutil.rmtree(project_path)
        except Exception as exc:
            QMessageBox.critical(self.host, self.host.tr("Delete failed"), str(exc))
            return False
        if session:
            self.close(key, confirm=False, record_history=False)
        return True

    def close(self, key: str | None = None, *, confirm=True, record_history=True) -> bool:
        key = key or self.sessions.active_key
        session = self.sessions.get(key)
        if not key or not session:
            return False
        if confirm and not self.host._confirm_tabs_close(session.tabs):
            return False

        was_active = key == self.sessions.active_key
        for tab in list(session.tabs):
            self.host._close_tab_object(tab, record_history=record_history)
        next_session = self.sessions.remove_project(key)

        if was_active and next_session:
            self.activate(next_session.path, next_session.game)
            self.host.proj_dock.discard_project_state(session.path)
            return True
        if was_active:
            self.sessions.activate(None)
            self.host.current_project = self.host.current_game = None
            self.host.proj_dock.set_project(None)
            self.host.proj_dock.hide()
            self.host.status_bar.showMessage("Project closed", 3000)

        self.host.proj_dock.discard_project_state(session.path)
        self._sync_tabs()
        return True

    def _sync_tabs(self):
        with QSignalBlocker(self.tab_bar):
            while self.tab_bar.count():
                self.tab_bar.removeTab(0)
            for session in self.sessions.project_sessions():
                icon = self.host.style().standardIcon(QStyle.SP_DirIcon)
                index = self.tab_bar.addTab(icon, session.title)
                self.tab_bar.setTabData(index, session.key)
                self.tab_bar.setTabToolTip(index, session.path)
                self.tab_bar.setTabButton(index, QTabBar.RightSide, self._close_button(session.key))
                if session.key == self.sessions.active_key:
                    self.tab_bar.setCurrentIndex(index)
        has_projects = self.tab_bar.count() > 0
        self.toolbar.setVisible(has_projects)
        self.host._refresh_homepage()

    def _on_tab_changed(self, index: int):
        key = self.tab_bar.tabData(index)
        if key and key != self.sessions.active_key:
            session = self.sessions.get(key)
            self.activate(session.path, session.game)
