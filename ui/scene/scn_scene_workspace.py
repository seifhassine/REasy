from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDockWidget,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMenuBar,
    QStyle,
    QStatusBar,
    QTabBar,
    QToolBar,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from file_handlers.rsz.scn_scene_loader import ScnSceneLoader, ScnSceneSource, scn_source_identity_keys
from file_handlers.rsz.scn_scene_preview import ScnScenePreviewWidget
from file_handlers.rsz.rsz_handler import RszHandler


def scn_source_from_tab(tab) -> ScnSceneSource | None:
    handler = getattr(tab, "handler", None)
    rsz_file = getattr(handler, "rsz_file", None)
    if not isinstance(handler, RszHandler) or not getattr(rsz_file, "is_scn", False):
        return None
    path = str(getattr(tab, "pak_source_path", None) or "")
    if not path:
        return None
    session = getattr(getattr(getattr(handler, "app", None), "project_workspace", None), "sessions", None)
    session = session.session_for_tab(tab) if session is not None else None
    label = Path(path.replace("\\", "/")).name or "SCN"
    return ScnSceneSource(
        path=path,
        handler=handler,
        label=label,
        project_dir=str(getattr(tab, "pak_project_dir", None) or getattr(session, "path", "") or ""),
        game_version=str(getattr(session, "game", "") or getattr(handler, "game_version", "") or ""),
    )


def _source_project_name(source: ScnSceneSource) -> str:
    return Path(source.project_dir).name if source.project_dir else "No Project"


def _source_name(source: ScnSceneSource) -> str:
    return source.label or Path(str(source.path).replace("\\", "/")).name or source.path


def _source_display_name(source: ScnSceneSource) -> str:
    return f"{_source_project_name(source)} / {_source_name(source)}"


