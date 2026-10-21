"""Clickable file and object breadcrumbs for the active editor."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPersistentModelIndex, QSize, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton

lazy from ui.editor_widgets import tool_button, toolbar_separator
lazy from ui.icons import line_icon
lazy from ui.styles import current_colors


def breadcrumb_separator(parent) -> QLabel:
    label = QLabel(parent)
    label.setObjectName("breadcrumbSeparator")
    label.setPixmap(line_icon("chevron_right", current_colors()["text_subtle"]).pixmap(QSize(12, 12)))
    label.setFixedWidth(14)
    label.setAlignment(Qt.AlignCenter)
    return label


class BreadcrumbBar(QFrame):
    def __init__(self, app_window, parent=None):
        super().__init__(parent)
        self.app_window = app_window
        self.setObjectName("editorBreadcrumbs")
        self.setFrameShape(QFrame.NoFrame)
        self.setFixedHeight(26)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(6, 0, 6, 0)
        self._layout.setSpacing(0)
        colors = current_colors()
        self.setStyleSheet(f"""
            QToolButton#breadcrumbButton[segment="current"] {{ color: {colors['text_bright']}; }}
            QToolButton#breadcrumbButton[segment="current"]:hover,
            QToolButton#breadcrumbButton[segment="current"]:pressed {{
                background: transparent; color: {colors['text_bright']};
            }}
            QToolButton#breadcrumbButton[segment="static"]:hover,
            QToolButton#breadcrumbButton[segment="static"]:pressed {{
                background: transparent; color: {colors['text_muted']};
            }}
            QToolButton#breadcrumbButton::menu-indicator {{ image: none; width: 0px; }}
        """)
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
        file_segments = self._file_segments()
        if not file_segments:
            self.hide()
            return
        self.show()
        for position, (label, callback, tooltip) in enumerate(file_segments):
            if position:
                self._separator()
            if label is None:
                self._overflow_button(callback)
            else:
                self._button(label, callback, tooltip, "current" if position == len(file_segments) - 1 else None)

        current = current if current is not None and current.isValid() else (
            self._selection_model.currentIndex() if self._selection_model is not None else None
        )
        object_segments = self._object_segments(current)
        if object_segments:
            self._layout.addSpacing(6)
            divider = toolbar_separator(14)
            divider.setObjectName("breadcrumbDivider")
            self._layout.addWidget(divider)
            self._layout.addSpacing(6)
            for position, (label, persistent) in enumerate(object_segments):
                if position:
                    self._separator()
                self._button(
                    label,
                    lambda idx=persistent: self._select_index(idx),
                    label,
                    "current" if position == len(object_segments) - 1 else None,
                )
        self._layout.addStretch(1)

    def _file_segments(self):
        tab = self._tab
        virtual = str(getattr(tab, "pak_source_path", "") or "").replace("\\", "/")
        if virtual:
            parts = [part for part in virtual.split("/") if part]
            segments = [(self.tr("PAK"), None, virtual)]
            for index, part in enumerate(parts):
                prefix = "/".join(parts[: index + 1])
                callback = (
                    (lambda path=prefix + "/": self.app_window.proj_dock._reveal_pak_folder(path))
                    if index < len(parts) - 1 else None
                )
                segments.append((part, callback, prefix))
            return self._compact(segments)

        filename = str(getattr(tab, "filename", "") or "")
        if not filename:
            return []
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
            segments.append((part, callback if index < len(parts) - 1 else None, str(current)))
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
        """Fold the middle of long paths into one entry whose label is None and callback the hidden segments."""
        if len(segments) <= 7:
            return segments
        return [segments[0], (None, segments[1:-5], ""), *segments[-5:]]

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

    def _button(self, text, callback, tooltip, segment=None):
        button = QToolButton(self)
        button.setObjectName("breadcrumbButton")
        button.setProperty("segment", segment or ("link" if callback is not None else "static"))
        button.setText(str(text))
        button.setToolTip(str(tooltip or text))
        button.setAccessibleName(str(text))
        button.setAutoRaise(True)
        button.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        if callback is not None:
            button.clicked.connect(lambda _checked=False: callback())
        self._layout.addWidget(button)

    def _overflow_button(self, hidden):
        button = tool_button("more", self.tr("Show hidden path segments"), parent=self)
        button.setObjectName("breadcrumbButton")
        button.setProperty("segment", "link")
        button.setIconSize(QSize(14, 14))
        button.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(button)
        for label, callback, tooltip in hidden:
            action = menu.addAction(str(label))
            action.setToolTip(str(tooltip or label))
            action.setEnabled(callback is not None)
            if callback is not None:
                action.triggered.connect(lambda _checked=False, callback=callback: callback())
        button.setMenu(menu)
        self._layout.addWidget(button)

    def _separator(self):
        self._layout.addWidget(breadcrumb_separator(self))

    def _select_index(self, persistent) -> None:
        if self._tree is None or not persistent.isValid():
            return
        self._tree.setCurrentIndex(persistent)
        self._tree.scrollTo(persistent)

    def _on_object_changed(self, current, previous):
        self._rebuild(current, previous)

