"""Clickable file and object breadcrumbs for the active editor."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPersistentModelIndex, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton


class BreadcrumbBar(QFrame):
    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app_window = app_window
        self.setObjectName("editorBreadcrumbs")
        self.setFrameShape(QFrame.NoFrame)
        self.setMinimumHeight(28)
        self.setMaximumHeight(32)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(7, 1, 7, 1)
        self._layout.setSpacing(1)
        self._tab = None
        self._tree = None
        self._selection_model = None
        self.hide()

    def bind_tab(self, tab) -> None:
        if self._selection_model is not None:
            try:
                self._selection_model.currentChanged.disconnect(self._on_object_changed)
            except (RuntimeError, TypeError):
                pass
        self._tab = tab
        self._tree = self._tree_for(tab)
        self._selection_model = self._tree.selectionModel() if self._tree is not None else None
        if self._selection_model is not None:
            self._selection_model.currentChanged.connect(self._on_object_changed)
        self._rebuild()

    @staticmethod
    def _tree_for(tab):
        if tab is None:
            return None
        viewer_tree = getattr(getattr(tab, "viewer", None), "tree", None)
        return viewer_tree or getattr(tab, "tree", None)

    def _clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            if widget := item.widget():
                widget.deleteLater()

    def _rebuild(self, current=None, _previous=None) -> None:
        self._clear()
        if self._tab is None:
            self.hide()
            return
        self.show()
        for position, (label, callback, tooltip) in enumerate(self._file_segments()):
            if position:
                self._separator()
            self._button(label, callback, tooltip)

        current = current if current is not None and current.isValid() else (
            self._selection_model.currentIndex() if self._selection_model is not None else None
        )
        object_segments = self._object_segments(current)
        if object_segments:
            divider = QLabel("  •  ", self)
            divider.setObjectName("breadcrumbDivider")
            self._layout.addWidget(divider)
            for position, (label, persistent) in enumerate(object_segments):
                if position:
                    self._separator()
                self._button(label, lambda idx=persistent: self._select_index(idx), label)
        self._layout.addStretch(1)

    def _file_segments(self):
        tab = self._tab
        virtual = str(getattr(tab, "pak_source_path", "") or "").replace("\\", "/")
        if virtual:
            parts = [part for part in virtual.split("/") if part]
            segments = [(self.tr("PAK"), lambda: None, virtual)]
            for index, part in enumerate(parts):
                prefix = "/".join(parts[: index + 1])
                callback = (
                    (lambda path=prefix + "/": self.app_window.proj_dock._reveal_pak_folder(path))
                    if index < len(parts) - 1 else lambda: None
                )
                segments.append((part, callback, prefix))
            return self._compact(segments)

        filename = str(getattr(tab, "filename", "") or "")
        if not filename:
            return [(self.tr("Untitled"), lambda: None, self.tr("Untitled"))]
        path = Path(filename)
        sessions = getattr(getattr(self.app_window, "project_workspace", None), "sessions", None)
        session = sessions.session_for_tab(tab) if sessions is not None else None
        project = getattr(session, "path", None)
        root = project if project and self._is_relative_to(path, Path(project)) else None
        if root:
            parts = [Path(root).name, *path.relative_to(root).parts]
            base = Path(root).parent
            scope = "project"
        else:
            parts = list(path.parts)
            base = Path(parts[0]) if parts else path.parent
            parts = parts[1:] if parts else [path.name]
            scope = "unpacked"
        segments = []
        current = base
        for index, part in enumerate(parts):
            current = current / part
            target = str(current if current.is_dir() else current.parent)
            callback = (
                lambda target=target, scope=scope:
                self.app_window.proj_dock._reveal_filesystem_folder(target, scope)
            )
            segments.append((part, callback if index < len(parts) - 1 else lambda: None, str(current)))
        return self._compact(segments)

    @staticmethod
    def _is_relative_to(path: Path, root: Path) -> bool:
        try:
            path.resolve().relative_to(root.resolve())
            return True
        except (OSError, ValueError):
            return False

    @staticmethod
    def _compact(segments):
        if len(segments) <= 7:
            return segments
        return [segments[0], ("…", lambda: None, ""), *segments[-5:]]

    @staticmethod
    def _object_segments(index):
        if index is None or not index.isValid():
            return []
        segments = []
        current = index.siblingAtColumn(0)
        while current.isValid():
            label = str(current.data(Qt.DisplayRole) or "")
            if label:
                segments.append((label, QPersistentModelIndex(current)))
            current = current.parent()
        return list(reversed(segments))[-6:]

    def _button(self, text, callback, tooltip):
        button = QToolButton(self)
        button.setObjectName("breadcrumbButton")
        button.setText(str(text))
        button.setToolTip(str(tooltip or text))
        button.setAccessibleName(str(text))
        button.setAutoRaise(True)
        button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        button.clicked.connect(lambda _checked=False: callback())
        self._layout.addWidget(button)

    def _separator(self):
        label = QLabel("›", self)
        label.setObjectName("breadcrumbSeparator")
        self._layout.addWidget(label)

    def _select_index(self, persistent) -> None:
        if self._tree is None or not persistent.isValid():
            return
        self._tree.setCurrentIndex(persistent)
        self._tree.scrollTo(persistent)

    def _on_object_changed(self, current, previous):
        self._rebuild(current, previous)

