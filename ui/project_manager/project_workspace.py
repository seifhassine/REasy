import os
import shutil
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QMessageBox, QSizePolicy, QStyle, QTabBar, QToolBar, QToolButton

from .project_sessions import ProjectSessionManager


class ProjectWorkspaceController:
    def __init__(self, host, notebook, tab_lookup):
        self.host = host
        self.sessions = ProjectSessionManager(notebook, tab_lookup)
        self._scene_icon = self._make_scene_icon()

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
        notebook.currentChanged.connect(lambda _index: self._sync_tabs())
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

    def _close_button(self, callback, tip):
        button = QToolButton(self.tab_bar)
        button.setObjectName("projectTabClose")
        button.setText("×")
        button.setToolTip(self.host.tr(tip))
        button.setFixedSize(20, 20)
        button.clicked.connect(lambda _checked=False: callback())
        return button

    @staticmethod
    def _make_scene_icon() -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor("#cfd8e3"), 1.4))
        painter.setBrush(QColor("#1f2a36"))
        painter.drawRoundedRect(2, 5, 12, 8, 2, 2)
        painter.drawEllipse(6, 7, 4, 4)
        painter.drawLine(5, 5, 8, 2)
        painter.drawLine(8, 2, 11, 5)
        painter.end()
        return QIcon(pixmap)

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
        if self._scene_blocks_project_close(project_path):
            return False
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
        if self._scene_blocks_project_close(session.path):
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

    def _scene_blocks_project_close(self, project_path) -> bool:
        scenes = getattr(self.host, "scenes", None)
        scene = scenes.scene_using_project(str(project_path)) if scenes and project_path else None
        if scene is None:
            return False
        message = f'Scene "{scene.title}" contains SCNs from this project. Delete the scene first.'
        QMessageBox.information(self.host, self.host.tr("Scene uses project"), self.host.tr(message))
        return True

    def _sync_tabs(self):
        scenes = getattr(self.host, "scenes", None)
        scene_tabs = scenes.tabs() if scenes else ()
        with QSignalBlocker(self.tab_bar):
            while self.tab_bar.count():
                self.tab_bar.removeTab(0)
            for session in self.sessions.project_sessions():
                icon = self.host.style().standardIcon(QStyle.SP_DirIcon)
                index = self.tab_bar.addTab(icon, session.title)
                self.tab_bar.setTabData(index, session.key)
                self.tab_bar.setTabToolTip(index, session.path)
                close_button = self._close_button(lambda key=session.key: self.close(key), "Close project")
                self.tab_bar.setTabButton(index, QTabBar.RightSide, close_button)
                if session.key == self.sessions.active_key:
                    self.tab_bar.setCurrentIndex(index)
            current = self.host.tabs.get(self.sessions.notebook.currentWidget())
            for scene in scene_tabs:
                index = self.tab_bar.addTab(self._scene_icon, scene.title)
                self.tab_bar.setTabData(index, ("scene", scene))
                self.tab_bar.setTabToolTip(index, scene.title)
                close_button = self._close_button(lambda scene=scene: self.host.scenes.close_scene(scene), "Close scene")
                self.tab_bar.setTabButton(index, QTabBar.RightSide, close_button)
                if scene is current:
                    self.tab_bar.setCurrentIndex(index)
        fullscreen = any(scene.is_view_fullscreen() for scene in scene_tabs)
        self.toolbar.setVisible(self.tab_bar.count() > 0 and not fullscreen)
        self.host._refresh_homepage()

    def activate_current_project_tab(self) -> None:
        data = self.tab_bar.tabData(self.tab_bar.currentIndex())
        if not isinstance(data, tuple) and (session := self.sessions.get(data)) and session.path:
            self.activate(session.path, session.game)

    def _on_tab_changed(self, index: int):
        data = self.tab_bar.tabData(index)
        if isinstance(data, tuple) and data[0] == "scene":
            self.host.scenes.focus(data[1])
            return
        key = data
        if key and key != self.sessions.active_key:
            session = self.sessions.get(key)
            self.activate(session.path, session.game)
