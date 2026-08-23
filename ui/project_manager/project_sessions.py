import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ProjectSession:
    key: str
    path: str | None
    game: str | None = None
    tabs: list[Any] = field(default_factory=list)
    current_widget: Any = None

    @property
    def title(self) -> str:
        name = Path(self.path).name if self.path else ""
        return f"{name} ({self.game})" if name and self.game else name


class ProjectSessionManager:
    def __init__(self, notebook, tab_lookup, group_host=None):
        self.notebook = notebook
        self.tab_lookup = tab_lookup
        self.group_host = group_host
        self._sessions: dict[str, ProjectSession] = {}
        self._scratch = ProjectSession("", None)
        self.active_key: str | None = None

    @staticmethod
    def key_for(path: str | os.PathLike) -> str:
        return os.path.normcase(os.path.abspath(os.fspath(path)))

    def project_sessions(self) -> list[ProjectSession]:
        return list(self._sessions.values())

    def get(self, key: str | None) -> ProjectSession | None:
        return self._scratch if key is None else self._sessions.get(key)

    def ensure_project(self, path: str | os.PathLike, game: str | None = None) -> ProjectSession:
        resolved = str(Path(path).resolve())
        key = self.key_for(resolved)
        session = self._sessions.get(key)
        if session is None:
            session = ProjectSession(key=key, path=resolved, game=game)
            self._sessions[key] = session
        elif game:
            session.game = game
        return session

    def activate(self, key: str | None) -> ProjectSession | None:
        if key == self.active_key:
            return self.get(key)
        if current := self.get(self.active_key):
            self._hide_session(current)
        self.active_key = key
        target = self.get(key)
        if target:
            self._restore_session(target)
        return target

    def active_tabs(self) -> list[Any]:
        return list(self.get(self.active_key).tabs)

    def all_tabs(self) -> list[Any]:
        tabs = []
        seen = set()
        for session in [self._scratch, *self._sessions.values()]:
            for tab in session.tabs:
                if id(tab) in seen:
                    continue
                seen.add(id(tab))
                tabs.append(tab)
        return tabs

    def session_for_tab(self, tab: Any) -> ProjectSession | None:
        sessions = [self._scratch, *self._sessions.values()]
        return next((session for session in sessions if tab in session.tabs), None)

    def add_tab(self, tab: Any) -> None:
        self.get(self.active_key).tabs.append(tab)

    def add_global_tab(self, tab: Any) -> None:
        self._scratch.tabs.append(tab)

    def capture_active_order(self, *_signal_args) -> None:
        """Persist the live tab-bar order without recreating any page widgets."""
        if (session := self.get(self.active_key)) is not None:
            self._capture_session(session)

    def remove_tab(self, tab: Any) -> None:
        for session in [self._scratch, *self._sessions.values()]:
            if tab in session.tabs:
                session.tabs.remove(tab)
            if session.current_widget is getattr(tab, "notebook_widget", None):
                session.current_widget = None

    def remove_project(self, key: str) -> ProjectSession | None:
        self._sessions.pop(key, None)
        return next(iter(self._sessions.values()), None)

    def rename_project(self, old_key: str, new_path: str | os.PathLike) -> ProjectSession | None:
        session = self._sessions.pop(old_key, None)
        if session is None:
            return None
        resolved = str(Path(new_path).resolve())
        session.path = resolved
        session.key = self.key_for(resolved)
        self._sessions[session.key] = session
        return session

    def _hide_session(self, session: ProjectSession) -> None:
        self._capture_session(session)
        for tab in session.tabs:
            widget = tab.notebook_widget
            notebook = self.notebook_for(widget)
            index = notebook.indexOf(widget) if notebook is not None else -1
            if index != -1:
                tab._editor_group_index = self.group_index_for(widget)
                notebook.removeTab(index)
                widget.hide()

        self._show_windows(session, False)

    def _restore_session(self, session: ProjectSession) -> None:
        detached = {window.file_tab for window in self.windows_for(session.tabs)}
        for tab in session.tabs:
            if tab in detached or getattr(tab, "_workspace_hidden", False):
                continue
            widget = tab.notebook_widget
            notebook = self.notebook_for(widget)
            if notebook is None:
                notebook = self._restore_notebook_for(tab)
            index = notebook.indexOf(widget)
            if index == -1:
                index = notebook.addTab(widget, "")
            tab.update_tab_title()
            if getattr(tab, "hide_notebook_tab", False):
                notebook.tabBar().setTabVisible(index, False)

        if session.current_widget and (current_book := self.notebook_for(session.current_widget)) is not None:
            current_book.setCurrentWidget(session.current_widget)
        elif session.tabs:
            last = next((tab for tab in reversed(session.tabs) if self.notebook_for(tab.notebook_widget)), None)
            if last is not None:
                book = self.notebook_for(last.notebook_widget)
                book.setCurrentWidget(last.notebook_widget)

        self._show_windows(session, True)
        if self.group_host is not None:
            self.group_host.prune_empty_groups()

    def _capture_session(self, session: ProjectSession) -> None:
        widgets = [
            notebook.widget(index)
            for notebook in self.notebooks()
            for index in range(notebook.count())
        ]
        docked = [tab for widget in widgets if (tab := self.tab_lookup.get(widget)) in session.tabs]
        session.tabs = docked + [tab for tab in session.tabs if tab not in docked]
        for tab in docked:
            tab._editor_group_index = self.group_index_for(tab.notebook_widget)
        current_widget = self.current_widget()
        session.current_widget = current_widget if self.tab_lookup.get(current_widget) in session.tabs else None

    def windows_for(self, tabs) -> list[Any]:
        return [
            window for notebook in self.notebooks()
            for window in getattr(notebook, "_floating_windows", [])
            if getattr(window, "file_tab", None) in tabs
        ]

    def set_tab_hidden(self, tab: Any, hidden: bool) -> None:
        if bool(getattr(tab, "_workspace_hidden", False)) == hidden:
            return
        tab._workspace_hidden = hidden
        (self._hide_tab if hidden else self._show_tab)(tab)

    def _hide_tab(self, tab: Any) -> None:
        widget = tab.notebook_widget
        notebook = self.notebook_for(widget)
        if notebook is not None and (index := notebook.indexOf(widget)) != -1:
            tab._editor_group_index = self.group_index_for(widget)
            notebook.removeTab(index)
            widget.hide()
        if (session := self.session_for_tab(tab)) and session.current_widget is widget:
            session.current_widget = None
        for window in self.windows_for([tab]):
            window.setVisible(False)

    def _show_tab(self, tab: Any) -> None:
        session = self.session_for_tab(tab)
        active = session is self.get(self.active_key)
        windows = self.windows_for([tab])
        if active and not windows and self.notebook_for(tab.notebook_widget) is None:
            self._restore_notebook_for(tab).addTab(tab.notebook_widget, "")
            tab.update_tab_title()
        for window in windows:
            window.setVisible(active)

    def _show_windows(self, session: ProjectSession, visible: bool) -> None:
        for window in self.windows_for(session.tabs):
            window.setVisible(visible and not getattr(window.file_tab, "_workspace_hidden", False))

    def notebooks(self):
        if self.group_host is not None:
            return self.group_host.notebooks()
        return (self.notebook,) if self.notebook is not None else ()

    def notebook_for(self, widget):
        if widget is None:
            return None
        return next((notebook for notebook in self.notebooks() if notebook.indexOf(widget) >= 0), None)

    def current_widget(self):
        if self.group_host is not None:
            return self.group_host.active_page()
        return self.notebook.currentWidget() if self.notebook is not None else None

    def group_index_for(self, widget) -> int:
        notebook = self.notebook_for(widget)
        notebooks = self.notebooks()
        return notebooks.index(notebook) if notebook in notebooks else 0

    def _restore_notebook_for(self, tab):
        if self.group_host is not None:
            return self.group_host.ensure_group(max(0, int(getattr(tab, "_editor_group_index", 0))))
        return self.notebook