class ScnSceneTab:
    skip_detached_menus = suppress_general_shortcuts = True
    hide_notebook_tab = True

    def __init__(self, app, title: str):
        self.app = app
        self.title = title
        self.parent_notebook = None
        self.filename = None
        self.handler = None
        self.viewer = None
        self.tree = None
        self.modified = False
        self.pak_source_path = None
        self.sources: list[ScnSceneSource] = []
        self._owned_scn_paths: set[str] = set()
        self._hidden_renderables: set[str] = set()
        self._selected_renderables: set[str] = set()
        self._scene_popup = self._scene_popup_body = None
        self._fullscreen_restore = None
        self._scn_icon = self._gameobject_icon = self._remove_icon = QIcon()
        self._eye_icon = self._eye_off_icon = QIcon()

        self.notebook_widget = QWidget()
        self.notebook_widget.parent_tab = self
        self._scn_icon = self.notebook_widget.style().standardIcon(QStyle.SP_DirIcon)
        self._gameobject_icon = self._make_gameobject_icon()
        self._remove_icon = self._make_remove_icon()
        self._eye_icon = self._make_eye_icon(False)
        self._eye_off_icon = self._make_eye_icon(True)
        layout = QVBoxLayout(self.notebook_widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.preview = ScnScenePreviewWidget(
            self.notebook_widget,
            sources_getter=lambda: self.sources,
            graphs_changed_callback=self._on_graphs_changed,
            settings=getattr(self.app, "settings", None),
        )
        self.preview.preview.object_clicked.connect(self._select_renderable_from_view)
        self.preview.preview._external_fullscreen_owner = self
        layout.addWidget(self.preview, 1)

        panel = QFrame(self.preview.preview)
        panel.setObjectName("sceneInspector")
        panel.viewport_anchor = "right"
        panel.setMinimumSize(460, 44)
        panel.setMaximumSize(1120, 1040)
        panel.resize(680, 860)
        panel.setStyleSheet("""
            QFrame#sceneInspector { background:rgba(9,13,18,218); border:1px solid rgba(90,108,126,180); border-radius:8px; color:#dce3ea; }
            QWidget#sceneInspectorBody { background-color:transparent; border:0; }
            QLabel { color:#dce3ea; background-color:transparent; }
            QLabel#sceneStatus { color:#9fb0bf; background-color:transparent; font-size:10px; }
            QLabel#overlayResizeGrip { color:#5d6f80; background-color:transparent; font-size:10px; }
            QTreeWidget { background:rgba(8,12,16,185); border:1px solid #2d3640; outline:0; padding:2px; }
            QTreeWidget::item { padding:3px 4px; }
            QTreeWidget::item:selected { background:#276f75; }
            QLineEdit { background:#101720; color:#dce3ea; border:1px solid #3a4652; border-radius:4px; padding:4px; selection-background-color:#276f75; }
            QToolButton { background:#1a232c; border:1px solid #3a4652; padding:3px; border-radius:4px; }
            QToolButton:hover { background:#253342; border-color:#4eb4a6; }
            QToolButton:disabled { color:#69737d; background:#121820; }
        """)
        self.source_panel = panel

        side = QVBoxLayout(panel)
        side.setContentsMargins(10, 10, 10, 10)
        side.setSpacing(6)

        header = QHBoxLayout()
        self.source_fold_button = self._tool_button("v", "Fold panel")
        self.source_label = QLabel(panel)
        self.menu_button = self._tool_button("≡", "Scene menu", lambda: self._show_scene_popup(self.menu_button.mapToGlobal(self.menu_button.rect().bottomLeft())))
        self.add_button = self._tool_button(QStyle.SP_FileDialogNewFolder, "Add open SCN", self._show_add_source_popup)
        self.remove_button = self._tool_button(self._remove_icon, "Remove selected from scene", self.remove_selected_sources)
        self.rename_button = self._tool_button(QStyle.SP_FileDialogDetailedView, "Rename scene", lambda: self.app.scenes.rename_scene(self))
        self.close_button = self._tool_button(QStyle.SP_DialogCloseButton, "Close scene", lambda: self.app.scenes.close_scene(self))
        header.addWidget(self.source_fold_button)
        header.addWidget(self.source_label, 1)
        for button in (self.menu_button, self.add_button, self.preview.refresh_button, self.remove_button, self.rename_button, self.close_button):
            header.addWidget(button)
        side.addLayout(header)
        self.source_body = QWidget(panel)
        self.source_body.setObjectName("sceneInspectorBody")
        self.source_body.setAttribute(Qt.WA_StyledBackground, True)
        body = QVBoxLayout(self.source_body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)
        side.addWidget(self.source_body, 1)
        self.preview.status_label.setObjectName("sceneStatus")
        self.preview.status_label.setAttribute(Qt.WA_StyledBackground, False)
        body.addWidget(self.preview.status_label)

        self.source_tree = QTreeWidget(panel)
        self.source_tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.source_tree.setColumnCount(2)
        self.source_tree.setHeaderHidden(True)
        self.source_tree.header().setStretchLastSection(False)
        self.source_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.source_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.source_tree.setColumnWidth(1, 22)
        self.source_tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.source_tree.customContextMenuRequested.connect(lambda pos: self._show_scene_popup(self.source_tree.mapToGlobal(pos), include_source_actions=True))
        self.source_tree.itemSelectionChanged.connect(self._refresh_controls)
        self.source_tree.itemClicked.connect(self._toggle_visibility_item)
        body.addWidget(self.source_tree, 1)
        self.warning_label = QLabel(panel)
        self.warning_label.setStyleSheet("color:#e6c84f; background-color:transparent;")
        self.warning_label.setWordWrap(True)
        self.warning_label.hide()
        body.addWidget(self.warning_label)
        self.preview.diagnostics.setMaximumHeight(88)
        body.addWidget(self.preview.diagnostics)
        self.shortcut_note = QLabel("Main REasy shortcuts are disabled in Scene tabs.", panel)
        self.shortcut_note.setStyleSheet("color:#7f8b96; background-color:transparent; font-size:10px;")
        self.shortcut_note.setAttribute(Qt.WA_StyledBackground, False)
        self.shortcut_note.setWordWrap(True)
        body.addWidget(self.shortcut_note)
        self.update_source_summary()
        self._refresh_controls()
        self.preview.preview.setup_viewport_overlay(panel, self.source_body, self.source_fold_button)
        self.preview.preview.place_viewport_overlays()

    def _tool_button(self, icon: QStyle.StandardPixmap | QIcon | str, tip: str, slot=None) -> QToolButton:
        button = QToolButton(self.notebook_widget)
        if isinstance(icon, str):
            button.setText(icon)
        else:
            button.setIcon(icon if isinstance(icon, QIcon) else self.notebook_widget.style().standardIcon(icon))
        button.setToolTip(tip)
        button.setFixedSize(26, 26)
        button.setFocusPolicy(Qt.NoFocus)
        if slot:
            button.clicked.connect(slot)
        return button

    def _popup_button(self, text: str, slot=None, enabled: bool = True) -> QToolButton:
        button = QToolButton(self._scene_popup or self.notebook_widget)
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        button.setFocusPolicy(Qt.NoFocus)
        button.setEnabled(enabled)
        if slot:
            button.clicked.connect(slot)
        return button

    def _discard_scene_popup(self) -> None:
        if self._scene_popup is not None:
            self._scene_popup.hide()
            self._scene_popup.deleteLater()
        self._scene_popup = self._scene_popup_body = None

    def _ensure_scene_popup(self, title: str):
        if self._scene_popup is not None and self._scene_popup.parentWidget() is not self.preview.preview:
            self._discard_scene_popup()
        if self._scene_popup is None:
            panel = QFrame(self.preview.preview)
            panel.setObjectName("sceneInspector")
            panel.viewport_anchor = "manual"
            panel.setMinimumSize(560, 180)
            panel.setMaximumSize(980, 820)
            panel.resize(660, 420)
            panel.setStyleSheet(self.source_panel.styleSheet())
            outer = QVBoxLayout(panel)
            outer.setContentsMargins(8, 8, 8, 8)
            outer.setSpacing(6)
            header = QHBoxLayout()
            self._scene_popup_fold = self._popup_button("v")
            self._scene_popup_title = QLabel(panel)
            header.addWidget(self._scene_popup_fold)
            header.addWidget(self._scene_popup_title, 1)
            header.addWidget(self._popup_button("x", panel.hide))
            outer.addLayout(header)
            self._scene_popup_body_widget = QWidget(panel)
            self._scene_popup_body_widget.setObjectName("sceneInspectorBody")
            self._scene_popup_body_widget.setAttribute(Qt.WA_StyledBackground, True)
            self._scene_popup_body = QVBoxLayout(self._scene_popup_body_widget)
            self._scene_popup_body.setContentsMargins(0, 0, 0, 0)
            self._scene_popup_body.setSpacing(6)
            outer.addWidget(self._scene_popup_body_widget, 1)
            self.preview.preview.setup_viewport_overlay(panel, self._scene_popup_body_widget, self._scene_popup_fold)
            self._scene_popup = panel
        self._scene_popup_title.setText(title)
        self._scene_popup_body_widget.show()
        self._scene_popup_fold.setText("v")
        self._clear_layout(self._scene_popup_body)
        return self._scene_popup, self._scene_popup_body

    @staticmethod
    def _clear_layout(layout) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                ScnSceneTab._clear_layout(item.layout())

    def _popup_run(self, callback, *args):
        def run(_checked=False):
            with suppress(RuntimeError):
                if self._scene_popup is not None:
                    self._scene_popup.hide()
            QTimer.singleShot(0, lambda: callback(*args))
        return run

    def _add_popup_row(self, label: str, actions) -> None:
        row = QHBoxLayout()
        row.addWidget(QLabel(label, self._scene_popup), 1)
        for text, callback, enabled in actions:
            row.addWidget(self._popup_button(text, self._popup_run(callback), enabled))
        self._scene_popup_body.addLayout(row)

    def _show_popup_at(self, global_pos) -> None:
        panel = self._scene_popup
        panel.adjustSize()
        view = self.preview.preview
        pos = view.mapFromGlobal(global_pos)
        max_x = max(8, view.width() - panel.width() - 8)
        max_y = max(8, view.height() - panel.height() - 8)
        panel.move(max(8, min(pos.x(), max_x)), max(8, min(pos.y(), max_y)))
        panel.show()
        panel.raise_()

    def is_view_fullscreen(self) -> bool:
        return self._fullscreen_restore is not None

    def enter_view_fullscreen(self) -> None:
        if self._fullscreen_restore is not None:
            return
        self._discard_scene_popup()
        window = self.notebook_widget.window()
        hidden = [(widget, widget.isVisible()) for widget in self._fullscreen_chrome(window)]
        self._fullscreen_restore = (window, window.windowState(), hidden)
        for widget, _visible in hidden:
            widget.hide()
        self.preview.preview.fullscreen_button.setText("x")
        window.showFullScreen()
        QTimer.singleShot(0, self._after_view_fullscreen_change)

    def leave_view_fullscreen(self, *, defer_update: bool = True) -> None:
        if self._fullscreen_restore is None:
            return
        window, state, hidden = self._fullscreen_restore
        self._fullscreen_restore = None
        with suppress(RuntimeError):
            self._discard_scene_popup()
        for widget, visible in hidden:
            with suppress(RuntimeError):
                widget.setVisible(visible)
        with suppress(RuntimeError):
            self.preview.preview.fullscreen_button.setText("⛶")
        with suppress(RuntimeError):
            if state & Qt.WindowFullScreen:
                window.setWindowState(state)
            elif state & Qt.WindowMaximized:
                window.showMaximized()
            else:
                window.showNormal()
        if defer_update:
            QTimer.singleShot(0, self._after_view_fullscreen_change)

    def _fullscreen_chrome(self, window) -> list[QWidget]:
        widgets, seen = [], set()
        for cls in (QMenuBar, QStatusBar, QDockWidget, QToolBar, QTabBar):
            for widget in window.findChildren(cls):
                if id(widget) not in seen and widget is not self.source_panel and not self.source_panel.isAncestorOf(widget):
                    seen.add(id(widget))
                    widgets.append(widget)
        return widgets

    def _after_view_fullscreen_change(self) -> None:
        with suppress(RuntimeError, AttributeError):
            if self._fullscreen_restore is not None:
                for widget, _visible in self._fullscreen_restore[2]:
                    widget.hide()
            self.preview.preview.place_viewport_overlays()
            self.preview.preview.setFocus(Qt.OtherFocusReason)

    def _show_add_source_popup(self) -> None:
        sources = self.app.scenes.open_scn_sources()
        panel, body = self._ensure_scene_popup("Add SCN")
        if not sources:
            body.addWidget(QLabel("Open an SCN from a PAK first.", panel))
        for source in sources:
            owner = self.app.scenes.owner_for_source(source)
            label = _source_display_name(source) + (f" | In {owner.title}" if owner else "")
            actions = [(
                f"In {owner.title}" if owner else "Add",
                lambda src=source: self.app.scenes.add_to_scene(self, src),
                owner is None,
            )]
            self._add_popup_row(label, actions)
        self._show_popup_at(self.add_button.mapToGlobal(self.add_button.rect().bottomLeft()))

    def show_rename_prompt(self) -> None:
        panel, body = self._ensure_scene_popup("Rename Scene")
        edit = QLineEdit(self.title, panel)
        body.addWidget(edit)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self._popup_button("Cancel", panel.hide))
        row.addWidget(self._popup_button("Apply", lambda _checked=False: self._apply_scene_title(edit.text())))
        body.addLayout(row)
        self._show_popup_at(self.rename_button.mapToGlobal(self.rename_button.rect().bottomLeft()))
        edit.selectAll()
        edit.setFocus()

    def _apply_scene_title(self, title: str) -> None:
        title = title.strip()
        if title:
            self.title = title
            self.update_tab_title()
            self.update_source_summary()
            self.app.scenes.refresh_actions()
            self.app.project_workspace._sync_tabs()
            self._scene_popup.hide()

    @staticmethod
    def _painted_icon(draw) -> QIcon:
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        draw(painter)
        painter.end()
        return QIcon(pixmap)

    @staticmethod
    def _make_gameobject_icon() -> QIcon:
        def draw(painter):
            painter.setPen(QPen(QColor("#dce3ea"), 1.2))
            painter.setBrush(QColor("#222a33"))
            painter.drawRoundedRect(2, 5, 12, 7, 3, 3)
            painter.drawEllipse(4, 7, 2, 2)
            painter.drawEllipse(10, 7, 2, 2)
        return ScnSceneTab._painted_icon(draw)

    @staticmethod
    def _make_remove_icon() -> QIcon:
        def draw(painter):
            painter.setPen(QPen(QColor("#dce3ea"), 1.4))
            painter.drawEllipse(3, 3, 10, 10)
            painter.drawLine(5, 8, 11, 8)
        return ScnSceneTab._painted_icon(draw)

    @staticmethod
    def _make_eye_icon(closed: bool) -> QIcon:
        def draw(painter):
            painter.setPen(QPen(QColor("#dce3ea"), 1.2))
            painter.drawEllipse(3, 5, 10, 6)
            if closed:
                painter.drawLine(3, 12, 13, 4)
            else:
                painter.setBrush(QColor("#dce3ea"))
                painter.drawEllipse(7, 7, 2, 2)
        return ScnSceneTab._painted_icon(draw)

    def add_source(self, source: ScnSceneSource, owned_keys: set[str] | None = None) -> bool:
        if owned_keys is None:
            owned_keys = self.app.scenes.source_identity_keys(source)
        root_keys = scn_source_identity_keys(source)
        if any(scn_source_identity_keys(existing) & root_keys for existing in self.sources):
            return False
        overlap = (self.owned_scn_paths() & owned_keys) - root_keys
        self.sources.append(source)
        self._owned_scn_paths.update(owned_keys)
        self._refresh_warning(bool(overlap))
        self.update_source_summary()
        self.preview.add_source(source)
        return True

    def remove_selected_sources(self) -> None:
        rows = sorted({row for row in (item.data(0, Qt.UserRole) for item in self.source_tree.selectedItems()) if isinstance(row, int)}, reverse=True)
        if not rows:
            return
        removed_keys = {key for item in self.source_tree.selectedItems() for key in (item.data(1, Qt.UserRole) or ())}
        self._hidden_renderables.difference_update(removed_keys)
        for row in rows:
            del self.sources[row]
        self.warning_label.hide()
        self.preview.remove_sources(set(rows))

    def owned_scn_paths(self) -> set[str]:
        return set(self._owned_scn_paths)

    def rebuild_owned_scn_paths(self, graphs=None) -> None:
        self._owned_scn_paths = self.app.scenes.sources_identity_keys(self.sources, graphs)

    def update_source_summary(self) -> None:
        count = len(self.sources)
        self.source_label.setText(f"{self.title} | {count} SCN source{'s' if count != 1 else ''}")
        self.source_tree.clear()
        if not self.sources:
            self.source_tree.addTopLevelItem(QTreeWidgetItem(["Empty scene", ""]))
            self._refresh_controls()
            return
        graphs = self.preview.graphs if len(self.preview.graphs) == len(self.sources) else []
        root_rows = {graph.root_document_id: row for row, graph in enumerate(graphs)}
        root_document_ids = set(root_rows)
        nested_rows = {
            root_rows[link.resolved_document_id]
            for graph in graphs
            for link in graph.links
            if link.resolved_document_id in root_rows and link.resolved_document_id != graph.root_document_id
        }
        if len(nested_rows) == len(self.sources):
            nested_rows.discard(0)
        used_rows: set[int] = set()
        project_items: dict[str, QTreeWidgetItem] = {}
        project_keys: dict[str, set[str]] = {}

        def item_keys(item: QTreeWidgetItem) -> set[str]:
            keys = set(item.data(1, Qt.UserRole) or ())
            for index in range(item.childCount()):
                keys.update(item_keys(item.child(index)))
            return keys

        def linked_item(document_id: str) -> QTreeWidgetItem | None:
            row = root_rows.get(document_id)
            if row is None or row in used_rows:
                return None
            used_rows.add(row)
            return make_item(row)

        def make_item(row: int) -> QTreeWidgetItem:
            item = self._scn_item(_source_name(self.sources[row]), row)
            if graphs:
                self._populate_graph_item(item, graphs[row], linked_item, root_document_ids)
                self._set_visibility_keys(item, item_keys(item))
            return item

        for row, source in enumerate(self.sources):
            if row in nested_rows or row in used_rows:
                continue
            used_rows.add(row)
            project = _source_project_name(source)
            project_item = project_items.get(project)
            if project_item is None:
                project_item = self._scn_item(project)
                project_items[project] = project_item
                project_keys[project] = set()
                self.source_tree.addTopLevelItem(project_item)
            item = make_item(row)
            project_item.addChild(item)
            project_keys[project].update(item.data(1, Qt.UserRole) or ())
        for project, item in project_items.items():
            self._set_visibility_keys(item, project_keys[project])
        self.source_tree.expandToDepth(1)
        self._refresh_controls()

    def update_tab_title(self) -> None:
        if self.parent_notebook is None:
            return
        index = self.parent_notebook.indexOf(self.notebook_widget)
        if index != -1:
            self.parent_notebook.setTabText(index, self.title)
            self.parent_notebook.tabBar().setTabVisible(index, not self.hide_notebook_tab)

    def _on_graphs_changed(self) -> None:
        if self.app is not None:
            self.rebuild_owned_scn_paths(self.preview.graphs)
            self.update_source_summary()
            self._refresh_warning()
            self.app.scenes.refresh_actions()
            self.app.scenes.refresh_buttons()

    def cleanup(self) -> None:
        self.leave_view_fullscreen(defer_update=False)
        self.sources.clear()
        self._owned_scn_paths.clear()
        self.preview.cleanup()
        self.preview.deleteLater()
        self.notebook_widget.parent_tab = None
        self.notebook_widget.deleteLater()

    def on_save(self) -> bool:
        return False

    def direct_save(self) -> bool:
        return False

    def reload_file(self) -> None:
        self.preview.request_refresh()

    def open_find_dialog(self) -> None:
        return None

    def _refresh_controls(self, *, focus_changed: bool = True) -> None:
        has_selection = any(isinstance(item.data(0, Qt.UserRole), int) for item in self.source_tree.selectedItems())
        keys = {key for item in self.source_tree.selectedItems() for key in (item.data(1, Qt.UserRole) or ())}
        changed = keys != self._selected_renderables
        self._selected_renderables = keys
        self.preview.set_selection(keys, focus=focus_changed and changed and bool(keys))
        self.add_button.setEnabled(bool(self.app.scenes.open_scn_sources()))
        self.remove_button.setEnabled(has_selection)

    def _select_renderable_from_view(self, key: str) -> None:
        if not key:
            self.source_tree.clearSelection()
            self._refresh_controls(focus_changed=False)
            return
        item = self._find_renderable_item(key)
        if item is None:
            return
        blocked = self.source_tree.blockSignals(True)
        self.source_tree.clearSelection()
        item.setSelected(True)
        self.source_tree.setCurrentItem(item)
        self.source_tree.scrollToItem(item)
        self.source_tree.blockSignals(blocked)
        self._refresh_controls(focus_changed=False)

    def _find_renderable_item(self, key: str) -> QTreeWidgetItem | None:
        matches = [item for item in self._tree_items() if key in (item.data(1, Qt.UserRole) or ())]
        return next((item for item in matches if item.data(0, Qt.UserRole + 1) == "renderable"), None) or next((item for item in matches if item.data(0, Qt.UserRole + 1) != "scn"), None)

    def _refresh_warning(self, include_overlap: bool = False) -> None:
        show = include_overlap or any(d.code == "duplicate_linked_scn" for graph in self.preview.graphs for d in graph.diagnostics)
        self.warning_label.setVisible(show)
        self.warning_label.setText("Warning: linked SCNs already in this scene will be skipped." if show else "")

    def _show_scene_popup(self, global_pos, include_source_actions: bool = False) -> None:
        panel, body = self._ensure_scene_popup("Scene Manager")
        if include_source_actions:
            self._add_popup_row("Selection", [("Remove SCN", self.remove_selected_sources, self.remove_button.isEnabled())])
        active_source = scn_source_from_tab(self.app.get_active_tab())
        source_owner = self.app.scenes.owner_for_source(active_source)
        scenes = self.app.scenes.tabs()
        if not scenes:
            body.addWidget(QLabel("No scenes.", panel))
        for scene in scenes:
            actions = []
            if active_source is not None:
                text = f"In {source_owner.title}" if source_owner else f"Add {_source_display_name(active_source)}"
                actions.append((
                    text,
                    lambda tab=scene, src=active_source: self._add_active_source_to_scene(tab, src),
                    source_owner is None,
                ))
            actions += [
                ("Go To", lambda tab=scene: self._go_to_scene(tab), True),
                ("Delete", lambda tab=scene: self.app.scenes.close_scene(tab), True),
            ]
            self._add_popup_row(scene.title, actions)
        body.addWidget(self._popup_button("Create Scene", self._popup_run(self._create_scene)))
        self._show_popup_at(global_pos)

    def _release_fullscreen(self):
        restore = self._fullscreen_restore
        if restore is not None:
            self._fullscreen_restore = None
            self.preview.preview.fullscreen_button.setText("⛶")
        return restore

    def _adopt_fullscreen(self, restore) -> None:
        if restore is None:
            return
        self._fullscreen_restore = restore
        for widget, _visible in restore[2]:
            with suppress(RuntimeError):
                widget.hide()
        self.preview.preview.fullscreen_button.setText("x")
        QTimer.singleShot(0, self._after_view_fullscreen_change)

    def _handoff_fullscreen(self, scene, action) -> None:
        if scene.notebook_widget.window() is self.notebook_widget.window():
            restore = self._release_fullscreen()
            action(scene)
            scene._adopt_fullscreen(restore)
        else:
            self.leave_view_fullscreen()
            action(scene)
            QTimer.singleShot(0, scene.enter_view_fullscreen)

    def _go_to_scene(self, scene) -> None:
        if self.is_view_fullscreen() and scene is not self:
            self._handoff_fullscreen(scene, self.app.scenes.focus)
        else:
            self.app.scenes.focus(scene)

    def _create_scene(self) -> None:
        restore = self._release_fullscreen()
        scene = self.app.scenes.create_tab()
        scene._adopt_fullscreen(restore)

    def _add_active_source_to_scene(self, scene, source) -> None:
        if scene is not self and self.is_view_fullscreen():
            self._handoff_fullscreen(scene, lambda tab: self.app.scenes.add_to_scene(tab, source))
        else:
            self.app.scenes.add_to_scene(scene, source)

    def _toggle_visibility_item(self, item: QTreeWidgetItem, column: int) -> None:
        keys = set(item.data(1, Qt.UserRole) or ())
        if column != 1 or not keys:
            return
        if keys <= self._hidden_renderables:
            self._hidden_renderables.difference_update(keys)
        else:
            self._hidden_renderables.update(keys)
        self.preview.set_hidden_renderables(self._hidden_renderables)
        self._refresh_visibility_icons()

    def _refresh_visibility_icons(self) -> None:
        for item in self._tree_items():
            self._set_visibility_keys(item, item.data(1, Qt.UserRole) or ())

    def _tree_items(self):
        stack = [self.source_tree.topLevelItem(i) for i in range(self.source_tree.topLevelItemCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _set_visibility_keys(self, item: QTreeWidgetItem, keys) -> None:
        keys = tuple(keys)
        item.setData(1, Qt.UserRole, keys)
        item.setIcon(1, self._eye_off_icon if keys and set(keys) <= self._hidden_renderables else self._eye_icon if keys else QIcon())

    def _scn_item(self, label: str, source_row: int | None = None, keys=()) -> QTreeWidgetItem:
        item = QTreeWidgetItem([Path(label.replace("\\", "/")).name or label, ""])
        item.setIcon(0, self._scn_icon)
        item.setData(0, Qt.UserRole, source_row)
        item.setData(0, Qt.UserRole + 1, "scn")
        self._set_visibility_keys(item, keys)
        return item

    def _gameobject_item(self, scene_object, keys=()) -> QTreeWidgetItem:
        label = scene_object.name or scene_object.type_name or f"GameObject {scene_object.id.local_object_id}"
        item = QTreeWidgetItem([label, ""])
        item.setIcon(0, self._gameobject_icon)
        item.setData(0, Qt.UserRole + 1, "object")
        self._set_visibility_keys(item, keys)
        return item

    def _renderable_item(self, renderable) -> QTreeWidgetItem:
        group = renderable.source_group_instance_id or 0
        transform = renderable.source_transform_instance_id or 0
        component = renderable.source_component_id.instance_id
        label = f"Composite {component}:{group}:{transform} - {Path(renderable.mesh_path.replace('\\', '/')).name or 'mesh'}"
        item = QTreeWidgetItem([label, ""])
        item.setIcon(0, self._gameobject_icon)
        item.setData(0, Qt.UserRole + 1, "renderable")
        self._set_visibility_keys(item, (renderable.key,))
        return item

    def _populate_graph_item(self, root_item: QTreeWidgetItem, graph, linked_item_factory=None, linked_document_ids=()) -> None:
        children: dict[str, list] = {}
        for instance in graph.document_instances.values():
            if instance.parent_instance_id:
                children.setdefault(instance.parent_instance_id, []).append(instance)
        by_instance: dict[str, set[str]] = {}
        by_object: dict[tuple[str, object], set[str]] = {}
        composite_by_object: dict[tuple[str, object], list] = {}
        for renderable in graph.renderables:
            by_instance.setdefault(renderable.document_instance_id, set()).add(renderable.key)
            by_object.setdefault((renderable.document_instance_id, renderable.source_object_id), set()).add(renderable.key)
            if renderable.source_kind == "composite_mesh":
                composite_by_object.setdefault((renderable.document_instance_id, renderable.source_object_id), []).append(renderable)
        subtree_cache: dict[str, set[str]] = {}

        def subtree_keys(instance_id: str) -> set[str]:
            if instance_id not in subtree_cache:
                keys = set(by_instance.get(instance_id, ()))
                for child in children.get(instance_id, ()):
                    keys.update(subtree_keys(child.instance_id))
                subtree_cache[instance_id] = keys
            return subtree_cache[instance_id]

        def add_document(parent_item: QTreeWidgetItem, instance_id: str, root: bool = False) -> None:
            instance = graph.document_instances.get(instance_id)
            document = graph.documents.get(instance.document_id if instance else graph.root_document_id)
            if document is None:
                return
            if not root and document.document_id in linked_document_ids:
                item = linked_item_factory(document.document_id) if linked_item_factory else None
                if item is not None:
                    parent_item.addChild(item)
                return
            item = parent_item if root else self._scn_item(document.source_path or document.document_id, keys=subtree_keys(instance_id))
            if root:
                self._set_visibility_keys(item, subtree_keys(instance_id))
            if not root:
                parent_item.addChild(item)
            for child in children.get(instance_id, []):
                add_document(item, child.instance_id)
            for scene_object in document.objects.values():
                if keys := by_object.get((instance_id, scene_object.id), ()):
                    object_item = self._gameobject_item(scene_object, keys)
                    for renderable in sorted(composite_by_object.get((instance_id, scene_object.id), ()), key=lambda r: (r.source_group_instance_id or 0, r.source_transform_instance_id or 0, r.key)):
                        object_item.addChild(self._renderable_item(renderable))
                    item.addChild(object_item)

        add_document(root_item, graph.root_instance_id, True)


class ScnSceneController:
    def __init__(self, app):
        self.app = app
        self.counter = 0
        self.loader = ScnSceneLoader()
        self._identity_cache: dict[tuple[int, str], set[str]] = {}

    def tabs(self) -> list[ScnSceneTab]:
        return [tab for tab in self.app.tabs.values() if isinstance(tab, ScnSceneTab)]

    def scene_using_project(self, project_dir: str) -> ScnSceneTab | None:
        key = self._project_key(project_dir)
        if not key:
            return None
        return next(
            (
                scene for scene in self.tabs()
                if any(source.project_dir and self._project_key(source.project_dir) == key for source in scene.sources)
            ),
            None,
        )

    @staticmethod
    def _project_key(path: str) -> str:
        return os.path.normcase(os.path.abspath(path)) if path else ""

    def _notice(self, message: str) -> None:
        self.app.status_bar.showMessage(message, 5000)

    def create_tab(self) -> ScnSceneTab:
        self.counter += 1
        tab = ScnSceneTab(self.app, f"Scene {self.counter}")
        tab.parent_notebook = self.app.notebook
        index = self.app.notebook.addTab(tab.notebook_widget, tab.title)
        self.app.notebook.tabBar().setTabVisible(index, False)
        self.app.tabs[tab.notebook_widget] = tab
        self.app.project_workspace.sessions.add_global_tab(tab)
        self.focus(tab)
        self.refresh_actions()
        self.app._refresh_homepage()
        return tab

    def add_active_scn(self) -> None:
        source = scn_source_from_tab(self.app.get_active_tab())
        self.add_to_scene(source=source, scene_tab=None if source is not None else self.active_scene())

    def add_to_scene(self, scene_tab: ScnSceneTab | None = None, source: ScnSceneSource | None = None) -> None:
        source = source or scn_source_from_tab(self.app.get_active_tab()) or self._choose_open_source()
        if source is None:
            self._notice("Open an SCN from a PAK first.")
            return
        owner = self.owner_for_source(source)
        if owner is not None and owner is not scene_tab:
            self.focus(owner)
            self._notice(f"{_source_display_name(source)} is already open in {owner.title}.")
            return
        owned_keys = self.source_identity_keys(source)
        conflict = self.owner_for(owned_keys, exclude=scene_tab)
        if conflict is not None and conflict is not owner:
            self.focus(conflict)
            self._notice(f"{_source_display_name(source)} is already open in {conflict.title}.")
            return
        scene_tab = scene_tab or owner or self._choose_tab()
        if scene_tab is None:
            return
        if scene_tab.add_source(source, owned_keys):
            self.focus(scene_tab)
            self.app.status_bar.showMessage(f"Added {_source_display_name(source)} to {scene_tab.title}", 3000)
        else:
            self._notice(f"{_source_display_name(source)} is already in {scene_tab.title}.")
        self.refresh_actions()
        self.refresh_buttons()

    def rename_scene(self, scene_tab: ScnSceneTab | None = None) -> None:
        scene_tab = scene_tab or self.active_scene()
        if scene_tab is not None:
            scene_tab.show_rename_prompt()

    def close_scene(self, scene_tab: ScnSceneTab | None = None) -> None:
        scene_tab = scene_tab or self.active_scene()
        if scene_tab is None:
            return
        if scene_tab.is_view_fullscreen():
            scene_tab.leave_view_fullscreen(defer_update=False)
            QTimer.singleShot(0, lambda tab=scene_tab: self._close_scene_tab(tab))
        else:
            self._close_scene_tab(scene_tab)

    def _close_scene_tab(self, scene_tab: ScnSceneTab) -> None:
        if scene_tab.notebook_widget in self.app.tabs:
            self.app._close_tab_object(scene_tab, record_history=False)
            self.app.project_workspace._sync_tabs()

    def populate_scene_menu(self, menu: QMenu, *, clear: bool = True) -> None:
        if clear:
            menu.clear()
        active_source = scn_source_from_tab(self.app.get_active_tab())
        source_owner = self.owner_for_source(active_source)
        scenes = self.tabs()
        if scenes:
            for scene in scenes:
                scene_menu = menu.addMenu(scene.title)
                if active_source is not None:
                    text = f"In {source_owner.title}" if source_owner else f"Add {_source_display_name(active_source)}"
                    add_act = scene_menu.addAction(text, lambda _checked=False, tab=scene, source=active_source: self.add_to_scene(tab, source))
                    add_act.setEnabled(source_owner is None)
                    scene_menu.addSeparator()
                scene_menu.addAction("Go To", lambda _checked=False, tab=scene: self.focus(tab))
                scene_menu.addAction("Delete", lambda _checked=False, tab=scene: self.close_scene(tab))
        else:
            empty = menu.addAction("No Scenes")
            empty.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Create Scene", lambda _checked=False: self.create_tab())

    def populate_add_to_scene_menu(self, menu: QMenu, source: ScnSceneSource | None) -> None:
        menu.clear()
        if source is None:
            action = menu.addAction("Open an SCN from a PAK first")
            action.setEnabled(False)
            return
        scenes = self.tabs()
        owner = self.owner_for_source(source)
        for scene in scenes:
            text = f"In {scene.title}" if scene is owner else scene.title
            action = menu.addAction(text, lambda _checked=False, tab=scene, src=source: self.add_to_scene(tab, src))
            action.setEnabled(owner is None)
        if scenes:
            menu.addSeparator()
        action = menu.addAction("Add to New Scene", lambda _checked=False, src=source: self.add_to_new_scene(src))
        action.setEnabled(owner is None)

    def add_to_new_scene(self, source: ScnSceneSource) -> ScnSceneTab:
        if owner := self.owner_for_source(source):
            self.focus(owner)
            return owner
        scene = self.create_tab()
        self.add_to_scene(scene, source)
        return scene

    def sync_tab(self, tab) -> None:
        source = scn_source_from_tab(tab)
        if source is None:
            return
        keys = scn_source_identity_keys(source)
        for scene in self.tabs():
            for existing in scene.sources:
                if scn_source_identity_keys(existing) & keys:
                    existing.path, existing.handler, existing.label = source.path, source.handler, source.label
                    existing.project_dir, existing.game_version = source.project_dir, source.game_version
                    scene.update_source_summary()
                    scene.rebuild_owned_scn_paths()
                    scene.preview.set_stale()
        self.refresh_actions()
        self.refresh_buttons()

    def owner_for(self, keys: set[str], *, exclude: ScnSceneTab | None = None) -> ScnSceneTab | None:
        return next((scene for scene in self.tabs() if scene is not exclude and scene.owned_scn_paths() & keys), None)

    def owner_for_source(self, source: ScnSceneSource | None, *, exclude: ScnSceneTab | None = None) -> ScnSceneTab | None:
        return self.owner_for(scn_source_identity_keys(source), exclude=exclude) if source is not None else None

    def active_scene(self) -> ScnSceneTab | None:
        tab = self.app.get_active_tab()
        return tab if isinstance(tab, ScnSceneTab) else None

    def source_identity_keys(self, source: ScnSceneSource) -> set[str]:
        key = self._identity_cache_key(source)
        if key not in self._identity_cache:
            keys = scn_source_identity_keys(source)
            try:
                graphs = self.loader.build_graphs([source])
                keys.update(ScnSceneLoader.document_identity_keys(graphs))
            except Exception:
                pass
            self._identity_cache[key] = keys
        return set(self._identity_cache[key])

    def sources_identity_keys(self, sources: list[ScnSceneSource], graphs=None) -> set[str]:
        if graphs is None:
            return set().union(*(self.source_identity_keys(source) for source in sources)) if sources else set()
        keys = set().union(*(scn_source_identity_keys(source) for source in sources)) if sources else set()
        keys.update(ScnSceneLoader.document_identity_keys(list(graphs)))
        return keys

    @staticmethod
    def _identity_cache_key(source: ScnSceneSource) -> tuple[int, str]:
        path = f"{source.project_dir}|{source.path}".replace("\\", "/").lower()
        return (id(getattr(source.handler, "rsz_file", None)), path)

    def focus(self, tab) -> None:
        workspace = self.app.project_workspace
        scene_tab = isinstance(tab, ScnSceneTab)
        session = None if scene_tab else workspace.sessions.session_for_tab(tab)
        if scene_tab and workspace.sessions.active_key is not None:
            workspace.sessions.activate(None)
            self.app.current_project = self.app.current_game = None
            self.app.proj_dock.set_project(None)
            self.app.proj_dock.hide()
        if session is not None and session.key != workspace.sessions.active_key:
            if session.path:
                workspace.activate(session.path, session.game)
            else:
                workspace.sessions.activate(None)
                workspace._sync_tabs()
                self.app.current_project = self.app.current_game = None
                self.app.proj_dock.set_project(None)
                self.app.proj_dock.hide()
        index = self.app.notebook.indexOf(tab.notebook_widget)
        if index != -1:
            self.app.notebook.setCurrentIndex(index)
            workspace._sync_tabs()
            return
        for window in self.app.project_workspace.sessions.windows_for([tab]):
            window.show()
            window.raise_()
            window.activateWindow()
        workspace._sync_tabs()

    def refresh_actions(self) -> None:
        menu = getattr(self.app, "scene_menu", None)
        if menu is not None and menu.isVisible():
            self.populate_scene_menu(menu)

    def refresh_buttons(self) -> None:
        for tab in self.app.tabs.values():
            if isinstance(tab, ScnSceneTab):
                tab._refresh_controls()
            refresh = getattr(getattr(tab, "viewer", None), "refresh_scene_button", None)
            if callable(refresh):
                refresh()

    def mark_stale(self, handler) -> None:
        stale_id = id(getattr(handler, "rsz_file", None))
        self._identity_cache = {key: value for key, value in self._identity_cache.items() if key[0] != stale_id}
        for scene in self.tabs():
            if any(source.handler is handler for source in scene.sources):
                scene.preview.set_stale()

    def open_scn_sources(self) -> list[ScnSceneSource]:
        sources: list[ScnSceneSource] = []
        seen: set[str] = set()
        for tab in self.app.tabs.values():
            source = scn_source_from_tab(tab)
            if source is None:
                continue
            keys = scn_source_identity_keys(source)
            key = min(keys) if keys else source.path
            if key in seen:
                continue
            seen.add(key)
            sources.append(source)
        return sources

    def _choose_open_source(self) -> ScnSceneSource | None:
        sources = self.open_scn_sources()
        if len(sources) <= 1:
            return sources[0] if sources else None
        labels = [_source_display_name(source) for source in sources]
        label, ok = QInputDialog.getItem(self.app, "Add SCN to Scene", "Open SCN:", labels, 0, False)
        return sources[labels.index(label)] if ok and label in labels else None

    def _choose_tab(self) -> ScnSceneTab | None:
        scenes = self.tabs()
        if not scenes:
            return self.create_tab()
        titles = [scene.title for scene in scenes] + ["New Scene"]
        title, ok = QInputDialog.getItem(self.app, "Add SCN to Scene", "Scene:", titles, 0, False)
        if not ok:
            return None
        return self.create_tab() if title == "New Scene" else scenes[titles.index(title)]