def save_modified_tabs(tabs) -> dict[str, Any]:
    """Save every currently modified tab and report failures without stopping."""

    unique_tabs = []
    seen = set()
    for tab in tabs:
        if tab is None or id(tab) in seen:
            continue
        seen.add(id(tab))
        unique_tabs.append(tab)

    def label_for(tab) -> str:
        return str(
            getattr(tab, "filename", "")
            or getattr(tab, "title", "")
            or "Untitled"
        )

    candidates = [tab for tab in unique_tabs if bool(getattr(tab, "modified", False))]
    candidates.sort(
        key=lambda tab: not bool(getattr(tab, "hide_notebook_tab", False))
    )
    saved = []
    failed = []
    for tab in candidates:
        if not bool(getattr(tab, "modified", False)):
            continue
        label = label_for(tab)
        save = getattr(tab, "direct_save", None)
        if not callable(save):
            failed.append({"file": label, "error_code": "unsupported"})
            continue
        try:
            succeeded = bool(save())
        except Exception as exc:
            failed.append(
                {
                    "file": label,
                    "error_code": "exception",
                    "detail": str(exc),
                }
            )
            continue
        if succeeded and not bool(getattr(tab, "modified", False)):
            saved.append(label)
        else:
            failed.append({"file": label, "error_code": "still_modified"})

    remaining = [
        label_for(tab)
        for tab in unique_tabs
        if bool(getattr(tab, "modified", False))
    ]
    return {
        "requested_count": len(candidates),
        "saved": saved,
        "saved_count": len(saved),
        "failed": failed,
        "failed_count": len(failed),
        "remaining_modified": remaining,
        "remaining_modified_count": len(remaining),
        "success": not failed and not remaining,
    }
